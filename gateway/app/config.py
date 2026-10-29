"""
Configuration module for the IntelliCache Gateway backend.
Loads settings from environment variables and .env file.
"""

from pathlib import Path
import os
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator
from dotenv import load_dotenv

# Resolve paths
# Base directory is gateway/
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

# Load .env if it exists
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()


def _get_list_from_env(key: str, default: List[str]) -> List[str]:
    raw = os.getenv(key)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseModel):
    """Application settings and configuration schema."""

    # LLM Provider Configuration
    llm_provider: str = Field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "groq").lower().strip()
    )

    # Groq API Configuration
    groq_api_key: str = Field(
        default_factory=lambda: os.getenv("GROQ_API_KEY", "")
    )
    groq_model: str = Field(
        default_factory=lambda: os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    )

    # Server Configuration
    host: str = Field(
        default_factory=lambda: os.getenv("HOST", "127.0.0.1")
    )
    port: int = Field(
        default_factory=lambda: int(os.getenv("PORT", "8000"))
    )
    debug: bool = Field(
        default_factory=lambda: os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
    )
    admin_api_token: Optional[str] = Field(
        default_factory=lambda: os.getenv("ADMIN_API_TOKEN") or None
    )
    client_api_token: Optional[str] = Field(
        default_factory=lambda: os.getenv("CLIENT_API_TOKEN") or None
    )

    # CORS Configuration
    cors_origins: List[str] = Field(
        default_factory=lambda: _get_list_from_env("CORS_ORIGINS", ["*"])
    )

    # Caching Configuration
    cache_backend: str = Field(
        default_factory=lambda: os.getenv("CACHE_BACKEND", "memory").lower().strip()
    )
    database_url: Optional[str] = Field(
        default_factory=lambda: os.getenv("DATABASE_URL") or None
    )
    cache_namespace: str = Field(
        default_factory=lambda: os.getenv("CACHE_NAMESPACE", "default").strip()
    )
    db_pool_min_size: int = Field(
        default_factory=lambda: int(os.getenv("DB_POOL_MIN_SIZE", "1"))
    )
    db_pool_max_size: int = Field(
        default_factory=lambda: int(os.getenv("DB_POOL_MAX_SIZE", "10"))
    )
    cache_enabled: bool = Field(
        default_factory=lambda: os.getenv("CACHE_ENABLED", "true").lower() in ("true", "1", "yes")
    )
    cache_ttl_seconds: int = Field(
        default_factory=lambda: int(os.getenv("CACHE_TTL_SECONDS", "3600"))
    )
    cache_max_entries: int = Field(
        default_factory=lambda: int(os.getenv("CACHE_MAX_ENTRIES", "1000"))
    )

    # Inference Defaults
    default_temperature: float = Field(
        default_factory=lambda: float(os.getenv("DEFAULT_TEMPERATURE", "0.7"))
    )
    default_max_tokens: Optional[int] = Field(
        default_factory=lambda: int(os.getenv("DEFAULT_MAX_TOKENS")) if os.getenv("DEFAULT_MAX_TOKENS") else None
    )

    # Input Payload Size Limits (Character bounds, distinct from model token limits)
    max_prompt_chars: int = Field(
        default_factory=lambda: int(os.getenv("MAX_PROMPT_CHARS", "131072"))
    )
    max_system_prompt_chars: int = Field(
        default_factory=lambda: int(os.getenv("MAX_SYSTEM_PROMPT_CHARS", "65536"))
    )
    max_messages_count: int = Field(
        default_factory=lambda: int(os.getenv("MAX_MESSAGES_COUNT", "100"))
    )
    max_message_chars: int = Field(
        default_factory=lambda: int(os.getenv("MAX_MESSAGE_CHARS", "131072"))
    )

    @model_validator(mode="after")
    def validate_database_config(self) -> "Settings":
        if self.cache_backend == "postgres" and not self.database_url:
            raise ValueError("DATABASE_URL must be configured when CACHE_BACKEND='postgres'.")
        return self


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """Get the cached Settings singleton instance."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance


def reload_settings() -> Settings:
    """Force reload settings from environment and .env file."""
    global _settings_instance
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=True)
    else:
        load_dotenv(override=True)
    _settings_instance = Settings()
    return _settings_instance
