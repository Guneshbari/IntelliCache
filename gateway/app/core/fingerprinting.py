"""
Conservative text normalization and deterministic SHA-256 exact fingerprint generation.
Core modules MUST NOT import FastAPI or Groq SDK.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional
from .contracts import ChatMessage


def normalize_text(text: str) -> str:
    """
    Conservative normalization:
    1. Normalizes CRLF (\\r\\n) and CR (\\r) line endings to LF (\\n).
    2. Trims overall outer leading and trailing whitespace of the prompt/message.
    Does NOT strip trailing whitespace from individual lines (preserving Markdown linebreaks).
    Does NOT collapse spaces, tabs, or newlines within lines (preserving code indentation).
    """
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.strip()


def build_canonical_messages(
    prompt: Optional[str] = None,
    query: Optional[str] = None,
    messages: Optional[List[Any]] = None,
    system_prompt: Optional[str] = None,
) -> List[ChatMessage]:
    """
    Builds a deterministic, normalized sequence of ChatMessage objects.
    Prepends normalized system_prompt if provided.
    Preserves role order, message boundaries, and internal whitespace.
    """
    canonical_list: List[ChatMessage] = []

    if system_prompt:
        norm_system = normalize_text(system_prompt)
        if norm_system:
            canonical_list.append(ChatMessage(role="system", content=norm_system))

    if messages:
        for m in messages:
            if isinstance(m, ChatMessage):
                role = m.role.strip().lower()
                content = normalize_text(m.content)
            elif isinstance(m, dict):
                role = str(m.get("role", "user")).strip().lower()
                content = normalize_text(str(m.get("content", "")))
            else:
                role = str(getattr(m, "role", "user")).strip().lower()
                content = normalize_text(str(getattr(m, "content", "")))
            canonical_list.append(ChatMessage(role=role, content=content))
    else:
        effective = prompt or query or ""
        norm_prompt = normalize_text(effective)
        canonical_list.append(ChatMessage(role="user", content=norm_prompt))

    return canonical_list


def generate_fingerprint(
    messages: List[ChatMessage],
    model: str,
    temperature: float,
    max_tokens: Optional[int] = None,
    provider: str = "groq",
) -> str:
    """
    Generates a deterministic SHA-256 fingerprint from all answer-affecting inputs:
    - Provider identifier
    - Effective model identifier (casing preserved consistently with generation)
    - Effective unrounded temperature
    - Effective max tokens ceiling
    - Exact normalized message sequence (role and content)
    Uses deterministic JSON serialization with sorted keys.
    """
    canonical_payload: Dict[str, Any] = {
        "max_tokens": max_tokens,
        "messages": [m.to_dict() for m in messages],
        "model": model.strip(),
        "provider": provider.strip().lower(),
        "temperature": temperature,
    }
    serialized = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
