"""Read-only git tool, hardened per the git_guard policy."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.tools.computer_tools.util import workspace_root_for_context

from ..constants import MAX_GIT_OUTPUT, MAX_GIT_TIMEOUT_SECONDS
from .. import git_guard
from ..git_guard import READ_ONLY_SUBCOMMANDS

# Minimal env forwarded to git: no whole os.environ (avoids leaking host env).
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


@dataclass(frozen=True)
class GitResult:
    ok: bool
    text: str


def _minimal_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if os.environ.get(k)}
    env.update(
        {
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            # Never invoke external diff even if config requests it; textconv is
            # also disabled via the --no-textconv flag on supporting commands.
            "GIT_EXTERNAL_DIFF": "",
        }
    )
    return env


def _resolve_repo(repo_path: str, workspace: Path) -> Path:
    candidate = Path(repo_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve(strict=True)
    if not candidate.is_relative_to(workspace):
        raise PermissionError(
            f"repo_path must stay inside the session workspace: {workspace}"
        )
    return candidate if candidate.is_dir() else candidate.parent


async def execute(
    subcommand: str, repo_path: str | None, extra_args: Any, context: Any
) -> GitResult:
    """Core: workspace resolution (fail-closed), validate, run git."""
    args, error = git_guard.validate(subcommand, extra_args)
    if error:
        return GitResult(False, f"error: {error}")

    try:
        workspace = await workspace_root_for_context(context)
    except Exception as exc:  # noqa: BLE001 - fail closed, surfaced to model
        return GitResult(
            False,
            "error: cannot resolve session workspace; refusing to run git "
            f"outside a known workspace ({exc}).",
        )

    cwd: Path = workspace
    if repo_path:
        try:
            cwd = _resolve_repo(repo_path, workspace)
        except PermissionError as exc:
            return GitResult(False, f"error: {exc}")
        except OSError:
            return GitResult(False, f"error: repo_path does not exist: {repo_path}")

    command = ["git", "--no-pager", subcommand, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_minimal_env(),
        )
    except Exception as exc:  # noqa: BLE001
        return GitResult(False, f"error: failed to launch git: {exc}")

    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(), timeout=MAX_GIT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return GitResult(
            False,
            f"error: git {subcommand} timed out after {MAX_GIT_TIMEOUT_SECONDS}s "
            "(process killed).",
        )
    except Exception as exc:  # noqa: BLE001
        proc.kill()
        await proc.communicate()
        return GitResult(False, f"error: git execution failed: {exc}")

    out = (out_b or b"").decode("utf-8", errors="replace")
    err = (err_b or b"").decode("utf-8", errors="replace")
    body = out if not err.strip() else f"{out}\n[stderr]\n{err}".strip()
    body = f"$ {' '.join(command)}\n{body}".strip()
    if len(body) > MAX_GIT_OUTPUT:
        body = body[:MAX_GIT_OUTPUT] + "\n...[truncated]"
    # Non-zero exit (unknown revision, invalid args, etc.) must surface as a
    # failed result so callers don't treat the stderr text as a valid snapshot.
    return GitResult(proc.returncode == 0, body)


@pydantic_dataclass
class ReasonixGitReadTool(FunctionTool[AstrAgentContext]):
    """Run a read-only git subcommand inside the session workspace."""

    name: str = "reasonix_git_read"
    description: str = (
        "Run a READ-ONLY git subcommand on a repository inside the session workspace. "
        "Allowed subcommands: status, diff, log, show, blame, ls-files, rev-parse, "
        "branch (listing only), remote (listing/get-url/show), describe, shortlog, "
        "tag (listing only). Returns command output (truncated). Never writes. Use to "
        "inspect pending changes (`diff`/`diff --stat`) before reviewing. If no "
        "repo_path is given the session workspace root is used."
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "subcommand": {
                    "type": "string",
                    "description": "Read-only git subcommand to run.",
                    "enum": sorted(READ_ONLY_SUBCOMMANDS),
                },
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Optional absolute or workspace-relative path to the git "
                        "repository. Defaults to workspace root. Must stay inside "
                        "the workspace."
                    ),
                },
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional read-only arguments after the subcommand, e.g. "
                        "['--stat'] or ['<base>...HEAD']. Dangerous global options "
                        "(-c/-C/--git-dir/--exec/--output) are rejected."
                    ),
                },
            },
            "required": ["subcommand"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        subcommand = str(kwargs.get("subcommand") or "").strip().lower()
        repo_path = str(kwargs.get("repo_path") or "").strip() or None
        result = await execute(subcommand, repo_path, kwargs.get("extra_args"), context)
        return result.text
