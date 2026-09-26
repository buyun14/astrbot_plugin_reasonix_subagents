"""Static name/marker lists.

These lists define the *built-in* read-only boundaries. Advanced config can
fully replace them (never silently append): marker lists are a security
boundary and silent append would make the boundary unauditable.
"""

from __future__ import annotations

# Code-reading tools provided by AstrBot's Computer Use runtime. Resolution is
# call-time against the live FunctionToolManager, so inactive tools are skipped.
CODE_READ_TOOLS: tuple[str, ...] = (
    "astrbot_file_read_tool",
    "astrbot_grep_tool",
)

WEB_TOOLS: tuple[str, ...] = (
    "web_search_tavily",
    "tavily_extract_web_page",
    "web_search_bocha",
    "web_search_brave",
    "web_search_firecrawl",
    "firecrawl_extract_web_page",
    "web_search_baidu",
    "web_search_exa",
    "exa_get_contents",
    "web_search_anysearch",
    "astr_kb_search",
)

# Markers for discovering web/search/extract tools from third-party plugins.
# Note: over-broad markers like "query"/"knowledge" were removed in 0.4 because
# they match dangerous data tools (sql_query, knowledge_base_write).
WEB_READ_MARKERS: tuple[str, ...] = (
    "search",
    "fetch",
    "extract",
    "searxng",
    "web",
    "github_search",
    "ddg",
    "duckduckgo",
    "serp",
    "crawl",
)

# Phrases checked against a tool description when its name matched nothing.
DESCRIPTION_WEB_MARKERS: tuple[str, ...] = (
    "search",
    "fetch",
    "extract",
    "web page",
    "webpage",
    "scrape",
    "read a url",
)

# Substrings marking side-effecting tools that must never leak into a read-only
# subagent. Extended in 0.4 to cover data/mutation tools.
SIDE_EFFECT_MARKERS: tuple[str, ...] = (
    "write",
    "edit",
    "delete",
    "remove",
    "upload",
    "download",
    "exec",
    "shell",
    "python",
    "terminal",
    "install",
    "commit",
    "push",
    "deploy",
    "set_",
    "create_",
    "mkdir",
    "move",
    "rename",
    "send_",
    "kill",
    "reboot",
    "approve",
    "submit",
    "background",
    # data-store / API mutation tools discovered via name heuristics
    "sql",
    "database",
    "db_",
    "graphql",
    "mutation",
    "insert",
    "update_",
    "alter",
    "drop",
    "modify",
    "patch",
)

# Exact tool names that must never be discovered (delegation loops, agent
# control, the plugin's own entry points).
SKIP_DISCOVERY_TOOLS: frozenset[str] = frozenset(
    (
        "explore",
        "research",
        "review",
        "security_review",
        "deep_review",
        "reasonix_git_read",
        "run_skill",
        "read_only_skill",
        "read_skill",
        "use_capability",
    )
)

# Output / time caps.
MAX_GIT_OUTPUT = 60_000
MAX_GIT_TIMEOUT_SECONDS = 60
MAX_PASTED_DIFF_CHARS = 40_000
MAX_PARALLEL_REVIEWERS = 3
