"""Configuration holder and coercion helpers.

The old design used a module-level list of plugin instances because tool
objects were created at import time. Tools are now created after the plugin
exists and receive a :class:`ConfigHolder`, so no global mutable state is
needed and ``shutdown`` clears the reference.
"""

from __future__ import annotations

from typing import Any, Callable

# Section + key names shared by policy resolver, discovery and schema.
SEC_DEFAULTS = "defaults"
SEC_SUBAGENTS = "subagents"
SEC_ADVANCED = "advanced"


class ConfigHolder:
    """Returns the latest plugin config on every ``get()``.

    Two construction modes:

    - Snapshot: pass a dict, and the holder keeps that dict until ``update``
      is called. Fine when no one else owns the dict.
    - Provider: pass a zero-arg callable that returns the *current* config
      dict. The plugin should use this with ``lambda: self.config`` so that
      if AstrBot ever replaces ``plugin.config`` in place, every subsequent
      ``get()`` reflects the new value without the plugin having to wire
      a reload hook. ``update`` is ignored in provider mode (and
      ``shutdown`` no longer needs to clear anything).

    The provider mode avoids a class of bugs where a config dict is
    snapshotted at __init__ time and never refreshed, leaving "hot reload"
    knobs silently stale until the plugin instance is rebuilt.
    """

    def __init__(
        self,
        config: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._provider: Callable[[], dict[str, Any]] | None = None
        if callable(config):
            self._provider = config
        else:
            self._snapshot: dict[str, Any] = dict(config or {})

    def update(self, config: dict[str, Any] | None) -> None:
        # Provider mode: external code owns the truth, so update is a no-op.
        # The provider (e.g. ``lambda: self.config``) is consulted on every
        # ``get()`` and will return the latest dict.
        if self._provider is not None:
            return
        self._snapshot = dict(config or {})

    def get(self) -> dict[str, Any]:
        if self._provider is not None:
            value = self._provider()
            return value if isinstance(value, dict) else {}
        return self._snapshot


def as_str_list(value: Any) -> tuple[str, ...]:
    """Coerce a config field to stripped, non-empty strings.

    Non-string elements are dropped, not ``str()``-coerced, so a malformed
    entry cannot silently change the tool boundary.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(s for s in (v.strip() for v in value if isinstance(v, str)) if s)


def clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    """Parse int, clamp to [low, high], fallback on error/non-positive."""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return fallback
    if num <= 0:
        return fallback
    return max(low, min(high, num))


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    section = cfg.get(name)
    return section if isinstance(section, dict) else {}


def defaults_section(cfg: dict[str, Any]) -> dict[str, Any]:
    return _section(cfg, SEC_DEFAULTS)


def subagents_section(cfg: dict[str, Any]) -> dict[str, Any]:
    return _section(cfg, SEC_SUBAGENTS)


def advanced_section(cfg: dict[str, Any]) -> dict[str, Any]:
    return _section(cfg, SEC_ADVANCED)


def subagent_override(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    sub = subagents_section(cfg).get(name)
    return sub if isinstance(sub, dict) else {}


def subagent_enabled(cfg: dict[str, Any], name: str) -> bool:
    over = subagent_override(cfg, name)
    if not over:
        return True
    enabled = over.get("enabled", True)
    return bool(enabled) if isinstance(enabled, bool) else True


def advanced_override(
    cfg: dict[str, Any], key: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    """Return advanced list for ``key`` or ``default``; advanced fully replaces."""
    value = advanced_section(cfg).get(key)
    out = as_str_list(value)
    return out if out else default
