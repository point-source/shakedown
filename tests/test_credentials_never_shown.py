"""Never logged, never answered (§spec:dashboard-credentials "Never logged",
"Status, never values"): an entered or stored credential appears in no log line,
error, validation result, `status` output, `/api/*` answer, or `shakedown.yaml` —
through entry, a wrong password, an unreachable source, a run, validate, and a save."""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

import shakedown.sync as sync_module
from shakedown.config import load
from shakedown.server import build_app
from shakedown.status import print_status
from shakedown.sync import run_sync
from shakedown.validate import run_validate
from tests.fake_archive import (
    FakeArchive,
    Reply,
    fake_archive,
    json_reply,
    login_accepted,
    login_rejected,
)

EMAIL, PASSWORD = "sentinel-email-4d1e@example.org", "sentinel-password-8b2f"
SECRETS = ("sentinel-email-4d1e", "sentinel-password-8b2f")
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
CONFIG = """\
archive_root: {archive}
library_root: {library}
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'collection:GratefulDead'
"""


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IA_EMAIL", raising=False)
    monkeypatch.delenv("IA_PASSWORD", raising=False)
    monkeypatch.setattr("internetarchive.config.sleep", lambda _s: None)
    monkeypatch.setattr(sync_module, "_sleep", lambda _s: None)


@pytest.fixture()
def archive() -> Iterator[FakeArchive]:
    with fake_archive() as fake:
        content = b"audio"
        fake.route("/services/search/v1/scrape",
                   json_reply({"items": [{"identifier": "gd1"}], "count": 1, "total": 1}))
        fake.item("gd1", [{"name": "d1t01.flac", "size": str(len(content)),
                           "md5": hashlib.md5(content).hexdigest(), "format": "Flac"}],
                  {"title": "Show", "date": "1977-05-08"})
        fake.file("gd1", "d1t01.flac", Reply(200, content))
        yield fake


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


@pytest.mark.asyncio
async def test_no_answer_log_or_output_shows_a_credential(
    tmp_path: Path, archive: FakeArchive, caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    archive_root, library = tmp_path / "archive", tmp_path / "library"
    archive_root.mkdir()
    library.mkdir()
    config_path = tmp_path / "shakedown.yaml"
    config_path.write_text(CONFIG.format(archive=archive_root, library=library))
    app = build_app(config_path)
    answers: list[str] = []

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        body = {"source": "ia", "email": EMAIL, "password": PASSWORD}
        # Entered: accepted, then a wrong password, then the source unreachable.
        for reply in (login_accepted(), login_rejected(), ConnectionError(PASSWORD)):
            archive.login(reply)
            response = await client.put("/api/credentials", json=body, headers=SAME_ORIGIN)
            assert response.status_code == 200
            answers.append(response.text)
        # Refused bodies, each holding a value.
        for bad in ({**body, "password": 1}, {**body, "extra": PASSWORD},
                    {"source": "ia", "email": EMAIL}, {**body, "source": "nope"}):
            response = await client.put("/api/credentials", json=bad, headers=SAME_ORIGIN)
            assert response.status_code in (404, 422)
            answers.append(response.text)

        # Stored and accepted: a run, validate, status, and every read answer.
        archive.login(login_accepted())
        await client.put("/api/credentials", json=body, headers=SAME_ORIGIN)
        config = load(config_path)
        assert run_sync(config) == 0
        archive.login(login_rejected())
        run_sync(config)  # a run whose stored login is now refused
        assert run_validate(config_path) != 0
        assert run_validate(config_path, as_json=True) != 0
        print_status(config, as_json=False)
        print_status(config, as_json=True)
        response = await client.post("/validate", headers=SAME_ORIGIN)
        answers.append(response.text)

        for route in app.routes:
            path = getattr(route, "path", "")
            if (path.startswith("/api/") or path in ("/status", "/metrics")) and (
                "GET" in getattr(route, "methods", set())
            ):
                response = await client.get(path)
                assert response.status_code == 200, path
                answers.append(response.text)

        # A save, then forgetting.
        await client.post("/api/config/override", json={}, headers=SAME_ORIGIN)
        loaded = (await client.get("/api/config")).json()
        response = await client.put(
            "/api/config", json={"revision": loaded["revision"], "document": loaded["document"]},
            headers=SAME_ORIGIN,
        )
        answers.append(response.text)
        response = await client.delete("/api/credentials", params={"source": "ia"},
                                       headers=SAME_ORIGIN)
        answers.append(response.text)

    out, err = capfd.readouterr()
    for text in (*answers, caplog.text, out, err, config_path.read_text()):
        _assert_clean(text)
    # The run did use the login: the sweep covered the paths that hold it.
    assert any(SECRETS[0].encode() in c.body for c in archive.calls_to("/services/xauthn/"))
    assert "rejected the login for source 'ia'" in caplog.text
