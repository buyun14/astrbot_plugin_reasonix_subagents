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
* ``deep_review`` runs several specialist reviewers in parallel, then a merge
  step gates the result by confidence (adapted from Anthropic's
  claude-plugins-official ``code-review`` + ``pr-review-toolkit``; the
  security checklist in ``security_review`` is adapted from
  ``security-guidance``). Apache-2.0 content, reused with attribution.

Reference prompts:
    DeepSeek-Reasonix/internal/skill/builtins.go (explore/research/review/
    security-review bodies) and internal/agent/task.go (read-only discipline).
"""

from __future__ import annotations

import asyncio
import logging
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

_logger = logging.getLogger("astrbot_plugin_reasonix_subagents")

# --------------------------------------------------------------------------- #
# Configuration plumbing.
# --------------------------------------------------------------------------- #
# The plugin's tool instances (EXPLORE_TOOL / RESEARCH_TOOL / ...) are created
# at import time, BEFORE the plugin instance exists, so they cannot capture a
# config object at construction. The plugin instance appends itself to this
# module-level list in ``__init__``; tools read the *current* config via
# ``_plugin_config()`` on every call, which is both simpler than snapshotting
# and robust to AstrBot replacing the plugin instance on reload.
_PLUGIN_REF: list[Any] = []


def _plugin_config() -> dict[str, Any]:
    """Return the latest plugin ``config`` dict, or ``{}`` if unavailable."""
    for plugin in _PLUGIN_REF:
        cfg = getattr(plugin, "config", None)
        if isinstance(cfg, dict):
            return cfg
    return {}


def _as_str_list(value: Any) -> tuple[str, ...]:
    """Coerce a config field to a tuple of stripped, non-empty strings."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(s for s in (str(v).strip() for v in value) if s)


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    """Parse ``value`` as an int and clamp it to ``[low, high]``; fallback on error."""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return fallback
    if num <= 0:
        return fallback
    return max(low, min(high, num))


def _subagent_enabled(name: str) -> bool:
    """Return whether the subagent named ``name`` should be registered."""
    cfg = _plugin_config()
    if not isinstance(cfg.get("subagents"), dict):
        return True
    over = cfg["subagents"].get(name)
    if not isinstance(over, dict):
        return True
    enabled = over.get("enabled", True)
    return bool(enabled) if isinstance(enabled, bool) else True


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
        "deep_review",
        "reasonix_git_read",
        "run_skill",
        "read_only_skill",
        "read_skill",
        "use_capability",
    )
)


def _marker_override(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Return advanced override list for ``key`` or ``default`` if not configured.

    The ``advanced`` section fully *replaces* (not extends) the built-in
    markers; an empty list means "use built-ins". This is deliberate: marker
    lists are a security boundary, and silent append would make it hard to
    audit what is currently in scope.
    """
    cfg = _plugin_config()
    advanced = cfg.get("advanced") if isinstance(cfg.get("advanced"), dict) else {}
    value = advanced.get(key) if isinstance(advanced, dict) else None
    out = _as_str_list(value)
    return out if out else default


def _skip_set_override(default: frozenset[str]) -> frozenset[str]:
    out = _marker_override("skip_discovery", tuple(default))
    return frozenset(out)


def _is_web_readonly_tool(tool: FunctionTool) -> bool:
    """True for an active tool that looks like a read-only web/search/extract tool.

    Third-party search plugins register tools under arbitrary names (e.g.
    searxng_web_search_general). We detect them by name/description markers and
    exclude anything that looks side-effecting. Best-effort heuristic: if a
    plugin tool is not detected, add its exact name to the subagent's
    ``allowed_tools`` (e.g. ``_WEB_TOOLS``) instead.

    Marker lists are read from ``advanced`` on every call (overrides only -
    empty means built-in). This is intentional: the markers are a security
    boundary and silent append would make the boundary unauditable.

    Args:
        tool: The candidate tool.

    Returns:
        Whether the tool is safe to expose to a read-only research sub-agent.
    """
    if not bool(getattr(tool, "active", True)):
        return False
    name = (tool.name or "").lower().strip()
    skip_set = _skip_set_override(_SKIP_DISCOVERY_TOOLS)
    if not name or name in skip_set or name.startswith("transfer_to_"):
        return False
    side_effect_markers = _marker_override("side_effect_markers", _SIDE_EFFECT_MARKERS)
    if any(marker in name for marker in side_effect_markers):
        return False
    web_markers = _marker_override("web_markers", _WEB_READ_MARKERS)
    if any(marker in name for marker in web_markers):
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

# Dangerous-API checklist distilled from Anthropic's claude-plugins-official
# ``security-guidance/hooks/patterns.py`` (25 pattern rules). Folded into the
# security-review prompt as an explicit scan list.
_SECURITY_PATTERN_CHECKLIST = """\
- eval() / new Function() / document.write() / innerHTML / outerHTML /
  insertAdjacentHTML / dangerouslySetInnerHTML fed with untrusted input (XSS /
  code injection).
- child_process.exec / execSync / os.system / subprocess(shell=True) / go exec
  through a shell - prefer argument arrays / shell=False; flag interpolated
  untrusted input.
- deserialization: pickle / marshal.loads / shelve / torch.load
  (weights_only=False) / unsafe yaml.load of untrusted data.
- crypto: homemade crypto, MD5/SHA-1 for passwords, AES-ECB, missing IV/nonce,
  TLS certificate verification disabled.
- XML: parsing untrusted XML without hardening (XXE) via xml.etree / lxml.
- GitHub Actions / CI workflow: interpolating issue/PR/event fields into run:
  or ref: (command / ref injection).
- script/link tags without SRI / subresource integrity.
Flag each construct only where it can be reached by untrusted input or weakens
a security control."""


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

Additional dangerous-API scan: actively grep the touched code for these constructs and flag
any that handle untrusted input or disable checks:
{_SECURITY_PATTERN_CHECKLIST}

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
    # Config-section key for ``_BUILTIN_POLICY``. Subclasses override.
    policy_name: str = ""

    def _policy(self) -> dict[str, Any]:
        """Resolve the effective policy for this subagent from config + builtin.

        Resolution order (per task spec):
        - ``subagents.<name>.allowed_tools`` if non-empty, else builtin baseline.
        - Append ``defaults.extra_allowed`` (dedup, order preserved).
        - Remove ``defaults.excluded_tools`` and ``subagents.<name>.excluded_tools``.
        - ``max_steps``: per-subagent > defaults > builtin. Clamped to [1, 50].
        - ``timeout``: ``defaults.tool_timeout`` clamped to [5, 600], default 120.
        - ``discover_web``: per-subagent > builtin.
        - ``provider_id``: per-subagent (empty = follow current session).

        Returns:
            Dict with keys ``allowed_tools``, ``extra_tools``, ``max_steps``,
            ``timeout``, ``discover_web``, ``provider_id``.
        """
        cfg = _plugin_config()
        base = _BUILTIN_POLICY.get(self.policy_name, {})
        defaults_section = cfg.get("defaults")
        defaults = defaults_section if isinstance(defaults_section, dict) else {}
        subagents_section = cfg.get("subagents")
        subagents = subagents_section if isinstance(subagents_section, dict) else {}
        over = (
            subagents.get(self.policy_name)
            if isinstance(subagents.get(self.policy_name), dict)
            else {}
        )

        # allowed_tools: per-subagent non-empty wins, else builtin baseline.
        # ``_as_str_list`` strips whitespace and drops empties.
        allowed_over = _as_str_list(over.get("allowed_tools"))
        baseline = (
            tuple(allowed_over)
            if allowed_over
            else tuple(base.get("allowed_tools", self.allowed_tools))
        )
        # Apply advanced.web_tool_names override: any entry in the built-in
        # baseline that came from ``_WEB_TOOLS`` is replaced by the configured
        # web_tool_names list (empty -> keep built-in). The advanced list
        # fully replaces (not appends) the built-in web set, mirroring how
        # the other advanced markers behave: marker lists are a security
        # boundary and silent append would make them unauditable.
        builtin_web = set(_WEB_TOOLS)
        if any(name in builtin_web for name in baseline):
            web_override = _marker_override("web_tool_names", _WEB_TOOLS)
            rewritten: list[str] = []
            for name in baseline:
                if name in builtin_web:
                    rewritten.extend(n for n in web_override if n not in rewritten)
                else:
                    if name not in rewritten:
                        rewritten.append(name)
            baseline = tuple(rewritten)

        # Append ``defaults.extra_allowed`` (dedup, preserve order).
        allowed: list[str] = list(baseline)
        for t in _as_str_list(defaults.get("extra_allowed")):
            if t not in allowed:
                allowed.append(t)

        # Remove global + per-subagent exclusions (highest priority).
        banned: set[str] = set(_as_str_list(defaults.get("excluded_tools"))) | set(
            _as_str_list(over.get("excluded_tools"))
        )
        allowed_tuple = tuple(t for t in allowed if t not in banned)

        # max_steps: per-subagent > defaults > builtin.
        fallback_steps = int(base.get("max_steps", self.max_steps))
        over_steps = over.get("max_steps")
        default_steps = defaults.get("max_steps")
        if isinstance(over_steps, int) and over_steps > 0:
            steps = _clamp_int(over_steps, 1, 50, fallback_steps)
        elif isinstance(default_steps, int) and default_steps > 0:
            steps = _clamp_int(default_steps, 1, 50, fallback_steps)
        else:
            steps = fallback_steps

        timeout = _clamp_int(defaults.get("tool_timeout"), 5, 600, 120)

        discover_web = over.get("discover_web")
        if not isinstance(discover_web, bool):
            discover_web = bool(base.get("discover_web", self.discover_web_readonly))

        provider_id = str(over.get("provider_id") or "").strip()

        return {
            "allowed_tools": allowed_tuple,
            "extra_tools": tuple(base.get("extra_tools", self.extra_tools)),
            "max_steps": steps,
            "timeout": timeout,
            "discover_web": discover_web,
            "provider_id": provider_id,
        }

    def _build_toolset(self, tool_mgr: Any) -> tuple[ToolSet, list[str]]:
        """Build the ToolSet from current policy.

        Returns:
            (toolset, unknown_tool_names). The second list contains names that
            were configured by the user but not present (or inactive) in the
            FunctionToolManager - surfaced as a warning so typos do not pass
            silently.
        """
        policy = self._policy()
        toolset = ToolSet()
        unknown: list[str] = []
        for name in policy["allowed_tools"]:
            try:
                tool = tool_mgr.get_func(name) if tool_mgr is not None else None
            except Exception:  # noqa: BLE001 - manager lookup is best-effort.
                tool = None
            if tool is not None and bool(getattr(tool, "active", True)):
                toolset.add_tool(tool)
            else:
                # Either missing entirely OR present-but-disabled: both mean
                # the user's configuration will silently lose this entry at
                # runtime, so surface them in the unknown list so the warning
                # below actually catches misconfigured tools.
                unknown.append(name)
        for instance in policy["extra_tools"]:
            toolset.add_tool(instance)
        if policy["discover_web"] and tool_mgr is not None:
            for tool in getattr(tool_mgr, "func_list", ()):
                if _is_web_readonly_tool(tool):
                    toolset.add_tool(tool)
        if unknown:
            available = sorted(
                t.name
                for t in getattr(tool_mgr, "func_list", ())
                if bool(getattr(t, "active", True)) and getattr(t, "name", "")
            )
            _logger.warning(
                "[%s] %d configured tool name(s) not found: %s. "
                "Currently active tools: %s",
                self.policy_name or self.__class__.__name__,
                len(unknown),
                unknown,
                available,
            )
        return toolset, unknown

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
        toolset, _unknown = (
            self._build_toolset(tool_mgr) if tool_mgr is not None else (ToolSet(), [])
        )
        policy = self._policy()
        if toolset.empty():
            return (
                "error: none of this subagent's read-only tools are currently available. "
                f"Needed tool names (whichever apply): {', '.join(policy['allowed_tools'])}. "
                "For code reading enable Computer Use (computer_use_runtime); for research "
                "configure a web-search provider. "
            )

        provider_id = policy["provider_id"] or await ctx.get_current_chat_provider_id(
            event.unified_msg_origin
        )
        llm_resp = await ctx.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt=task,
            system_prompt=self.system_prompt,
            tools=toolset,
            max_steps=policy["max_steps"],
            tool_call_timeout=policy["timeout"],
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
    policy_name: str = "explore"


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
    policy_name: str = "research"


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
    policy_name: str = "review"


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
    policy_name: str = "security_review"


EXPLORE_TOOL = ExploreTool()
RESEARCH_TOOL = ResearchTool()
REVIEW_TOOL = ReviewTool()
SECURITY_REVIEW_TOOL = SecurityReviewTool()


# --------------------------------------------------------------------------- #
# Deep review: parallel specialist reviewers + confidence gating.
# Adapted from Anthropic's claude-plugins-official plugins:
#   - code-review: independent reviewers + 0-100 confidence rubric, drop < 80.
#   - pr-review-toolkit: specialist reviewer personas (bugs, guidelines,
#     silent failures, tests, comments/types).
# The per-issue scorer agents are folded into one merge/arbiter step to keep
# the tool call budget bounded.
# --------------------------------------------------------------------------- #
_MAX_PARALLEL_REVIEWERS = 3
_MAX_REVIEWER_STEPS = 8
_MAX_AGGREGATOR_STEPS = 4

_REVIEWER_OPERATION = """\
How to operate (read-only):
- Review exactly the "Parent-provided diff" in the task if present; otherwise use the read-only
  git tool to read the change. You may read files with the file-read/grep tools for context.
- Do not run builds, tests, typecheckers or linters - assume CI does that separately.
- Do not write, edit, commit, or call any review/deep-review tools.
- Cap your tool calls at ~10. Focus on real, high-signal issues; skip nits and likely false
  positives.
"""

_REVIEWER_OUTPUT_RULES = """\
Output each candidate issue in this exact structure (Markdown bullets), one per issue:
- confidence: <0-100 (75+ = very likely real; >=80 = reportable)>
- location: <file:line range>
- issue: <one-line description>
- why: <evidence from the code or git history; for guideline claims quote the rule file>

If you find no real candidate issues, output exactly: NO_ISSUES
Do not restate the diff or paste whole files.
"""

_REVIEWER_BUGS = f"""\
You are specialist reviewer #1 (correctness & behavior) in a parallel code-review team.

Mission: shallow-scan the change for real bugs and hidden behavior changes only:
- off-by-one, wrong operator/condition, None/null handling, races, unhandled edge cases.
- behavior the diff hides: renames missing callers, removed load-bearing branches, error
  handling that now swallows what used to surface.
Ignore style, tests and security (other specialists cover them) and pre-existing issues.
{_REVIEWER_OPERATION}
{_REVIEWER_OUTPUT_RULES}
"""

_REVIEWER_GUIDELINES = f"""\
You are specialist reviewer #2 (guidelines & code quality) in a parallel code-review team.

Mission: check the change against the repo's explicit guidance and quality bar:
- Read AGENTS.md / README / conventions files if present in the workspace; flag deviations
  that matter (import patterns, error-handling/logging, naming, architecture, platform
  compatibility).
- Code quality: significant duplication, missing critical error handling, dead code introduced.
Ignore cosmetic nits and anything a linter would catch. Style only if a rule file says so.
{_REVIEWER_OPERATION}
{_REVIEWER_OUTPUT_RULES}
"""

_REVIEWER_SILENT_FAILURES = f"""\
You are specialist reviewer #3 (error handling) in a parallel code-review team.

Mission: hunt silent failures and poor error handling introduced or touched by the change:
- empty catch blocks; broad exception catching that hides unrelated errors.
- catch/log-and-continue that swallows failures; returning default/None on error without logging.
- fallbacks that mask the real problem, or fall back to a mock/stub in production.
- optional chaining / null-coalescing that silently skips operations that can fail.
- user-facing error messages that are generic, unactionable, or leak internals.
Flag each with where the error is hidden and what a user would experience.
{_REVIEWER_OPERATION}
{_REVIEWER_OUTPUT_RULES}
"""

_REVIEWER_TESTS = f"""\
You are specialist reviewer #4 (test coverage) in a parallel code-review team.

Mission: assess whether the change is adequately tested (behavioral, not line coverage):
- Is the new behavior covered? Edge cases, boundary conditions, and negative tests for any
  new validation/parsing.
- Are error paths / failure branches tested?
- Async/concurrency paths if the change touches them.
Only report concrete gaps tied to the change; do not demand 100% coverage or nitpick.
{_REVIEWER_OPERATION}
{_REVIEWER_OUTPUT_RULES}
"""

_REVIEWER_COMMENTS_TYPES = f"""\
You are specialist reviewer #5 (comments & types) in a parallel code-review team.

Mission: inspect the change for documentation/type rot:
- Comments/docstrings added or touched: do they accurately match the code (signatures,
  behavior, edge cases)? Flag claims that are wrong or will rot.
- Types/data models added or changed: are invariants explicit and encapsulated (illegal states
  should be unrepresentable), preconditions/postconditions clear?
Only flag issues that will bite maintainers; ignore nitpicks.
{_REVIEWER_OPERATION}
{_REVIEWER_OUTPUT_RULES}
"""

_DEEP_REVIEW_SPECIALISTS: tuple[tuple[str, str], ...] = (
    ("correctness", _REVIEWER_BUGS),
    ("guidelines", _REVIEWER_GUIDELINES),
    ("silent-failures", _REVIEWER_SILENT_FAILURES),
    ("tests", _REVIEWER_TESTS),
    ("comments-types", _REVIEWER_COMMENTS_TYPES),
)

_CONFIDENCE_RUBRIC = """\
Confidence rubric (apply verbatim):
- 0: false positive - does not stand up to light scrutiny, or pre-existing.
- 25: possibly real, unverified.
- 50: verified real but low importance / rare / nitpick-ish.
- 75: highly confident it is real and will be hit in practice; existing approach insufficient.
- 100: certain - evidence directly confirms a real, frequent issue.
"""

_FALSE_POSITIVE_EXAMPLES = """\
Likely false positives (drop unless strongly evidenced):
- pre-existing issues (not introduced by the diff);
- things that only *look* like bugs;
- pedantic nits a senior engineer would not raise;
- anything a linter/typechecker/compiler/CI would catch (imports, types, broken tests, formatting);
- general quality complaints (coverage, docs) unless a repo rule explicitly requires it;
- functional changes that are likely intentional or required by the broader change;
- issues on lines the diff did not touch.
"""

_DEEP_REVIEW_AGGREGATOR_PROMPT = f"""\
You are the aggregator of a parallel code review. Several independent specialist reviewers
audited the same diff; each returned candidate issues tagged with a confidence score and evidence.

Merge them into ONE high-signal review:
1. Score every candidate with this rubric: {_CONFIDENCE_RUBRIC}
2. Drop any issue scored below 80.
3. Drop false positives: {_FALSE_POSITIVE_EXAMPLES}
4. Deduplicate overlapping issues across reviewers - keep the most specific description and the
   strongest evidence; merge issues that share a root cause.
5. Re-rank survivors by severity; keep only issues that are real, actionable and worth the
   author's time.

Output exactly this structure (English):
- verdict: <one line, e.g. "LGTM - no high-confidence issues" | "N high-confidence issues">
- blocking_findings: <each: file:line - issue (1 sentence) - why it matters (1 sentence)>
- non_blocking: <each: file:line - issue (1 sentence)>
- required_changes: <concrete asks, optional>
If nothing survives the filter, say so plainly; do not manufacture findings.
"""


@dataclass
class DeepReviewTool(ReasonixSubagentTool):
    """Run several specialist reviewers in parallel, then gate by confidence."""

    name: str = "deep_review"
    description: str = (
        "Run a deeper read-only code review: several specialist reviewers (correctness, "
        "guidelines, silent failures, tests, comments/types) audit the same diff in parallel, "
        "then a merge step filters false positives by confidence (>=80) and returns one "
        "structured report (verdict / blocking_findings / non_blocking / required_changes). "
        "Slower and more thorough than `review` - use for large or risky changes, or when a "
        "first review left many uncertain findings. Read-only; never edits."
    )
    parameters: dict = Field(
        default_factory=lambda: _review_parameters(
            "What to focus on, plus the same optional 'diff' / 'repo_path' carriers as review. "
            "If neither a diff nor a reachable git repo is given the tool fails fast and asks "
            "for one."
        )
    )
    allowed_tools: tuple[str, ...] = _CODE_READ_TOOLS
    extra_tools: tuple[FunctionTool, ...] = (GIT_READ_TOOL,)
    max_steps: int = _MAX_REVIEWER_STEPS
    policy_name: str = "deep_review"

    async def _run_agent(
        self,
        ctx: Any,
        event: Any,
        provider_id: str,
        *,
        system_prompt: str,
        prompt: str,
        tools: ToolSet | None,
        max_steps: int,
        tool_timeout: int,
    ) -> str:
        """Run one isolated agent loop and return its final text."""
        llm_resp = await ctx.tool_loop_agent(
            event=event,
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
            max_steps=max_steps,
            tool_call_timeout=tool_timeout,
            stream=False,
        )
        return (getattr(llm_resp, "completion_text", None) or "").strip()

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return "error: deep_review needs a non-empty 'task'."

        agent_context: AstrAgentContext = context.context
        ctx = agent_context.context
        event = agent_context.event

        try:
            tool_mgr = ctx.get_llm_tool_manager()
        except Exception:  # noqa: BLE001
            tool_mgr = None
        toolset, _unknown = (
            self._build_toolset(tool_mgr) if tool_mgr is not None else (ToolSet(), [])
        )
        policy = self._policy()
        if toolset.empty():
            return (
                "error: none of deep_review's read-only tools are available. Enable Computer "
                "Use (computer_use_runtime) for code reading."
            )

        # Build ONE diff snapshot so all parallel reviewers share identical input
        # instead of each hitting git concurrently.
        pasted_diff = str(kwargs.get("diff") or "").strip()
        repo_path = str(kwargs.get("repo_path") or "").strip()
        diff_text = pasted_diff
        if not diff_text:
            git_result = await GIT_READ_TOOL.call(
                context, subcommand="diff", repo_path=repo_path or None
            )
            if isinstance(
                git_result, str
            ) and not git_result.strip().lower().startswith("error:"):
                diff_text = git_result
        if len(diff_text) > _MAX_PASTED_DIFF_CHARS:
            diff_text = diff_text[:_MAX_PASTED_DIFF_CHARS] + "\n...[truncated]"
        if not diff_text:
            return (
                "error: deep_review needs a reachable git repo (workspace root or repo_path) "
                "or a pasted 'diff' to review. Nothing to audit."
            )

        shared_task = (
            f"{task}\n\nTarget repo_path for the read-only git tool (if needed): "
            f"{repo_path or '(workspace root)'}\n\n"
            "Parent-provided diff to review (review exactly this diff):\n```diff\n"
            f"{diff_text}\n```"
        )

        provider_id = policy["provider_id"] or await ctx.get_current_chat_provider_id(
            event.unified_msg_origin
        )
        semaphore = asyncio.Semaphore(_MAX_PARALLEL_REVIEWERS)
        reviewer_max_steps = policy["max_steps"]
        tool_timeout = policy["timeout"]

        async def run_reviewer(name: str, system_prompt: str) -> tuple[str, str]:
            async with semaphore:
                body = await self._run_agent(
                    ctx,
                    event,
                    provider_id,
                    system_prompt=system_prompt,
                    prompt=f"{shared_task}\n\nReviewer focus: {name}.",
                    tools=toolset,
                    max_steps=reviewer_max_steps,
                    tool_timeout=tool_timeout,
                )
            return name, body or "NO_ISSUES"

        reviewer_results = await asyncio.gather(
            *(run_reviewer(name, prompt) for name, prompt in _DEEP_REVIEW_SPECIALISTS)
        )

        if not any(
            body.strip() and body.strip() != "NO_ISSUES" for _, body in reviewer_results
        ):
            return "error: all specialist reviewers failed to produce findings."

        reports_block = "\n\n".join(
            f"## Reviewer: {name}\n{body}" for name, body in reviewer_results
        )
        # aggregator_max_steps: explicit per-subagent override > builtin (_MAX_AGGREGATOR_STEPS).
        cfg = _plugin_config()
        subagents = (
            cfg.get("subagents") if isinstance(cfg.get("subagents"), dict) else {}
        )
        over = (
            subagents.get("deep_review")
            if isinstance(subagents.get("deep_review"), dict)
            else {}
        )
        agg_raw = over.get("aggregator_max_steps") if isinstance(over, dict) else None
        fallback_agg = int(_BUILTIN_POLICY["deep_review"]["aggregator_max_steps"])
        if isinstance(agg_raw, int) and agg_raw > 0:
            aggregator_max_steps = _clamp_int(agg_raw, 1, 50, fallback_agg)
        else:
            aggregator_max_steps = fallback_agg

        final_text = await self._run_agent(
            ctx,
            event,
            provider_id,
            system_prompt=_DEEP_REVIEW_AGGREGATOR_PROMPT,
            prompt=f"{shared_task}\n\nSpecialist reports to merge:\n\n{reports_block}",
            tools=None,
            max_steps=aggregator_max_steps,
            tool_timeout=tool_timeout,
        )
        return final_text or "error: the aggregator returned no final review."


DEEP_REVIEW_TOOL = DeepReviewTool()


# --------------------------------------------------------------------------- #
# Built-in policy baseline.
# --------------------------------------------------------------------------- #
# Mirrors the per-subagent defaults that were hardcoded on the dataclass
# fields before this plugin became configurable. Used as the *fallback* when
# the user's _conf_schema.json omits a field. Keep in sync with the dataclass
# defaults on ExploreTool / ResearchTool / ReviewTool / SecurityReviewTool /
# DeepReviewTool above - any drift here is a regression.
_BUILTIN_POLICY: dict[str, dict[str, Any]] = {
    "explore": {
        "max_steps": 12,
        "allowed_tools": _CODE_READ_TOOLS,
        "extra_tools": (GIT_READ_TOOL,),
        "discover_web": False,
    },
    "research": {
        "max_steps": 12,
        "allowed_tools": (*_CODE_READ_TOOLS, *_WEB_TOOLS),
        "extra_tools": (GIT_READ_TOOL,),
        "discover_web": True,
    },
    "review": {
        "max_steps": 8,
        "allowed_tools": _CODE_READ_TOOLS,
        "extra_tools": (GIT_READ_TOOL,),
        "discover_web": False,
    },
    "security_review": {
        "max_steps": 8,
        "allowed_tools": _CODE_READ_TOOLS,
        "extra_tools": (GIT_READ_TOOL,),
        "discover_web": False,
    },
    "deep_review": {
        "max_steps": _MAX_REVIEWER_STEPS,
        "aggregator_max_steps": _MAX_AGGREGATOR_STEPS,
        "allowed_tools": _CODE_READ_TOOLS,
        "extra_tools": (GIT_READ_TOOL,),
        "discover_web": False,
    },
}


# --------------------------------------------------------------------------- #
# Plugin entry.
# --------------------------------------------------------------------------- #
# NOTE: The @register decorator is deprecated. AstrBot auto-detects classes
# that inherit from Star; identity/description come from metadata.yaml.
class ReasonixSubagentsPlugin(Star):
    """Registers the Reasonix subagent delegation tools (incl. deep_review) on the main LLM."""

    _TOOL_REGISTRY: tuple[tuple[str, FunctionTool], ...] = (
        ("explore", EXPLORE_TOOL),
        ("research", RESEARCH_TOOL),
        ("review", REVIEW_TOOL),
        ("security_review", SECURITY_REVIEW_TOOL),
        ("deep_review", DEEP_REVIEW_TOOL),
    )

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config or {}
        # Register so module-level tool instances can read the latest config
        # via ``_plugin_config()``. Always replace the list with just this
        # instance so plugin reloads (where AstrBot rebuilds the plugin
        # object and overwrites self.config) take effect immediately and no
        # stale instance keeps serving the old config.
        _PLUGIN_REF[:] = [self]
        try:
            enabled_tools = [
                tool for name, tool in self._TOOL_REGISTRY if _subagent_enabled(name)
            ]
            disabled = [
                name
                for name, _tool in self._TOOL_REGISTRY
                if not _subagent_enabled(name)
            ]
            self.context.add_llm_tools(*enabled_tools)
            names = ", ".join(t.name for t in enabled_tools)
            if disabled:
                self.logger.info(
                    "Registered Reasonix subagent tools: %s (disabled by config: %s).",
                    names,
                    ", ".join(disabled),
                )
            else:
                self.logger.info("Registered Reasonix subagent tools: %s.", names)
        except Exception:  # noqa: BLE001
            self.logger.exception("Failed to register Reasonix subagent tools.")

    @filter.command(
        "reasonix_subagents", alias={"reasonix-subagents", "reasonix子代理"}
    )
    async def reasonix_subagents_status(self, event: AstrMessageEvent):
        """Show the registered Reasonix subagent tools and their current policy."""
        lines = [
            "Reasonix 子代理（agent-as-tool）：",
            "- explore: 只读代码库调查",
            "- research: 代码 + 网页研究",
            "- review: 只读代码评审（git diff）",
            "- security_review: 只读安全评审",
            "- deep_review: 并行多专家评审 + 置信度门禁（更慢更细）",
            "",
            "当前生效策略：",
        ]
        for name, _tool in self._TOOL_REGISTRY:
            if not _subagent_enabled(name):
                lines.append(f"- {name}: ❌ 已禁用 (subagents.{name}.enabled=false)")
                continue
            tool_obj = dict(self._TOOL_REGISTRY)[name]
            try:
                p = tool_obj._policy()  # noqa: SLF001 - same module, intentional
            except Exception:  # noqa: BLE001
                p = {}
            allowed = p.get("allowed_tools", ())
            steps = p.get("max_steps", "?")
            timeout = p.get("timeout", "?")
            discover = p.get("discover_web", False)
            provider = p.get("provider_id") or "(沿用当前会话)"
            allowed_str = ", ".join(allowed) if allowed else "(空)"
            lines.append(
                f"- {name}: max_steps={steps}, timeout={timeout}s, "
                f"discover_web={discover}, provider={provider}"
            )
            lines.append(f"    白名单: {allowed_str}")
        lines.append("")
        lines.append(
            "依赖：代码读取需启用 Computer Use；research 需配置 web 搜索；"
            "review/security_review 需把 git 仓库放进会话 workspace。"
        )
        lines.append(
            "直接对主 LLM 说『explore 一下 …』或『review 当前改动』即可触发委派。"
        )
        yield event.plain_result("\n".join(lines))
