"""Run the pytest suite so ``check_all`` can treat it like any other check.

``check_all`` launches every check as ``python <script>``; pytest is normally
run as ``python -m pytest``, and making the runner special-case that is worse
than thirty lines of wrapper.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        import pytest
    except ImportError:
        print('  没有 pytest。装一下：pip install -e ".[dev]"（或 pip install pytest）')
        return 1
    return int(pytest.main([str(ROOT / "tests"), "-q"]))


if __name__ == "__main__":
    raise SystemExit(main())
