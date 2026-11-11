"""The core's per-item lookup: bounded retries, the one description validator, and
failure isolation (SPEC §spec:per-item-isolation, §spec:slow-metadata)."""
from __future__ import annotations

import httpx
import pytest
import requests

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.discovery import (
    LOOKUP_ATTEMPTS,
    LOOKUP_TIMEOUT,
    ItemDetails,
    LookupFailure,
    SourceBudget,
    describe,
    lookup,
)
from shakedown.plugins.base import ItemLookup
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

GOOD_MD5 = "d41d8cd98f00b204e9800998ecf8427e"


def _plugin() -> FakePlugin:
    return FakePlugin(
        SourceConfig(name="fake-src", type="fake", collections=[CollectionConfig(name="c", query="*")])
    )


def _requests_error(status: int, retry_after: str | None = None) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return requests.HTTPError(f"{status} Server Error", response=response)


def _httpx_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    request = httpx.Request("GET", "https://archive.org/metadata/x")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, headers=headers)
    )


def _answer(*files: dict, metadata: dict | None = None) -> ItemLookup:
    return ItemLookup(metadata=metadata or {"title": "t"}, files=list(files))


def _run(plugin: FakePlugin, identifier: str = "a", budget: SourceBudget | None = None):
    sleeps: list[float] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("shakedown.discovery._sleep", sleeps.append)
        result = lookup(plugin, identifier, budget or SourceBudget(2))
    return result, sleeps


def _calls(identifier: str = "a") -> int:
    return sum(1 for i, _ in FakePlugin.lookup_calls if i == identifier)


def test_lookup_returns_validated_details() -> None:
    FakePlugin.items["a"] = FakeItem("a", [FakeFile("t1.flac", b"abc", format="Flac")])
    result, _ = _run(_plugin())
    assert isinstance(result, ItemDetails)
    assert [f.name for f in result.files] == ["t1.flac"]
    assert result.files[0].size == 3
    assert result.files[0].format == "Flac"
    assert result.metadata["identifier"] == "a"


def test_lookup_passes_the_core_timeout_to_the_plugin() -> None:
    FakePlugin.items["a"] = FakeItem("a", [FakeFile("t1.flac", b"abc")])
    _run(_plugin())
    assert FakePlugin.lookup_calls == [("a", LOOKUP_TIMEOUT)]


@pytest.mark.parametrize("make", [_requests_error, _httpx_error])
def test_server_error_on_every_attempt_fails_after_bounded_retries(make) -> None:
    FakePlugin.lookup_faults["a"] = [make(503)]
    result, sleeps = _run(_plugin())
    assert isinstance(result, LookupFailure)
    assert result.reason == f"lookup failed after {LOOKUP_ATTEMPTS} attempts: HTTP 503"
    assert _calls() == LOOKUP_ATTEMPTS
    assert len(sleeps) == LOOKUP_ATTEMPTS - 1


def test_transient_fault_then_success_recovers() -> None:
    FakePlugin.lookup_faults["a"] = [_requests_error(502), _answer({"name": "x.flac"})]
    result, sleeps = _run(_plugin())
    assert isinstance(result, ItemDetails)
    assert _calls() == 2
    assert len(sleeps) == 1


@pytest.mark.parametrize("make", [_requests_error, _httpx_error])
def test_rate_limit_honors_retry_after(make) -> None:
    FakePlugin.lookup_faults["a"] = [make(429, "7"), _answer({"name": "x.flac"})]
    result, sleeps = _run(_plugin())
    assert isinstance(result, ItemDetails)
    assert sleeps == [7.0]


@pytest.mark.parametrize("header", ["-5", "nan", "inf"])
def test_unusable_retry_after_falls_back_to_backoff(header: str) -> None:
    """An upstream header the core cannot wait on must not escape as an error: that
    would stop the whole listing instead of failing (or retrying) one item."""
    FakePlugin.lookup_faults["a"] = [_requests_error(503, header), _answer({"name": "x"})]
    result, sleeps = _run(_plugin())
    assert isinstance(result, ItemDetails)
    assert sleeps == [1.0]


def test_retry_after_is_clamped_to_the_backoff_cap() -> None:
    FakePlugin.lookup_faults["a"] = [_requests_error(429, "100000"), _answer({"name": "x"})]
    _, sleeps = _run(_plugin())
    assert sleeps and sleeps[0] <= 30.0


@pytest.mark.parametrize(
    ("exc", "said"),
    [
        (requests.Timeout("read timed out"), "timed out"),
        (httpx.ReadTimeout("read timed out"), "timed out"),
        (requests.ConnectionError("reset"), "connection error"),
        (httpx.ConnectError("reset"), "connection error"),
        (TimeoutError("slow"), "timed out"),
        (ConnectionResetError("reset"), "connection error"),
    ],
)
def test_timeouts_and_connection_errors_are_retried(exc: BaseException, said: str) -> None:
    FakePlugin.lookup_faults["a"] = [exc]
    result, _ = _run(_plugin())
    assert isinstance(result, LookupFailure)
    assert result.reason == f"lookup failed after {LOOKUP_ATTEMPTS} attempts: {said}"
    assert _calls() == LOOKUP_ATTEMPTS


def test_client_error_fails_at_once() -> None:
    FakePlugin.lookup_faults["a"] = [_requests_error(404)]
    result, sleeps = _run(_plugin())
    assert isinstance(result, LookupFailure)
    assert result.reason == "lookup failed: HTTP 404"
    assert _calls() == 1
    assert sleeps == []


def test_any_plugin_exception_fails_at_once() -> None:
    FakePlugin.lookup_faults["a"] = [KeyError("files")]
    result, _ = _run(_plugin())
    assert isinstance(result, LookupFailure)
    assert result.reason == "lookup failed: KeyError: 'files'"
    assert _calls() == 1


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (
            _answer({"name": "a.flac"}, {"size": "10"}),
            "source sent a malformed file list: file entry 2 has no name",
        ),
        (
            _answer({"name": ""}),
            "source sent a malformed file list: file entry 1 has no name",
        ),
        (
            _answer({"name": "a.flac", "size": "ten"}),
            "source sent a malformed file list: size of 'a.flac' is not a number",
        ),
        (
            _answer({"name": "a.flac", "size": -1}),
            "source sent a malformed file list: size of 'a.flac' is not a number",
        ),
        (
            _answer({"name": "a.flac", "md5": "not-a-checksum"}),
            "source sent a malformed file list: checksum of 'a.flac' is not an md5",
        ),
        (
            _answer("a.flac"),  # type: ignore[arg-type]
            "source sent a malformed file list: file entry 1 is not a file record",
        ),
        (
            ItemLookup(metadata={}, files="a.flac"),  # type: ignore[arg-type]
            "source sent a malformed answer: its file list is not a list",
        ),
        (
            ItemLookup(metadata=["x"], files=[]),  # type: ignore[arg-type]
            "source sent a malformed answer: its metadata is not a mapping",
        ),
    ],
)
def test_malformed_answer_fails_at_once(answer: ItemLookup, reason: str) -> None:
    FakePlugin.lookup_faults["a"] = [answer]
    result, sleeps = _run(_plugin())
    assert isinstance(result, LookupFailure)
    assert result.reason == reason
    assert _calls() == 1
    assert sleeps == []


def test_answer_of_the_wrong_type_is_malformed() -> None:
    FakePlugin.lookup_faults["a"] = [{"files": []}]  # type: ignore[list-item]
    result, _ = _run(_plugin())
    assert isinstance(result, LookupFailure)
    assert result.reason == "source sent a malformed answer: not an item lookup"


def test_missing_size_and_checksum_are_allowed() -> None:
    """archive.org lists its own `<id>_files.xml` with neither."""
    FakePlugin.lookup_faults["a"] = [
        _answer({"name": "a_files.xml"}, {"name": "a.flac", "size": "12", "md5": GOOD_MD5.upper()})
    ]
    result, _ = _run(_plugin())
    assert isinstance(result, ItemDetails)
    assert result.files[0].size is None and result.files[0].md5 is None
    assert result.files[1].size == 12


def test_raw_fields_quality_selection_needs_are_carried() -> None:
    FakePlugin.lookup_faults["a"] = [
        _answer(
            {"name": "a.mp3", "format": "VBR MP3", "refused": False,
             "derived_from": "a.shn", "bitrate": "192"},
            {"name": "a.shn", "format": "Shorten", "refused": True},
        )
    ]
    result, _ = _run(_plugin())
    assert isinstance(result, ItemDetails)
    mp3, shn = result.files
    assert (mp3.format, mp3.derived_from, mp3.bitrate, mp3.refused) == ("VBR MP3", "a.shn", 192, False)
    assert (shn.refused, shn.derived_from, shn.bitrate) == (True, None, None)


def test_per_file_title_and_track_are_carried_to_the_manifest() -> None:
    """Track title and number reach the recorded manifest for tag filling
    (§spec:tag-filling). They are the source's word, not identity: a non-text value
    is dropped rather than failing the show."""
    FakePlugin.lookup_faults["a"] = [
        _answer(
            {"name": "a1.flac", "format": "Flac", "title": "Scarlet Begonias", "track": "03"},
            {"name": "a2.flac", "format": "Flac", "title": ["odd"], "track": 4},
            {"name": "a3.flac", "format": "Flac"},
        )
    ]
    result, _ = _run(_plugin())
    assert isinstance(result, ItemDetails)
    assert [(f.title, f.track) for f in result.files] == [
        ("Scarlet Begonias", "03"), (None, "4"), (None, None),
    ]
    manifest = describe("a", result).manifest
    assert [(f.title, f.track) for f in manifest.files] == [
        ("Scarlet Begonias", "03"), (None, "4"), (None, None),
    ]


def test_item_restriction_is_carried() -> None:
    FakePlugin.lookup_faults["a"] = [
        ItemLookup(metadata={}, files=[], restriction_reason="access-restricted")
    ]
    result, _ = _run(_plugin())
    assert isinstance(result, ItemDetails)
    assert result.restriction_reason == "access-restricted"


def test_budget_slot_is_free_while_backing_off() -> None:
    """A worker sleeping between attempts holds no connection, so it must not hold a
    budget slot either (§spec:source-budget)."""
    FakePlugin.lookup_faults["a"] = [_requests_error(503), _answer({"name": "x"})]
    budget = SourceBudget(1)
    free_during_sleep: list[bool] = []

    def sleep(_seconds: float) -> None:
        got = budget._sem.acquire(blocking=False)
        free_during_sleep.append(got)
        if got:
            budget._sem.release()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("shakedown.discovery._sleep", sleep)
        lookup(_plugin(), "a", budget)
    assert free_during_sleep == [True]
