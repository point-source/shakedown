"""Internet Archive source plugin. Wraps the official `internetarchive` package."""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterator, Sequence
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

import internetarchive as ia
import requests
from internetarchive.auth import S3Auth
from internetarchive.config import get_auth_config
from internetarchive.exceptions import AuthenticationError
from urllib3 import Retry
from urllib3.exceptions import MaxRetryError

from shakedown import stopping
from shakedown.config import CollectionConfig, SourceConfig
from shakedown.credentials import Login, LoginCheck
from shakedown.models import ManifestFile
from shakedown.plugins.archive_org import file_records
from shakedown.plugins.base import (
    FailureCause,
    FetchResult,
    ItemLookup,
    ListedItem,
    SourcePlugin,
    VerifyResult,
    downloaded_file_mismatch,
    failure_cause,
    http_status,
    retry_after_seconds,
    reusable_leftover,
    status_cause,
)
from shakedown.plugins.registry import register
from shakedown.selection import DOWNLOAD_REFUSED_REASON

log = logging.getLogger(__name__)


# Connect timeout for downloads; the read timeout bounds a stalled transfer. File
# downloads make one request per file per attempt; the core retries the fetch
# (SPEC §spec:failure-behavior). Metadata lookups take the core's timeout.
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 30.0
_DOWNLOAD_TIMEOUT = (_CONNECT_TIMEOUT, _READ_TIMEOUT)
_DOWNLOAD_CHUNK = 1024 * 1024

# The search has no shakedown retry loop around it, so the session's own retry is
# its one layer: bounded, and honoring a Retry-After only up to a cap.
_SEARCH_RETRIES = 3
_SEARCH_BACKOFF_BASE_SECONDS = 1.0
_SEARCH_MAX_BACKOFF_SECONDS = 30.0


def _attempt(status: int | None, error: Exception | None) -> dict[str, Any]:
    """What a failed search attempt was, as `report_listing_retry` takes it."""
    if status is not None:
        return {"cause": status_cause(status), "http_status": status, "detail": f"HTTP {status}"}
    return {
        "cause": failure_cause(error) if error is not None else None,
        "http_status": None,
        "detail": str(error) if error is not None else None,
    }


class _ReportingRetry(Retry):
    """The search's urllib3 retry, reporting each failed attempt to the core: its cause
    and the wait urllib3 takes before the retry, or that it gave up
    (§spec:network-activity)."""

    report: Callable[..., None] | None = None

    def new(self, **kw: Any) -> Self:
        retry = super().new(**kw)
        retry.report = self.report
        return retry

    def increment(
        self,
        method: str | None = None,
        url: str | None = None,
        response: Any = None,
        error: Exception | None = None,
        _pool: Any = None,
        _stacktrace: Any = None,
    ) -> Self:
        try:
            return super().increment(method, url, response, error, _pool, _stacktrace)
        except MaxRetryError:
            if self.report is not None:
                status = response.status if response is not None and error is None else None
                self.report(**_attempt(status, error), gave_up=True)
            raise

    def sleep(self, response: Any = None) -> None:
        if self.report is not None and self.history:
            last = self.history[-1]
            wait: float | None = None
            if self.respect_retry_after_header and response is not None:
                wait = self.get_retry_after(response)
            if not wait:
                wait = self.get_backoff_time()
            self.report(**_attempt(last.status, last.error), wait_seconds=wait)
        super().sleep(response)


def _search_retry(report: Callable[..., None] | None = None) -> Retry:
    retry = _ReportingRetry(
        total=_SEARCH_RETRIES,
        connect=_SEARCH_RETRIES,
        read=_SEARCH_RETRIES,
        redirect=False,
        allowed_methods=["GET", "POST"],
        status_forcelist=[429, 500, 502, 503, 504],
        backoff_factor=_SEARCH_BACKOFF_BASE_SECONDS,
        backoff_max=_SEARCH_MAX_BACKOFF_SECONDS,
        respect_retry_after_header=True,
        retry_after_max=int(_SEARCH_MAX_BACKOFF_SECONDS),
    )
    retry.report = report
    return retry


def _get_ia_session(auth_config: dict[str, Any] | None = None) -> ia.ArchiveSession:
    """Build an ArchiveSession — logged in with ``auth_config`` (the S3 keys and
    cookies archive.org's login returns) when given — with the urllib3 retries on its
    archive.org adapter turned off."""
    http_adapter_kwargs = {"max_retries": 0}
    return (
        ia.get_session(config=auth_config, http_adapter_kwargs=http_adapter_kwargs)
        if auth_config
        else ia.get_session(http_adapter_kwargs=http_adapter_kwargs)
    )


def _log_in(login: Login) -> tuple[LoginCheck, dict[str, Any] | None]:
    """archive.org's verdict on ``login``, and on `accepted` the S3 keys and cookies
    its login service returns (§spec:dashboard-credentials). `rejected` is the
    service's own refusal; anything else that stops the answer — no connection, a
    timeout, a reply that is not its JSON — is `unchecked`."""
    try:
        return "accepted", get_auth_config(login.email, login.password)
    except AuthenticationError:
        return "rejected", None
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return "unchecked", None


def _restriction_reason(item_meta: dict[str, Any]) -> str | None:
    """Why the whole item is stream-only / download-disabled, or None (PRD §6, §13)."""
    if item_meta.get("access-restricted") in (True, "true", "1"):
        return item_meta.get("access-restricted-item-reason") or "access-restricted"
    if str(item_meta.get("noindex")).lower() == "true":
        return "noindex (item hidden by uploader)"
    return None


def _combine_change_signal(hit: dict[str, Any]) -> str | None:
    """Combine IA's oai_updatedate and item_size into one change signal.

    oai_updatedate is the monotone component the correctness bound leans on: a
    re-derivation (new checksum, possibly identical byte size) bumps it, so it
    catches same-size content changes that item_size alone would miss. The
    signal therefore *requires* oai_updatedate — when the search did not expose
    it we return None so the core falls through to the full manifest comparison
    rather than trusting a size-only signal that is not monotone in the item's
    contents (§spec:incremental-discovery, correctness bound). When present,
    item_size is folded in as a second discriminator.
    """
    updated = hit.get("oai_updatedate")
    # IA returns multi-valued fields as lists; take the most recent.
    if isinstance(updated, list):
        updated = updated[-1] if updated else None
    if updated is None:
        return None
    return f"{updated}|{hit.get('item_size')}"


@register
class IAPlugin(SourcePlugin):
    type_name = "ia"
    upstream_host = "archive.org"
    template_fields = ("identifier", "title", "creator", "date", "year", "venue", "coverage")
    # `identifier` is the item's IA identifier — globally unique, the composite key
    # (§spec:layout-collision-safety); no other surfaced field is guaranteed distinct.
    per_item_unique_fields = ("identifier",)
    # Download-restricted items need an archive.org login (`auth`).
    takes_credentials = True

    # Read when the file's `auth` names none (§spec:dashboard-credentials).
    credential_env = ("IA_EMAIL", "IA_PASSWORD")

    def __init__(self, source_config: SourceConfig, login: Login | None = None) -> None:
        super().__init__(source_config, login)
        # Opened on first use, logged in with `login` when there is one: a plugin
        # that never asks archive.org (`verify`) never logs in, and one that does
        # logs in once, whichever collection's thread gets there first.
        self._opening = threading.Lock()
        self._opened: tuple[ia.ArchiveSession, ia.ArchiveSession] | None = None
        self._verdict: LoginCheck = "unchecked"

    def _sessions(self) -> tuple[ia.ArchiveSession, ia.ArchiveSession]:
        with self._opening:
            if self._opened is None:
                auth_config = None
                if self.login is not None:
                    self._verdict, auth_config = _log_in(self.login)
                    if auth_config is None:
                        log.warning(
                            "archive.org %s the login for source %r; continuing without it",
                            "rejected" if self._verdict == "rejected" else "could not check",
                            self.source_config.name,
                        )
                # Exactly one retry layer per request (SPEC §spec:failure-behavior).
                # The library remounts its own retries on the session every search,
                # so searches get a session of their own. Metadata lookups and
                # downloads go through `_session` as plain requests, retried only by
                # shakedown.
                self._opened = (_get_ia_session(auth_config), _get_ia_session(auth_config))
            return self._opened

    @property
    def _session(self) -> ia.ArchiveSession:
        return self._sessions()[0]

    @property
    def _search_session(self) -> ia.ArchiveSession:
        return self._sessions()[1]

    def check_login(self) -> LoginCheck:
        """archive.org's verdict on the login this plugin holds, asked once."""
        if self.login is None:
            return "unchecked"
        self._sessions()
        return self._verdict

    def list_items(self, collection: CollectionConfig) -> Iterator[ListedItem]:
        log.info("IA listing: query=%r", collection.query)
        search = self._search_session.search_items(
            collection.query,
            fields=["identifier", "oai_updatedate", "item_size"],
            max_retries=_search_retry(self.report_listing_retry),
        )
        for hit in search:
            yield ListedItem(hit["identifier"], _combine_change_signal(hit))

    def lookup_item(self, identifier: str, timeout: float) -> ItemLookup:
        """One Metadata API request. An HTTP error keeps its response, so the core
        sees the status and any Retry-After."""
        response = self._session.get(
            self._url(f"metadata/{quote(identifier)}"),
            timeout=timeout,
            auth=self._auth(),
        )
        response.raise_for_status()
        item = response.json()
        metadata: dict[str, Any] = dict(item.get("metadata") or {})
        return ItemLookup(
            metadata=_normalize_template_fields(identifier, metadata),
            files=file_records(identifier, item.get("files") or []),
            restriction_reason=_restriction_reason(metadata),
        )

    def _auth(self) -> S3Auth | None:
        if self._session.access_key and self._session.secret_key:
            return S3Auth(self._session.access_key, self._session.secret_key)
        return None

    def _url(self, path: str) -> str:
        return f"{self._session.protocol}//{self._session.host}/{path}"

    def fetch(
        self, identifier: str, files: Sequence[ManifestFile], dest_dir: Path
    ) -> FetchResult:
        """Download the given files into dest_dir (a core-owned temp directory).

        The plugin only writes bytes and checks each file's size and md5 against the
        manifest; the core owns the temp-dir lifecycle and the atomic rename into the
        archive (SPEC.md §spec:sync-workflow).
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Files an earlier attempt (or a killed run) left in dest_dir are kept once
        # they match the manifest's size and md5; download only the rest. Any other
        # leftover is removed first so the library never resumes it blindly.
        to_fetch = [mf for mf in files if not reusable_leftover(dest_dir / mf.name, mf)]
        for mf in to_fetch:
            (dest_dir / mf.name).unlink(missing_ok=True)

        try:
            for mf in to_fetch:
                self._download_file(identifier, mf.name, dest_dir / mf.name)
        except Exception as e:
            if http_status(e) == 403:
                # archive.org serves some shows stream-only (often soundboards) with no
                # restriction flag in their metadata; the refusal is the only signal.
                log.info("IA download refused for %s: %s", identifier, e)
                return FetchResult(
                    success=False,
                    error=DOWNLOAD_REFUSED_REASON,
                    unavailable=True,
                    cause=FailureCause.REFUSED,
                    http_status=403,
                )
            log.warning("IA download failed for %s: %s", identifier, e)
            return FetchResult(
                success=False,
                error=str(e),
                retriable=_is_retriable(e),
                retry_after=retry_after_seconds(e),
                cause=failure_cause(e),
                http_status=http_status(e),
            )

        # Check every downloaded file against the manifest (reused leftovers were
        # checked above). A corrupt file is removed and the fetch retried.
        for mf in to_fetch:
            mismatch = downloaded_file_mismatch(dest_dir / mf.name, mf)
            if mismatch is not None:
                (dest_dir / mf.name).unlink()
                return FetchResult(
                    success=False,
                    error=mismatch,
                    retriable=True,
                )

        return FetchResult(success=True)

    def _download_file(self, identifier: str, name: str, path: Path) -> None:
        """Stream one file straight to its place in the temp dir, so every finished
        file sits where the next attempt looks for it, even if the run is killed.

        One plain request: the library's own download retries at least twice under
        any setting and remounts retries on the session, so it is not used here.
        """
        with self._session.get(
            self._url(f"download/{quote(identifier)}/{quote(name)}"),
            params={"cnt": "0"},  # don't count mirroring as views, like the library
            stream=True,
            timeout=_DOWNLOAD_TIMEOUT,
            auth=self._auth(),
        ) as response:
            response.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    # A stop abandons the file here; the next attempt discards it
                    # (§spec:graceful-stop).
                    stopping.check()
                    fh.write(chunk)
            last_modified = response.headers.get("Last-Modified")
        if last_modified:
            # Keep the upstream timestamp, as the library did.
            try:
                mtime = parsedate_to_datetime(last_modified).timestamp()
            except (TypeError, ValueError):
                return
            os.utime(path, (mtime, mtime))

    def verify(self, files: Sequence[ManifestFile], archive_path: Path) -> VerifyResult:
        """Cheap existence-only verify. Hashing belongs in `verify --deep`."""
        missing = [f.name for f in files if not (archive_path / f.name).is_file()]
        return VerifyResult(ok=not missing, missing_files=missing)


_RETRIABLE_MARKERS = (
    "checksum",
    "rate limit",
    "too many requests",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    # A file cut off mid-stream ("Connection broken: IncompleteRead(...)",
    # "Response ended prematurely").
    "connection broken",
    "ended prematurely",
    "temporarily unavailable",
    "429",
    "502",
    "503",
    "504",
)


def _is_retriable(exc: BaseException) -> bool:
    """Classify a download failure as a transient fault worth a bounded retry.

    Rate limits (429), server errors (5xx), connection resets/timeouts, and
    checksum mismatches are transient; anything else fails fast so a permanent
    fault isn't retried needlessly (SPEC §spec:failure-behavior).
    """
    status = http_status(exc)
    if status is not None and (status == 429 or 500 <= status < 600):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _RETRIABLE_MARKERS)


def _normalize_template_fields(identifier: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Surface a stable set of template-friendly fields alongside raw IA metadata."""
    out = dict(metadata)
    out.setdefault("identifier", identifier)
    date = metadata.get("date") or ""
    if date and "year" not in out:
        out["year"] = date.split("-", 1)[0] if "-" in date else date[:4]
    if "venue" not in out and "coverage" in out:
        out["venue"] = out["coverage"]
    return out
