"""Test package.

Adds the repository's ``src`` directory to ``sys.path`` so the suite runs
with plain ``python -m unittest discover`` even when PYTHONPATH is unset.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
