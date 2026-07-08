"""Plugin registry: type: <name> in config → plugin class."""
from __future__ import annotations

from shakedown.config import SourceConfig
from shakedown.plugins.base import SourcePlugin


class UnknownPluginError(Exception):
    pass


_REGISTRY: dict[str, type[SourcePlugin]] = {}
_builtins_loaded = False


def register[PluginT: SourcePlugin](plugin_cls: type[PluginT]) -> type[PluginT]:
    """Record a plugin class under its ``type_name``.

    Type-preserving: the decorated class keeps its concrete type so callers
    (and tests) can reference subclass-only attributes on instances.
    """
    _REGISTRY[plugin_cls.type_name] = plugin_cls
    return plugin_cls


def _ensure_builtins_loaded() -> None:
    """Lazy import of bundled plugins. Avoids the registry↔plugin import cycle."""
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    from shakedown.plugins.etree.plugin import EtreePlugin  # noqa: F401
    from shakedown.plugins.ia.plugin import IAPlugin  # noqa: F401


def for_source(source_config: SourceConfig) -> SourcePlugin:
    return plugin_class(source_config.type)(source_config)


def plugin_class(type_name: str) -> type[SourcePlugin]:
    """Look up a plugin class by its `type:` name. Used by config validation."""
    _ensure_builtins_loaded()
    cls = _REGISTRY.get(type_name)
    if cls is None:
        raise UnknownPluginError(
            f"unknown source type {type_name!r}; available: {sorted(_REGISTRY)}"
        )
    return cls


def known_types() -> list[str]:
    _ensure_builtins_loaded()
    return sorted(_REGISTRY)
