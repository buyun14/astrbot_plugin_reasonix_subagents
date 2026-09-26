"""deep_review: parallel specialist reviewers + confidence-gated merge."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from reasonix import tasks as task_builder
from reasonix.constants import MAX_PARALLEL_REVIEWERS
from reasonix.policy import (
    GIT_TOOL_NAME,
    effective_bans,
    resolve_policy,
)
from reasonix.prompts import DEEP_REVIEW_AGGREGATOR_PROMPT, DEEP_REVIEW_SPECIALISTS
from reasonix.runner import resolve_provider_id, run_agent
from reasonix.tools.base import ReasonixSubagentTool, build_toolset
from reasonix.tools.git_read import execute as git_execute


def _strip_command_prefix(text: str) -> str:
    """Drop the leading ``$ git ...`` line added by the git tool formatter."""
    if text.startswith("$ "):
        return text.split("\n", 1)[1] if "\n" in text else ""
    return text


def reports_block(results: list[tuple[str, str]]) -> str:
    """Format specialist outputs as the aggregator's input block."""
    return "\n\n".join(f"## Reviewer: {n}\n{b}" for n, b in results)


async def _envelope(reviewer_phase, aggregator_phase):
    """Run reviewer + aggregator serially and return (results, aggregator_text).

    A single coroutine is needed so ``asyncio.wait_for`` can wrap both
    phases under one deadline. The reviewer phase must finish before the
    aggregator gets its input, so the two cannot run in parallel.
    """
    results = await reviewer_phase()
    final_text = await aggregator_phase(results)
    return results, final_text


@dataclass
class DeepReviewTool(ReasonixSubagentTool):
    """Several reviewers in parallel, then a confidence-gated merge step."""

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return "error: deep_review needs a non-empty 'task'."

        cfg = self.config_holder.get()
        policy = resolve_policy(cfg, self.spec)

        agent_context: AstrAgentContext = context.context
        ctx, event = agent_context.context, agent_context.event

        # Reviewers must not touch git themselves: build a toolset without the
        # git tool so the single shared snapshot is the authoritative input.
        reviewer_policy = dataclasses.replace(policy, extra_tool_names=())
        reviewer_toolset = build_toolset(
            self._tool_mgr(ctx), reviewer_policy, self.spec, self.git_tool, cfg
        )
        if reviewer_toolset.empty():
            return (
                "error: none of deep_review's read-only tools are available. "
                "Enable Computer Use (computer_use_runtime) for code reading."
            )

        diff_text = await self._snapshot(context, kwargs, cfg)
        if isinstance(diff_text, str) and diff_text.startswith("error:"):
            return diff_text

        shared_task = task_builder.build_snapshot_task(task, diff_text, "")
        provider_id = await resolve_provider_id(ctx, event, policy.provider_id)

        semaphore = asyncio.Semaphore(MAX_PARALLEL_REVIEWERS)

        async def run_reviewer(name: str, system_prompt: str) -> tuple[str, str]:
            async with semaphore:
                body = await run_agent(
                    ctx,
                    event,
                    provider_id=provider_id,
                    prompt=f"{shared_task}\n\nReviewer focus: {name}.",
                    system_prompt=system_prompt,
                    tools=reviewer_toolset,
                    max_steps=policy.max_steps,
                    tool_timeout=policy.timeout,
                )
            return name, body or "NO_ISSUES"

        async def reviewer_phase() -> list[tuple[str, str]]:
            return await asyncio.gather(
                *(run_reviewer(n, p) for n, p in DEEP_REVIEW_SPECIALISTS)
            )

        async def aggregator_phase(results: list[tuple[str, str]]) -> str:
            return await run_agent(
                ctx,
                event,
                provider_id=provider_id,
                prompt=f"{shared_task}\n\nSpecialist reports to merge:\n\n{reports_block(results)}",
                system_prompt=DEEP_REVIEW_AGGREGATOR_PROMPT,
                tools=None,
                max_steps=policy.aggregator_max_steps or 4,
                tool_timeout=policy.timeout,
            )

        # Wrap BOTH reviewer gather and aggregator in one asyncio.wait_for so
        # the documented overall_timeout (wall-clock budget for the entire
        # deep_review call) actually bounds the whole pipeline. The previous
        # version only covered the reviewer phase, leaving a slow aggregator
        # able to run unbounded beyond the user-set budget.
        try:
            results, final_text = await asyncio.wait_for(
                _envelope(reviewer_phase, aggregator_phase),
                timeout=policy.overall_timeout,
            )
        except asyncio.TimeoutError:
            return (
                "error: deep_review exceeded the overall budget "
                f"({policy.overall_timeout}s). Try a smaller diff or raise "
                "subagents.deep_review.overall_timeout."
            )

        meaningful = [
            (n, b) for n, b in results if b.strip() and b.strip() != "NO_ISSUES"
        ]
        # All reviewers converged on "no issues" -> that is a clean verdict,
        # not an error.
        if not meaningful:
            return "LGTM - all five specialist reviewers found no reportable issues."

        # Degrade gracefully: if the aggregator produced nothing, hand back the
        # raw specialist reports rather than failing the whole run.
        return final_text or (
            "Aggregator returned no text; raw specialist reports:\n\n"
            + reports_block(results)
        )

    @staticmethod
    def _tool_mgr(ctx: Any) -> Any:
        try:
            return ctx.get_llm_tool_manager()
        except Exception:  # noqa: BLE001
            return None

    async def _snapshot(self, context: Any, kwargs: dict, cfg: dict) -> str:
        pasted = str(kwargs.get("diff") or "").strip()
        repo_path = str(kwargs.get("repo_path") or "").strip() or None
        if pasted:
            return pasted

        # Honor excluded_tools: do not silently use the blacklisted git tool.
        if GIT_TOOL_NAME in effective_bans(cfg, self.spec.name):
            return (
                "error: reasonix_git_read is in excluded_tools for deep_review, "
                "so the internal diff snapshot is disabled. Pass a 'diff' "
                "argument (the full diff text)."
            )

        result = await git_execute("diff", repo_path, None, context)
        if not result.ok:
            if "no output" in result.text:
                return (
                    "error: 'git diff' produced no changes - nothing to audit. "
                    "Stage/make changes, point repo_path at the right repo, or "
                    "pass a 'diff' explicitly."
                )
            return result.text
        return _strip_command_prefix(result.text)
