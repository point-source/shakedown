"""tools/governance-lint.sh: status lines and slug grammar in the governance docs.

Each test builds a small repo in tmp_path, copies the script into its tools/,
and runs it there. PATH is cut down to the system directories, so the optional
markdownlint-cli2 and vale steps take their skip path and the tests exercise
only the script's own checks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "governance-lint.sh"
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}

REQUIREMENTS = """\
# Requirements

## Problem statement §req:problem-statement

The problem.

## Constraints §req:constraints

Limits.
"""

SPEC = """\
# Spec

## Sync workflow §spec:sync-workflow

*Status: complete*

Serves §req:problem-statement.

### Ordering §spec:ordering

Detail.

## Tag filling §spec:tag-filling

*Status: in progress*

Follows §spec:sync-workflow and §spec:ordering, within §req:constraints.
"""


def _repo(tmp_path: Path, spec: str = SPEC, requirements: str = REQUIREMENTS) -> Path:
    (tmp_path / "tools").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "tools" / "governance-lint.sh")
    (tmp_path / "SPEC.md").write_text(spec)
    (tmp_path / "REQUIREMENTS.md").write_text(requirements)
    return tmp_path


def _lint(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(root / "tools" / "governance-lint.sh")],
        cwd=root,
        env=ENV,
        capture_output=True,
        text=True,
        check=False,
    )


def _fails_with(root: Path, *expected: str) -> str:
    result = _lint(root)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    for text in expected:
        assert text in out, out
    return out


def test_script_is_an_executable_posix_sh_script() -> None:
    assert SCRIPT.stat().st_mode & 0o111
    assert SCRIPT.read_text().splitlines()[0] == "#!/bin/sh"


def test_clean_docs_pass(tmp_path: Path) -> None:
    result = _lint(_repo(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_optional_tools_absent_are_skipped_with_a_note(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".markdownlint.json").write_text("{}\n")
    result = _lint(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "markdownlint-cli2 not found" in result.stdout


def test_markdownlint_failure_fails_the_run(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / ".markdownlint.json").write_text("{}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "markdownlint-cli2"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >"$(dirname "$0")/args"\nexit 1\n')
    stub.chmod(0o755)
    result = subprocess.run(
        [str(root / "tools" / "governance-lint.sh")],
        cwd=root,
        env={**ENV, "PATH": f"{bin_dir}:{ENV['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    args = (bin_dir / "args").read_text().splitlines()
    assert args[:3] == ["**/REQUIREMENTS.md", "**/SPEC.md", "**/README.md"]


def test_copies_inside_dot_directories_are_ignored(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    copy = root / ".claude" / "worktrees" / "other"
    copy.mkdir(parents=True)
    (copy / "SPEC.md").write_text(SPEC)
    (copy / "REQUIREMENTS.md").write_text(REQUIREMENTS)
    result = _lint(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_repository_itself_passes() -> None:
    result = subprocess.run(
        [str(SCRIPT)], cwd=REPO, env=ENV, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- status lines ---------------------------------------------------------


def test_missing_status_line_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("*Status: complete*\n\n", "")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:3:", "status line")


def test_status_word_outside_the_grammar_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("*Status: complete*", "*Status: implemented*")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:3:", "*Status: implemented*")


def test_status_line_with_a_qualifier_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("*Status: complete*", "*Status: complete; X not started*")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:3:")


def test_section_at_end_of_file_without_status_line_fails(tmp_path: Path) -> None:
    spec = SPEC + "\n## Last §spec:last\n"
    _fails_with(_repo(tmp_path, spec), "SPEC.md:19:", "status line")


def test_headings_inside_fenced_blocks_are_ignored(tmp_path: Path) -> None:
    spec = SPEC + "\n```markdown\n## Example §spec:example\n§spec:nowhere\n```\n"
    result = _lint(_repo(tmp_path, spec))
    assert result.returncode == 0, result.stdout + result.stderr


# --- slug definitions -----------------------------------------------------


def test_section_without_slug_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("## Tag filling §spec:tag-filling", "## Tag filling")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:13:", "slug")


def test_slug_with_the_wrong_prefix_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("§spec:tag-filling", "§req:tag-filling")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:13:", "§spec:")


def test_requirements_sections_need_req_slugs(tmp_path: Path) -> None:
    requirements = REQUIREMENTS.replace("## Constraints §req:constraints", "## Constraints")
    _fails_with(_repo(tmp_path, requirements=requirements), "REQUIREMENTS.md:7:")


def test_ordinal_heading_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("## Tag filling", "## 8. Tag filling")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:13:", "ordinal")


def test_heading_starting_with_a_number_that_is_not_an_ordinal_passes(tmp_path: Path) -> None:
    spec = SPEC.replace("## Tag filling", "## 3D visualization")
    result = _lint(_repo(tmp_path, spec))
    assert result.returncode == 0, result.stdout + result.stderr


def test_duplicate_slug_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("§spec:ordering", "§spec:tag-filling", 1)
    out = _fails_with(_repo(tmp_path, spec), "duplicate", "§spec:tag-filling")
    assert "SPEC.md:9" in out and "SPEC.md:13" in out


# --- references -----------------------------------------------------------


def test_dangling_reference_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("§spec:ordering,", "§spec:sorting,")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:17:", "§spec:sorting")


def test_reference_split_by_a_line_wrap_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("Serves §req:problem-statement.", "Serves §req:problem-\nstatement.")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:7:", "§req:problem-")


def test_numeric_reference_fails(tmp_path: Path) -> None:
    spec = SPEC.replace("Detail.", "See §8.9.")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:11:", "§8")


def test_references_in_code_spans_are_ignored(tmp_path: Path) -> None:
    spec = SPEC.replace("Detail.", "Write `§spec:anything` and `§8.9` like this.")
    result = _lint(_repo(tmp_path, spec))
    assert result.returncode == 0, result.stdout + result.stderr


def test_references_across_files_resolve(tmp_path: Path) -> None:
    requirements = REQUIREMENTS.replace("Limits.", "Shaped by §spec:tag-filling.")
    result = _lint(_repo(tmp_path, requirements=requirements))
    assert result.returncode == 0, result.stdout + result.stderr


def test_roadmap_slugs_are_definitions_and_references(tmp_path: Path) -> None:
    root = _repo(tmp_path, SPEC.replace("Detail.", "Planned in §road:first-pass."))
    (root / "ROADMAP.md").write_text("# Roadmap\n\n## First pass §road:first-pass\n\nBuilds §spec:tag-filling.\n")
    result = _lint(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_roadmap_reference_without_a_roadmap_is_dangling(tmp_path: Path) -> None:
    spec = SPEC.replace("Detail.", "Planned in §road:first-pass.")
    _fails_with(_repo(tmp_path, spec), "SPEC.md:11:", "§road:first-pass")


def test_every_violation_is_reported_in_one_run(tmp_path: Path) -> None:
    spec = SPEC.replace("*Status: complete*", "*Status: done*").replace(
        "§spec:ordering,", "§spec:sorting,"
    )
    _fails_with(_repo(tmp_path, spec), "SPEC.md:3:", "SPEC.md:17:")


def test_a_reference_fails_when_no_heading_defines_any_slug(tmp_path: Path) -> None:
    root = _repo(tmp_path, spec="# Spec\n\nSee §req:problem-statement.\n",
                 requirements="# Requirements\n\nNothing yet.\n")
    _fails_with(root, "SPEC.md:3: dangling reference §req:problem-statement")
