"""
Provider-independent error hierarchy and secret redaction for IntelliCache.
Core modules MUST NOT import FastAPI or Groq SDK.
"""

import re
from typing import Optional


def sanitize_error_message(message: str) -> str:
    """
    Strips API keys, bearer tokens, absolute filesystem paths, and IP addresses
    from error strings and tracebacks to prevent sensitive information leakage.
    """
    if not message:
        return ""

    # Redact Groq API keys: gsk_<alphanumeric>
    redacted = re.sub(r"gsk_[a-zA-Z0-9_-]+", "gsk_***", message)

    # Redact Authorization bearer tokens
    redacted = re.sub(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]+", "Bearer ***", redacted)

    # Redact absolute filesystem paths (Unix and Windows style)
    redacted = re.sub(r"(?:/home|/root|/var|/usr|/tmp|[A-Z]:\\[a-zA-Z0-9_-]+)(?:/[a-zA-Z0-9_.\-]+)+", "<redacted_path>", redacted)

    # Redact IP addresses (v4)
    redacted = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<redacted_ip>", redacted)

    return redacted


class IntelliCacheError(Exception):
    """Base exception for all IntelliCache domain errors."""

    def __init__(self, message: str):
        self.raw_message = message
        self.message = sanitize_error_message(message)
        super().__init__(self.message)


class ProviderError(IntelliCacheError):
    """Base exception for LLM provider failures."""
    pass


class ProviderAuthenticationError(ProviderError):
    """Authentication failed with the LLM provider (e.g. invalid or revoked API key)."""
    pass


class ProviderRateLimitError(ProviderError):
    """Rate limit or quota exceeded on the upstream LLM provider."""
    pass


class ProviderConnectionError(ProviderError):
    """Network connection failure or timeout reaching the LLM provider."""
    pass


class ProviderUpstreamError(ProviderError):
    """Upstream LLM provider returned an unexpected HTTP error status."""

    def __init__(self, message: str, status_code: int = 502):
        self.status_code = status_code
        super().__init__(message)


class ProviderConfigurationError(ProviderError):
    """Provider is misconfigured (e.g. missing API key, unsupported model, unknown provider)."""
    pass
