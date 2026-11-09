"""
Conversations package for IntelliCache Gateway.
"""

from .identity import AuthenticatedClient, derive_principal_id, verify_conversation_client
from .models import (
    ConversationListResponse,
    ConversationMessageRecord,
    ConversationMessageResponse,
    ConversationMessagesListResponse,
    ConversationRecord,
    ConversationResponse,
    ConversationTurnRecord,
    CreateConversationRequest,
)
from .repository import (
    ActiveTurnConflictError,
    ConversationNotFoundError,
    PostgresConversationRepository,
)
from .service import ConversationService

__all__ = [
    "ActiveTurnConflictError",
    "AuthenticatedClient",
    "ConversationListResponse",
    "ConversationMessageRecord",
    "ConversationMessageResponse",
    "ConversationMessagesListResponse",
    "ConversationNotFoundError",
    "ConversationRecord",
    "ConversationResponse",
    "ConversationService",
    "ConversationTurnRecord",
    "CreateConversationRequest",
    "PostgresConversationRepository",
    "derive_principal_id",
    "verify_conversation_client",
]
