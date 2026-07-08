from __future__ import annotations

from pathlib import Path

import pytest

from shakedown.config import CollectionConfig, Config, SourceConfig
from tests.fake_plugin import FakePlugin, install_fake, reset_fake


@pytest.fixture(autouse=True)
def _fake_registered() -> None:
    install_fake()
    reset_fake()
    yield
    reset_fake()


@pytest.fixture()
def tmp_roots(tmp_path: Path) -> tuple[Path, Path]:
    archive = tmp_path / "archive"
    library = tmp_path / "library"
    archive.mkdir()
    library.mkdir()
    return archive, library


def make_config(
    archive_root: Path,
    library_root: Path,
    *,
    library_layout: str = "passthrough",
    on_complete=None,
    prune_disappeared: bool = False,
    incremental_discovery: bool = False,
    format_filters=None,
) -> Config:
    return Config(
        archive_root=archive_root,
        library_root=library_root,
        sources=[
            SourceConfig(
                name="fake-src",
                type="fake",
                collections=[
                    CollectionConfig(
                        name="coll1",
                        query="*",
                        format_filters=format_filters or [],
                        library_layout=library_layout,
                        on_complete=on_complete,
                        prune_disappeared=prune_disappeared,
                        incremental_discovery=incremental_discovery,
                    )
                ],
            )
        ],
    )


__all__ = ["FakePlugin", "make_config"]
