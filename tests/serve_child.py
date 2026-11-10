"""A `shakedown` child process that sees the fake source, for tests of runs that
`serve` starts in a separate process (§spec:api-sync-runs).

Run as `python -m tests.serve_child <shakedown CLI args>`. It registers the fake
plugin, seeds it from the JSON file named by SHAKEDOWN_FAKE_SPEC, and then runs the
real CLI entry point exactly as `python -m shakedown` would:

    {"items": {"<identifier>": {"<file name>": "<content>"}},
     "gate": "<path>" | null}

With a gate, every fetch waits until that path exists, so a test can hold a run
mid-flight (to hit the writer lock or kill it) and then let it finish.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from tests.fake_plugin import FakeFile, FakeItem, FakePlugin, install_fake

SPEC_ENV = "SHAKEDOWN_FAKE_SPEC"
GATE_TIMEOUT_S = 60.0


def _seed() -> None:
    raw = os.environ.get(SPEC_ENV)
    if not raw:
        return
    spec = json.loads(Path(raw).read_text())
    for identifier, files in spec.get("items", {}).items():
        FakePlugin.items[identifier] = FakeItem(
            identifier=identifier,
            files=[FakeFile(name=n, content=c.encode()) for n, c in files.items()],
        )
    gate = spec.get("gate")
    if gate:
        real_fetch = FakePlugin.fetch

        def gated_fetch(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            deadline = time.monotonic() + GATE_TIMEOUT_S
            while not Path(gate).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            return real_fetch(self, *args, **kwargs)

        FakePlugin.fetch = gated_fetch  # type: ignore[method-assign]


def main() -> None:
    install_fake()
    _seed()
    from shakedown.cli import main as cli_main

    cli_main(prog_name="shakedown")


if __name__ == "__main__":
    main()
