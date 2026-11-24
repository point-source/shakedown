"""Optional HTTP control plane: `shakedown serve`. PRD §10.

Lives behind an extras dep (`pip install shakedown[serve]`) so the core CLI
container stays small. Every mutating endpoint passes the write gate
(`shakedown.write_gate`): the bearer token, or the dashboard's own page. The
operator's reverse proxy decides who reaches serve at all; the port is not for
public exposure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing, suppress
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)
from pydantic import BaseModel

from shakedown import __version__, config_save, dashboard_credentials, first_run
from shakedown.activity import HISTORY_DAYS
from shakedown.config import (
    Config,
    ConfigError,
    ConfigRejected,
    UnknownScopeError,
    check_scope,
)
from shakedown.dashboard import (
    LIVE_TTL_S,
    VIEW_TTL_S,
    ConfigTracker,
    SharedAnswers,
    Spawn,
    in_thread,
    register_api,
    shared_answer,
)
from shakedown.dashboard_activity import activity_answer
from shakedown.dashboard_attention import attention_answer
from shakedown.dashboard_config import config_answer
from shakedown.dashboard_health import health_answer
from shakedown.dashboard_library import library_answer
from shakedown.dashboard_network import network_answer
from shakedown.dashboard_page import PAGE_DIR, register_page
from shakedown.dashboard_runs import runs_answer
from shakedown.db import connect_reader
from shakedown.purge import PurgeRefused, check_purgeable
from shakedown.scheduler_watch import start_watching
from shakedown.state import ItemRepo
from shakedown.status import collection_summaries, status_answer
from shakedown.trigger import (
    DEFAULT_ARGV_PREFIX,
    Busy,
    ExitedEarly,
    Failed,
    launch,
    stop_runs,
)
from shakedown.validate import validate_config
from shakedown.write_gate import Refusal, install_refusals, require_token, write_gate

log = logging.getLogger(__name__)

BUSY_MESSAGE = "a run is in progress; try again when it finishes"
# A run stopped on request exits 128 + SIGTERM or SIGINT (§spec:graceful-stop).
STOPPED_EXIT_CODES = (143, 130)
# Why setup's first sync did not start (§spec:first-run-setup `reason`).
SETUP_REASONS: dict[type, str] = {Busy: "busy", Failed: "failed", ExitedEarly: "exited_early"}


class SaveBody(BaseModel):
    """`PUT /api/config`: the revision the editor loaded and the whole document."""
    revision: str | None
    document: Any


class PathsBody(BaseModel):
    """`POST /api/config/check-paths`: the paths to check; no `state_db` is the
    default beside the archive."""
    archive_root: Path
    library_root: Path
    state_db: Path | None = None


class PurgeBody(BaseModel):
    """`POST /api/collections/purge`: the collection, and whether to delete its
    library files or everything, archive included."""
    source: str
    collection: str
    scope: Literal["library", "everything"]


def build_app(
    config_path: Path,
    *,
    argv_prefix: tuple[str, ...] = DEFAULT_ARGV_PREFIX,
    child_env: dict[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    spawn: Spawn = in_thread,
    page_dir: Path = PAGE_DIR,
) -> FastAPI:
    """The control plane over the config file at ``config_path``. Every endpoint
    that needs the config reads the file as it is at the request, so an edit takes
    effect without a restart, and a broken file is a 503 naming the problem rather
    than a crash (§spec:api-sync-runs). ``clock`` times the dashboard's shared
    answers and ``spawn`` starts their refreshes, a thread each by default
    (§spec:dashboard). ``page_dir`` holds the built dashboard page
    (§spec:dashboard-page)."""
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Watch the scheduler's heartbeat and send the stale-scheduler notice
        # (§spec:schedules), from the last config this serve loaded.
        watching = threading.Event()
        start_watching(configs.last_good, watching)
        yield
        watching.set()
        # Shutting down (SIGTERM, Ctrl-C): stop the runs this serve started, so they
        # end `stopped` rather than killed with serve's container (§spec:graceful-stop).
        await asyncio.to_thread(stop_runs, app.state.triggered)

    app = FastAPI(title="shakedown", version=__version__, lifespan=lifespan)
    # Answers and page assets over a kilobyte go out gzipped to a client that asks;
    # a proxy that passes Content-Encoding through hands them on (§spec:dashboard).
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    install_refusals(app)
    app.state.answers = SharedAnswers(clock, spawn)
    # Every load goes through the tracker, so the dashboard knows the last config
    # this serve loaded, including the one at start-up.
    configs = ConfigTracker(config_path)
    app.state.configs = configs
    with suppress(ConfigError):
        configs.load_now()
    # Runs this serve started that may still be going, newest last; each one's
    # watcher sets `done` when it exits.
    app.state.triggered = []
    registry = CollectorRegistry()
    g_items = Gauge(
        "shakedown_items_total",
        "Items per (source, collection, status).",
        ["source", "collection", "status"],
        registry=registry,
    )
    g_bytes = Gauge(
        "shakedown_bytes_on_disk",
        "Size of the archived files on record per (source, collection).",
        ["source", "collection"],
        registry=registry,
    )
    g_drift = Gauge(
        "shakedown_drifted_files",
        "Files reported as drifted at last verify --deep.",
        ["source", "collection"],
        registry=registry,
    )
    c_sync = Counter(
        "shakedown_sync_runs_total",
        "Number of sync runs triggered via the control plane.",
        ["result"],
        registry=registry,
    )

    def scope_args(source: str | None, collection: str | None) -> list[str]:
        return [
            *(["--source", source] if source else []),
            *(["--collection", collection] if collection else []),
        ]

    def start_run(
        config: Config,
        verb: str,
        source: str | None,
        collection: str | None,
        args: list[str],
        *,
        on_exit: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        """Start the run as a separate one-shot process and answer once it holds the
        writer lock (§spec:api-sync-runs)."""
        refused = launch_run(config, args, on_exit=on_exit)
        if isinstance(refused, Busy):
            raise HTTPException(409, BUSY_MESSAGE)
        if isinstance(refused, Failed):
            raise HTTPException(503, refused.error)
        if isinstance(refused, ExitedEarly):
            raise HTTPException(
                500,
                f"the run exited before starting (exit {refused.returncode}); "
                f"see the serve logs",
            )
        # Each collection's run record opens when it starts; GET /status shows its id
        # as the collection's latest run (§spec:api-sync-runs).
        return {"started": verb, "source": source, "collection": collection}

    def launch_run(
        config: Config, args: list[str], *, on_exit: Callable[[int], None] | None = None
    ) -> Busy | Failed | ExitedEarly | None:
        """Start the run; None once it holds the writer lock, else why it did not."""
        assert config.state_db is not None  # filled in by Config validation
        result = launch(
            config_path,
            args,
            state_db=config.state_db,
            on_exit=on_exit,
            argv_prefix=argv_prefix,
            env=child_env,
        )
        if isinstance(result, Busy | Failed | ExitedEarly):
            return result
        # serve lives for months: keep only the runs still going, plus this one.
        app.state.triggered = [t for t in app.state.triggered if not t.done.is_set()]
        app.state.triggered.append(result.run)
        return None

    def count_sync(returncode: int) -> None:
        c_sync.labels(
            "ok" if returncode == 0
            else "stopped" if returncode in STOPPED_EXIT_CODES
            else "fail"
        ).inc()

    def current_config() -> Config:
        try:
            return configs.load_now()
        except ConfigError as e:
            raise HTTPException(503, str(e)) from e

    def require_scope(config: Config, source: str | None, collection: str | None) -> None:
        try:
            check_scope(config, source, collection)
        except UnknownScopeError as e:
            raise HTTPException(404, str(e)) from e

    # The dashboard's read views (§spec:dashboard).
    register_api(app, "/api/health", VIEW_TTL_S, health_answer)
    register_api(app, "/api/activity", LIVE_TTL_S, activity_answer)
    register_api(app, "/api/library", VIEW_TTL_S, library_answer)
    register_api(app, "/api/attention", VIEW_TTL_S, attention_answer)

    # A window longer than the history runs keep would read as empty; outside 1..30
    # the request is a 422, never a cache entry.
    @app.get("/api/runs")
    def runs(days: int = Query(HISTORY_DAYS, ge=1, le=HISTORY_DAYS)) -> dict[str, Any]:
        return shared_answer(app, ("/api/runs", days), VIEW_TTL_S, runs_answer(days))

    @app.get("/api/network")
    def network(days: int = Query(HISTORY_DAYS, ge=1, le=HISTORY_DAYS)) -> dict[str, Any]:
        return shared_answer(app, ("/api/network", days), VIEW_TTL_S, network_answer(days))

    # The config resource (§spec:dashboard-settings): read at each request, never a
    # shared answer, so a hand edit shows on the next refresh.
    @app.get("/api/config")
    def config_resource() -> dict[str, Any]:
        return config_answer(config_path)

    # First-run setup (§spec:first-run-setup): the defaults it offers, read-only.
    @app.get("/api/setup")
    def setup_defaults() -> dict[str, Any]:
        return first_run.setup_defaults()

    # Finishing setup: write the file, then start the collection's first sync as
    # POST /sync does. The file stays written when the sync cannot start. The body
    # is read by hand so a bad one never answers FastAPI's detail, which quotes it.
    @app.post("/api/setup", dependencies=[Depends(write_gate)])
    async def finish_setup(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            raise Refusal(422, "invalid_body") from None
        try:
            done = await asyncio.to_thread(first_run.finish, config_path, body)
        except ConfigRejected as e:
            return JSONResponse({"errors": [p.to_dict() for p in e.errors]}, status_code=422)
        config = await asyncio.to_thread(configs.load_now)
        refused = await asyncio.to_thread(
            launch_run,
            config,
            ["sync", *scope_args(done.source, done.collection)],
            on_exit=count_sync,
        )
        answer: dict[str, Any] = {"revision": done.revision, "warnings": done.warnings}
        if refused is None:
            answer["started"] = {"source": done.source, "collection": done.collection}
        else:
            log.warning("setup's first sync did not start: %s", refused)
            answer["started"] = None
            answer["reason"] = SETUP_REASONS[type(refused)]
        return JSONResponse(answer, status_code=201)

    # Saving it (§spec:dashboard-settings) and the edit lock's override
    # (§spec:edit-lock), behind the write gate like every write.
    @app.put("/api/config", dependencies=[Depends(write_gate)])
    def save_config(body: SaveBody) -> JSONResponse:
        try:
            saved = config_save.save(config_path, body.revision, body.document)
        except ConfigRejected as e:
            return JSONResponse({"errors": [p.to_dict() for p in e.errors]}, status_code=422)
        return JSONResponse({"revision": saved.revision, "warnings": saved.warnings})

    @app.post("/api/config/check-paths", dependencies=[Depends(write_gate)])
    def check_paths(body: PathsBody) -> dict[str, Any]:
        return {
            "warnings": config_save.check_paths(
                body.archive_root, body.library_root, body.state_db
            )
        }

    @app.post("/api/config/override", dependencies=[Depends(write_gate)])
    def override() -> dict[str, str]:
        config_save.override(config_path)
        return {"edit": "override"}

    # Credentials entered in the dashboard (§spec:dashboard-credentials): behind the
    # write gate, not under the edit lock. The body is read by hand so that a bad
    # one never answers FastAPI's validation detail, which quotes the input.
    @app.put("/api/credentials", dependencies=[Depends(write_gate)])
    async def put_credentials(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            raise Refusal(422, "invalid_body") from None
        return await asyncio.to_thread(
            dashboard_credentials.put_credentials, current_config(), body
        )

    @app.delete("/api/credentials", dependencies=[Depends(write_gate)])
    def delete_credentials(source: str = Query()) -> dict[str, Any]:
        return dashboard_credentials.delete_credentials(current_config(), source)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    def status() -> JSONResponse:
        config = current_config()
        with closing(connect_reader(config.state_db)) as conn:  # type: ignore[arg-type]
            answer = status_answer(config, conn)
        return JSONResponse(json.loads(json.dumps(answer, default=str)))

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        config = current_config()
        # Refresh gauges from current DB state on each scrape. Clear them first so a
        # collection removed from the config stops reporting.
        for gauge in (g_items, g_bytes, g_drift):
            gauge.clear()
        with closing(connect_reader(config.state_db)) as conn:  # type: ignore[arg-type]
            items = ItemRepo(conn)
            for summary in collection_summaries(config, conn):
                scope = (summary["source"], summary["collection"])
                for status_, n in items.count_by_status(*scope).items():
                    g_items.labels(*scope, status_.value).set(n)
                g_bytes.labels(*scope).set(summary["bytes_on_disk"])
                g_drift.labels(*scope).set(summary["drift_files"])
        body = generate_latest(registry)
        return PlainTextResponse(body.decode(), media_type=CONTENT_TYPE_LATEST)

    # The write gate runs as a route dependency, before the handler: a refused caller
    # learns nothing about the config or its scopes (§spec:dashboard-run-controls).
    @app.post("/sync", status_code=202, dependencies=[Depends(write_gate)])
    def trigger_sync(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
    ) -> dict[str, Any]:
        config = current_config()
        require_scope(config, source, collection)
        return start_run(
            config, "sync", source, collection, ["sync", *scope_args(source, collection)],
            on_exit=count_sync,
        )

    @app.post("/verify", status_code=202, dependencies=[Depends(write_gate)])
    def trigger_verify(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        deep: bool = Query(default=False),
        reconform: bool = Query(default=False),
    ) -> dict[str, Any]:
        config = current_config()
        require_scope(config, source, collection)
        if reconform and not deep:
            raise HTTPException(400, "reconform requires deep")
        args = ["verify", *scope_args(source, collection)]
        if deep:
            args.append("--deep")
        if reconform:
            args += ["--reconform", "--yes"]
        return start_run(config, "verify", source, collection, args)

    @app.post("/api/history/trim", status_code=202, dependencies=[Depends(write_gate)])
    def trim_history() -> dict[str, Any]:
        """A fresh `trim` run (§spec:run-error-retention)."""
        start_run(current_config(), "trim", None, None, ["trim-history"])
        return {"started": "trim"}

    # Deleting a removed collection's files (§spec:collection-removal). What the file
    # does not hold stays open while the file is locked, so no edit-lock check.
    @app.post("/api/collections/purge", status_code=202, dependencies=[Depends(write_gate)])
    def purge_collection(body: PurgeBody) -> dict[str, Any]:
        config = current_config()
        try:
            check_purgeable(config, body.source, body.collection)
        except PurgeRefused as e:
            if e.code == "configured":
                raise Refusal(409, "configured") from e
            raise HTTPException(404, str(e)) from e
        args = ["purge", "--source", body.source, "--collection", body.collection]
        if body.scope == "everything":
            args.append("--archive")
        started = start_run(config, "purge", body.source, body.collection, args)
        return {**started, "scope": body.scope}

    @app.post("/validate", dependencies=[Depends(write_gate)])
    def validate(
        request: Request,
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        live_handoff: bool = Query(default=False),
    ) -> JSONResponse:
        # Validation records its outcomes, so it passes the write gate. A live handoff
        # test sends a real webhook or runs the configured command, and the dashboard
        # never offers it: it takes the token only (§spec:dashboard-run-controls).
        if live_handoff:
            require_token(request)
        config = current_config()
        require_scope(config, source, collection)
        report = validate_config(
            config,
            source_filter=source,
            collection_filter=collection,
            live_handoff=live_handoff,
        )
        return JSONResponse(json.loads(json.dumps(report.to_dict(), default=str)))

    register_page(app, page_dir)
    return app


def serve(config_path: Path, *, host: str, port: int) -> None:
    import uvicorn

    app = build_app(config_path)
    uvicorn.run(app, host=host, port=port)
