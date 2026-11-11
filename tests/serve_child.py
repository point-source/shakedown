"""A `shakedown` child process that sees the fake source, for tests of runs that
`serve` starts in a separate process (§spec:api-sync-runs).

Run as `python -m tests.serve_child <shakedown CLI args>`. It registers the fake
plugin, seeds it from the JSON file named by SHAKEDOWN_FAKE_SPEC, and then runs the
real CLI entry point exactly as `python -m shakedown` would:

    {"items": {"<identifier>": {"<file name>": "<content>"}},
     "gate": "<path>" | null,
     "audio_items": {"<identifier>": {"metadata": {...}, "files": ["<name>", ...]}},
     "kill_at": "backed_up" | "saved" | "torn" | null}

With a gate, every fetch waits until that path exists, so a test can hold a run
mid-flight (to hit the writer lock or kill it) and then let it finish.

`audio_items` are shows of tiny real audio files (tests.audio_fixtures), the n-th
file listed with title "Track n" and track "n". `kill_at` SIGKILLs the process at
its first tag write into the archive: once the backup is made, once the tags are
written, or partway through writing them ("torn").
"""
from __future__ import annotations

import json
import os
import signal
import tempfile
import time
from pathlib import Path

from tests.audio_fixtures import make_audio
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
    for identifier, show in spec.get("audio_items", {}).items():
        folder = Path(tempfile.mkdtemp())
        FakePlugin.items[identifier] = FakeItem(
            identifier=identifier,
            files=[
                FakeFile(name=n, content=make_audio(folder / n).read_bytes(), format="Flac",
                         title=f"Track {i}", track=str(i))
                for i, n in enumerate(show["files"], start=1)
            ],
            metadata=show["metadata"],
        )
    kill_at = spec.get("kill_at")
    if kill_at:
        _kill_at(kill_at)
    gate = spec.get("gate")
    if gate:
        real_fetch = FakePlugin.fetch

        def gated_fetch(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            deadline = time.monotonic() + GATE_TIMEOUT_S
            while not Path(gate).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            return real_fetch(self, *args, **kwargs)

        FakePlugin.fetch = gated_fetch  # type: ignore[method-assign]


def _kill_at(stage: str) -> None:
    from shakedown import tagging

    def die() -> None:
        os.kill(os.getpid(), signal.SIGKILL)

    if stage == "torn":
        def torn_save(self: object, target: Path) -> None:
            with target.open("r+b") as f:
                f.write(b"\x00" * 64)
                f.flush()
                os.fsync(f.fileno())
            die()

        tagging._Tags.save = torn_save  # type: ignore[method-assign]
    else:
        def fault(at: str) -> None:
            if at == stage:
                die()

        tagging._fault_point = fault


def main() -> None:
    install_fake()
    _seed()
    from shakedown.cli import main as cli_main

    cli_main(prog_name="shakedown")


if __name__ == "__main__":
    main()
