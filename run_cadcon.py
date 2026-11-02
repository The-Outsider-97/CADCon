"""``python applications/CADCon/run_cadcon.py`` (or double-click) starts CADCon; the same as ``python -m applications.CADCon``."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))          # the SLAI root, so ``applications`` and ``src`` import

from applications.CADCon.app import main  # type: ignore # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
