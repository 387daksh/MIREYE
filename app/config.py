import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MIREYE_API_KEY = os.environ.get("MIREYE_API_KEY", "")
MIREYE_BASE_URL = os.environ.get("MIREYE_BASE_URL", "https://api.mireye.com")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SANDBOX_AGENT_MODEL = os.environ.get("SANDBOX_AGENT_MODEL", "gpt-5.6-sol")
SANDBOX_AGENT_REASONING_EFFORT = os.environ.get("SANDBOX_AGENT_REASONING_EFFORT", "high")
MIREYE_ENRICHMENT_BATCH_SIZE = int(os.environ.get("MIREYE_ENRICHMENT_BATCH_SIZE", "2"))


DATA_MODE = "live" if MIREYE_API_KEY else "local"

PARQUET_DIR = ROOT / "app" / "data" / "parquet"
WORKSPACE_DB = Path(os.environ.get("WORKSPACE_DB", ROOT / "app" / "data" / "workspaces.db"))
WORLD_ASSET_DIR = Path(os.environ.get("WORLD_ASSET_DIR", ROOT / "app" / "data" / "world-assets"))

# Fixes bug (C) from the ideation doc: instead of silently dropping fields
# past this cap, we paginate and tell the caller explicitly.
MAX_FIELDS_PER_REQUEST = 15
