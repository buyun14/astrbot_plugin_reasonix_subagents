"""Subagent-as-tool base class.

Runs an isolated read-only ``tool_loop_agent`` in a forked context and returns
only the final text. Policy is resolved exactly once per call.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext

from .. import tasks as task_builder
from ..config import ConfigHolder
from ..discovery import discover_web_tools
from ..policy import AgentSpec, Policy, resolve_policy
from ..runner import resolve_provider_id, run_agent


def _get_tool_manager(ctx: Any) -> Any:
    try:
        return ctx.get_llm_tool_manager()
    except Exception:  # noqa: BLE001
        return None


def build_toolset(
    tool_mgr: Any,
    policy: Policy,
    spec: AgentSpec,
    git_tool: FunctionTool,
    cfg: dict,
) -> ToolSet:
    """Build toolset from a resolved policy; log unknown/discovered tools."""
    toolset = ToolSet()
    unknown: list[str] = []

    def resolve(name: str) -> Any:
        if tool_mgr is None:
            return None
        try:
            return tool_mgr.get_func(name)
        except Exception:  # noqa: BLE001
            return None

    for name in policy.allowed_tools:
        tool = resolve(name)
        if tool is not None and bool(getattr(tool, "active", True)):
            toolset.add_tool(tool)
        else:
            unknown.append(name)

    for name in policy.extra_tool_names:
        if name == getattr(git_tool, "name", None):
            toolset.add_tool(git_tool)
        else:
            extra = resolve(name)
            if extra is not None and bool(getattr(extra, "active", True)):
                toolset.add_tool(extra)
            else:
                unknown.append(name)

    if policy.discover_web and tool_mgr is not None:
        discovered = discover_web_tools(getattr(tool_mgr, "func_list", ()), cfg)
        # The banned set must also apply to auto-discovered tools: a tool
        # blacklisted via excluded_tools would otherwise slip back in if it
        # happened to match the web discovery heuristic. Filter before adding.
        kept = [
            t
            for t in discovered
            if getattr(t, "name", "") not in policy.banned_tool_names
        ]
        banned_discovered = [
            t for t in discovered if getattr(t, "name", "") in policy.banned_tool_names
        ]
        for tool in kept:
            toolset.add_tool(tool)
        # Auditability: every auto-discovered tool is logged and named.
        if kept:
            logger.info(
                "[%s] auto-discovered read-only web tool(s): %s",
                spec.name,
                sorted(getattr(t, "name", "") for t in kept),
            )
        if banned_discovered:
            logger.info(
                "[%s] %d auto-discovered tool(s) excluded by banned set: %s",
                spec.name,
                len(banned_discovered),
                sorted(getattr(t, "name", "") for t in banned_discovered),
            )

    if unknown:
        available = sorted(
            getattr(t, "name", "")
            for t in getattr(tool_mgr, "func_list", ())
            if bool(getattr(t, "active", True))
        )
        logger.warning(
            "[%s] %d configured tool name(s) unavailable: %s. Active tools: %s",
            spec.name,
            len(unknown),
            unknown,
            available,
        )
    return toolset


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ReasonixSubagentTool(FunctionTool[AstrAgentContext]):
    """A subagent exposed as a tool: isolated read-only agent loop."""

    spec: AgentSpec = None  # type: ignore[assignment]
    config_holder: ConfigHolder = None  # type: ignore[assignment]
    git_tool: FunctionTool = None  # type: ignore[assignment]

    name: str = ""
    description: str = ""
    parameters: dict = Field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.spec is not None:
            self.name = self.spec.name
            self.description = self.spec.description
            schema_builder = (
                task_builder.review_parameters
                if self.spec.review_params
                else task_builder.task_parameters
            )
            self.parameters = schema_builder(self.spec.task_hint)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return (
                "error: this subagent needs a non-empty 'task' describing the question."
            )

        cfg = self.config_holder.get()
        policy = resolve_policy(cfg, self.spec)

        agent_context: AstrAgentContext = context.context
        ctx, event = agent_context.context, agent_context.event
        tool_mgr = _get_tool_manager(ctx)
        toolset = build_toolset(tool_mgr, policy, self.spec, self.git_tool, cfg)

        if toolset.empty():
            return (
                "error: none of this subagent's read-only tools are currently available. "
                f"Needed tool names (whichever apply): {', '.join(policy.allowed_tools)}. "
                "For code reading enable Computer Use (computer_use_runtime); for research "
                "configure a web-search provider."
            )

        pasted_diff = str(kwargs.get("diff") or "").strip()
        repo_path = str(kwargs.get("repo_path") or "").strip()
        prompt = task_builder.build_review_task(task, pasted_diff, repo_path)

        provider_id = await resolve_provider_id(ctx, event, policy.provider_id)
        text = await run_agent(
            ctx,
            event,
            provider_id=provider_id,
            prompt=prompt,
            system_prompt=self.spec.system_prompt,
            tools=toolset,
            max_steps=policy.max_steps,
            tool_timeout=policy.timeout,
        )
        return text or "error: the subagent returned no final text."
