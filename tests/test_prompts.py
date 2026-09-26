"""Drift guard: sub-agent prompts must match the toolsets they are given.

The read-only sub-agents (explore / research) lost the shell tools in the
security hardening pass, but their system prompts kept instructing them to
use a "read-only shell tool" -- a dangling reference the test suite never
caught. These tests fail if a prompt starts referencing tools that are not
configured for the sub-agent.
"""

from reasonix.constants import CODE_READ_TOOLS

from reasonix.prompts.subagents import (
    EXPLORE_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
)


def test_readonly_prompts_do_not_reference_shell_tools():
    """The read-only toolset no longer contains shell tools; the prompts
    must not instruct the sub-agent to use one (no terminal access)."""
    assert "astrbot_execute_shell" not in CODE_READ_TOOLS
    assert "astrbot_shell_session" not in CODE_READ_TOOLS
    for prompt in (EXPLORE_SYSTEM_PROMPT, RESEARCH_SYSTEM_PROMPT):
        lowered = prompt.lower()
        assert "shell" not in lowered, (
            "read-only sub-agent prompt references shell tools that are not "
            "in CODE_READ_TOOLS"
        )


def test_explore_prompt_points_at_git_fallback_for_structure():
    """Without shell, structure discovery must route through the read-only
    git tool (ls-files) -- make sure the compensation is documented."""
    assert "read-only git tool" in EXPLORE_SYSTEM_PROMPT
    assert "ls-files" in EXPLORE_SYSTEM_PROMPT


def test_prompts_only_reference_tools_the_subagent_has():
    """Every tool the explore/research prompts name must exist in the
    read-only toolset (or be the git tool handed to them separately)."""
    allowed = set(CODE_READ_TOOLS) | {"reasonix_git_read"}
    for prompt in (EXPLORE_SYSTEM_PROMPT, RESEARCH_SYSTEM_PROMPT):
        for fragment in ("file-read", "file_read", "grep", "git"):
            if fragment in prompt.lower():
                matched = any(fragment.replace("-", "_") in t or fragment in t for t in allowed)
                assert matched, f"prompt references {fragment!r} but toolset lacks it: {sorted(allowed)}"
