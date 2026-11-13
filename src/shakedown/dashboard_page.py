"""The built dashboard page, served by `serve` at its root (§spec:dashboard-page).

The page build (`web/dist/`) ships as package data in `shakedown/dashboard_web/`:
`index.html` plus content-hashed files under `assets/`. A source checkout without
a build has no `index.html`, so `GET /` is a 404 and every other route works.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

PAGE_DIR = Path(str(files("shakedown") / "dashboard_web"))

# Asset names change with their content, so a cached copy is never stale.
ASSET_CACHE = "public, max-age=31536000, immutable"


class _Assets(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = ASSET_CACHE
        return response


def register_page(app: FastAPI, page_dir: Path) -> None:
    """Serve the page in ``page_dir``. Call after every other route: these match
    last, and there is no catch-all, so an unknown path stays a JSON 404."""

    @app.get("/", include_in_schema=False)
    def page() -> FileResponse:
        index = page_dir / "index.html"
        if not index.is_file():
            raise HTTPException(404)
        # no-cache: after an upgrade the browser loads the new page, never a stale
        # one against the newer API.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    # Without a build there is no mount, so /assets/* is the router's JSON 404.
    if (page_dir / "assets").is_dir():
        app.mount("/assets", _Assets(directory=page_dir / "assets"))
