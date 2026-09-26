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
- R1 regression: deep_review's overall_timeout only covered the reviewer
  phase; a slow aggregator could run unbounded beyond the budget.
- R2 regression: execute() reported ok=True regardless of proc.returncode,
  so failed git invocations looked like valid snapshots.
- R3 regression: ConfigHolder snapshotted config at __init__ and never
  refreshed, so any later mutation/replacement of plugin.config stayed
  invisible to the running tools.
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


# --- R1 guard: overall_timeout covers the whole deep_review pipeline ---


def test_deep_review_timeout_covers_aggregator():
    """The envelope must bound reviewers + aggregator under a single deadline.

    A reviewer that finishes quickly is paired with an aggregator that
    sleeps past the budget. With the fix the call raises TimeoutError;
    without it the aggregator would have run unbounded.

    Uses asyncio.wait_for directly with a small timeout rather than going
    through resolve_policy (which clamps overall_timeout to [30, 1800]).
    """
    import asyncio

    from reasonix.tools import deep_review as dr_mod

    overall_budget = 0.1

    async def fake_reviewer():
        return ("r1", "ok")

    async def fake_aggregator():
        await asyncio.sleep(1.0)  # blows past the 0.1s budget
        return "should never see this"

    async def reviewer_phase():
        return await asyncio.gather(
            fake_reviewer(),
            fake_reviewer(),
            fake_reviewer(),
        )

    async def aggregator_phase(results):
        return await fake_aggregator()

    async def run_envelope():
        return await asyncio.wait_for(
            dr_mod._envelope(reviewer_phase, aggregator_phase),
            timeout=overall_budget,
        )

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_envelope())


# --- R2 guard: git execute() reflects proc.returncode ---


@pytest.mark.asyncio
async def test_git_execute_propagates_return_code(monkeypatch):
    """execute() must mark result.ok=False when git exits non-zero.

    Without the fix, GitResult(True, body) was always returned regardless
    of proc.returncode, so callers treated 'fatal: unknown revision' as a
    valid diff snapshot.
    """
    import asyncio
    from dataclasses import dataclass

    from reasonix.tools import git_read

    @dataclass
    class FakeProc:
        returncode: int = 128  # typical git "fatal error" exit code
        _out: bytes = b""
        _err: bytes = b"fatal: unknown revision 'HEAD~99'\n"

        async def communicate(self):
            return self._out, self._err

    class FakeSubprocess:
        def __init__(self, proc):
            self._proc = proc

        async def communicate(self):
            return await self._proc.communicate()

        @property
        def returncode(self) -> int:
            return self._proc.returncode

    async def fake_create(*_args, **_kwargs):
        return FakeSubprocess(FakeProc())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    class _Event:
        unified_msg_origin = "test:1"

    class _Core:
        # No _db attribute -> workspace_root_for_context falls back to
        # workspace_root(umo), which returns a real path on disk.
        pass

    class _Inner:
        event = _Event()
        context = _Core()

    class FakeCtx:
        context = _Inner()

    # Use a valid read-only subcommand so we reach the post-execute code path.
    result = await git_read.execute("log", None, ["-1"], FakeCtx())
    assert result.ok is False
    assert "fatal" in result.text


@pytest.mark.asyncio
async def test_git_execute_zero_exit_returns_ok(monkeypatch):
    """Counterpart: success exit must still surface as ok=True."""
    from dataclasses import dataclass

    from reasonix.tools import git_read

    @dataclass
    class FakeProc:
        returncode: int = 0
        _out: bytes = b"some output\n"
        _err: bytes = b""

        async def communicate(self):
            return self._out, self._err

    class FakeSubprocess:
        def __init__(self, proc):
            self._proc = proc

        async def communicate(self):
            return await self._proc.communicate()

        @property
        def returncode(self) -> int:
            return self._proc.returncode

    async def fake_create(*_args, **_kwargs):
        return FakeSubprocess(FakeProc())

    import asyncio

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    class _Event:
        unified_msg_origin = "test:1"

    class _Core:
        pass

    class _Inner:
        event = _Event()
        context = _Core()

    class FakeCtx:
        context = _Inner()

    result = await git_read.execute("log", None, ["-1"], FakeCtx())
    assert result.ok is True
    assert "some output" in result.text


# --- R3 guard: ConfigHolder provider mode reflects later config changes ---


def test_config_holder_provider_reflects_live_updates():
    """The plugin must observe later changes to self.config."""
    holder_dict = {"value": 1}
    holder = ConfigHolder(lambda: holder_dict)
    assert holder.get() == {"value": 1}

    # Simulate AstrBot (or a test) replacing plugin.config in place.
    holder_dict["value"] = 2
    holder_dict["new_key"] = "added"
    assert holder.get() == {"value": 2, "new_key": "added"}

    # And replace the dict entirely.
    holder_dict.clear()
    holder_dict.update({"replaced": True})
    assert holder.get() == {"replaced": True}


def test_config_holder_snapshot_mode_still_works():
    """Backward-compat: existing dict-based construction must keep working."""
    h = ConfigHolder({"a": 1})
    assert h.get() == {"a": 1}
    h.update({"a": 2})
    assert h.get() == {"a": 2}


def test_config_holder_provider_update_is_noop():
    """Provider mode owns the truth; update() must not overwrite it."""
    provider_value = {"from": "provider"}
    h = ConfigHolder(lambda: provider_value)
    h.update({"from": "snapshot"})
    # The update must NOT switch the holder into snapshot mode.
    assert h.get() == {"from": "provider"}


def test_config_holder_provider_non_dict_returns_empty():
    """Defensive: provider returning non-dict is treated as empty config."""
    h = ConfigHolder(lambda: None)
    assert h.get() == {}
    h2 = ConfigHolder(lambda: "not a dict")
    assert h2.get() == {}
