"""Optional HTTP control plane: `shakedown serve`. PRD §10.

Lives behind an extras dep (`pip install shakedown[serve]`) so the core CLI
container stays small. Auth is a shared secret via `Authorization: Bearer`;
the endpoints are explicitly NOT for public exposure.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from contextlib import closing, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

from shakedown import __version__
from shakedown.config import Config, ConfigError, UnknownScopeError, check_scope
from shakedown.dashboard import (
    LIVE_TTL_S,
    VIEW_TTL_S,
    ConfigTracker,
    SharedAnswers,
    register_api,
    shared_answer,
)
from shakedown.dashboard_activity import activity_answer
from shakedown.dashboard_attention import attention_answer
from shakedown.dashboard_health import health_answer
from shakedown.dashboard_library import library_answer
from shakedown.dashboard_runs import MAX_DAYS, runs_answer
from shakedown.db import connect_reader
from shakedown.state import ItemRepo
from shakedown.status import collection_summaries
from shakedown.trigger import DEFAULT_ARGV_PREFIX, Busy, ExitedEarly, Failed, launch
from shakedown.validate import validate_config

log = logging.getLogger(__name__)

TOKEN_ENV = "SHAKEDOWN_API_TOKEN"
BUSY_MESSAGE = "a run is in progress; try again when it finishes"


def build_app(
    config_path: Path,
    *,
    argv_prefix: tuple[str, ...] = DEFAULT_ARGV_PREFIX,
    child_env: dict[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """The control plane over the config file at ``config_path``. Every endpoint
    that needs the config reads the file as it is at the request, so an edit takes
    effect without a restart, and a broken file is a 503 naming the problem rather
    than a crash (§spec:api-sync-runs). ``clock`` times the dashboard's shared
    answers (§spec:dashboard)."""
    app = FastAPI(title="shakedown", version=__version__)
    app.state.answers = SharedAnswers(clock)
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

    def require_token(authorization: str | None) -> None:
        expected = os.environ.get(TOKEN_ENV)
        if not expected:
            raise HTTPException(
                503, f"mutating endpoints disabled: set {TOKEN_ENV} in env"
            )
        scheme, _, token = (authorization or "").partition(" ")
        # Compare as bytes: hmac.compare_digest rejects non-ASCII str operands
        # with TypeError, which would surface a malformed header as a 500.
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.encode(), expected.encode()
        ):
            raise HTTPException(401, "invalid or missing bearer token")

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
        writer lock and has opened its run records (§spec:api-sync-runs)."""
        assert config.state_db is not None  # filled in by Config validation
        result = launch(
            config_path,
            args,
            state_db=config.state_db,
            on_exit=on_exit,
            argv_prefix=argv_prefix,
            env=child_env,
        )
        if isinstance(result, Busy):
            raise HTTPException(409, BUSY_MESSAGE)
        if isinstance(result, Failed):
            raise HTTPException(503, result.error)
        if isinstance(result, ExitedEarly):
            raise HTTPException(
                500,
                f"the run exited before starting (exit {result.returncode}); "
                f"see the serve logs",
            )
        # serve lives for months: keep only the runs still going, plus this one.
        app.state.triggered = [t for t in app.state.triggered if not t.done.is_set()]
        app.state.triggered.append(result.run)
        return {
            "started": verb,
            "source": source,
            "collection": collection,
            "run_ids": [r["run_id"] for r in result.run.runs],
        }

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

    @app.get("/api/runs")
    def runs(days: int = Query(MAX_DAYS, ge=1, le=MAX_DAYS)) -> dict[str, Any]:
        return shared_answer(app, ("/api/runs", days), VIEW_TTL_S, runs_answer(days))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    def status() -> JSONResponse:
        config = current_config()
        with closing(connect_reader(config.state_db)) as conn:  # type: ignore[arg-type]
            summaries = collection_summaries(config, conn)
        return JSONResponse(json.loads(json.dumps(summaries, default=str)))

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

    @app.post("/sync", status_code=202)
    def trigger_sync(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization)
        config = current_config()
        require_scope(config, source, collection)

        def count(returncode: int) -> None:
            c_sync.labels("ok" if returncode == 0 else "fail").inc()

        return start_run(
            config, "sync", source, collection, ["sync", *scope_args(source, collection)],
            on_exit=count,
        )

    @app.post("/verify", status_code=202)
    def trigger_verify(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        deep: bool = Query(default=False),
        reconform: bool = Query(default=False),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization)
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

    @app.post("/validate")
    def validate(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        live_handoff: bool = Query(default=False),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        # Default validation is read-only and follows the /status posture (no token).
        # A live handoff test sends a real webhook / runs the configured command, so it
        # is a mutating operation and carries the same bearer-token posture as ad-hoc
        # sync and verify (§spec:serve, §spec:setup-readiness-validation).
        if live_handoff:
            require_token(authorization)
        config = current_config()
        require_scope(config, source, collection)
        report = validate_config(
            config,
            source_filter=source,
            collection_filter=collection,
            live_handoff=live_handoff,
        )
        return JSONResponse(json.loads(json.dumps(report.to_dict(), default=str)))

    return app


def serve(config_path: Path, *, host: str, port: int) -> None:
    import uvicorn

    app = build_app(config_path)
    uvicorn.run(app, host=host, port=port)
