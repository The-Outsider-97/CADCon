"""Start CADCon from its own checkout or SLAI/applications/CADCon."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
