from dataclasses import dataclass

from astrbot_plugin_reasonix_subagents.reasonix.discovery import (
    discover_web_tools,
    is_web_readonly_tool,
)


@dataclass
class FakeTool:
    name: str
    description: str = ""
    active: bool = True


def test_basic_search_tool_accepted():
    assert is_web_readonly_tool(FakeTool("searxng_web_search"), {})
    assert is_web_readonly_tool(FakeTool("web_search_tavily"), {})


def test_side_effect_tools_rejected():
    assert not is_web_readonly_tool(FakeTool("web_search_write"), {})
    assert not is_web_readonly_tool(FakeTool("sql_query"), {})
    assert not is_web_readonly_tool(FakeTool("run_graphql_query"), {})
    assert not is_web_readonly_tool(FakeTool("database_insert"), {})


def test_description_side_effect_rejected():
    # Name matches nothing; description describes a search but mentions writing.
    t = FakeTool("internal_tool", "search the API and write results to disk")
    assert not is_web_readonly_tool(t, {})


def test_description_only_match():
    t = FakeTool("internal_tool", "read a url and return the page")
    assert is_web_readonly_tool(t, {})


def test_inactive_skipped_and_skip_names():
    assert not is_web_readonly_tool(FakeTool("searxng_web_search", active=False), {})
    assert not is_web_readonly_tool(FakeTool("explore"), {})
    assert not is_web_readonly_tool(FakeTool("transfer_to_agent_x"), {})


def test_discover_batch():
    tools = [
        FakeTool("searxng_search"),
        FakeTool("file_write"),
        FakeTool("web_fetch_pro"),
    ]
    found = discover_web_tools(tools, {})
    assert [t.name for t in found] == ["searxng_search", "web_fetch_pro"]


def test_advanced_marker_override():
    cfg = {"advanced": {"web_markers": ["portal_"]}}
    assert is_web_readonly_tool(FakeTool("portal_lookup"), cfg)
    # Override fully replaces, so built-in marker no longer matches.
    assert not is_web_readonly_tool(FakeTool("searxng_web_search"), cfg)
