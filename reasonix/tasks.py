"""JSON schemas and task-message construction (no AstrBot imports)."""

from __future__ import annotations

from reasonix.constants import MAX_PASTED_DIFF_CHARS


def task_parameters(task_hint: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": task_hint},
        },
        "required": ["task"],
    }


def review_parameters(task_hint: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": task_hint},
            "diff": {
                "type": "string",
                "description": (
                    "Optional: paste the full diff text to review when the git repo is not "
                    "reachable from the bot's environment. The sub-agent reviews exactly "
                    "this diff and does not require git."
                ),
            },
            "repo_path": {
                "type": "string",
                "description": (
                    "Optional: absolute or workspace-relative path of the target git "
                    "repository inside the session workspace. Ignored when a diff is "
                    "provided."
                ),
            },
        },
        "required": ["task"],
    }


def truncate_diff(diff: str) -> str:
    if len(diff) > MAX_PASTED_DIFF_CHARS:
        return (
            diff[:MAX_PASTED_DIFF_CHARS]
            + f"\n...[truncated: diff was {len(diff)} chars, only the head was kept]"
        )
    return diff


def build_review_task(task: str, pasted_diff: str, repo_path: str) -> str:
    """Append pasted-diff / repo-path carriers to ``task``."""
    message = task
    if pasted_diff:
        message += (
            "\n\nParent-provided diff to review (no git repo needed - review "
            "exactly this diff):\n```diff\n" + truncate_diff(pasted_diff) + "\n```"
        )
    if repo_path:
        message += (
            "\n\nTarget git repository path (pass this repo_path to the "
            "read-only git tool): " + repo_path
        )
    return message


def build_snapshot_task(task: str, diff_text: str, repo_path: str) -> str:
    """Shared task for parallel reviewers around one diff snapshot."""
    return (
        f"{task}\n\n"
        "Parent-provided diff to review (review exactly this diff; do not call git):\n"
        "```diff\n"
        f"{truncate_diff(diff_text)}\n```"
    )
