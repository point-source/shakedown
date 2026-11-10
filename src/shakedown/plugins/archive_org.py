"""archive.org's per-item file list, shared by the two sources that read it (`ia`
and `etree`): one mapping from its metadata API's file entries to the core's
ItemLookup file records (§spec:per-item-isolation)."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


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
        }
        for f in files
    ]
