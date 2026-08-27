from pathlib import Path

from app.infrastructure.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
_settings = get_settings()

MIREYE_API_KEY = _settings.mireye_api_key.get_secret_value()
MIREYE_BASE_URL = _settings.mireye_base_url
OPENAI_API_KEY = _settings.openai_api_key.get_secret_value()
SANDBOX_AGENT_MODEL = _settings.sandbox_agent_model
SANDBOX_AGENT_REASONING_EFFORT = _settings.sandbox_agent_reasoning_effort
MODEL_PRICING = _settings.model_pricing
MIREYE_ENRICHMENT_BATCH_SIZE = _settings.mireye_enrichment_batch_size

DATA_MODE = _settings.data_mode

PARQUET_DIR = ROOT / "app" / "data" / "parquet"
WORKSPACE_DB = _settings.workspace_db
WORLD_ASSET_DIR = _settings.world_asset_dir

# Fixes bug (C) from the ideation doc: instead of silently dropping fields
# past this cap, we paginate and tell the caller explicitly.
MAX_FIELDS_PER_REQUEST = 15
