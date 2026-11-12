"""
Configuration module for the IntelliCache Gateway backend.
Loads settings from environment variables and .env file.
"""

from pathlib import Path
import json
import logging
import os
import sys
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, model_validator
from dotenv import load_dotenv

logger = logging.getLogger("intellicache.gateway.config")

# Resolve paths
# Base directory is gateway/
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

# Load .env if it exists and not running under unit tests
if "unittest" not in sys.modules and "pytest" not in sys.modules:
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
        default_factory=lambda: os.getenv("CLIENT_API_TOKEN")
    )
    client_api_tokens: Optional[str] = Field(
        default_factory=lambda: os.getenv("CLIENT_API_TOKENS")
    )
    allow_unauthenticated_loopback: bool = Field(
        default_factory=lambda: os.getenv("ALLOW_UNAUTHENTICATED_LOOPBACK", "false").lower() in ("true", "1", "yes")
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

    # Persistent Conversation & Context Builder Configuration
    conversation_identity_secret: Optional[str] = Field(
        default_factory=lambda: (os.getenv("CONVERSATION_IDENTITY_SECRET") or "").strip() or None
    )
    conversation_turn_lease_seconds: int = Field(
        default_factory=lambda: int(os.getenv("CONVERSATION_TURN_LEASE_SECONDS", "120"))
    )
    conversation_max_context_messages: int = Field(
        default_factory=lambda: int(os.getenv("CONVERSATION_MAX_CONTEXT_MESSAGES", "50"))
    )
    conversation_max_context_chars: int = Field(
        default_factory=lambda: int(os.getenv("CONVERSATION_MAX_CONTEXT_CHARS", "65536"))
    )

    # Embedding Configuration
    embedding_provider: str = Field(
        default_factory=lambda: os.getenv("EMBEDDING_PROVIDER", "ollama").lower().strip()
    )
    embedding_model: str = Field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "nomic-embed-text").strip()
    )
    embedding_base_url: str = Field(
        default_factory=lambda: os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    )
    embedding_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "30.0"))
    )

    # Semantic Cache & Vector Storage Configuration
    semantic_cache_enabled: bool = Field(
        default_factory=lambda: os.getenv("SEMANTIC_CACHE_ENABLED", "false").lower() in ("true", "1", "yes")
    )
    semantic_cache_min_similarity: float = Field(
        default_factory=lambda: float(os.getenv("SEMANTIC_CACHE_MIN_SIMILARITY", "0.92"))
    )
    semantic_cache_min_margin: float = Field(
        default_factory=lambda: float(os.getenv("SEMANTIC_CACHE_MIN_MARGIN", "0.03"))
    )
    semantic_cache_top_k: int = Field(
        default_factory=lambda: int(os.getenv("SEMANTIC_CACHE_TOP_K", "5"))
    )
    vector_store_backend: str = Field(
        default_factory=lambda: os.getenv("VECTOR_STORE_BACKEND", "postgres").lower().strip()
    )
    vector_dimensions: int = Field(
        default_factory=lambda: int(
            os.getenv(
                "VECTOR_DIMENSIONS",
                os.getenv(
                    "EMBEDDING_DIMENSIONS",
                    "384" if os.getenv("EMBEDDING_PROVIDER", "").lower().strip() in ("fastembed", "local") else "768",
                ),
            )
        )
    )

    @model_validator(mode="after")
    def validate_database_config(self) -> "Settings":
        if self.cache_backend == "postgres" and not self.database_url:
            raise ValueError("DATABASE_URL must be configured when CACHE_BACKEND='postgres'.")
        return self

    @model_validator(mode="after")
    def validate_embedding_config(self) -> "Settings":
        if self.embedding_timeout_seconds <= 0:
            raise ValueError("EMBEDDING_TIMEOUT_SECONDS must be positive.")
        if self.embedding_provider not in ("fastembed", "local"):
            if not (self.embedding_base_url.startswith("http://") or self.embedding_base_url.startswith("https://")):
                raise ValueError("EMBEDDING_BASE_URL must be a valid HTTP or HTTPS URL.")
        return self

    @model_validator(mode="after")
    def validate_semantic_cache_config(self) -> "Settings":
        if self.vector_dimensions <= 0:
            raise ValueError("VECTOR_DIMENSIONS must be positive.")
        if not (0.0 <= self.semantic_cache_min_similarity <= 1.0):
            raise ValueError("SEMANTIC_CACHE_MIN_SIMILARITY must be between 0.0 and 1.0.")
        if not (0.0 <= self.semantic_cache_min_margin <= 1.0):
            raise ValueError("SEMANTIC_CACHE_MIN_MARGIN must be between 0.0 and 1.0.")
        if self.semantic_cache_top_k < 1:
            raise ValueError("SEMANTIC_CACHE_TOP_K must be at least 1.")
        if self.semantic_cache_enabled:
            if self.vector_store_backend == "memory":
                if self.cache_backend not in ("memory", "postgres"):
                    raise ValueError("SEMANTIC_CACHE_ENABLED with VECTOR_STORE_BACKEND='memory' requires CACHE_BACKEND to be 'memory' or 'postgres'.")
            elif self.cache_backend != "postgres":
                raise ValueError("SEMANTIC_CACHE_ENABLED requires CACHE_BACKEND='postgres'.")
            elif not self.database_url:
                raise ValueError("DATABASE_URL must be configured when SEMANTIC_CACHE_ENABLED is true.")
        return self

    @model_validator(mode="after")
    def validate_client_token_config(self) -> "Settings":
        # Eagerly validate client token configuration at startup
        self.get_client_token_map()
        return self

    @model_validator(mode="after")
    def validate_conversation_config(self) -> "Settings":
        if self.conversation_max_context_messages <= 0:
            raise ValueError("CONVERSATION_MAX_CONTEXT_MESSAGES must be positive.")
        if self.conversation_max_context_chars <= 0:
            raise ValueError("CONVERSATION_MAX_CONTEXT_CHARS must be positive.")
        if self.conversation_turn_lease_seconds <= 0:
            raise ValueError("CONVERSATION_TURN_LEASE_SECONDS must be positive.")
        if self.conversation_identity_secret is not None:
            cleaned = self.conversation_identity_secret.strip()
            insecure_placeholders = {
                "intellicache-default-identity-secret-change-in-production",
                "replace_with_a_secure_random_hex_or_passphrase_at_least_16_chars",
                "replace_with_your_key",
                "change-me",
                "secret",
            }
            if cleaned.lower() in insecure_placeholders:
                raise ValueError(
                    f"CONVERSATION_IDENTITY_SECRET cannot be a known public/placeholder string ('{cleaned}'). "
                    "Configure a random 256-bit hex or secure private key."
                )
            if len(cleaned) < 16:
                raise ValueError("CONVERSATION_IDENTITY_SECRET must be at least 16 characters long.")
        return self

    def get_client_token_map(self) -> Dict[str, str]:
        """
        Returns a mapping of {token: namespace} for client authentication.
        Supports:
        - CLIENT_API_TOKENS as a JSON object: '{"tok_a": "ns_a", "tok_b": "ns_a"}'
        - CLIENT_API_TOKENS as comma-separated pairs: 'tok_a:ns_a,tok_b:ns_a'
        - Fallback to legacy CLIENT_API_TOKEN only if CLIENT_API_TOKENS is absent (None).
        """
        token_map: Dict[str, str] = {}

        if self.client_api_tokens is not None:
            # An explicitly configured CLIENT_API_TOKENS is authoritative.
            # When legacy CLIENT_API_TOKEN is also set, emit a non-sensitive warning.
            if self.client_api_token is not None and self.client_api_token.strip():
                logger.warning(
                    "Both CLIENT_API_TOKENS and CLIENT_API_TOKEN are configured. "
                    "CLIENT_API_TOKENS is authoritative; CLIENT_API_TOKEN is ignored."
                )

            raw = self.client_api_tokens.strip()
            if not raw:
                raise ValueError("CLIENT_API_TOKENS cannot be empty when configured.")

            if raw.startswith("{"):
                def _detect_duplicate_pairs(pairs):
                    seen = set()
                    d = {}
                    for k, v in pairs:
                        if not isinstance(k, str) or not k.strip():
                            raise ValueError("Token key in CLIENT_API_TOKENS JSON mapping must be a non-empty string.")
                        k_clean = k.strip()
                        if k_clean in seen:
                            raise ValueError("Duplicate token key detected in CLIENT_API_TOKENS configuration.")
                        seen.add(k_clean)
                        if not isinstance(v, str) or not v.strip():
                            raise ValueError("Namespace value in CLIENT_API_TOKENS JSON mapping must be a non-empty string.")
                        d[k_clean] = v.strip()
                    return d

                try:
                    parsed = json.loads(raw, object_pairs_hook=_detect_duplicate_pairs)
                except json.JSONDecodeError:
                    raise ValueError("Malformed JSON in CLIENT_API_TOKENS configuration.") from None

                if not isinstance(parsed, dict):
                    raise ValueError("CLIENT_API_TOKENS JSON configuration must be a JSON object.")
                if not parsed:
                    raise ValueError("CLIENT_API_TOKENS JSON mapping cannot be empty.")
                token_map = parsed
            elif raw.startswith("["):
                raise ValueError("CLIENT_API_TOKENS JSON configuration must be a JSON object, not an array.")
            else:
                entries = raw.split(",")
                for entry in entries:
                    entry = entry.strip()
                    if not entry:
                        raise ValueError("Malformed delimiter entry in CLIENT_API_TOKENS; empty token-namespace segment detected.")
                    if ":" not in entry:
                        raise ValueError("Malformed delimiter entry in CLIENT_API_TOKENS; expected 'token:namespace'.")
                    token_part, ns_part = entry.split(":", 1)
                    tok = token_part.strip()
                    ns = ns_part.strip()
                    if not tok:
                        raise ValueError("Token key in CLIENT_API_TOKENS delimiter mapping must be a non-empty string.")
                    if not ns:
                        raise ValueError("Namespace value in CLIENT_API_TOKENS delimiter mapping must be a non-empty string.")
                    if tok in token_map:
                        raise ValueError("Duplicate token key detected in CLIENT_API_TOKENS delimiter mapping.")
                    token_map[tok] = ns
                if not token_map:
                    raise ValueError("CLIENT_API_TOKENS delimiter mapping cannot be empty.")
        else:
            # Legacy fallback: only active when CLIENT_API_TOKENS is absent (None)
            if self.client_api_token is not None:
                tok = self.client_api_token.strip()
                if not tok:
                    raise ValueError("CLIENT_API_TOKEN cannot be empty when configured.")
                token_map[tok] = self.cache_namespace

        return token_map


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
