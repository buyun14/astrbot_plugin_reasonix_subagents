"""Layout guards: the plugin must import the way AstrBot loads it.

AstrBot injects plugins at runtime as ``data.plugins.<name>.main`` (an
``__import__`` of the package module), so intra-plugin imports must be
relative. Absolute ``reasonix`` imports -- or tests that monkeypatch bare
``main`` / ``reasonix`` module targets -- silently work under pytest's cwd on
sys.path but break production loading with ``No module named 'reasonix'``.
"""

import ast
import importlib
import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PKG = "astrbot_plugin_reasonix_subagents"
TESTS_DIR = PLUGIN_ROOT / "tests"

ABS_IMPORT = re.compile(r"^\s*(?:from|import)\s+reasonix\b", re.MULTILINE)


def _monkeypatch_targets(path: Path) -> list[tuple[str, int]]:
    """Yield (target, lineno) for every `monkeypatch.setattr("...", ...)` call."""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "setattr":
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((first.value, node.lineno))
    return found


def test_plugin_sources_use_relative_imports():
    """No module inside the plugin may import `reasonix` as a top-level name."""
    offenders = []
    for path in sorted(PLUGIN_ROOT.glob("reasonix/**/*.py")) + [
        PLUGIN_ROOT / "main.py"
    ]:
        if ABS_IMPORT.search(path.read_text()):
            offenders.append(str(path.relative_to(PLUGIN_ROOT)))
    assert not offenders, (
        f"absolute `reasonix` imports break plugin loading: {offenders}"
    )


def test_monkeypatch_targets_are_package_qualified():
    """monkeypatch targets must name the module the plugin actually loads."""
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        for target, lineno in _monkeypatch_targets(path):
            if not target.startswith(f"{PKG}."):
                offenders.append(
                    f"{path.relative_to(PLUGIN_ROOT)}:{lineno} -> {target!r}"
                )
    assert not offenders, (
        f"monkeypatch targets must be `{PKG}.…` (bare `main`/`reasonix` resolve to a "
        f"different module than the plugin loads): {offenders}"
    )


def test_main_imports_as_package_module():
    """main.py resolves its package-qualified path (the production import mode)."""
    module = importlib.import_module(f"{PKG}.main")
    assert module.__package__ == PKG, module.__package__
    assert hasattr(module, "ReasonixSubagentsPlugin")


# Claims the docs must not make about the read-only sub-agent toolset. The
# security pass removed the shell tools; prose describing them drifts just as
# silently as the prompts did.
FORBIDDEN_DOC_CLAIMS = ("只读 shell", "shell 工具", "grep/shell", "文件/grep/shell")


def test_docs_match_the_shell_free_toolset():
    """README/metadata must not advertise a read-only shell tool."""
    offenders = []
    for name in ("README.md", "metadata.yaml"):
        text = (PLUGIN_ROOT / name).read_text()
        for claim in FORBIDDEN_DOC_CLAIMS:
            if claim in text:
                offenders.append(f"{name}: {claim!r}")
    assert not offenders, (
        f"docs advertise tools the sub-agents do not have: {offenders}"
    )
