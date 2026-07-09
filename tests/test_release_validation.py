"""Release validation gate command."""
from __future__ import annotations

import subprocess

from click.testing import CliRunner

from shakedown.cli import main
from shakedown.release_validation import run_release_validation


def test_release_validate_deterministic_summary_names_workflows() -> None:
    result = CliRunner().invoke(main, ["release-validate", "--deterministic-only"])

    assert result.exit_code == 0
    assert "Release validation summary" in result.output
    assert "PASS setup readiness" in result.output
    assert "PASS unsafe config rejection" in result.output
    assert "PASS handoff failure" in result.output
    assert "PASS partial-failure recovery" in result.output
    assert "PASS sync-to-library staging" in result.output
    assert "real-source IA seam" not in result.output


def test_release_validate_real_source_failure_is_loud(monkeypatch) -> None:
    def fail_network(cmd, text, capture_output, timeout):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="network unavailable\n",
        )

    monkeypatch.setattr(subprocess, "run", fail_network)

    assert run_release_validation(deterministic=False, real_source=True) == 1


def test_release_validate_default_includes_real_source_failure(monkeypatch) -> None:
    def fail_network(cmd, text, capture_output, timeout):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="network unavailable\n",
        )

    monkeypatch.setattr(subprocess, "run", fail_network)

    result = CliRunner().invoke(main, ["release-validate"])

    assert result.exit_code == 1
    assert "PASS setup readiness" in result.output
    assert "FAIL real-source IA seam" in result.output
    assert "network unavailable" in result.output
