"""Reasonix-style read-only subagents for AstrBot (agent-as-tool).

This plugin ports the four built-in read-only subagents of DeepSeek-Reasonix
(explore / research / review / security-review) onto AstrBot using the
"agent-as-tool" pattern (FunctionTool + tool_loop_agent):

* Each subagent is a FunctionTool whose ``description`` is written for the main
  LLM's delegation (handoff) decision ("what it does AND when to use it").
* When invoked it runs a *fresh* ``tool_loop_agent`` (its own system prompt +
  a read-only minimal ToolSet) in an isolated context and returns only the
  final text to the parent.
* The subagent toolset is deliberately read-only: file-read / grep / read-only
  shell / read-only git / web search. No writer tools are ever included.
* A ``reasonix_git_read`` read-only git tool backs review / security-review.

Reference prompts:
    DeepSeek-Reasonix/internal/skill/builtins.go (explore/research/review/
    security-review bodies) and internal/agent/task.go (read-only discipline).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.tools.computer_tools.util import workspace_root_for_context

# Tool names the subagents may expose. Resolution happens at call time against
# the live FunctionToolManager, so tools that are not configured/active are
# simply skipped (e.g. Computer Use or a specific web-search provider).
_CODE_READ_TOOLS: tuple[str, ...] = (
    "astrbot_file_read_tool",
    "astrbot_grep_tool",
    "astrbot_execute_shell",
    "astrbot_shell_session",
)
_WEB_TOOLS: tuple[str, ...] = (
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

# Markers used to discover web/search/extract tools provided by third-party
# plugins (e.g. astrbot_plugin_web_searcher_pro registers searxng_* tools),
# which are not part of AstrBot's built-in web tool set.
_WEB_READ_MARKERS: tuple[str, ...] = (
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
    "knowledge",
    "query",
)
# Substrings of names of side-effecting tools that must never leak into a
# read-only subagent even if they match a web marker.
_SIDE_EFFECT_MARKERS: tuple[str, ...] = (
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
)
# Tools that must never be discovered into a subagent's toolset (delegation
# loops, agent control, the plugin's own subagent entry points, read_skill...).
_SKIP_DISCOVERY_TOOLS: frozenset[str] = frozenset(
    (
        "explore",
        "research",
        "review",
        "security_review",
        "reasonix_git_read",
        "run_skill",
        "read_only_skill",
        "read_skill",
        "use_capability",
    )
)


def _is_web_readonly_tool(tool: FunctionTool) -> bool:
    """True for an active tool that looks like a read-only web/search/extract tool.

    Third-party search plugins register tools under arbitrary names (e.g.
    searxng_web_search_general). We detect them by name/description markers and
    exclude anything that looks side-effecting. Best-effort heuristic: if a
    plugin tool is not detected, add its exact name to the subagent's
    ``allowed_tools`` (e.g. ``_WEB_TOOLS``) instead.

    Args:
        tool: The candidate tool.

    Returns:
        Whether the tool is safe to expose to a read-only research sub-agent.
    """
    if not bool(getattr(tool, "active", True)):
        return False
    name = (tool.name or "").lower().strip()
    if not name or name in _SKIP_DISCOVERY_TOOLS or name.startswith("transfer_to_"):
        return False
    if any(marker in name for marker in _SIDE_EFFECT_MARKERS):
        return False
    if any(marker in name for marker in _WEB_READ_MARKERS):
        return True
    description = (tool.description or "").lower()
    return any(
        phrase in description
        for phrase in (
            "search",
            "fetch",
            "extract",
            "web page",
            "webpage",
            "scrape",
            "read a url",
        )
    )


_MAX_GIT_OUTPUT = 60_000  # Characters.
_MAX_GIT_TIMEOUT_SECONDS = 60
_MAX_PASTED_DIFF_CHARS = 40_000  # Characters.


# --------------------------------------------------------------------------- #
# Shared prompt fragments (ported verbatim from Reasonix).
# --------------------------------------------------------------------------- #
_NEGATIVE_CLAIM_RULE = (
    "When you claim something does NOT exist (no caller, no usage, not "
    "implemented), say which searches you ran to reach that conclusion - a "
    "negative claim is only as trustworthy as the search behind it."
)
_TUI_FORMATTING = (
    "Keep the final answer compact and terminal-friendly: short paragraphs or "
    "bullets, no walls of text, no restating the question."
)


# --------------------------------------------------------------------------- #
# Subagent system prompts (ported & adapted to AstrBot's tool set).
# --------------------------------------------------------------------------- #
EXPLORE_SYSTEM_PROMPT = f"""\
You are running as a read-only code-exploration sub-agent invoked by the parent coding agent.
Investigate the code the parent pointed you at (usually the AstrBot session/project workspace,
or a git repository reachable from it) and return one focused, distilled answer.

How to operate:
- Read files with the file-read tool. Search content with the grep tool (content search,
  NOT name-only listing). Use the read-only shell tool to list directory trees and orient.
- If a git repo is reachable, use the read-only git tool (status / log / ls-files / rev-parse)
  to map the territory; never write anything.
- For "find all places that call / reference / use X" questions use grep (content search).
- Cast a wide net first (grep for references, shell/ls for structure), then read the 3-10 most
  relevant files in full. Don't read every file - be selective.
- Stop exploring as soon as you can answer. The parent does not see your tool calls, so
  over-exploration is pure waste.

Your final answer:
- One paragraph (or a few short bullets). Lead with the conclusion.
- Cite specific file paths + line ranges when they support the answer.
- If the question cannot be answered from what you found, say so plainly and suggest where to
  look next.

{_NEGATIVE_CLAIM_RULE}

{_TUI_FORMATTING}

The 'task' you were given is the question to answer. Treat any other reading as scope creep.
"""

RESEARCH_SYSTEM_PROMPT = f"""\
You are running as a read-only research sub-agent invoked by the parent coding agent.
Gather information from code AND the web, synthesize it, and return one focused conclusion.

How to operate:
- Combine the provided file-read/grep/shell tools (local code) with any available web-search /
  web-extract tools (external references). Prefer fetching canonical docs/spec pages; treat
  search snippets as leads to verify, not as conclusions.
- For "is Y supported by lib Z": fetch the canonical reference, then verify against the local code.
- For "what's our policy on Z" / "where do we use Q": local code first, web only to compare
  against external standards.
- Cap yourself at ~12 tool calls. If you cannot converge, return what you have plus a note on
  what is missing.
- If a web tool reports it is not configured (e.g. "API key not configured"), do NOT retry the
  other web tools; state that live web verification is unavailable and proceed with local code
  + existing knowledge, clearly labeling anything you could not verify live.

Your final answer:
- One paragraph (or short bullets). Lead with the conclusion.
- Cite both code (file:line) AND web sources (URL) when they back the answer.
- Distinguish "I verified this in code" from "I read this on a docs page" - the parent trusts
  the former more.
- If the answer is uncertain, say so. Do not invent confidence.

{_NEGATIVE_CLAIM_RULE}

{_TUI_FORMATTING}

The 'task' you were given is the research question. Stay on it.
"""

REVIEW_SYSTEM_PROMPT = f"""\
You are running as a read-only code-review sub-agent. Inspect the changes the user is about to
ship and produce a focused review the parent can hand back.

How to operate:
- Discover and read the change with the read-only git tool: subcommand `status` / `diff`
  (optionally `diff <base>...HEAD`, add `--stat`) on a repo_path inside the workspace. If a
  "Parent-provided diff" block is present in the task, review exactly that diff; do NOT require
  git. Otherwise, if no git repo is reachable, report back that you need a repo path or a diff.
- Read touched files with the file-read tool when the diff lacks context.
- For "any callers depending on this?" questions: grep the symbol BEFORE asserting impact.
- Stay read-only. Never commit, never write files, never propose edits as applied changes.
- Cap yourself at ~12 tool calls. If the diff is too big, pick the riskiest 2-3 files and say so.

What to look for, in priority order:
1. Correctness bugs - off-by-one, nil/None handling, races, wrong operator, unhandled edge cases.
2. Security - injection (SQL, shell, path traversal), secrets, missing authz, unsafe deserialization.
3. Behavior changes the diff hides - renames missing callers, removed load-bearing branches,
   error-handling that now swallows what used to surface.
4. Tests - does the change have tests for the new behavior? Are existing tests still meaningful?
5. Style + consistency - only flag deviations that matter; don't pile on cosmetic nits.

Your final answer MUST be structured as:
- verdict
- blocking_findings
- non_blocking
- required_changes
Do not restate complete files or complete test logs. Keep your whole run under ~8 steps.

{_NEGATIVE_CLAIM_RULE}

{_TUI_FORMATTING}

The 'task' names WHAT to review (a branch, a file set, or "the pending changes"). Stay on it;
don't redesign the feature.
"""

SECURITY_REVIEW_SYSTEM_PROMPT = f"""\
You are running as a read-only security-review sub-agent. Inspect the changes the user is about
to ship through a security lens specifically, and report exploitable issues.

How to operate:
- Default scope: the current branch's diff vs the default branch. Honor a named range or
  directory if given. If a "Parent-provided diff" block is present in the task, review exactly
  that diff; do NOT require git.
- Discover scope first with the read-only git tool: `status`, `diff --stat`, `diff <base>...HEAD`.
  Read touched files (file-read tool) when the diff lacks security context - auth checks, input
  validation, the handler that calls the changed code.
- Use grep to verify "is this user-controlled input ever sanitized later?" / "what other call
  sites depend on this validation?" before asserting impact.
- Stay read-only. Never write, never run destructive commands. The parent decides what to act on.
- Cap yourself at ~12 tool calls. If the diff is too big, focus on the riskiest 2-3 files.

Threat model - flag with severity:
CRITICAL (do-not-ship): SQL/NoSQL/shell/template injection; path traversal; missing authn/authz;
hardcoded secrets; deserialization of untrusted input; cryptographic mistakes (homemade crypto,
MD5/SHA-1 for passwords, ECB, predictable nonces).
HIGH: XSS; SSRF; TOCTOU on auth/file checks; open redirects.
MEDIUM: verbose errors leaking internals; missing rate limiting on credential endpoints; missing
cookie flags (Secure/HttpOnly/SameSite).

Out of scope here (regular review covers them): style, naming, performance, non-security test
gaps, "extract this helper".

Your final answer:
- Lead with a one-sentence verdict: "no security issues found", "minor concerns", or
  "blocking issues".
- Then a list grouped by severity. Each item: file:line + 1-sentence threat + 1-sentence fix
  direction.
- If clean, say so plainly. Don't manufacture findings. Keep your whole run under ~8 steps.

{_NEGATIVE_CLAIM_RULE}

{_TUI_FORMATTING}

The 'task' names what to review. Stay on it; don't redesign the feature.
"""


# --------------------------------------------------------------------------- #
# Read-only git tool.
# --------------------------------------------------------------------------- #
# Only read-only subcommands are reachable. Each is non-mutating by itself;
# combined with a no-shell subprocess call this keeps the tool safe to expose.
_READ_ONLY_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    (
        "status",
        "diff",
        "log",
        "show",
        "blame",
        "ls-files",
        "rev-parse",
        "branch",
        "remote",
        "describe",
        "shortlog",
        "tag",
    )
)
# Flags that can make even a "read-only" subcommand dangerous (e.g. -c config,
# --upload-pack / --output plumbing). Never forwarded.
_FORBIDDEN_GIT_ARGS_PREFIXES: tuple[str, ...] = (
    "-c",
    "--upload-pack",
    "--exec",
    "--output",
)


@dataclass
class ReasonixGitReadTool(FunctionTool[AstrAgentContext]):
    """Run a read-only git subcommand inside the session workspace."""

    name: str = "reasonix_git_read"
    description: str = (
        "Run a READ-ONLY git subcommand on a repository inside the session workspace. "
        "Allowed subcommands: status, diff, log, show, blame, ls-files, rev-parse, branch, "
        "remote, describe, shortlog, tag. Returns command output (truncated). Never writes. "
        "Use this to inspect pending changes of a repo (e.g. `diff`/`diff --stat`/`diff "
        "<base>...HEAD`) before reviewing or reporting. If no repo_path is given the session "
        "workspace root is used."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "description": "Read-only git subcommand to run.",
                    "enum": sorted(_READ_ONLY_GIT_SUBCOMMANDS),
                },
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Optional absolute or workspace-relative path to the git repository. "
                        "Defaults to the session workspace root. Must stay inside the workspace."
                    ),
                },
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional read-only arguments appended after the subcommand, e.g. "
                        "['--stat'] or ['<base>...HEAD']. Flags that change git behavior "
                        "(-c / --upload-pack / --exec / --output) are rejected."
                    ),
                },
            },
            "required": ["subcommand"],
        }
    )

    @staticmethod
    def _sanitize_extra_args(extra_args: Any) -> list[str]:
        if not isinstance(extra_args, list):
            return []
        cleaned: list[str] = []
        for arg in extra_args:
            if not isinstance(arg, str) or not arg.strip():
                continue
            arg = arg.strip()
            if any(
                arg == prefix or arg.startswith(prefix + "=")
                for prefix in _FORBIDDEN_GIT_ARGS_PREFIXES
            ):
                raise ValueError(f"Forbidden git flag: {arg}")
            cleaned.append(arg)
        return cleaned

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        subcommand = str(kwargs.get("subcommand") or "").strip().lower()
        if subcommand not in _READ_ONLY_GIT_SUBCOMMANDS:
            return f"error: unsupported git subcommand '{subcommand}' (read-only allowlist)."
        try:
            extra_args = self._sanitize_extra_args(kwargs.get("extra_args"))
        except ValueError as exc:
            return f"error: {exc}"

        repo_path = kwargs.get("repo_path")
        cwd: Path | None = None
        try:
            cwd = await workspace_root_for_context(context)
        except Exception:  # noqa: BLE001 - workspace resolution is best-effort.
            cwd = None

        if repo_path:
            candidate = Path(str(repo_path).strip()).expanduser()
            if not candidate.is_absolute() and cwd is not None:
                candidate = cwd / candidate
            try:
                candidate = candidate.resolve(strict=True)
            except OSError:
                return f"error: repo_path does not exist: {repo_path}"
            if cwd is not None and not candidate.is_relative_to(cwd):
                return f"error: repo_path must stay inside the session workspace: {cwd}"
            cwd = candidate if candidate.is_dir() else candidate.parent

        command = ["git", "--no-pager", subcommand, *extra_args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd) if cwd is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "GIT_PAGER": "cat", "PAGER": "cat"},
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_MAX_GIT_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - surface git/process errors to the model.
            return f"error: git execution failed: {exc}"

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        body = stdout if not stderr.strip() else f"{stdout}\n[stderr]\n{stderr}".strip()
        body = f"$ {' '.join(command)}\n{body}".strip()
        if len(body) > _MAX_GIT_OUTPUT:
            body = body[:_MAX_GIT_OUTPUT] + "\n...[truncated]"
        return body if body else "error: git produced no output."


GIT_READ_TOOL = ReasonixGitReadTool()


# --------------------------------------------------------------------------- #
# Subagent-as-tool base + four concrete subagents.
# --------------------------------------------------------------------------- #
def _task_parameters(task_hint: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": task_hint,
            },
        },
        "required": ["task"],
    }


def _review_parameters(task_hint: str) -> dict:
    """Schema for review/security_review: task plus optional diff / repo_path carriers."""
    return {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": task_hint,
            },
            "diff": {
                "type": "string",
                "description": (
                    "Optional: paste the full diff text to review when the git repo is not "
                    "reachable from the bot's environment (e.g. sandbox vs host mismatch). The "
                    "sub-agent reviews exactly this diff and does not require git."
                ),
            },
            "repo_path": {
                "type": "string",
                "description": (
                    "Optional: absolute or workspace-relative path of the target git repository "
                    "inside the session workspace. The sub-agent passes it to the read-only git "
                    "tool. Ignored when a diff is provided."
                ),
            },
        },
        "required": ["task"],
    }


@dataclass
class ReasonixSubagentTool(FunctionTool[AstrAgentContext]):
    """A subagent exposed as a tool: runs an isolated read-only agent loop."""

    system_prompt: str = ""
    max_steps: int = 8
    allowed_tools: tuple[str, ...] = ()
    extra_tools: tuple[FunctionTool, ...] = ()
    # When true, also discover any active read-only web/search/extract tool that
    # other plugins registered (e.g. searxng_*), so research works without a
    # built-in AstrBot web-search provider.
    discover_web_readonly: bool = False

    def _build_toolset(self, tool_mgr: Any) -> ToolSet:
        toolset = ToolSet()
        for name in self.allowed_tools:
            try:
                tool = tool_mgr.get_func(name)
            except Exception:  # noqa: BLE001 - manager lookup is best-effort.
                tool = None
            if tool is not None and bool(getattr(tool, "active", True)):
                toolset.add_tool(tool)
        for instance in self.extra_tools:
            toolset.add_tool(instance)
        if self.discover_web_readonly and tool_mgr is not None:
            for tool in getattr(tool_mgr, "func_list", ()):
                if _is_web_readonly_tool(tool):
                    toolset.add_tool(tool)
        return toolset

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return "error: this subagent needs a non-empty 'task' describing the concrete question."

        # Optional review carriers: a pasted diff and/or an explicit repo path decouple
        # review/security_review from "a git repo must be reachable at the workspace root"
        # (e.g. host vs. sandbox environment mismatch). Harmless for other subagents.
        pasted_diff = str(kwargs.get("diff") or "").strip()
        repo_path = str(kwargs.get("repo_path") or "").strip()
        if pasted_diff:
            if len(pasted_diff) > _MAX_PASTED_DIFF_CHARS:
                pasted_diff = pasted_diff[:_MAX_PASTED_DIFF_CHARS] + "\n...[truncated]"
            task = (
                f"{task}\n\nParent-provided diff to review (no git repo needed - review "
                f"exactly this diff):\n```diff\n{pasted_diff}\n```"
            )
        if repo_path:
            task = (
                f"{task}\n\nTarget git repository path (pass this repo_path to the "
                f"read-only git tool): {repo_path}"
            )

        agent_context: AstrAgentContext = context.context
        ctx = agent_context.context
        event = agent_context.event

        try:
            tool_mgr = ctx.get_llm_tool_manager()
        except Exception:  # noqa: BLE001
            tool_mgr = None
        toolset = self._build_toolset(tool_mgr) if tool_mgr is not None else ToolSet()
        if toolset.empty():
            return (
                "error: none of this subagent's read-only tools are currently available. "
                f"Needed tool names (whichever apply): {', '.join((*self.allowed_tools,))}. "
                "For code reading enable Computer Use (computer_use_runtime); for research "
                "configure a web-search provider. "
            )

        provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
        llm_resp = await ctx.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt=task,
            system_prompt=self.system_prompt,
            tools=toolset,
            max_steps=self.max_steps,
            tool_call_timeout=120,
            stream=False,
        )
        text = (getattr(llm_resp, "completion_text", None) or "").strip()
        return text if text else "error: the subagent returned no final text."


@dataclass
class ExploreTool(ReasonixSubagentTool):
    """Read-only codebase investigation in an isolated sub-agent."""

    name: str = "explore"
    description: str = (
        "Run a read-only codebase investigation in an isolated sub-agent (forked context). Use "
        "for broad survey questions across many files - 'find all places that X', 'how does Y "
        "work across the project', 'audit Z'. The sub-agent reads code with its own read-only "
        "tools (its reads/reasoning never enter your context) and returns one distilled answer "
        "with file:line citations. Do NOT use for making edits."
    )
    parameters: dict = Field(
        default_factory=lambda: _task_parameters(
            "Concrete investigation question. The sub-agent has none of your context - write a "
            "self-contained task naming the symbol / pattern / behavior to survey."
        )
    )
    system_prompt: str = EXPLORE_SYSTEM_PROMPT
    max_steps: int = 12
    allowed_tools: tuple[str, ...] = _CODE_READ_TOOLS
    extra_tools: tuple[FunctionTool, ...] = (GIT_READ_TOOL,)


@dataclass
class ResearchTool(ReasonixSubagentTool):
    """Combines local code reading with web search/extract in a sub-agent."""

    name: str = "research"
    description: str = (
        "Run a read-only research sub-agent that combines local code reading with web "
        "search/extract tools. Use when the answer needs both an external reference and local "
        "verification - 'is X supported by lib Y', 'compare our impl against the spec'. Returns "
        "one synthesis citing code (file:line) and web (URL)."
    )
    parameters: dict = Field(
        default_factory=lambda: _task_parameters(
            "Concrete research question. The sub-agent has none of your context - name the "
            "external thing to look up and the local code to compare against."
        )
    )
    system_prompt: str = RESEARCH_SYSTEM_PROMPT
    max_steps: int = 12
    allowed_tools: tuple[str, ...] = (*_CODE_READ_TOOLS, *_WEB_TOOLS)
    extra_tools: tuple[FunctionTool, ...] = (GIT_READ_TOOL,)
    discover_web_readonly: bool = True


@dataclass
class ReviewTool(ReasonixSubagentTool):
    """Read-only code review over the pending changes of a workspace git repo."""

    name: str = "review"
    description: str = (
        "Run a read-only code-review sub-agent over the pending changes of a git repository "
        "inside the session workspace (or a diff you can point it to). Flags correctness, "
        "security, missing tests and hidden behavior changes; returns structured verdict / "
        "blocking_findings / non_blocking / required_changes with file:line. Use before shipping "
        "a PR-shaped change or after finishing a multi-step edit."
    )
    parameters: dict = Field(
        default_factory=lambda: _review_parameters(
            "What to focus the review on (e.g. 'focus on the auth changes' or 'general'). "
            "Provide 'diff' to review a pasted diff, or 'repo_path' to point at a git repo "
            "inside the workspace. If neither is given the sub-agent reads the workspace "
            "repo's pending changes itself with read-only git."
        )
    )
    system_prompt: str = REVIEW_SYSTEM_PROMPT
    max_steps: int = 8
    allowed_tools: tuple[str, ...] = _CODE_READ_TOOLS
    extra_tools: tuple[FunctionTool, ...] = (GIT_READ_TOOL,)


@dataclass
class SecurityReviewTool(ReasonixSubagentTool):
    """Security-focused review of the current diff, severity-tagged."""

    name: str = "security_review"
    description: str = (
        "Run a read-only security-review sub-agent over the current diff of a git repository "
        "inside the session workspace. Flags injection / authz / secrets / deserialization / "
        "path-traversal / crypto issues, severity-tagged (CRITICAL/HIGH/MEDIUM). Use when "
        "shipping changes that touch auth, input parsing, file IO, or external requests."
    )
    parameters: dict = Field(
        default_factory=lambda: _review_parameters(
            "Optional scope hint (e.g. 'focus on token handling in internal/auth/') or 'full' "
            "for everything in the diff. Provide 'diff' to review a pasted diff, or 'repo_path' "
            "to point at a git repo inside the workspace. If neither is given the sub-agent "
            "reads the workspace repo's pending changes itself with read-only git."
        )
    )
    system_prompt: str = SECURITY_REVIEW_SYSTEM_PROMPT
    max_steps: int = 8
    allowed_tools: tuple[str, ...] = _CODE_READ_TOOLS
    extra_tools: tuple[FunctionTool, ...] = (GIT_READ_TOOL,)


EXPLORE_TOOL = ExploreTool()
RESEARCH_TOOL = ResearchTool()
REVIEW_TOOL = ReviewTool()
SECURITY_REVIEW_TOOL = SecurityReviewTool()


# --------------------------------------------------------------------------- #
# Plugin entry.
# --------------------------------------------------------------------------- #
# NOTE: The @register decorator is deprecated. AstrBot auto-detects classes
# that inherit from Star; identity/description come from metadata.yaml.
class ReasonixSubagentsPlugin(Star):
    """Registers the four Reasonix subagent delegation tools on the main LLM."""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config or {}
        try:
            self.context.add_llm_tools(
                EXPLORE_TOOL,
                RESEARCH_TOOL,
                REVIEW_TOOL,
                SECURITY_REVIEW_TOOL,
            )
            self.logger.info(
                "Registered Reasonix subagent tools: explore, research, review, security_review."
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed to register Reasonix subagent tools.")

    @filter.command(
        "reasonix_subagents", alias={"reasonix-subagents", "reasonix子代理"}
    )
    async def reasonix_subagents_status(self, event: AstrMessageEvent):
        """Show the registered Reasonix subagent tools and their availability."""
        lines = [
            "Reasonix 子代理（agent-as-tool）：",
            "- explore: 只读代码库调查",
            "- research: 代码 + 网页研究",
            "- review: 只读代码评审（git diff）",
            "- security_review: 只读安全评审",
            "",
            "依赖：代码读取需启用 Computer Use；research 需配置 web 搜索；",
            "review/security_review 需把 git 仓库放进会话 workspace。",
            "直接对主 LLM 说“explore 一下 …”或“review 当前改动”即可触发委派。",
        ]
        yield event.plain_result("\n".join(lines))
