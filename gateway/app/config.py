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
    embedding_model: Optional[str] = Field(
        default_factory=lambda: (os.getenv("EMBEDDING_MODEL") or "").strip() or None
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
    semantic_cache_max_retrieval_candidates: int = Field(
        default_factory=lambda: int(os.getenv("SEMANTIC_CACHE_MAX_RETRIEVAL_CANDIDATES", "25"))
    )
    semantic_cache_evidence_secret: Optional[str] = Field(
        default_factory=lambda: (os.getenv("SEMANTIC_CACHE_EVIDENCE_SECRET") or "").strip() or None
    )
    semantic_cache_evidence_previous_secrets: List[str] = Field(
        default_factory=lambda: _get_list_from_env("SEMANTIC_CACHE_EVIDENCE_PREVIOUS_SECRETS", [])
    )
    vector_store_backend: Optional[str] = Field(
        default_factory=lambda: (os.getenv("VECTOR_STORE_BACKEND") or "").lower().strip() or None
    )
    vector_store_max_entries: int = Field(
        default_factory=lambda: int(os.getenv("VECTOR_STORE_MAX_ENTRIES", os.getenv("CACHE_MAX_ENTRIES", "1000")))
    )
    vector_dimensions: Optional[int] = Field(
        default_factory=lambda: (
            int(os.getenv("VECTOR_DIMENSIONS") or os.getenv("EMBEDDING_DIMENSIONS"))
            if (os.getenv("VECTOR_DIMENSIONS") or os.getenv("EMBEDDING_DIMENSIONS"))
            else None
        )
    )

    # Shadow Evaluation Configuration (Stage 3.3)
    shadow_evaluation_enabled: bool = Field(
        default_factory=lambda: os.getenv("SHADOW_EVALUATION_ENABLED", "false").lower() in ("true", "1", "yes")
    )
    shadow_sampling_rate: float = Field(
        default_factory=lambda: float(os.getenv("SHADOW_SAMPLING_RATE", "1.0"))
    )
    shadow_max_concurrency: int = Field(
        default_factory=lambda: int(os.getenv("SHADOW_MAX_CONCURRENCY", "5"))
    )
    shadow_queue_max_size: int = Field(
        default_factory=lambda: int(os.getenv("SHADOW_QUEUE_MAX_SIZE", "256"))
    )
    shadow_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("SHADOW_TIMEOUT_SECONDS", "2.0"))
    )
    shadow_retention_days: int = Field(
        default_factory=lambda: int(os.getenv("SHADOW_RETENTION_DAYS", "30"))
    )

    @model_validator(mode="after")
    def validate_backends(self) -> "Settings":
        valid_cache_backends = {"memory", "postgres"}
        if self.cache_backend not in valid_cache_backends:
            raise ValueError(
                f"Invalid CACHE_BACKEND '{self.cache_backend}'. Supported backends are: 'memory', 'postgres'."
            )

        # Coherent default: if vector_store_backend is not explicitly specified, default to match cache_backend
        if not self.vector_store_backend:
            object.__setattr__(self, "vector_store_backend", self.cache_backend)

        valid_vector_backends = {"memory", "postgres"}
        if self.vector_store_backend not in valid_vector_backends:
            raise ValueError(
                f"Invalid VECTOR_STORE_BACKEND '{self.vector_store_backend}'. Supported backends are: 'memory', 'postgres'."
            )

        if self.cache_backend == "postgres" and not self.database_url:
            raise ValueError("DATABASE_URL must be configured when CACHE_BACKEND='postgres'.")

        # Supported combinations: (memory, memory) and (postgres, postgres)
        if self.cache_backend == "memory" and self.vector_store_backend != "memory":
            raise ValueError(
                f"Unsupported cache and vector-store backend combination: "
                f"CACHE_BACKEND='{self.cache_backend}' cannot be paired with VECTOR_STORE_BACKEND='{self.vector_store_backend}'."
            )
        if self.cache_backend == "postgres" and self.vector_store_backend != "postgres":
            raise ValueError(
                f"Unsupported cache and vector-store backend combination: "
                f"CACHE_BACKEND='{self.cache_backend}' cannot be paired with VECTOR_STORE_BACKEND='{self.vector_store_backend}'."
            )
        if self.semantic_cache_enabled and self.cache_backend == "postgres" and not self.database_url:
            raise ValueError("DATABASE_URL must be configured when SEMANTIC_CACHE_ENABLED is true.")
        if self.semantic_cache_top_k < 1:
            raise ValueError("SEMANTIC_CACHE_TOP_K must be >= 1.")
        if (
            self.semantic_cache_max_retrieval_candidates < self.semantic_cache_top_k
            or self.semantic_cache_max_retrieval_candidates > 50
        ):
            raise ValueError(
                "SEMANTIC_CACHE_MAX_RETRIEVAL_CANDIDATES must be between SEMANTIC_CACHE_TOP_K and 50."
            )

        return self

    @model_validator(mode="after")
    def validate_embedding_config(self) -> "Settings":
        if self.embedding_timeout_seconds <= 0:
            raise ValueError("EMBEDDING_TIMEOUT_SECONDS must be positive.")
        if self.embedding_provider not in ("fastembed", "local"):
            if not (self.embedding_base_url.startswith("http://") or self.embedding_base_url.startswith("https://")):
                raise ValueError("EMBEDDING_BASE_URL must be a valid HTTP or HTTPS URL.")

        if not self.embedding_model:
            if self.embedding_provider in ("fastembed", "local"):
                object.__setattr__(self, "embedding_model", "BAAI/bge-small-en-v1.5")
            else:
                object.__setattr__(self, "embedding_model", "nomic-embed-text")

        if self.vector_dimensions is None:
            if self.embedding_provider in ("fastembed", "local"):
                object.__setattr__(self, "vector_dimensions", 384)
            else:
                object.__setattr__(self, "vector_dimensions", 768)
        else:
            if self.vector_dimensions <= 0:
                raise ValueError("VECTOR_DIMENSIONS must be positive.")
            # Enforce that explicit vector_dimensions does not conflict with well-known model dimensions
            if self.embedding_provider in ("fastembed", "local"):
                if self.embedding_model == "BAAI/bge-small-en-v1.5" and self.vector_dimensions != 384:
                    raise ValueError(
                        f"Explicit VECTOR_DIMENSIONS ({self.vector_dimensions}) conflicts with FastEmbed model "
                        f"'{self.embedding_model}' (requires 384 dimensions)."
                    )
            elif self.embedding_provider == "ollama":
                if self.embedding_model == "nomic-embed-text" and self.vector_dimensions != 768:
                    raise ValueError(
                        f"Explicit VECTOR_DIMENSIONS ({self.vector_dimensions}) conflicts with Ollama model "
                        f"'{self.embedding_model}' (requires 768 dimensions)."
                    )

        return self

    @staticmethod
    def _validate_evidence_secret_strength(raw_secret: str, env_name: str) -> str:
        """
        Minimum-format sanity check for evidence signing secrets (not cryptographic proof of
        256-bit entropy). Rejects known placeholders, short strings (<32 chars), low symbol
        diversity (<10 unique chars), single-character dominance, and short repeating patterns.
        Production deployments should generate keys with `openssl rand -hex 32`.
        """
        cleaned = raw_secret.strip()
        insecure_placeholders = {
            "secret",
            "changeme",
            "change-me",
            "default",
            "password",
            "replace_with_your_key",
            "replace_with_a_secure_random_hex_or_passphrase_at_least_16_chars",
            "replace_with_a_strong_256bit_hex_secret",
            "intellicache-default-identity-secret-change-in-production",
        }
        if cleaned.lower() in insecure_placeholders:
            raise ValueError(
                f"{env_name} cannot use a known public or placeholder value. "
                "Generate a dedicated 256-bit secret (e.g., `openssl rand -hex 32`)."
            )
        if len(cleaned) < 32:
            raise ValueError(
                f"{env_name} fails minimum secret format check (must be at least 32 characters; "
                "use `openssl rand -hex 32` for a 256-bit production secret)."
            )
        if len(set(cleaned)) < 10:
            raise ValueError(
                f"{env_name} fails minimum secret format check (requires at least 10 distinct characters; "
                "use `openssl rand -hex 32` for a 256-bit production secret)."
            )
        max_freq = max(cleaned.count(ch) for ch in set(cleaned))
        if max_freq > len(cleaned) // 3:
            raise ValueError(
                f"{env_name} fails minimum secret format check (single-character dominance exceeds threshold; "
                "use `openssl rand -hex 32` for a 256-bit production secret)."
            )
        for period in range(1, min(17, (len(cleaned) // 2) + 1)):
            pattern = cleaned[:period]
            repeated = (pattern * ((len(cleaned) // period) + 1))[: len(cleaned)]
            if cleaned == repeated:
                raise ValueError(
                    f"{env_name} fails minimum secret format check (repeating pattern detected; "
                    "use `openssl rand -hex 32` for a 256-bit production secret)."
                )
        return cleaned

    @model_validator(mode="after")
    def validate_semantic_cache_config(self) -> "Settings":
        if self.vector_dimensions is not None and self.vector_dimensions <= 0:
            raise ValueError("VECTOR_DIMENSIONS must be positive.")
        if self.vector_store_max_entries <= 0:
            raise ValueError("VECTOR_STORE_MAX_ENTRIES must be positive.")
        if not (0.0 <= self.semantic_cache_min_similarity <= 1.0):
            raise ValueError("SEMANTIC_CACHE_MIN_SIMILARITY must be between 0.0 and 1.0.")
        if not (0.0 <= self.semantic_cache_min_margin <= 1.0):
            raise ValueError("SEMANTIC_CACHE_MIN_MARGIN must be between 0.0 and 1.0.")
        if self.semantic_cache_top_k <= 0:
            raise ValueError("SEMANTIC_CACHE_TOP_K must be positive.")

        if self.semantic_cache_evidence_secret is not None:
            validated = self._validate_evidence_secret_strength(
                self.semantic_cache_evidence_secret,
                "SEMANTIC_CACHE_EVIDENCE_SECRET",
            )
            object.__setattr__(self, "semantic_cache_evidence_secret", validated)

        validated_prev: List[str] = []
        for idx, prev_raw in enumerate(self.semantic_cache_evidence_previous_secrets):
            validated_prev.append(
                self._validate_evidence_secret_strength(
                    prev_raw,
                    f"SEMANTIC_CACHE_EVIDENCE_PREVIOUS_SECRETS[{idx}]",
                )
            )
        object.__setattr__(self, "semantic_cache_evidence_previous_secrets", validated_prev)

        if self.semantic_cache_enabled and self.vector_store_backend == "postgres":
            if not self.semantic_cache_evidence_secret:
                raise ValueError(
                    "SEMANTIC_CACHE_EVIDENCE_SECRET is required when SEMANTIC_CACHE_ENABLED=true "
                    "with VECTOR_STORE_BACKEND='postgres'. Persistent semantic records require a stable "
                    "HMAC signing key shared across all gateway workers and restarts."
                )

        return self

    @model_validator(mode="after")
    def validate_shadow_evaluation_config(self) -> "Settings":
        if not (0.0 <= self.shadow_sampling_rate <= 1.0):
            raise ValueError(
                f"SHADOW_SAMPLING_RATE must be between 0.0 and 1.0, got {self.shadow_sampling_rate}."
            )
        if self.shadow_max_concurrency < 1 or self.shadow_max_concurrency > 50:
            raise ValueError(
                f"SHADOW_MAX_CONCURRENCY must be between 1 and 50, got {self.shadow_max_concurrency}."
            )
        if self.shadow_queue_max_size < 1 or self.shadow_queue_max_size > 10000:
            raise ValueError(
                f"SHADOW_QUEUE_MAX_SIZE must be between 1 and 10000, got {self.shadow_queue_max_size}."
            )
        if self.shadow_timeout_seconds <= 0.0 or self.shadow_timeout_seconds > 60.0:
            raise ValueError(
                f"SHADOW_TIMEOUT_SECONDS must be strictly greater than 0.0 and <= 60.0, got {self.shadow_timeout_seconds}."
            )
        if self.shadow_retention_days < 1 or self.shadow_retention_days > 365:
            raise ValueError(
                f"SHADOW_RETENTION_DAYS must be between 1 and 365, got {self.shadow_retention_days}."
            )
        if self.shadow_evaluation_enabled and self.cache_backend == "postgres" and not self.database_url:
            raise ValueError("DATABASE_URL must be configured when SHADOW_EVALUATION_ENABLED is true.")
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
    if "unittest" not in sys.modules and "pytest" not in sys.modules:
        if ENV_PATH.exists():
            load_dotenv(dotenv_path=ENV_PATH, override=True)
        else:
            load_dotenv(override=True)
    _settings_instance = Settings()
    return _settings_instance
