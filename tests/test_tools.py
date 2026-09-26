"""End-to-end smoke tests for the tool layer.

The pure-function unit tests live in test_policy.py / test_discovery.py /
test_git_guard.py / test_tasks.py. These tests exercise the tool classes
themselves with minimal fakes so that wiring regressions (missing imports,
pydantic config errors, dataclass field failures) get caught before runtime.

In particular, this module guards against:
- B1 regression: 'from reasonix import git_guard' missing in git_read.py.
  Without it, ReasonixGitReadTool.call -> execute() -> git_guard.validate()
  raises NameError on every invocation.
- B2 regression: ConfigHolder field on ReasonixSubagentTool without
  arbitrary_types_allowed causes PydanticSchemaGenerationError at class
  definition time, breaking every ReasonixSubagentTool subclass.
"""

from __future__ import annotations


import pytest

from reasonix.config import ConfigHolder
from reasonix.policy import SPECS
from reasonix.tools.agents import build_tools
from reasonix.tools.base import ReasonixSubagentTool, build_toolset
from reasonix.tools.deep_review import DeepReviewTool
from reasonix.tools.git_read import ReasonixGitReadTool, execute as git_execute


# --- B2 guard: tool classes must instantiate without pydantic errors ---


def test_subagent_tool_class_can_be_constructed():
    """B2 guard: ConfigHolder field would crash without arbitrary_types_allowed."""
    git = ReasonixGitReadTool()
    ch = ConfigHolder({})
    t = ReasonixSubagentTool(spec=SPECS["explore"], config_holder=ch, git_tool=git)
    assert t.name == "explore"
    assert t.description.startswith("Run a read-only codebase investigation")


def test_deep_review_tool_class_can_be_constructed():
    ch = ConfigHolder({})
    git = ReasonixGitReadTool()
    t = DeepReviewTool(spec=SPECS["deep_review"], config_holder=ch, git_tool=git)
    assert t.name == "deep_review"


def test_build_tools_returns_all_five_with_git():
    ch = ConfigHolder({})
    git, tools = build_tools(ch)
    assert isinstance(git, ReasonixGitReadTool)
    assert [t.name for t in tools] == [
        "explore",
        "research",
        "review",
        "security_review",
        "deep_review",
    ]


# --- B1 guard: git_guard module is actually bound in the tools namespace ---


def test_git_guard_module_is_imported():
    """B1 guard: without 'from reasonix import git_guard' the validate()
    call inside execute() raises NameError at runtime. This static check
    catches that refactor regression immediately."""
    from reasonix.tools import git_read

    assert hasattr(git_read, "git_guard"), (
        "reasonix.tools.git_read no longer has the 'git_guard' module "
        "reference; execute() will NameError at runtime."
    )
    assert hasattr(git_read.git_guard, "validate")


@pytest.mark.asyncio
async def test_git_execute_unknown_subcommand_returns_error_not_name_error():
    """B1 guard: hits the validate() path that previously NameError'd.

    With an unsupported subcommand, git_guard.validate returns an error
    tuple and execute() never reaches git. If the import is missing, this
    test raises NameError instead.
    """

    class Ctx:
        async def workspace_root_for_context(self):
            raise RuntimeError("should not be reached")

    result = await git_execute("rebase", None, [], Ctx())
    assert result.ok is False
    assert "unsupported" in result.text


# --- resolve_policy + excluded_tools plumbing through tools ---


def test_build_toolset_filters_banned_extras():
    """Bans applied to extra_tool_names too (previous review fix)."""
    git = ReasonixGitReadTool()
    cfg = {"defaults": {"excluded_tools": ["reasonix_git_read"]}}

    class Mgr:
        @property
        def func_list(self):
            return []

        def get_func(self, name):
            return None

    from reasonix.policy import resolve_policy

    pol = resolve_policy(cfg, SPECS["explore"])
    ts = build_toolset(Mgr(), pol, SPECS["explore"], git, cfg)
    # Git tool was banned -> not added to toolset.
    added_names = [getattr(t, "name", "") for t in ts.tools]
    assert "reasonix_git_read" not in added_names
