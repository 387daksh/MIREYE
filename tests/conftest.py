"""Keep normal pytest offline and away from the production workspace database."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="mireye-pytest-"))
os.environ["MIREYE_API_KEY"] = ""
os.environ["WORKSPACE_DB"] = str(_TEST_DB_DIR / "workspaces.db")
os.environ["WORLD_ASSET_DIR"] = str(_TEST_DB_DIR / "world-assets")
