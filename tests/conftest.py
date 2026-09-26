import sys
from pathlib import Path

# Import the plugin the way AstrBot does: the plugin directory is a package
# (PEP 420 namespace package) whose parent is on sys.path, so
# `astrbot_plugin_reasonix_subagents.main` resolves and its relative imports
# (`from .reasonix... import ...`) work exactly as in production.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))
