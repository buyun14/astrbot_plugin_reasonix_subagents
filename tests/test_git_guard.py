import pytest

from reasonix.git_guard import READ_ONLY_SUBCOMMANDS, sanitize_args, validate


def _ok(sub, args):
    effective, error = validate(sub, args)
    assert error is None
    return effective


def test_unknown_subcommand_rejected():
    _, error = validate("rebase", [])
    assert "unsupported" in error


@pytest.mark.parametrize(
    "args",
    [
        ["-c"],
        ["-c", "user.name=Evil"],
        ["-C", "/etc"],
        ["--git-dir=/x"],
        ["--work-tree=/x"],
        ["--exec=/bin/sh"],
        ["--output=/x"],
        ["--upload-pack=x"],
    ],
)
def test_global_options_rejected(args):
    _, error = validate("status", args)
    assert error is not None


def test_diff_no_index_rejected():
    _, error = validate("diff", ["--no-index", "/etc/passwd", "/etc/shadow"])
    assert error is not None


def test_diff_appends_safety_flags():
    effective = _ok("diff", ["--stat"])
    assert "--stat" in effective
    assert "--no-ext-diff" in effective
    assert "--no-textconv" in effective


def test_blame_appends_textconv_only():
    effective = _ok("blame", ["file.py"])
    assert "--no-textconv" in effective
    assert "--no-ext-diff" not in effective


def test_branch_listing_allowed():
    _ok("branch", ["--list"])
    _ok("branch", ["-a"])
    _ok("branch", ["--list", "feat/*"])
    _ok("branch", ["--contains", "abc123"])


def test_branch_write_forms_rejected():
    for args in (
        ["new_branch"],
        ["-d", "old"],
        ["-D", "old"],
        ["-m", "renamed"],
        ["--set-upstream-to=origin/main"],
        ["--edit-description"],
    ):
        _, error = validate("branch", args)
        assert error is not None, args


def test_tag_listing_allowed():
    _ok("tag", ["--list"])
    _ok("tag", ["--list", "v1.*"])


def test_tag_write_forms_rejected():
    for args in (["v2"], ["-d", "v1"], ["-a", "v2", "-m", "msg"]):
        _, error = validate("tag", args)
        assert error is not None, args


def test_remote_read_forms_allowed():
    _ok("remote", [])
    _ok("remote", ["-v"])
    _ok("remote", ["show", "origin"])
    _ok("remote", ["get-url", "origin"])
    _ok("remote", ["--get-url", "origin"])


def test_remote_write_forms_rejected():
    for args in (
        ["add", "other", "git@x:y.git"],
        ["set-url", "origin", "git@x:y.git"],
        ["remove", "origin"],
        ["prune", "origin"],
    ):
        _, error = validate("remote", args)
        assert error is not None, args


def test_sanitize_drops_bad_types():
    assert sanitize_args(["--stat", 3, None, {"x": 1}, "  "]) == ["--stat"]
    assert sanitize_args(None) == []
    assert set(READ_ONLY_SUBCOMMANDS) >= {"status", "diff", "log", "show", "blame"}


def test_plain_subcommands_pass():
    for sub in (
        "status",
        "log",
        "ls-files",
        "rev-parse",
        "show",
        "describe",
        "shortlog",
    ):
        _ok(sub, [])
