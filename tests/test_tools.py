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

from astrbot_plugin_reasonix_subagents.reasonix.config import ConfigHolder
from astrbot_plugin_reasonix_subagents.reasonix.policy import SPECS
from astrbot_plugin_reasonix_subagents.reasonix.tools.agents import build_tools
from astrbot_plugin_reasonix_subagents.reasonix.tools.base import (
    ReasonixSubagentTool,
    build_toolset,
)
from astrbot_plugin_reasonix_subagents.reasonix.tools.deep_review import DeepReviewTool
from astrbot_plugin_reasonix_subagents.reasonix.tools.git_read import (
    ReasonixGitReadTool,
    execute as git_execute,
)


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
    from astrbot_plugin_reasonix_subagents.reasonix.tools import git_read

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

    from astrbot_plugin_reasonix_subagents.reasonix.policy import resolve_policy

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

    from astrbot_plugin_reasonix_subagents.reasonix.tools import deep_review as dr_mod

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

    from astrbot_plugin_reasonix_subagents.reasonix.tools import git_read

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

    from astrbot_plugin_reasonix_subagents.reasonix.tools import git_read

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


# --- Review 1 guard: description-side-effect blocks web-marker names ---


def test_discovery_rejects_named_web_tool_with_side_effect_description():
    """A tool whose NAME matches a web marker but whose DESCRIPTION mentions
    side effects must NOT be exposed to a read-only subagent.

    Without the fix the name match returned True and the description was
    never inspected.
    """
    from dataclasses import dataclass

    from astrbot_plugin_reasonix_subagents.reasonix.discovery import (
        is_web_readonly_tool,
    )

    @dataclass
    class FakeTool:
        name: str
        description: str
        active: bool = True

    tool = FakeTool(
        name="web_search_danger",
        description="Search the web and write results back to disk on success.",
    )
    assert is_web_readonly_tool(tool, {}) is False


# --- Review 2 guard: excluded_tools filters auto-discovered web tools ---


def test_build_toolset_excluded_filters_auto_discovered():
    """A blacklisted tool that ALSO matches web discovery must not be added."""
    from dataclasses import dataclass

    from astrbot_plugin_reasonix_subagents.reasonix.policy import resolve_policy
    from astrbot_plugin_reasonix_subagents.reasonix.tools.base import build_toolset
    from astrbot_plugin_reasonix_subagents.reasonix.tools.git_read import (
        ReasonixGitReadTool,
    )

    @dataclass
    class FakeTool:
        name: str
        description: str = ""
        active: bool = True

    git_tool = ReasonixGitReadTool()
    dangerous = FakeTool(
        name="searxng_web_search",
        description="Search the web",  # matches web marker
    )

    class Mgr:
        def __init__(self):
            self.func_list = [dangerous]

        def get_func(self, name):
            for t in self.func_list:
                if t.name == name:
                    return t
            return None

    cfg = {
        "defaults": {"excluded_tools": ["searxng_web_search"]},
        "subagents": {"research": {"discover_web": True}},
    }
    pol = resolve_policy(cfg, SPECS["research"])
    assert "searxng_web_search" in pol.banned_tool_names

    ts = build_toolset(Mgr(), pol, SPECS["research"], git_tool, cfg)
    added_names = [getattr(t, "name", "") for t in ts.tools]
    assert "searxng_web_search" not in added_names


# --- Review 3 guard: empty {} plugin.config is the current config ---


def test_plugin_empty_config_not_fallback_to_initial(monkeypatch):
    """Resetting plugin.config to {} must surface immediately, not silently
    fall back to the initial non-empty config."""
    from astrbot_plugin_reasonix_subagents.main import ReasonixSubagentsPlugin

    captured_cfg: list[dict] = []

    class FakeHolder:
        def __init__(self, provider):
            self._get = provider

        def get(self):
            return self._get()

    class FakeTool:
        name = "explore"
        description = ""

    class FakeCtx:
        def add_llm_tools(self, *_a, **_k):
            return None

    # Stub out build_tools + ConfigHolder so we only exercise the closure.
    monkeypatch.setattr(
        "astrbot_plugin_reasonix_subagents.main.ConfigHolder", FakeHolder
    )
    monkeypatch.setattr(
        "astrbot_plugin_reasonix_subagents.main.build_tools",
        lambda _holder: (None, [FakeTool()]),
    )

    initial = {"defaults": {"max_steps": 99}}
    plugin = ReasonixSubagentsPlugin(FakeCtx(), initial)
    # Star sets self.config = initial in __init__; simulate a real reload
    # that resets to {} (e.g. user clicked "reset to defaults").
    plugin.config = {}
    captured_cfg.append(plugin.config_holder.get())
    assert captured_cfg[-1] == {}, (
        "empty plugin.config must not silently fall back to initial; "
        f"got {captured_cfg[-1]!r}"
    )


# --- Review 4 guard: empty diff snapshot is an error, not a silent pass ---


@pytest.mark.asyncio
async def test_deep_review_snapshot_empty_diff_is_error(monkeypatch):
    """When git diff produces no output (clean repo) and no pasted diff is
    given, deep_review must return an explicit error rather than passing an
    empty task to the reviewers."""
    from dataclasses import dataclass, field

    from astrbot_plugin_reasonix_subagents.reasonix.config import ConfigHolder
    from astrbot_plugin_reasonix_subagents.reasonix.tools.deep_review import (
        DeepReviewTool,
    )
    from astrbot_plugin_reasonix_subagents.reasonix.tools.git_read import (
        ReasonixGitReadTool,
    )

    git_tool = ReasonixGitReadTool()
    ch = ConfigHolder({})
    tool = DeepReviewTool(
        spec=SPECS["deep_review"], config_holder=ch, git_tool=git_tool
    )

    # Fake git_execute to return an "empty diff" success: only the command
    # prefix line, nothing else.
    async def fake_git(*_a, **_k):
        from astrbot_plugin_reasonix_subagents.reasonix.tools.git_read import GitResult

        return GitResult(True, "$ git --no-pager diff\n")

    monkeypatch.setattr(
        "astrbot_plugin_reasonix_subagents.reasonix.tools.deep_review.git_execute",
        fake_git,
    )

    # Provide a code-read tool so the reviewer toolset isn't short-circuited
    # by the empty-toolset check before reaching the snapshot path.
    @dataclass
    class ReadTool:
        name: str = "astrbot_file_read_tool"
        description: str = ""
        active: bool = True

    @dataclass
    class FakeMgr:
        func_list: list = field(default_factory=lambda: [ReadTool()])

        def get_func(self, name):
            for t in self.func_list:
                if t.name == name:
                    return t
            return None

    class _Event:
        unified_msg_origin = "test:1"

    class _Core:
        pass

    class _Inner:
        event = _Event()
        context = _Core()

    class Ctx:
        def get_llm_tool_manager(self):
            return FakeMgr()

        def get_current_chat_provider_id(self, origin):
            return "p_test"

    class AgentCtx:
        def __init__(self):
            self.context = Ctx()
            self.event = _Event()

    class CtxW:
        def __init__(self):
            self.context = AgentCtx()

    result = await tool.call(CtxW(), task="audit")
    assert isinstance(result, str)
    assert result.startswith("error:"), result
    assert "no changes" in result, result


# --- Review 5 guard: git_guard rejects -Dfoo / -dbar style mutation forms ---


def test_git_guard_rejects_glued_short_mutation_options():
    """Git parses '-Dfoo' as '-D foo' (delete branch foo). The validator
    must reject glued forms in addition to standalone short options."""
    from astrbot_plugin_reasonix_subagents.reasonix.git_guard import validate

    # Each subcommand's mutation options live in different lists; the test
    # only asserts the ones that actually apply to that subcommand.
    cases = [
        ("branch", ["-Dfoo", "-dbar", "-mmain", "-Mfeature", "-cfoo"]),
        ("tag", ["-dfoo", "-mmain", "-ffoo", "-sfoo"]),
    ]
    for sub, args in cases:
        for arg in args:
            _, error = validate(sub, [arg])
            assert error is not None, f"{sub} {arg} should be rejected"
            assert "read-only" in error or "forbidden" in error, (
                f"{sub} {arg}: unexpected error {error!r}"
            )


def test_git_guard_rejects_long_equals_mutation():
    """--delete=foo must be rejected as a delete-branch mutation."""
    from astrbot_plugin_reasonix_subagents.reasonix.git_guard import validate

    _, error = validate("branch", ["--delete=feature"])
    assert error is not None
