"""Tiny template engine for library_layout. Intentionally not Jinja.

Grammar:
  template := (literal | placeholder)*
  placeholder := '{' field ('|' filter)* '}'

Example:
  "{year}/{date} - {venue|sanitize}/{filename}"
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from shakedown.utils.sanitize import sanitize


class TemplateError(Exception):
    pass


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")

# Segment rendered when a template field is missing or null. Library metadata is
# patchy (§spec:library-staging); dropping an item because its venue tag is absent
# serves no one, so the field renders as `unknown` and collision detection catches
# any resulting conflicts.
UNKNOWN = "unknown"


Filter = Callable[[str], str]
_FILTERS: dict[str, Filter] = {
    "sanitize": sanitize,
    # `short`: keep first segment (word, abbreviation) — useful for {lineage_short}
    "short": lambda v: v.split()[0] if v else v,
    "lower": str.lower,
    "upper": str.upper,
}


def render(template: str, fields: dict[str, Any]) -> str:
    """Substitute {placeholders} from fields; apply |filters."""
    def _sub(match: re.Match[str]) -> str:
        spec = match.group(1)
        parts = [p.strip() for p in spec.split("|")]
        name, filter_names = parts[0], parts[1:]
        value = fields.get(name)
        if value is None:
            # Missing or null field: render a literal `unknown` segment rather than
            # failing the item. Filters are skipped — the segment is the fixed
            # sentinel, not source metadata to be transformed.
            return UNKNOWN
        out = str(value)
        for fname in filter_names:
            if fname not in _FILTERS:
                raise TemplateError(f"unknown filter: {fname!r}")
            out = _FILTERS[fname](out)
        return out

    return _PLACEHOLDER_RE.sub(_sub, template)


def fields_in(template: str) -> set[str]:
    """Return the set of placeholder field names referenced in the template."""
    return {m.group(1).split("|", 1)[0].strip() for m in _PLACEHOLDER_RE.finditer(template)}
