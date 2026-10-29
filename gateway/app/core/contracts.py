"""
Provider-neutral contracts for generation requests, results, and streaming chunks.
Core modules MUST NOT import FastAPI or Groq SDK.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone


@dataclass
class ChatMessage:
    """A single normalized turn in a conversation."""
    role: str
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class UsageMetrics:
    """Token usage metrics."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if self.total_tokens == 0 and (self.prompt_tokens > 0 or self.completion_tokens > 0):
            self.total_tokens = self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class GenerationRequest:
    """Normalized provider-neutral request for completion or streaming generation."""
    model: str
    messages: List[ChatMessage]
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    provider: str = "groq"
    stream: bool = False
    bypass_cache: bool = False
    conversation_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationResult:
    """Normalized provider-neutral result for non-streaming completion."""
    text: str
    model: str
    usage: UsageMetrics
    latency_ms: float
    cached: bool = False
    cache_hit_count: int = 0
    fingerprint: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamChunk:
    """A single incremental event in a streaming response."""
    delta: str = ""
    done: bool = False
    cached: bool = False
    fingerprint: str = ""
    model: Optional[str] = None
    usage: Optional[UsageMetrics] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    status_code: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "delta": self.delta,
            "done": self.done,
            "cached": self.cached,
            "fingerprint": self.fingerprint,
        }
        if self.done:
            if self.model is not None:
                data["model"] = self.model
            if self.latency_ms is not None:
                data["latency_ms"] = round(self.latency_ms, 2)
            if self.usage is not None:
                data["usage"] = self.usage.to_dict()
        if self.error is not None:
            data["error"] = self.error
            if self.status_code is not None:
                data["status_code"] = self.status_code
        return data
