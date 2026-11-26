"""Pasted links (§spec:dashboard-settings "Pasted links"): a source plugin reads a
link from the source's site as its own search, from the link's text alone."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from shakedown.plugins.base import SourcePlugin
from shakedown.plugins.etree.plugin import EtreePlugin
from shakedown.plugins.ia.plugin import IAPlugin
from tests.fake_plugin import FakePlugin

# The page's fixture table, row for row: `web/src/fixtures/scenarios.ts` LINKS. The
# page's goldens and behaviour tests render these answers, so the server must give
# exactly these.
LINKS: list[tuple[str, str | None]] = [
    ("https://archive.org/details/GratefulDead", "collection:GratefulDead"),
    ("https://archive.org/details/BobWeir", "collection:BobWeir"),
    ("https://archive.org/details/etree?query=phish&tab=collection", "collection:etree AND (phish)"),
    ("https://archive.org/search?query=collection%3APhish+AND+year%3A1997", "collection:Phish AND year:1997"),
    (
        "https://archive.org/advancedsearch.php?q=creator%3A%22Grateful+Dead%22&fl%5B%5D=identifier",
        'creator:"Grateful Dead"',
    ),
    ("https://example.net/bob-weir", None),
    ("https://example.net/shows", None),
]

# The same rules on the link shapes people also paste.
MORE_LINKS: list[tuple[str, str | None]] = [
    ("http://archive.org/details/GratefulDead", "collection:GratefulDead"),
    ("https://www.archive.org/details/GratefulDead", "collection:GratefulDead"),
    ("https://archive.org/details/GratefulDead/", "collection:GratefulDead"),
    ("https://archive.org/details/GratefulDead?query=", "collection:GratefulDead"),
    ("https://archive.org/details/gd1977-05-08.sbd/track01.mp3", None),
    ("https://archive.org/details/", None),
    ("https://archive.org/search?query=", None),
    ("https://archive.org/search", None),
    ("https://archive.org/advancedsearch.php?fl%5B%5D=identifier", None),
    ("https://archive.org/", None),
    ("https://example.net/details/GratefulDead", None),
    ("collection:GratefulDead", None),
    ("https://[::1/details/GratefulDead", None),  # not a URL urlsplit can parse
]


@pytest.mark.parametrize("plugin", [IAPlugin, EtreePlugin])
@pytest.mark.parametrize(("url", "query"), LINKS + MORE_LINKS)
def test_archive_org_plugins_read_archive_org_links(
    plugin: type[SourcePlugin], url: str, query: str | None
) -> None:
    # Called on the class: reading a link needs no source and sends no request.
    assert plugin.query_from_link(url) == query


def test_a_plugin_offers_link_reading_by_implementing_the_hook() -> None:
    assert IAPlugin.reads_links() and EtreePlugin.reads_links()
    assert not FakePlugin.reads_links()
    assert FakePlugin.query_from_link("https://archive.org/details/GratefulDead") is None


# -- GET /api/source-link ----------------------------------------------------------

VALID = """\
archive_root: {root}/archive
library_root: {root}/library
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'collection:GratefulDead'
"""


def _config(tmp_path: Path, mode: str) -> Path:
    """The config file in each mode serve answers in: none, a valid one, a file that
    fails validation, and one that isn't YAML."""
    path = tmp_path / "shakedown-config" / "shakedown.yaml"
    if mode == "missing":
        return path
    (tmp_path / "archive").mkdir()
    (tmp_path / "library").mkdir()
    path.parent.mkdir()
    text = VALID.format(root=tmp_path)
    if mode == "invalid":
        text += "max_concurrent_collectons: 3\n"
    elif mode == "unreadable":
        text = "archive_root: [unclosed\n"
    path.write_text(text)
    return path


async def _ask(
    config_path: Path, headers: dict[str, str] | None = None, **params: str
) -> httpx.Response:
    pytest.importorskip("fastapi")
    from shakedown.server import build_app

    app = build_app(config_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.get("/api/source-link", params=params, headers=headers)


def _json(response: httpx.Response) -> tuple[int, Any]:
    return response.status_code, response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "query"), LINKS)
async def test_every_fixture_link_answers_as_the_page_expects(
    tmp_path: Path, url: str, query: str | None
) -> None:
    config_path = _config(tmp_path, "valid")
    assert _json(await _ask(config_path, type="ia", url=url)) == (200, {"query": query})


@pytest.mark.asyncio
async def test_the_etree_type_reads_the_same_links(tmp_path: Path) -> None:
    response = await _ask(
        _config(tmp_path, "valid"), type="etree", url="https://archive.org/details/GratefulDead"
    )
    assert _json(response) == (200, {"query": "collection:GratefulDead"})


@pytest.mark.asyncio
async def test_a_type_with_no_reading_answers_null(tmp_path: Path) -> None:
    response = await _ask(
        _config(tmp_path, "valid"), type="fake", url="https://archive.org/details/GratefulDead"
    )
    assert _json(response) == (200, {"query": None})


@pytest.mark.asyncio
async def test_an_unknown_type_is_source_type_unknown(tmp_path: Path) -> None:
    response = await _ask(
        _config(tmp_path, "valid"), type="nope", url="https://archive.org/details/GratefulDead"
    )
    assert _json(response) == (404, {"code": "source_type_unknown"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "invalid", "unreadable"])
async def test_it_answers_without_a_loaded_config(tmp_path: Path, mode: str) -> None:
    # Setup and the page with a broken file still read links (§spec:ui-kit Harness).
    config_path = _config(tmp_path, mode)
    response = await _ask(config_path, type="ia", url="https://archive.org/details/GratefulDead")
    assert _json(response) == (200, {"query": "collection:GratefulDead"})
    if mode == "missing":
        assert not config_path.parent.exists()  # nothing was written


@pytest.mark.asyncio
async def test_it_is_not_behind_the_write_gate(tmp_path: Path) -> None:
    # It reads nothing and writes nothing: no token, from another site, still answers.
    response = await _ask(
        _config(tmp_path, "valid"),
        headers={"Sec-Fetch-Site": "cross-site"},
        type="ia",
        url="https://archive.org/details/GratefulDead",
    )
    assert _json(response) == (200, {"query": "collection:GratefulDead"})
