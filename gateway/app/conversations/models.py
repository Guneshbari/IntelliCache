"""
Data models and API schemas for persistent conversations.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


@dataclass
class ConversationRecord:
    """Authoritative conversation metadata record."""
    id: str
    cache_namespace: str
    owner_principal_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime


@dataclass
class ConversationTurnRecord:
    """Turn execution record tracking user submission lifecycle."""
    id: str
    conversation_id: str
    idempotency_key: str
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    error_code: Optional[str] = None
    request_hash: Optional[str] = None
    lease_expires_at: Optional[datetime] = None


@dataclass
class ConversationMessageRecord:
    """Ordered conversation turn message record."""
    id: str
    conversation_id: str
    turn_id: str
    sequence_number: int
    role: str
    content: str
    created_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# API Request & Response Schemas
# ---------------------------------------------------------------------------

class CreateConversationRequest(BaseModel):
    """Payload to create a new persistent conversation."""
    title: Optional[str] = Field(default=None, max_length=255)
    id: Optional[str] = Field(default=None, max_length=64)


class ConversationResponse(BaseModel):
    """Metadata for an owned conversation."""
    id: str
    title: str
    cache_namespace: str
    created_at: str
    updated_at: str
    last_message_at: str


class ConversationListResponse(BaseModel):
    """List of conversations belonging to the authenticated client."""
    conversations: List[ConversationResponse]
    total: int


class ConversationMessageResponse(BaseModel):
    """Individual message within a conversation."""
    id: str
    conversation_id: str
    turn_id: str
    sequence_number: int
    role: str
    content: str
    created_at: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ConversationMessagesListResponse(BaseModel):
    """Chronologically ordered messages for a conversation."""
    conversation_id: str
    messages: List[ConversationMessageResponse]
    total: int
