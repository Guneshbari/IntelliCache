"""
Contracts and data models for the bounded Context Builder.
Core contracts MUST NOT import FastAPI or LLM provider SDKs.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from ..core.contracts import ChatMessage


class ContextStatus(str, Enum):
    """
    Classification of the reconstructed conversation context state.
    - complete: All required conversation messages fit within configured limits.
    - compacted: Older conversation content was omitted to remain within context limits.
    - incomplete: Required history was missing, inconsistent, or could not be reliably reconstructed.
    """
    COMPLETE = "complete"
    COMPACTED = "compacted"
    INCOMPLETE = "incomplete"


@dataclass
class ContextBuildResult:
    """The outcome of a context building operation."""
    messages: List[ChatMessage]
    status: ContextStatus
    total_messages: int
    total_characters: int
    omitted_messages_count: int = 0
    reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)
