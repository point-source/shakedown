"""Optional HTTP control plane: `shakedown serve`. PRD §10.

Lives behind an extras dep (`pip install shakedown[serve]`) so the core CLI
container stays small. Auth is a shared secret via X-Shakedown-Token; the
endpoints are explicitly NOT for public exposure.
"""
from __future__ import annotations

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
from shakedown.state import DriftRepo, ItemRepo, RunRepo
from shakedown.status import _collection_summary
from shakedown.sync import run_sync as do_sync
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

    def require_token(token: str | None) -> None:
        expected = os.environ.get(TOKEN_ENV)
        if not expected:
            raise HTTPException(503, f"server not configured: set {TOKEN_ENV} in env")
        if token != expected:
            raise HTTPException(401, "invalid token")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    def status() -> JSONResponse:
        conn = connect(config.state_db)  # type: ignore[arg-type]
        items = ItemRepo(conn)
        runs = RunRepo(conn)
        drift = DriftRepo(conn)
        summaries: list[dict[str, Any]] = []
        for source in config.sources:
            for collection in source.collections:
                summaries.append(
                    _collection_summary(config, source.name, collection.name, items, runs, drift)
                )
        return JSONResponse(json.loads(json.dumps(summaries, default=str)))

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        # Refresh gauges from current DB state on each scrape.
        conn = connect(config.state_db)  # type: ignore[arg-type]
        items = ItemRepo(conn)
        drift = DriftRepo(conn)
        for source in config.sources:
            for collection in source.collections:
                counts = items.count_by_status(source.name, collection.name)
                for status_, n in counts.items():
                    g_items.labels(source.name, collection.name, status_.value).set(n)
                summary = _collection_summary(
                    config, source.name, collection.name, items, RunRepo(conn), drift
                )
                g_bytes.labels(source.name, collection.name).set(summary["bytes_on_disk"])
                g_drift.labels(source.name, collection.name).set(summary["drift_files"])
        body = generate_latest(registry)
        return PlainTextResponse(body.decode(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/sync")
    def trigger_sync(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        x_shakedown_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_shakedown_token)
        rc = do_sync(config, source_filter=source, collection_filter=collection)
        c_sync.labels("ok" if rc == 0 else "fail").inc()
        return {"exit_code": rc}

    @app.post("/verify")
    def trigger_verify(
        source: str | None = Query(default=None),
        collection: str | None = Query(default=None),
        deep: bool = Query(default=False),
        reconform: bool = Query(default=False),
        x_shakedown_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_shakedown_token)
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

    return app


def serve(config: Config, *, host: str, port: int) -> None:
    import uvicorn

    app = build_app(config)
    uvicorn.run(app, host=host, port=port)
