from astrbot_plugin_reasonix_subagents.reasonix.config import ConfigHolder
from astrbot_plugin_reasonix_subagents.reasonix.constants import WEB_TOOLS
from astrbot_plugin_reasonix_subagents.reasonix.policy import (
    SPECS,
    effective_bans,
    resolve_policy,
)


def _pol(name, cfg):
    return resolve_policy(cfg, SPECS[name])


def test_builtin_defaults():
    explore = _pol("explore", {})
    assert explore.max_steps == 12
    assert explore.discover_web is False
    assert "astrbot_file_read_tool" in explore.allowed_tools
    assert "reasonix_git_read" in explore.extra_tool_names

    research = _pol("research", {})
    assert research.discover_web is True
    assert any(t in research.allowed_tools for t in WEB_TOOLS)


def test_per_subagent_allowed_tools_replace_baseline():
    cfg = {"subagents": {"explore": {"allowed_tools": ["custom_tool"]}}}
    pol = _pol("explore", cfg)
    assert pol.allowed_tools == ("custom_tool",)
    assert "astrbot_file_read_tool" not in pol.allowed_tools


def test_global_extra_allowed_dedup():
    cfg = {
        "defaults": {"extra_allowed": ["extra_one", "astrbot_file_read_tool"]},
    }
    pol = _pol("explore", cfg)
    assert pol.extra_tool_names  # git still present
    assert pol.allowed_tools.count("astrbot_file_read_tool") == 1
    assert "extra_one" in pol.allowed_tools


def test_exclusions_highest_priority():
    cfg = {
        "defaults": {"excluded_tools": ["astrbot_grep_tool"]},
        "subagents": {"explore": {"excluded_tools": ["reasonix_git_read"]}},
    }
    pol = _pol("explore", cfg)
    assert "astrbot_grep_tool" not in pol.allowed_tools
    assert "reasonix_git_read" not in pol.extra_tool_names
    bans = effective_bans(cfg, "explore")
    assert {"astrbot_grep_tool", "reasonix_git_read"} <= bans


def test_max_steps_priority_and_clamp():
    cfg = {"subagents": {"explore": {"max_steps": 3}}}
    assert _pol("explore", cfg).max_steps == 3

    cfg = {"defaults": {"max_steps": 5}}
    assert _pol("explore", cfg).max_steps == 5

    cfg = {"subagents": {"explore": {"max_steps": 999}}}
    assert _pol("explore", cfg).max_steps == 50


def test_bool_steps_ignored():
    # bool is an int subclass but must not be treated as a step override.
    cfg = {"subagents": {"explore": {"max_steps": True}}}
    assert _pol("explore", cfg).max_steps == 12


def test_timeout_default_and_override():
    assert _pol("explore", {}).timeout == 120
    cfg = {"defaults": {"tool_timeout": 30}}
    assert _pol("explore", cfg).timeout == 30
    cfg = {"defaults": {"tool_timeout": 0}}
    assert _pol("explore", cfg).timeout == 120


def test_provider_id():
    assert _pol("explore", {}).provider_id == ""
    cfg = {"subagents": {"research": {"provider_id": "provider_x"}}}
    assert _pol("research", cfg).provider_id == "provider_x"


def test_deep_review_special_fields():
    pol = _pol("deep_review", {})
    assert pol.aggregator_max_steps == 4
    assert pol.overall_timeout == 600
    cfg = {
        "subagents": {
            "deep_review": {"aggregator_max_steps": 6, "overall_timeout": 900}
        }
    }
    pol = _pol("deep_review", cfg)
    assert pol.aggregator_max_steps == 6
    assert pol.overall_timeout == 900


def test_advanced_web_tool_names_replaces_builtin():
    cfg = {"advanced": {"web_tool_names": ["my_custom_search"]}}
    pol = _pol("research", cfg)
    assert "my_custom_search" in pol.allowed_tools
    assert "web_search_tavily" not in pol.allowed_tools


def test_malformed_config_does_not_crash():
    holder = ConfigHolder({"defaults": "oops", "subagents": [1, 2]})
    pol = resolve_policy(holder.get(), SPECS["explore"])
    assert pol.max_steps == 12
