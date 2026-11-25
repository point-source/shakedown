"""The archive.org login is withdrawn (§spec:dashboard-credentials): no endpoint
takes one, no setting supplies one, and a run asks archive.org for none. A
stream-only show still mirrors its public files and lists the private ones as
refused, which is all a login could ever have changed."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

import shakedown.sync as sync_module
from shakedown.config import load
from shakedown.db import connect
from shakedown.selection import LOSSLESS_REFUSED_REASON
from shakedown.server import build_app
from shakedown.state import ItemRepo
from shakedown.sync import run_sync
from tests.fake_archive import FakeArchive, Reply, fake_archive, json_reply

LOGIN = "/services/xauthn/"
SEARCH = "/services/search/v1/scrape"
SHOW = "gd69-12-26.sbd"
MP3 = b"public lossy audio"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}

CONFIG = """\
archive_root: {archive}
library_root: {library}
sources:
  - name: ia
    type: ia
{auth}    collections:
      - name: gd
        query: 'collection:GratefulDead'
"""
AUTH = """\
    auth:
      email_env: IA_EMAIL
      password_env: IA_PASSWORD
"""


@pytest.fixture()
def archive() -> Iterator[FakeArchive]:
    with fake_archive() as fake:
        yield fake


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    archive, library = tmp_path / "archive", tmp_path / "library"
    archive.mkdir()
    library.mkdir()
    path = tmp_path / "shakedown.yaml"
    path.write_text(CONFIG.format(archive=archive, library=library, auth=""))
    return path


# -- the API takes no login --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_the_credentials_endpoint_is_gone(config_path: Path, method: str) -> None:
    app = build_app(config_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        response = await client.request(
            method, "/api/credentials", params={"source": "ia"},
            json={"source": "ia", "email": "me@example.org", "password": "pw"},
            headers=SAME_ORIGIN,
        )
    assert response.status_code == 404


# -- a run asks archive.org for no login -----------------------------------------


def _stream_only_show(archive: FakeArchive) -> None:
    """Shaped like gd69-12-26.sbd.murphy.1821.sbeok.shnf: archive.org marks the
    lossless original `private`; the MP3 stays public."""
    import hashlib

    archive.route(SEARCH, json_reply({"items": [{"identifier": SHOW}], "count": 1,
                                      "total": 1}))
    archive.item(
        SHOW,
        [
            {"name": "d1t01.shn", "size": "9", "md5": "a" * 32, "format": "Shorten",
             "private": "true"},
            {"name": "d1t01.mp3", "size": str(len(MP3)),
             "md5": hashlib.md5(MP3).hexdigest(), "format": "VBR MP3"},
        ],
        {"date": "1969-12-26", "title": "Show",
         "collection": ["GratefulDead", "etree", "stream_only"],
         "access-restricted-item": "true"},
    )
    archive.file(SHOW, "d1t01.shn", Reply(403, b"forbidden"))
    archive.file(SHOW, "d1t01.mp3", Reply(200, MP3))


@pytest.mark.parametrize("auth", ["", AUTH], ids=["no-auth-key", "leftover-auth-key"])
def test_a_sync_with_ia_variables_set_logs_in_nowhere_and_lists_private_files_refused(
    config_path: Path, archive: FakeArchive, monkeypatch: pytest.MonkeyPatch, auth: str
) -> None:
    monkeypatch.setattr(sync_module, "_sleep", lambda _s: None)
    monkeypatch.setenv("IA_EMAIL", "me@example.org")
    monkeypatch.setenv("IA_PASSWORD", "a-password")
    if auth:
        text = config_path.read_text()
        config_path.write_text(text.replace("    collections:", auth + "    collections:"))
    _stream_only_show(archive)
    config = load(config_path)

    assert run_sync(config) == 0

    assert archive.calls_to(LOGIN) == []
    assert archive.calls_to(f"/download/{SHOW}/d1t01.shn") == []
    assert all("Authorization" not in call.headers for call in archive.calls)
    assert (config.archive_root / "ia" / "gd" / SHOW / "d1t01.mp3").read_bytes() == MP3
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        item = ItemRepo(conn).get("ia", "gd", SHOW)
    assert item is not None and item.restriction_reason == LOSSLESS_REFUSED_REASON
