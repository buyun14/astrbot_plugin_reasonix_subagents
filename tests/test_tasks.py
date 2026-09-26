from astrbot_plugin_reasonix_subagents.reasonix import tasks
from astrbot_plugin_reasonix_subagents.reasonix.constants import MAX_PASTED_DIFF_CHARS


def test_task_schema_shape():
    schema = tasks.task_parameters("hint")
    assert schema["required"] == ["task"]
    assert schema["properties"]["task"]["description"] == "hint"


def test_review_schema_has_carriers():
    schema = tasks.review_parameters("hint")
    assert set(schema["properties"]) == {"task", "diff", "repo_path"}


def test_build_review_task():
    msg = tasks.build_review_task("do review", "abc", "/repo")
    assert "do review" in msg
    assert "```diff" in msg
    assert "abc" in msg
    assert "/repo" in msg


def test_build_review_task_no_carriers():
    msg = tasks.build_review_task("plain", "", "")
    assert msg == "plain"


def test_truncate_diff():
    big = "x" * (MAX_PASTED_DIFF_CHARS + 500)
    out = tasks.truncate_diff(big)
    assert out.startswith("x" * 100)
    assert "[truncated" in out
    assert len(out) < len(big)
    assert not out.endswith("x")


def test_snapshot_task_no_git_prompt():
    out = tasks.build_snapshot_task("review it", "diff-body", "")
    assert "do not call git" in out
    assert "diff-body" in out
