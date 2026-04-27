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
