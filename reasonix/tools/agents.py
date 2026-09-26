"""Tool factory: build all tool instances after config is available."""

from __future__ import annotations

from reasonix.config import ConfigHolder
from reasonix.policy import SPECS, AgentSpec
from reasonix.tools.base import ReasonixSubagentTool
from reasonix.tools.deep_review import DeepReviewTool
from reasonix.tools.git_read import ReasonixGitReadTool

_STANDARD = ("explore", "research", "review", "security_review")


def build_tools(
    config_holder: ConfigHolder,
) -> tuple[ReasonixGitReadTool, list[ReasonixSubagentTool]]:
    """Create the git tool and all five subagent tools, bound to config."""
    git_tool = ReasonixGitReadTool()
    tools: list[ReasonixSubagentTool] = []
    for name in _STANDARD:
        spec: AgentSpec = SPECS[name]
        tools.append(
            ReasonixSubagentTool(
                spec=spec, config_holder=config_holder, git_tool=git_tool
            )
        )
    tools.append(
        DeepReviewTool(
            spec=SPECS["deep_review"],
            config_holder=config_holder,
            git_tool=git_tool,
        )
    )
    return git_tool, tools
