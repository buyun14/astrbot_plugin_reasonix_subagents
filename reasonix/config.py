"""Configuration holder and coercion helpers.

The old design used a module-level list of plugin instances because tool
objects were created at import time. Tools are now created after the plugin
exists and receive a :class:`ConfigHolder`, so no global mutable state is
needed and ``shutdown`` clears the reference.
"""

from __future__ import annotations

from typing import Any

# Section + key names shared by policy resolver, discovery and schema.
SEC_DEFAULTS = "defaults"
SEC_SUBAGENTS = "subagents"
SEC_ADVANCED = "advanced"


class ConfigHolder:
    """Holds the current plugin config dict; replaceable atomically on reload."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config: dict[str, Any] = dict(config or {})

    def update(self, config: dict[str, Any] | None) -> None:
        self._config = dict(config or {})

    def get(self) -> dict[str, Any]:
        return self._config


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
