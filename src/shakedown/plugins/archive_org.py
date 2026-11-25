"""archive.org, shared by the two sources that read it (`ia` and `etree`): one
mapping from its metadata API's file entries to the core's ItemLookup file records
(§spec:per-item-isolation), and one reading of links from its site as searches
(§spec:dashboard-settings "Pasted links")."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

HOSTS = ("archive.org", "www.archive.org")


def file_records(identifier: str, files: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pass archive.org's file entries through as ItemLookup file records.

    - ``private`` is archive.org's per-file refusal: such a file answers 403 (e.g. the
      lossless originals of a stream-only show, whose MP3s stay public).
    - ``<identifier>_files.xml`` lists itself, so the checksum archive.org gives for
      it can never match the file served; it is passed without one.
    """
    index = f"{identifier}_files.xml"
    return [
        {
            "name": f.get("name"),
            "size": f.get("size"),
            "md5": None if f.get("name") == index else f.get("md5"),
            "format": f.get("format"),
            "refused": str(f.get("private")).lower() == "true",
            "derived_from": f.get("original"),
            "bitrate": f.get("bitrate"),
            "title": f.get("title"),
            "track": f.get("track"),
        }
        for f in files
    ]


def query_from_link(url: str) -> str | None:
    """The search an archive.org link stands for: `/details/<id>` is
    `collection:<id>`, or `collection:<id> AND (<q>)` with `?query=<q>`;
    `/search?query=<q>` and `/advancedsearch.php?q=<q>` are `<q>`. Other parameters
    are ignored; any other link is None."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:  # not a URL at all, e.g. an unclosed IPv6 bracket
        return None
    if parts.scheme not in ("http", "https") or host not in HOSTS:
        return None
    params = parse_qs(parts.query)

    def param(name: str) -> str | None:
        value = params.get(name, [""])[0].strip()
        return value or None

    segments = parts.path.strip("/").split("/")
    if len(segments) == 2 and segments[0] == "details" and segments[1]:
        query = param("query")
        return f"collection:{segments[1]} AND ({query})" if query else f"collection:{segments[1]}"
    if parts.path == "/search":
        return param("query")
    if parts.path == "/advancedsearch.php":
        return param("q")
    return None
