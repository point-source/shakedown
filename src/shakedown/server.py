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

from shakedown.config import Config
from shakedown.db import connect
from shakedown.state import DriftRepo, ItemRepo, OperationOutcomeRepo, RunRepo
from shakedown.status import _collection_summary
from shakedown.sync import run_sync as do_sync
from shakedown.validate import validate_config
from shakedown.verify import run_verify as do_verify

log = logging.getLogger(__name__)

TOKEN_ENV = "SHAKEDOWN_API_TOKEN"


def build_app(config: Config) -> FastAPI:
    app = FastAPI(title="shakedown", version="0.1.0")
    registry = CollectorRegistry()
    g_items = Gauge(
        "shakedown_items_total",
        "Items per (source, collection, status).",
        ["source", "collection", "status"],
        registry=registry,
    )
    g_bytes = Gauge(
        "shakedown_bytes_on_disk",
        "Archive disk usage per (source, collection).",
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

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    async def status() -> JSONResponse:
        conn = connect(config.state_db)  # type: ignore[arg-type]
        items = ItemRepo(conn)
        runs = RunRepo(conn)
        drift = DriftRepo(conn)
        outcomes = OperationOutcomeRepo(conn)
        summaries: list[dict[str, Any]] = []
        for source in config.sources:
            for collection in source.collections:
                summaries.append(
                    _collection_summary(
                        config, source.name, collection.name, items, runs, drift, outcomes
                    )
                )
        return JSONResponse(json.loads(json.dumps(summaries, default=str)))

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        # Refresh gauges from current DB state on each scrape.
        conn = connect(config.state_db)  # type: ignore[arg-type]
        items = ItemRepo(conn)
        drift = DriftRepo(conn)
        runs = RunRepo(conn)
        outcomes = OperationOutcomeRepo(conn)
        for source in config.sources:
            for collection in source.collections:
                counts = items.count_by_status(source.name, collection.name)
                for status_, n in counts.items():
                    g_items.labels(source.name, collection.name, status_.value).set(n)
                summary = _collection_summary(
                    config, source.name, collection.name, items, runs, drift, outcomes
                )
                g_bytes.labels(source.name, collection.name).set(summary["bytes_on_disk"])
                g_drift.labels(source.name, collection.name).set(summary["drift_files"])
        body = generate_latest(registry)
        return PlainTextResponse(body.decode(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/sync")
    async def trigger_sync(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization)
        rc = do_sync(config, source_filter=source, collection_filter=collection)
        c_sync.labels("ok" if rc == 0 else "fail").inc()
        return {"exit_code": rc}

    @app.post("/verify")
    async def trigger_verify(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        deep: bool = Query(default=False),
        reconform: bool = Query(default=False),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization)
        rc = do_verify(
            config,
            source_filter=source,
            collection_filter=collection,
            deep=deep,
            reconform=reconform,
            list_drift=False,
            assume_yes=True,
        )
        return {"exit_code": rc}

    @app.get("/validate")
    async def validate(
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
        report = validate_config(
            config,
            source_filter=source,
            collection_filter=collection,
            live_handoff=live_handoff,
        )
        return JSONResponse(json.loads(json.dumps(report.to_dict(), default=str)))

    return app


def serve(config: Config, *, host: str, port: int) -> None:
    import uvicorn

    app = build_app(config)
    uvicorn.run(app, host=host, port=port)
