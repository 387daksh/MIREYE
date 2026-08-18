import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MIREYE_API_KEY = os.environ.get("MIREYE_API_KEY", "")
MIREYE_BASE_URL = os.environ.get("MIREYE_BASE_URL", "https://api.mireye.com")


DATA_MODE = "live" if MIREYE_API_KEY else "local"

PARQUET_DIR = ROOT / "app" / "data" / "parquet"
WORKSPACE_DB = ROOT / "app" / "data" / "workspaces.db"

# Fixes bug (C) from the ideation doc: instead of silently dropping fields
# past this cap, we paginate and tell the caller explicitly.
MAX_FIELDS_PER_REQUEST = 15