"""
All application settings, read once from environment variables (the .env file next to this file is loaded on import).
Everything else in the project gets its settings from get_settings() - nothing else reads the environment.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # --- API server ---
    api_host: str
    api_port: int
    action_delay_seconds: float  # pause between browser actions; also scales the page-load waits

    # --- Auth ---
    jwt_secret: str
    jwt_algorithm: str
    jwt_expire_minutes: int
    jwt_refresh_expire_days: int
    # Fernet key used to encrypt platform passwords and cookies at rest
    credential_key: str

    # --- Azure Cosmos DB (NoSQL API) ---
    cosmos_endpoint: str
    cosmos_key: str
    cosmos_database: str

    # --- Azure Blob Storage ---
    storage_connection_string: str
    sas_expiry_days: int  # default lifetime of resume view/download links

    # --- Local working files (per-user browser profiles, temporary resume copies) ---
    data_dir: Path

    # --- LLM (reads resumes, scores jobs, drives the browser): azure | gemini | openai ---
    llm_provider: str
    azure_openai_api_key: str
    azure_openai_endpoint: str
    azure_openai_deployment: str
    azure_openai_api_version: str
    azure_openai_fallback_deployment: str  # optional second deployment used when the first is rate-limited
    google_api_key: str
    openai_api_key: str
    llm_model: str
    default_generation_model: str
    fallback_llm_model: str

    max_resume_bytes: int = 5 * 1024 * 1024

    @property
    def azure_openai_configured(self) -> bool:
        return bool(self.azure_openai_api_key and self.azure_openai_endpoint and self.azure_openai_deployment)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        api_host=os.getenv("API_HOST", "127.0.0.1"),
        api_port=_int("API_PORT", 8000),
        action_delay_seconds=_float("ACTION_DELAY_SECONDS", 2.0),
        jwt_secret=os.getenv("JWT_SECRET", ""),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
        jwt_expire_minutes=_int("JWT_EXPIRE_MINUTES", 60 * 24),
        jwt_refresh_expire_days=_int("JWT_REFRESH_EXPIRE_DAYS", 30),
        credential_key=os.getenv("CREDENTIAL_ENCRYPTION_KEY", ""),
        cosmos_endpoint=os.getenv("COSMOS_ENDPOINT", ""),
        cosmos_key=os.getenv("COSMOS_KEY", ""),
        cosmos_database=os.getenv("COSMOS_DATABASE", "jobapply"),
        storage_connection_string=os.getenv("AZURE_STORAGE_CONNECTION_STRING", ""),
        sas_expiry_days=_int("SAS_EXPIRY_DAYS", 365),
        data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
        llm_provider=os.getenv("LLM_PROVIDER", "azure").lower(),
        azure_openai_api_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
        azure_openai_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
        azure_openai_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT", ""),
        azure_openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        azure_openai_fallback_deployment=os.getenv("AZURE_OPENAI_FALLBACK_DEPLOYMENT", ""),
        google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", ""),
        default_generation_model=os.getenv("DEFAULT_GENERATION_MODEL", ""),
        fallback_llm_model=os.getenv("FALLBACK_LLM_MODEL", "gemini-2.5-flash"),
    )


def reset_settings_cache() -> None:
    """For tests: re-read the environment on the next get_settings()."""
    get_settings.cache_clear()
