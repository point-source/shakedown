"""Which login a run uses (§spec:dashboard-credentials "Which wins"): the environment
variables, else the source's stored credentials, else none — and the `ia` source logs
in to archive.org with it, so a file only a logged-in account may download arrives."""
from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

import shakedown.sync as sync_module
from shakedown.config import CollectionConfig, Config, IAAuth, SourceConfig
from shakedown.credentials import Login, resolve, store
from shakedown.plugins.ia.plugin import IAPlugin
from shakedown.sync import run_sync
from tests.fake_archive import (
    FakeArchive,
    Reply,
    fake_archive,
    json_reply,
    login_accepted,
    login_rejected,
)

LOGIN = "/services/xauthn/"
SEARCH = "/services/search/v1/scrape"
LOW = "LOW S3ACCESS:S3SECRET"


@pytest.fixture(autouse=True)
def _no_env_login(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("IA_EMAIL", "IA_PASSWORD", "MY_EMAIL", "MY_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    # The library's login waits two seconds after asking; tests need not.
    monkeypatch.setattr("internetarchive.config.sleep", lambda _s: None)


@pytest.fixture()
def archive() -> Iterator[FakeArchive]:
    with fake_archive() as fake:
        yield fake


def _source(auth: IAAuth | None = None, type: str = "ia") -> SourceConfig:
    return SourceConfig(
        name="ia", type=type, auth=auth,
        collections=[CollectionConfig(name="gd", query="collection:GratefulDead")],
    )


def _sent(call: Any) -> dict[str, list[str]]:
    return parse_qs(call.body.decode())


# -- which wins ------------------------------------------------------------------


def test_nothing_set_is_none(tmp_path: Path) -> None:
    assert resolve(_source(), tmp_path / "state.db") == ("none", None)


def test_stored_credentials_are_used_without_environment(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    store(state_db, "ia", Login("me@example.org", "pw"))
    assert resolve(_source(), state_db) == ("dashboard", Login("me@example.org", "pw"))


def test_the_standard_names_win_over_stored_when_the_file_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_db = tmp_path / "state.db"
    store(state_db, "ia", Login("me@example.org", "pw"))
    monkeypatch.setenv("IA_EMAIL", "env@example.org")
    monkeypatch.setenv("IA_PASSWORD", "envpw")
    assert resolve(_source(), state_db) == ("environment", Login("env@example.org", "envpw"))


def test_the_names_in_the_file_win_and_replace_the_standard_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_db = tmp_path / "state.db"
    store(state_db, "ia", Login("me@example.org", "pw"))
    monkeypatch.setenv("IA_EMAIL", "std@example.org")
    monkeypatch.setenv("IA_PASSWORD", "stdpw")
    auth = IAAuth(email_env="MY_EMAIL", password_env="MY_PASSWORD")
    # The file names its own variables and they are unset: the stored login, not
    # the standard names.
    assert resolve(_source(auth), state_db)[0] == "dashboard"
    monkeypatch.setenv("MY_EMAIL", "mine@example.org")
    monkeypatch.setenv("MY_PASSWORD", "minepw")
    assert resolve(_source(auth), state_db) == (
        "environment", Login("mine@example.org", "minepw")
    )


def test_a_name_the_file_leaves_out_falls_back_to_the_standard_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_EMAIL", "mine@example.org")
    monkeypatch.setenv("IA_PASSWORD", "stdpw")
    assert resolve(_source(IAAuth(email_env="MY_EMAIL")), tmp_path / "s.db") == (
        "environment", Login("mine@example.org", "stdpw")
    )


def test_one_variable_alone_is_not_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IA_EMAIL", "env@example.org")
    monkeypatch.setenv("IA_PASSWORD", "  \r\n")
    assert resolve(_source(), tmp_path / "state.db") == ("none", None)


def test_credentials_from_an_env_file_with_line_endings_are_trimmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: FakeArchive
) -> None:
    """A trailing CR/LF (an env file saved on Windows) would break the login, and
    an error quoting the request would carry the secret into run errors and logs."""
    monkeypatch.setenv("IA_EMAIL", "me@example.org\r\n")
    monkeypatch.setenv("IA_PASSWORD", "s3cret\r\n")
    status, login = resolve(_source(), tmp_path / "state.db")
    assert (status, login) == ("environment", Login("me@example.org", "s3cret"))

    archive.login(login_accepted())
    assert IAPlugin(_source(), login).check_login() == "accepted"
    [call] = archive.calls_to(LOGIN)
    assert _sent(call) == {"email": ["me@example.org"], "password": ["s3cret"]}


def test_a_type_that_takes_no_credentials_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_db = tmp_path / "state.db"
    store(state_db, "ia", Login("me@example.org", "pw"))
    monkeypatch.setenv("IA_EMAIL", "env@example.org")
    monkeypatch.setenv("IA_PASSWORD", "envpw")
    assert resolve(_source(type="etree"), state_db) == ("none", None)


# -- the ia source logs in -------------------------------------------------------


def _item(archive: FakeArchive, identifier: str, content: bytes) -> None:
    archive.item(identifier, [{
        "name": "d1t01.flac", "size": str(len(content)),
        "md5": hashlib.md5(content).hexdigest(), "format": "Flac",
    }], {"title": "Show", "date": "1977-05-08"})
    archive.file(identifier, "d1t01.flac", Reply(200, content))


def test_a_login_is_asked_once_and_signs_every_request(archive: FakeArchive) -> None:
    archive.login(login_accepted())
    _item(archive, "gd1", b"audio")
    plugin = IAPlugin(_source(), Login("me@example.org", "pw"))

    plugin.lookup_item("gd1", 5.0)
    plugin.lookup_item("gd1", 5.0)

    [login] = archive.calls_to(LOGIN)
    assert _sent(login) == {"email": ["me@example.org"], "password": ["pw"]}
    lookups = archive.calls_to("/metadata/gd1")
    assert [c.headers.get("Authorization") for c in lookups] == [LOW, LOW]
    assert plugin.check_login() == "accepted"
    assert len(archive.calls_to(LOGIN)) == 1


def test_parallel_collections_share_one_login(archive: FakeArchive) -> None:
    archive.login(login_accepted())
    _item(archive, "gd1", b"audio")
    plugin = IAPlugin(_source(), Login("me@example.org", "pw"))
    threads = [threading.Thread(target=plugin.lookup_item, args=("gd1", 5.0))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(archive.calls_to(LOGIN)) == 1


def test_no_login_asks_nothing_and_signs_nothing(archive: FakeArchive) -> None:
    _item(archive, "gd1", b"audio")
    plugin = IAPlugin(_source())
    plugin.lookup_item("gd1", 5.0)
    assert archive.calls_to(LOGIN) == []
    assert "Authorization" not in archive.calls_to("/metadata/gd1")[0].headers
    assert plugin.check_login() == "unchecked"


def test_a_plugin_that_never_asks_archive_org_never_logs_in(
    archive: FakeArchive, tmp_path: Path
) -> None:
    plugin = IAPlugin(_source(), Login("me@example.org", "pw"))
    plugin.verify((), tmp_path)
    assert archive.calls == []


def test_a_rejected_login_warns_and_continues_without_it(
    archive: FakeArchive, caplog: pytest.LogCaptureFixture
) -> None:
    archive.login(login_rejected())
    _item(archive, "gd1", b"audio")
    plugin = IAPlugin(_source(), Login("me@example.org", "wrong-pw"))
    with caplog.at_level(logging.WARNING):
        plugin.lookup_item("gd1", 5.0)
    assert plugin.check_login() == "rejected"
    assert "Authorization" not in archive.calls_to("/metadata/gd1")[0].headers
    assert "rejected the login for source 'ia'" in caplog.text
    assert "wrong-pw" not in caplog.text and "me@example.org" not in caplog.text


@pytest.mark.parametrize(
    "reply",
    [
        ConnectionError("no route to host"),
        Reply(503, b"<html>down</html>"),
        json_reply({"unexpected": True}),
    ],
)
def test_an_unreachable_login_service_is_unchecked(archive: FakeArchive, reply: Any) -> None:
    archive.login(reply)
    plugin = IAPlugin(_source(), Login("me@example.org", "pw"))
    assert plugin.check_login() == "unchecked"


# -- a run uses it ---------------------------------------------------------------


def _config(tmp_roots: tuple[Path, Path], auth: IAAuth | None = None) -> Config:
    archive, library = tmp_roots
    return Config(archive_root=archive, library_root=library, sources=[_source(auth)])


def _login_only_show(archive: FakeArchive) -> None:
    archive.login(login_accepted())
    archive.route(SEARCH, json_reply({"items": [{"identifier": "gd1"}], "count": 1,
                                      "total": 1}))
    _item(archive, "gd1", b"restricted audio")
    archive.required_auth["/download/gd1/d1t01.flac"] = LOW


def test_a_sync_with_stored_credentials_downloads_a_login_only_file(
    tmp_roots: tuple[Path, Path], archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_module, "_sleep", lambda _s: None)
    config = _config(tmp_roots)
    assert config.state_db is not None
    store(config.state_db, "ia", Login("me@example.org", "pw"))
    _login_only_show(archive)

    assert run_sync(config) == 0

    landed = config.archive_root / "ia" / "gd" / "gd1" / "d1t01.flac"
    assert landed.read_bytes() == b"restricted audio"
    assert _sent(archive.calls_to(LOGIN)[0])["email"] == ["me@example.org"]


def test_a_sync_with_environment_credentials_uses_them_over_stored(
    tmp_roots: tuple[Path, Path], archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_module, "_sleep", lambda _s: None)
    monkeypatch.setenv("IA_EMAIL", "env@example.org")
    monkeypatch.setenv("IA_PASSWORD", "envpw")
    config = _config(tmp_roots)
    assert config.state_db is not None
    store(config.state_db, "ia", Login("me@example.org", "pw"))
    _login_only_show(archive)

    assert run_sync(config) == 0

    assert (config.archive_root / "ia" / "gd" / "gd1" / "d1t01.flac").is_file()
    assert _sent(archive.calls_to(LOGIN)[0]) == {
        "email": ["env@example.org"], "password": ["envpw"]
    }


def test_a_sync_without_credentials_records_the_file_as_refused(
    tmp_roots: tuple[Path, Path], archive: FakeArchive, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync_module, "_sleep", lambda _s: None)
    config = _config(tmp_roots)
    _login_only_show(archive)
    run_sync(config)
    assert archive.calls_to(LOGIN) == []
    assert not (config.archive_root / "ia" / "gd" / "gd1" / "d1t01.flac").exists()


# -- validate retries the check --------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "ok", "says"),
    [
        (login_accepted(), True, None),
        (login_rejected(), False, "rejected the login"),
        (ConnectionError("down"), False, "could not be reached to check the login"),
    ],
)
def test_validate_asks_the_source_about_a_resolved_login(
    archive: FakeArchive, reply: Any, ok: bool, says: str | None
) -> None:
    archive.login(reply)
    probe = IAPlugin(_source(), Login("me@example.org", "pw")).check_credentials()
    assert probe.ok is ok
    if says:
        assert says in (probe.consequence or "")
        assert "me@example.org" not in (probe.consequence or "")


def test_validate_without_a_login_still_names_missing_variables(archive: FakeArchive) -> None:
    probe = IAPlugin(_source(IAAuth(email_env="MY_EMAIL"))).check_credentials()
    assert probe.ok is False and "MY_EMAIL" in (probe.consequence or "")
    assert IAPlugin(_source()).check_credentials().ok is True
    assert archive.calls == []
