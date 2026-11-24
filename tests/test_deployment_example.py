"""The shipped image and compose example match §spec:deployment.

These read the files a user builds and pastes; a live bring-up of the stack is
checked by hand (it needs Docker), these keep the shape from drifting.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _final_stage(dockerfile: str) -> list[str]:
    """The instructions of the Dockerfile's last stage, one logical line each."""
    lines = dockerfile.replace("\\\n", " ").splitlines()
    starts = [i for i, line in enumerate(lines) if line.upper().startswith("FROM ")]
    return [line.strip() for line in lines[starts[-1]:] if line.strip()
            and not line.lstrip().startswith("#")]


def _instruction(stage: list[str], name: str) -> list[str]:
    return [line[len(name):].strip() for line in stage if line.upper().startswith(name + " ")]


def test_image_reads_the_config_from_the_data_mount_by_default() -> None:
    """The entrypoint passes no --config, so every command (and a compose
    healthcheck, which skips the entrypoint) uses the CLI default
    /data/shakedown-config/shakedown.yaml; nothing refers to /config any more."""
    stage = _final_stage((REPO / "Dockerfile").read_text())
    (entrypoint,) = _instruction(stage, "ENTRYPOINT")
    assert json.loads(entrypoint) == ["shakedown"]
    (volume,) = _instruction(stage, "VOLUME")
    assert json.loads(volume) == ["/data"]
    assert not any("/config" in line for line in stage)
    assert not any("SHAKEDOWN_CONFIG" in line for line in stage)
