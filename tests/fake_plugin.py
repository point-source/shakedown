"""In-memory fake source plugin for testing sync end-to-end without the network."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from shakedown.config import CollectionConfig
from shakedown.models import ManifestFile
from shakedown.plugins.base import (
    FetchResult,
    ItemLookup,
    ListedItem,
    SourcePlugin,
    VerifyResult,
)


@dataclass
class FakeFile:
    name: str
    content: bytes
    format: str | None = None
    refused: bool = False
    derived_from: str | list[str] | None = None  # passed through raw, as a source would
    bitrate: int | None = None
    title: str | None = None
    track: str | None = None

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def md5(self) -> str:
        return hashlib.md5(self.content).hexdigest()


@dataclass
class FakeItem:
    identifier: str
    files: list[FakeFile] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    is_restricted: bool = False
    restriction_reason: str | None = None
    change_signal: str | None = None


class FakePlugin(SourcePlugin):
    type_name = "fake"

    # `identifier` is per-item-unique; `title` is a known-but-shared field, so a layout
    # keyed only on `{title}` is collision-risky (the readiness layout probe fails it).
    template_fields = ("identifier", "title")
    per_item_unique_fields = ("identifier",)

    # Class-level state so tests can mutate the source between sync runs.
    items: ClassVar[dict[str, FakeItem]] = {}
    fetch_count: ClassVar[dict[str, int]] = {}
    # Every file name each fetch was asked for, per identifier, in call order.
    fetched_files: ClassVar[dict[str, list[str]]] = {}
    # Source names whose enumeration should raise, simulating an unreachable source —
    # exactly as a real source fault would — to exercise the readiness reachability probe.
    unreachable_sources: ClassVar[set[str]] = set()
    # How many times a collection was listed.
    list_calls: ClassVar[int] = 0
    # Every lookup_item call as (identifier, timeout).
    lookup_calls: ClassVar[list[tuple[str, float]]] = []
    # Per-identifier lookup replies, in order; the last one repeats. An exception is
    # raised, an ItemLookup returned. Identifiers without an entry answer from `items`.
    lookup_faults: ClassVar[dict[str, list[BaseException | ItemLookup]]] = {}

    @classmethod
    def lookups(cls, identifier: str) -> int:
        """How many times identifier was looked up."""
        return sum(1 for looked_up, _ in cls.lookup_calls if looked_up == identifier)

    def list_items(self, collection: CollectionConfig) -> Iterator[ListedItem]:
        FakePlugin.list_calls += 1
        if self.source_config.name in self.unreachable_sources:
            raise RuntimeError(f"source {self.source_config.name!r} is unreachable")
        for identifier, it in self.items.items():
            yield ListedItem(identifier, it.change_signal)

    def lookup_item(self, identifier: str, timeout: float) -> ItemLookup:
        self.lookup_calls.append((identifier, timeout))
        replies = self.lookup_faults.get(identifier)
        if replies:
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            if isinstance(reply, BaseException):
                raise reply
            return reply
        it = self.items[identifier]
        return ItemLookup(
            metadata={**it.metadata, "identifier": it.identifier},
            files=[
                {
                    "name": f.name,
                    "size": f.size,
                    "md5": f.md5,
                    "format": f.format,
                    "refused": f.refused,
                    "derived_from": f.derived_from,
                    "bitrate": f.bitrate,
                    "title": f.title,
                    "track": f.track,
                }
                for f in it.files
            ],
            restriction_reason=(it.restriction_reason or "restricted") if it.is_restricted else None,
        )

    def fetch(
        self, identifier: str, files: Sequence[ManifestFile], dest_dir: Path
    ) -> FetchResult:
        self.fetch_count[identifier] = self.fetch_count.get(identifier, 0) + 1
        self.fetched_files.setdefault(identifier, []).extend(mf.name for mf in files)
        content = {f.name: f.content for f in self.items[identifier].files}
        dest_dir.mkdir(parents=True, exist_ok=True)
        bytes_total = 0
        for mf in files:
            (dest_dir / mf.name).write_bytes(content[mf.name])
            bytes_total += len(content[mf.name])
        return FetchResult(success=True, bytes_downloaded=bytes_total)

    def verify(self, files: Sequence[ManifestFile], archive_path: Path) -> VerifyResult:
        missing = [f.name for f in files if not (archive_path / f.name).is_file()]
        return VerifyResult(ok=not missing, missing_files=missing)


def reset_fake() -> None:
    FakePlugin.items.clear()
    FakePlugin.fetch_count.clear()
    FakePlugin.fetched_files.clear()
    FakePlugin.unreachable_sources.clear()
    FakePlugin.lookup_calls.clear()
    FakePlugin.lookup_faults.clear()
    FakePlugin.list_calls = 0


def install_fake() -> None:
    """Register FakePlugin and extend SourceConfig to accept type='fake' for tests."""
    from shakedown.config import SourceConfig as _SC
    from shakedown.plugins import registry

    registry.register(FakePlugin)
    # Allow type='fake' through config validation. Tests construct SourceConfig directly,
    # so we don't need to extend the Literal — they bypass the YAML loader.
    _ = _SC  # keep the import; explicit no-op
