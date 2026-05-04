"""Top-level conftest — adds the repo root to sys.path so `tests.frontend.*`
imports resolve when pytest is launched from anywhere in the tree."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
