"""Agent specs and policy resolution.

An :class:`AgentSpec` is the single source of built-in defaults for one
subagent (replacing the old duplicated dataclass fields + ``_BUILTIN_POLICY``).
:func:`resolve_policy` merges user config over the spec exactly once per call.

This module deliberately imports nothing from AstrBot so the whole resolution
chain can be unit-tested standalone.
"""

from __future__ import annotations

from dataclasses import dataclass

from reasonix import constants as C
from reasonix.config import (
    advanced_override,
    as_str_list,
    clamp_int,
    defaults_section,
    subagent_override,
)
from reasonix.prompts import (
    DEEP_REVIEW_AGGREGATOR_PROMPT,
    EXPLORE_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    SECURITY_REVIEW_SYSTEM_PROMPT,
)

GIT_TOOL_NAME = "reasonix_git_read"

# deep_review 整体墙钟预算（秒），独立于单次工具调用 timeout。
DEFAULT_DEEP_REVIEW_TIMEOUT = 600
DEFAULT_AGGREGATOR_STEPS = 4
DEFAULT_REVIEWER_STEPS = 8


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    task_hint: str
    system_prompt: str
    default_max_steps: int
    allowed_tools: tuple[str, ...]
    discover_web: bool
    review_params: bool = False
    extra_tool_names: tuple[str, ...] = (GIT_TOOL_NAME,)
    aggregator_max_steps: int | None = None
    overall_timeout: int | None = None


@dataclass(frozen=True)
class Policy:
    allowed_tools: tuple[str, ...]
    extra_tool_names: tuple[str, ...]
    max_steps: int
    timeout: int
    discover_web: bool
    provider_id: str
    aggregator_max_steps: int | None = None
    overall_timeout: int | None = None


_EXPLORE_DESC = (
    "Run a read-only codebase investigation in an isolated sub-agent (forked context). Use "
    "for broad survey questions across many files - 'find all places that X', 'how does Y "
    "work across the project', 'audit Z'. The sub-agent reads code with its own read-only "
    "tools (its reads/reasoning never enter your context) and returns one distilled answer "
    "with file:line citations. Do NOT use for making edits."
)
_RESEARCH_DESC = (
    "Run a read-only research sub-agent that combines local code reading with web "
    "search/extract tools. Use when the answer needs both an external reference and local "
    "verification - 'is X supported by lib Y', 'compare our impl against the spec'. Returns "
    "one synthesis citing code (file:line) and web (URL)."
)
_REVIEW_DESC = (
    "Run a read-only code-review sub-agent over the pending changes of a git repository "
    "inside the session workspace (or a diff you can point it to). Flags correctness, "
    "security, missing tests and hidden behavior changes; returns structured verdict / "
    "blocking_findings / non_blocking / required_changes with file:line. Use before shipping "
    "a PR-shaped change or after finishing a multi-step edit."
)
_SECURITY_DESC = (
    "Run a read-only security-review sub-agent over the current diff of a git repository "
    "inside the session workspace. Flags injection / authz / secrets / deserialization / "
    "path-traversal / crypto issues, severity-tagged (CRITICAL/HIGH/MEDIUM). Use when "
    "shipping changes that touch auth, input parsing, file IO, or external requests."
)
_DEEP_DESC = (
    "Run a deeper read-only code review: several specialist reviewers (correctness, "
    "guidelines, silent failures, tests, comments/types) audit the same diff in parallel, "
    "then a merge step filters false positives by confidence (>=80) and returns one "
    "structured report (verdict / blocking_findings / non_blocking / required_changes). "
    "Slower and more thorough than `review` - use for large or risky changes, or when a "
    "first review left many uncertain findings. Read-only; never edits."
)

_EXPLORE_HINT = (
    "Concrete investigation question. The sub-agent has none of your context - write a "
    "self-contained task naming the symbol / pattern / behavior to survey."
)
_RESEARCH_HINT = (
    "Concrete research question. The sub-agent has none of your context - name the "
    "external thing to look up and the local code to compare against."
)
_REVIEW_HINT = (
    "What to focus the review on (e.g. 'focus on the auth changes' or 'general'). "
    "Provide 'diff' to review a pasted diff, or 'repo_path' to point at a git repo "
    "inside the workspace. If neither is given the sub-agent reads the workspace "
    "repo's pending changes itself with read-only git."
)
_SECURITY_HINT = (
    "Optional scope hint (e.g. 'focus on token handling in internal/auth/') or 'full' "
    "for everything in the diff. Provide 'diff' to review a pasted diff, or 'repo_path' "
    "to point at a git repo inside the workspace. If neither is given the sub-agent "
    "reads the workspace repo's pending changes itself with read-only git."
)
_DEEP_HINT = (
    "What to focus on, plus the same optional 'diff' / 'repo_path' carriers as review. "
    "If neither a diff nor a reachable git repo is given the tool fails fast and asks "
    "for one."
)

SPECS: dict[str, AgentSpec] = {
    "explore": AgentSpec(
        name="explore",
        description=_EXPLORE_DESC,
        task_hint=_EXPLORE_HINT,
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        default_max_steps=12,
        allowed_tools=C.CODE_READ_TOOLS,
        discover_web=False,
    ),
    "research": AgentSpec(
        name="research",
        description=_RESEARCH_DESC,
        task_hint=_RESEARCH_HINT,
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        default_max_steps=12,
        allowed_tools=(*C.CODE_READ_TOOLS, *C.WEB_TOOLS),
        discover_web=True,
    ),
    "review": AgentSpec(
        name="review",
        description=_REVIEW_DESC,
        task_hint=_REVIEW_HINT,
        system_prompt=REVIEW_SYSTEM_PROMPT,
        default_max_steps=8,
        allowed_tools=C.CODE_READ_TOOLS,
        discover_web=False,
        review_params=True,
    ),
    "security_review": AgentSpec(
        name="security_review",
        description=_SECURITY_DESC,
        task_hint=_SECURITY_HINT,
        system_prompt=SECURITY_REVIEW_SYSTEM_PROMPT,
        default_max_steps=8,
        allowed_tools=C.CODE_READ_TOOLS,
        discover_web=False,
        review_params=True,
    ),
    "deep_review": AgentSpec(
        name="deep_review",
        description=_DEEP_DESC,
        task_hint=_DEEP_HINT,
        # system_prompt unused by DeepReviewTool but kept for spec completeness.
        system_prompt=DEEP_REVIEW_AGGREGATOR_PROMPT,
        default_max_steps=DEFAULT_REVIEWER_STEPS,
        allowed_tools=C.CODE_READ_TOOLS,
        discover_web=False,
        review_params=True,
        aggregator_max_steps=DEFAULT_AGGREGATOR_STEPS,
        overall_timeout=DEFAULT_DEEP_REVIEW_TIMEOUT,
    ),
}

SPEC_ORDER: tuple[str, ...] = (
    "explore",
    "research",
    "review",
    "security_review",
    "deep_review",
)


def _rewrite_builtin_web(baseline: tuple[str, ...], cfg: dict) -> tuple[str, ...]:
    """Replace built-in web tool names in baseline with advanced override."""
    builtin_web = set(C.WEB_TOOLS)
    if not any(name in builtin_web for name in baseline):
        return baseline
    web_override = advanced_override(cfg, "web_tool_names", C.WEB_TOOLS)
    out: list[str] = []
    for name in baseline:
        if name in builtin_web:
            out.extend(n for n in web_override if n not in out)
        elif name not in out:
            out.append(name)
    return tuple(out)


def resolve_policy(cfg: dict, spec: AgentSpec) -> Policy:
    """Resolve effective policy for ``spec`` from ``cfg``."""
    defaults = defaults_section(cfg)
    over = subagent_override(cfg, spec.name)

    allowed_over = as_str_list(over.get("allowed_tools"))
    baseline = allowed_over if allowed_over else tuple(spec.allowed_tools)
    baseline = _rewrite_builtin_web(baseline, cfg)

    allowed = list(baseline)
    for t in as_str_list(defaults.get("extra_allowed")):
        if t not in allowed:
            allowed.append(t)

    banned = set(as_str_list(defaults.get("excluded_tools"))) | set(
        as_str_list(over.get("excluded_tools"))
    )
    allowed_tuple = tuple(t for t in allowed if t not in banned)
    extra_tuple = tuple(t for t in spec.extra_tool_names if t not in banned)

    over_steps = over.get("max_steps")
    default_steps = defaults.get("max_steps")
    if (
        isinstance(over_steps, int)
        and not isinstance(over_steps, bool)
        and over_steps > 0
    ):
        steps = clamp_int(over_steps, 1, 50, spec.default_max_steps)
    elif (
        isinstance(default_steps, int)
        and not isinstance(default_steps, bool)
        and default_steps > 0
    ):
        steps = clamp_int(default_steps, 1, 50, spec.default_max_steps)
    else:
        steps = spec.default_max_steps

    timeout = clamp_int(defaults.get("tool_timeout"), 5, 600, 120)

    discover = over.get("discover_web")
    discover_web = bool(discover) if isinstance(discover, bool) else spec.discover_web

    provider_id = str(over.get("provider_id") or "").strip()

    agg_steps: int | None = None
    if spec.aggregator_max_steps is not None:
        agg_raw = over.get("aggregator_max_steps")
        if isinstance(agg_raw, int) and not isinstance(agg_raw, bool) and agg_raw > 0:
            agg_steps = clamp_int(agg_raw, 1, 50, spec.aggregator_max_steps)
        else:
            agg_steps = spec.aggregator_max_steps

    overall: int | None = spec.overall_timeout
    over_overall = over.get("overall_timeout")
    if (
        isinstance(over_overall, int)
        and not isinstance(over_overall, bool)
        and over_overall > 0
    ):
        overall = clamp_int(over_overall, 30, 1800, spec.overall_timeout or 600)

    return Policy(
        allowed_tools=allowed_tuple,
        extra_tool_names=extra_tuple,
        max_steps=steps,
        timeout=timeout,
        discover_web=discover_web,
        provider_id=provider_id,
        aggregator_max_steps=agg_steps,
        overall_timeout=overall,
    )


def effective_bans(cfg: dict, name: str) -> set[str]:
    """Union of global + per-subagent excluded tools (used by deep_review)."""
    defaults = defaults_section(cfg)
    over = subagent_override(cfg, name)
    return set(as_str_list(defaults.get("excluded_tools"))) | set(
        as_str_list(over.get("excluded_tools"))
    )
