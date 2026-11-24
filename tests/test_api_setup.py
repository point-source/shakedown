"""First-run setup (§spec:first-run-setup): `serve` with no config file, the
defaults setup offers, the path check, and finishing setup."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.server import build_app

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
INDEX = "<!doctype html><title>shakedown</title>"


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    """Where the config would be; nothing is there."""
    return tmp_path / "shakedown-config" / "shakedown.yaml"


def _page(tmp_path: Path) -> Path:
    page = tmp_path / "page"
    (page / "assets").mkdir(parents=True)
    (page / "index.html").write_text(INDEX)
    return page


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# -- serve with no config file -------------------------------------------------

@pytest.mark.asyncio
async def test_serve_with_no_file_opens_the_page_and_answers_missing(
    config_path: Path, tmp_path: Path
) -> None:
    app = build_app(config_path, page_dir=_page(tmp_path))
    async with _client(app) as client:
        page = await client.get("/")
        config = (await client.get("/api/config")).json()
        reads = {
            path: await client.get(path)
            for path in ("/api/health", "/api/activity", "/api/library", "/status")
        }
    assert (page.status_code, page.text) == (200, INDEX)
    assert config["state"] == "missing"
    assert (config["revision"], config["document"], config["edit"]) == (None, None, "locked")
    assert config["source_types"]
    # The read endpoints answer as they always have with no config: 503 naming it.
    for path, response in reads.items():
        assert response.status_code == 503, path
        assert str(config_path) in response.json()["detail"], path
    assert not config_path.parent.exists()  # nothing was written


# -- the defaults setup offers -------------------------------------------------

@pytest.mark.asyncio
async def test_setup_defaults_work_on_the_example_stack(config_path: Path) -> None:
    async with _client(build_app(config_path)) as client:
        response = await client.get("/api/setup")
        config = (await client.get("/api/config")).json()
    assert response.status_code == 200
    answer = response.json()
    assert {k: answer[k] for k in ("archive_root", "library_root", "state_db", "schedule")} == {
        "archive_root": "/data/archive",
        "library_root": "/data/library",
        "state_db": "/data/archive/.shakedown/state.db",
        "schedule": "0 3 * * 0",
    }
    assert answer["source_types"] == config["source_types"]
    assert answer["generated_at"]
    assert not config_path.parent.exists()  # nothing was written


# -- the path check before finishing -------------------------------------------

@pytest.mark.asyncio
async def test_check_paths_with_no_file_creates_the_folders(
    config_path: Path, tmp_path: Path
) -> None:
    archive, library = tmp_path / "data" / "archive", tmp_path / "data" / "library"
    async with _client(build_app(config_path)) as client:
        response = await client.post(
            "/api/config/check-paths",
            headers=SAME_ORIGIN,
            json={"archive_root": str(archive), "library_root": str(library)},
        )
    assert (response.status_code, response.json()) == (200, {"warnings": []})
    assert archive.is_dir() and library.is_dir()
    assert (archive / ".shakedown").is_dir()  # the default state_db's folder
    assert not config_path.parent.exists()
