"""Pasted links (§spec:dashboard-settings "Pasted links"): a source plugin reads a
link from the source's site as its own search, from the link's text alone."""
from __future__ import annotations

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
