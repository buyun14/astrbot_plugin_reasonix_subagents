"""Single entry point for running isolated ``tool_loop_agent`` loops."""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.core.agent.tool import ToolSet


async def resolve_provider_id(ctx: Any, event: Any, override: str) -> str:
    if override:
        return override
    return await ctx.get_current_chat_provider_id(event.unified_msg_origin)


async def run_agent(
    ctx: Any,
    event: Any,
    *,
    provider_id: str,
    prompt: str,
    system_prompt: str,
    tools: ToolSet | None,
    max_steps: int,
    tool_timeout: int,
) -> str:
    """Run one isolated agent loop and return its final text."""
    resp = await ctx.tool_loop_agent(
        event=event,
        chat_provider_id=provider_id,
        prompt=prompt,
        system_prompt=system_prompt,
        tools=tools,
        max_steps=max_steps,
        tool_call_timeout=tool_timeout,
        stream=False,
    )
    return (getattr(resp, "completion_text", None) or "").strip()
