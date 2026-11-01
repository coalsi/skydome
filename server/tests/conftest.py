"""Run with the pipeline environment from the repo root:

    uv run --project pipeline pytest server/tests
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
