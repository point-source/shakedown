"""`PUT /api/credentials` and `DELETE /api/credentials` (§spec:dashboard-credentials
"Write-only"): store or forget a source's credentials behind the write gate, never
under the edit lock, answering the source's verdict on the login and never a value."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.credentials import Login, stored
from shakedown.edit_lock import read_edit
from shakedown.server import build_app
from tests.fake_archive import FakeArchive, fake_archive, login_accepted, login_rejected

CONFIG = """\
archive_root: {archive}
library_root: {library}
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'collection:GratefulDead'
  - name: lma
    type: etree
    collections:
      - name: lma
        query: 'collection:etree'
"""
# The dashboard page marks every write same-origin JSON, a DELETE too (the write gate).
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
EMAIL, PASSWORD = "entered-email@example.org", "entered-password-91c"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IA_EMAIL", raising=False)
    monkeypatch.delenv("IA_PASSWORD", raising=False)
    monkeypatch.setattr("internetarchive.config.sleep", lambda _s: None)


@pytest.fixture()
def archive() -> Iterator[FakeArchive]:
    with fake_archive() as fake:
        yield fake


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, Path]:
    archive, library = tmp_path / "archive", tmp_path / "library"
    archive.mkdir()
    library.mkdir()
    config = tmp_path / "shakedown.yaml"
    config.write_text(CONFIG.format(archive=archive, library=library))
    return {"config": config, "state_db": archive / ".shakedown" / "state.db"}


def _client(paths: dict[str, Path]) -> httpx.AsyncClient:
    app = build_app(paths["config"])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _put(client: httpx.AsyncClient, body: Any, headers: Any = SAME_ORIGIN
               ) -> httpx.Response:
    return await client.put("/api/credentials", json=body, headers=headers)


def _body(**overrides: Any) -> dict[str, Any]:
    return {"source": "ia", "email": EMAIL, "password": PASSWORD, **overrides}


@pytest.mark.asyncio
async def test_storing_accepted_credentials(paths: dict[str, Path], archive: FakeArchive
                                            ) -> None:
    archive.login(login_accepted())
    async with _client(paths) as client:
        response = await _put(client, _body())
        config = (await client.get("/api/config")).json()
    assert (response.status_code, response.json()) == (
        200, {"source": "ia", "credentials": "dashboard", "check": "accepted"}
    )
    assert stored(paths["state_db"], "ia") == Login(EMAIL, PASSWORD)
    assert config["credentials"][0] == {"source": "ia", "credentials": "dashboard"}


@pytest.mark.asyncio
async def test_rejected_credentials_are_still_stored(
    paths: dict[str, Path], archive: FakeArchive
) -> None:
    archive.login(login_rejected())
    async with _client(paths) as client:
        response = await _put(client, _body())
    assert response.json() == {"source": "ia", "credentials": "dashboard", "check": "rejected"}
    assert stored(paths["state_db"], "ia") == Login(EMAIL, PASSWORD)


@pytest.mark.asyncio
async def test_an_unreachable_source_is_unchecked_and_still_stored(
    paths: dict[str, Path], archive: FakeArchive
) -> None:
    archive.login(ConnectionError("no route to host"))
    async with _client(paths) as client:
        response = await _put(client, _body())
    assert response.json()["check"] == "unchecked"
    assert stored(paths["state_db"], "ia") is not None


@pytest.mark.asyncio
async def test_environment_credentials_stay_in_effect(
    paths: dict[str, Path], archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer's status is the source's after the change: the environment still
    wins; the check is the entered login's."""
    monkeypatch.setenv("IA_EMAIL", "env@example.org")
    monkeypatch.setenv("IA_PASSWORD", "envpw")
    archive.login(login_rejected())
    async with _client(paths) as client:
        response = await _put(client, _body())
    assert response.json() == {
        "source": "ia", "credentials": "environment", "check": "rejected"
    }
    assert b"entered-email" in archive.calls_to("/services/xauthn/")[0].body


@pytest.mark.asyncio
async def test_values_are_trimmed(paths: dict[str, Path], archive: FakeArchive) -> None:
    archive.login(login_accepted())
    async with _client(paths) as client:
        await _put(client, _body(email=f" {EMAIL}\r\n", password=f"{PASSWORD}\n"))
    assert stored(paths["state_db"], "ia") == Login(EMAIL, PASSWORD)


@pytest.mark.asyncio
async def test_deleting_forgets_them(paths: dict[str, Path], archive: FakeArchive) -> None:
    archive.login(login_accepted())
    async with _client(paths) as client:
        await _put(client, _body())
        response = await client.delete("/api/credentials", params={"source": "ia"},
                                       headers=SAME_ORIGIN)
        again = await client.delete("/api/credentials", params={"source": "ia"},
                                    headers=SAME_ORIGIN)
        config = (await client.get("/api/config")).json()
    assert (response.status_code, response.json()) == (
        200, {"source": "ia", "credentials": "none"}
    )
    assert again.status_code == 200
    assert stored(paths["state_db"], "ia") is None
    assert config["credentials"][0] == {"source": "ia", "credentials": "none"}


@pytest.mark.asyncio
async def test_deleting_before_any_database_creates_none(paths: dict[str, Path]) -> None:
    async with _client(paths) as client:
        response = await client.delete("/api/credentials", params={"source": "ia"},
                                       headers=SAME_ORIGIN)
    assert response.status_code == 200
    assert not paths["state_db"].exists()


@pytest.mark.asyncio
async def test_an_unknown_source_or_one_without_credentials_is_404(
    paths: dict[str, Path], archive: FakeArchive
) -> None:
    async with _client(paths) as client:
        answers = [
            await _put(client, _body(source="nope")),
            await _put(client, _body(source="lma")),
            await client.delete("/api/credentials", params={"source": "nope"},
                                headers=SAME_ORIGIN),
            await client.delete("/api/credentials", params={"source": "lma"},
                                headers=SAME_ORIGIN),
        ]
    assert [(r.status_code, r.json()) for r in answers] == [
        (404, {"code": "unknown_source"}),
        (404, {"code": "takes_no_credentials"}),
        (404, {"code": "unknown_source"}),
        (404, {"code": "takes_no_credentials"}),
    ]
    assert archive.calls == []
    assert stored(paths["state_db"], "nope") is None
    assert stored(paths["state_db"], "lma") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _body(password=""),
        _body(email="   "),
        _body(password=123),
        {"source": "ia", "email": EMAIL},
        _body(extra=PASSWORD),
        [EMAIL, PASSWORD],
        "not an object",
    ],
)
async def test_a_bad_body_is_422_and_never_echoes_a_value(
    paths: dict[str, Path], archive: FakeArchive, body: Any
) -> None:
    async with _client(paths) as client:
        response = await _put(client, body)
    assert (response.status_code, response.json()) == (422, {"code": "invalid_body"})
    assert archive.calls == []
    assert stored(paths["state_db"], "ia") is None


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_422(paths: dict[str, Path]) -> None:
    async with _client(paths) as client:
        response = await client.put(
            "/api/credentials", content=f"{PASSWORD}{{",
            headers={**SAME_ORIGIN, "Content-Type": "application/json"},
        )
    assert (response.status_code, response.json()) == (422, {"code": "invalid_body"})
    assert PASSWORD not in response.text


@pytest.mark.asyncio
async def test_writes_need_the_write_gate(paths: dict[str, Path], archive: FakeArchive
                                          ) -> None:
    async with _client(paths) as client:
        for response in (
            await _put(client, _body(), headers={}),
            await client.delete("/api/credentials", params={"source": "ia"}),
        ):
            assert (response.status_code, response.json()) == (403, {"code": "cross_site"})
    assert archive.calls == []
    assert not paths["state_db"].exists()


@pytest.mark.asyncio
async def test_the_edit_lock_does_not_cover_credentials(
    paths: dict[str, Path], archive: FakeArchive
) -> None:
    archive.login(login_accepted())
    assert read_edit(paths["state_db"]) == "locked"  # a hand-written file
    async with _client(paths) as client:
        assert (await _put(client, _body())).status_code == 200
        delete = await client.delete("/api/credentials", params={"source": "ia"},
                                     headers=SAME_ORIGIN)
        assert delete.status_code == 200


@pytest.mark.asyncio
async def test_a_broken_config_is_503(paths: dict[str, Path], archive: FakeArchive) -> None:
    paths["config"].write_text(paths["config"].read_text() + "bogus: 1\n")
    async with _client(paths) as client:
        response = await _put(client, _body())
    assert response.status_code == 503
    assert PASSWORD not in response.text and EMAIL not in response.text
    assert archive.calls == []


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root writes anywhere")
@pytest.mark.asyncio
async def test_an_unwritable_database_is_write_failed(
    paths: dict[str, Path], archive: FakeArchive
) -> None:
    folder = paths["state_db"].parent
    folder.mkdir(parents=True)
    folder.chmod(0o555)
    try:
        async with _client(paths) as client:
            response = await _put(client, _body())
    finally:
        folder.chmod(0o755)
    assert (response.status_code, response.json()) == (
        500, {"code": "write_failed", "path": str(paths["state_db"])}
    )
    assert archive.calls == []


@pytest.mark.asyncio
async def test_a_store_waiting_on_a_save_that_removes_the_source_stores_nothing(
    paths: dict[str, Path], archive: FakeArchive
) -> None:
    """The source is looked up under the lock a save holds: a store that waited on a
    save removing the source answers unknown_source, and never brings the login
    back."""
    import asyncio

    from shakedown import config_save

    archive.login(login_accepted())
    async with _client(paths) as client:
        config_save.writing.acquire()
        try:
            waiting = asyncio.create_task(_put(client, _body()))
            await asyncio.sleep(0.3)  # the request is in, waiting on the save
            text = paths["config"].read_text()
            paths["config"].write_text(text[: text.index("  - name: ia")]
                                       + text[text.index("  - name: lma"):])
        finally:
            config_save.writing.release()
        response = await waiting
    assert (response.status_code, response.json()) == (404, {"code": "unknown_source"})
    assert stored(paths["state_db"], "ia") is None
