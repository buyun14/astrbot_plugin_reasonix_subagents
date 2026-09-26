"""Heuristic discovery of read-only web tools from third-party plugins.

Pure logic over a minimal duck-typed tool (``.name``/``.description``/
``.active``); the AstrBot FunctionTool satisfies it directly.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import constants as C
from .config import advanced_override


def _skip_set(cfg: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        advanced_override(cfg, "skip_discovery", tuple(C.SKIP_DISCOVERY_TOOLS))
    )


def _has_side_effect(text: str, side_markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in side_markers)


def is_web_readonly_tool(tool: Any, cfg: dict[str, Any]) -> bool:
    """True if ``tool`` looks like a safe read-only web/search/extract tool."""
    if not bool(getattr(tool, "active", True)):
        return False
    name = (getattr(tool, "name", "") or "").lower().strip()
    skip = _skip_set(cfg)
    if not name or name in skip or name.startswith("transfer_to_"):
        return False
    side_markers = advanced_override(cfg, "side_effect_markers", C.SIDE_EFFECT_MARKERS)
    # Side-effect check applies to BOTH name and description. We must run
    # the description check BEFORE accepting a tool on its name alone:
    # a tool named 'web_search_*' but described as writing/sending data
    # would otherwise be exposed to a read-only subagent.
    description = (getattr(tool, "description", "") or "").lower()
    if _has_side_effect(description, side_markers):
        return False
    if _has_side_effect(name, side_markers):
        return False
    web_markers = advanced_override(cfg, "web_markers", C.WEB_READ_MARKERS)
    if any(marker in name for marker in web_markers):
        return True
    return any(phrase in description for phrase in C.DESCRIPTION_WEB_MARKERS)


def discover_web_tools(func_list: Iterable[Any], cfg: dict[str, Any]) -> list[Any]:
    return [t for t in func_list if is_web_readonly_tool(t, cfg)]
