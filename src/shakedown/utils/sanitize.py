"""Filename sanitization for staging templates. PRD §8."""
from __future__ import annotations

import re

# Replace these with '_'. Conservative — preserves non-ASCII per PRD.
_HOSTILE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_TRAILING_DOTS_OR_SPACES = re.compile(r"[. ]+$")
_COLLAPSE_UNDERSCORES = re.compile(r"_{2,}")


def sanitize(value: str) -> str:
    """Replace filesystem-hostile characters; preserve non-ASCII."""
    if not value:
        return "_"
    cleaned = _HOSTILE.sub("_", value)
    cleaned = _TRAILING_DOTS_OR_SPACES.sub("", cleaned)
    cleaned = _COLLAPSE_UNDERSCORES.sub("_", cleaned)
    return cleaned or "_"


def path_safe(value: str) -> str:
    """Make one rendered field safe as part of a path segment, changing nothing else.

    Applied to every layout field by default. Unlike `sanitize`, it does not collapse
    underscores or trim trailing dots, so distinct safe values (two archive
    identifiers) never render to the same folder. A value made only of dots, or
    empty, becomes `_` so it can never form a `.` or `..` segment.
    """
    cleaned = _HOSTILE.sub("_", value)
    return "_" if cleaned.strip(".") == "" else cleaned
