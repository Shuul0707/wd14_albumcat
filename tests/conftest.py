"""Make ``import albumcat`` work without installing the package.

``pip install -e .`` puts ``src`` on the path for you; running ``pytest`` from a
fresh checkout does not, and requiring ``PYTHONPATH=src`` for every invocation
is the kind of thing contributors forget. One line here covers both.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
