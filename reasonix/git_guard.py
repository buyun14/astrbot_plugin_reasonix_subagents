"""Pure validation logic for read-only git invocations.

Hardening over the old "subcommand allowlist" design:

* ``branch`` / ``tag`` / ``remote`` are write-capable depending on their
  arguments; they are accepted only in their read-only listing forms.
* ``diff --no-index`` can read files outside the workspace -> rejected.
* Global options that relocate/configure git (``-c``/``-C``/``--git-dir``/
  ``--work-tree``/``--exec``/``--output``/``--upload-pack``) are rejected.
* External diff drivers / textconv (which can execute arbitrary programs) are
  disabled per-subcommand.

No AstrBot imports here, so this is fully unit-testable.
"""

from __future__ import annotations

READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset(
    (
        "status",
        "diff",
        "log",
        "show",
        "blame",
        "ls-files",
        "rev-parse",
        "branch",
        "remote",
        "describe",
        "shortlog",
        "tag",
    )
)

_FORBIDDEN_EXACT_OR_EQ: tuple[str, ...] = (
    "-c",
    "-C",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--super-prefix",
    "--upload-pack",
    "--exec",
    "--output",
)

# branch options that take a value (their following positional is not a name).
_BRANCH_VALUE_OPTS: frozenset[str] = frozenset(
    (
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--sort",
        "--format",
        "--points-at",
        "--column",
    )
)
_BRANCH_MUTATION_OPTS: tuple[str, ...] = (
    "-d",
    "-D",
    "--delete",
    "-m",
    "-M",
    "--move",
    "-c",
    "--copy",
    "-C",
    "-u",
    "--set-upstream-to",
    "--unset-upstream",
    "--edit-description",
)
_BRANCH_LIST_FLAGS: frozenset[str] = frozenset(("--list", "-l", "--all", "-a"))

_TAG_VALUE_OPTS: frozenset[str] = frozenset(
    ("--contains", "--no-contains", "--points-at", "--sort", "--format", "--column")
)
_TAG_MUTATION_OPTS: tuple[str, ...] = (
    "-d",
    "--delete",
    "-a",
    "--annotate",
    "-s",
    "--sign",
    "-m",
    "--message",
    "-f",
    "--force",
    "-u",
    "--local-user",
)

_REMOTE_READ_VERBS: frozenset[str] = frozenset(("get-url", "show"))
# value-taking remote options before the verb.
_REMOTE_VALUE_OPTS: frozenset[str] = frozenset(("--get", "--get-all", "--set-head"))
# Read-only plumbing forms whose remaining positional is a remote name.
_REMOTE_READ_PLUMBING: frozenset[str] = frozenset(("--get-url", "--get", "--get-all"))


class GitArgError(ValueError):
    """Raised when caller-supplied git arguments violate read-only policy."""


def sanitize_args(extra_args: object) -> list[str]:
    """Coerce to a clean list of non-empty string args."""
    if not isinstance(extra_args, (list, tuple)):
        return []
    cleaned: list[str] = []
    for arg in extra_args:
        if not isinstance(arg, str) or not arg.strip():
            continue
        cleaned.append(arg.strip())
    return cleaned


def _reject_global(arg: str) -> None:
    for prefix in _FORBIDDEN_EXACT_OR_EQ:
        if arg == prefix or arg.startswith(prefix + "="):
            raise GitArgError(f"forbidden git option: {arg}")


def _split_options(
    args: list[str], value_opts: frozenset[str]
) -> tuple[list[str], list[str]]:
    """Split (flags/options, positional), skipping values of value-taking opts."""
    flags: list[str] = []
    positional: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg in value_opts:
            flags.append(arg)
            skip_value = True
            continue
        flags.append(arg) if arg.startswith("-") else positional.append(arg)
    return flags, positional


def _is_mutation_opt(arg: str, mutation_opts: tuple[str, ...]) -> str | None:
    """Return the offending mutation option if ``arg`` matches one.

    Git parses short options as a cluster: ``-Dfoo`` is equivalent to
    ``-D foo`` and ``-dbar`` to ``-d bar``. A naive ``flag in mutation_opts``
    check accepts these glued forms and lets a delete-through-branch
    request slip past the read-only policy. We reject:
    - exact match (the standalone short or long option, e.g. ``-D``);
    - long option followed by ``=`` value (``--delete=foo``);
    - short option glued with additional characters (``-Dfoo``).
    """
    for opt in mutation_opts:
        if arg == opt:
            return opt
        if opt.startswith("--") and arg.startswith(opt + "="):
            return opt
        if (
            opt.startswith("-")
            and not opt.startswith("--")
            and arg.startswith(opt)
            and len(arg) > len(opt)
        ):
            return opt
    return None


def _validate_branch(args: list[str]) -> None:
    flags, positional = _split_options(args, _BRANCH_VALUE_OPTS)
    for flag in flags:
        offending = _is_mutation_opt(flag, _BRANCH_MUTATION_OPTS)
        if offending is not None:
            raise GitArgError(
                f"branch only allows read-only listing forms: {flag} (matches {offending})"
            )
        if flag.startswith("--set-upstream-to="):
            raise GitArgError("branch --set-upstream-to is not read-only")
    listing = any(f in _BRANCH_LIST_FLAGS for f in flags)
    if not listing and positional:
        # First positional would name a branch to create/modify.
        raise GitArgError("branch <name> is not read-only; use branch --list [pattern]")


def _validate_tag(args: list[str]) -> None:
    flags, positional = _split_options(args, _TAG_VALUE_OPTS)
    for flag in flags:
        offending = _is_mutation_opt(flag, _TAG_MUTATION_OPTS)
        if offending is not None:
            raise GitArgError(
                f"tag only allows read-only listing forms: {flag} (matches {offending})"
            )
    listing = any(f in ("--list", "-l") for f in flags)
    if not listing and positional:
        raise GitArgError("tag <name> is not read-only; use tag --list [pattern]")


def _validate_remote(args: list[str]) -> None:
    flags, positional = _split_options(args, _REMOTE_VALUE_OPTS)
    if any(f in _REMOTE_READ_PLUMBING for f in flags):
        return
    if not positional:
        return  # plain listing
    verb = positional[0]
    if verb not in _REMOTE_READ_VERBS:
        raise GitArgError(f"remote '{verb}' is not read-only")


def _validate_diff(args: list[str]) -> None:
    for arg in args:
        if arg == "--no-index":
            raise GitArgError("diff --no-index can read files outside the workspace")
        if arg in ("-o",) or arg.startswith("--output"):
            raise GitArgError("diff output redirection is not allowed")


# Safety flags appended to disable external diff/textconv execution.
_EXTRA_SAFETY: dict[str, tuple[str, ...]] = {
    "diff": ("--no-ext-diff", "--no-textconv"),
    "log": ("--no-ext-diff", "--no-textconv"),
    "show": ("--no-ext-diff", "--no-textconv"),
    "blame": ("--no-textconv",),
}


def validate(subcommand: str, extra_args: object) -> tuple[list[str], str | None]:
    """Validate args for ``subcommand``.

    Returns ``(effective_args, None)`` on success or ``([], error_message)``.
    """
    sub = (subcommand or "").strip().lower()
    if sub not in READ_ONLY_SUBCOMMANDS:
        return [], f"unsupported git subcommand '{sub}' (read-only allowlist)."
    try:
        args = sanitize_args(extra_args)
        for arg in args:
            _reject_global(arg)
        if sub == "branch":
            _validate_branch(args)
        elif sub == "tag":
            _validate_tag(args)
        elif sub == "remote":
            _validate_remote(args)
        elif sub == "diff":
            _validate_diff(args)
    except GitArgError as exc:
        return [], str(exc)
    effective = [*args, *_EXTRA_SAFETY.get(sub, ())]
    return effective, None
