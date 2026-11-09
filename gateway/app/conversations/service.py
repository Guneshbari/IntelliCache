"""
Conversation orchestration service for IntelliCache Gateway.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    ConversationListResponse,
    ConversationMessageRecord,
    ConversationMessageResponse,
    ConversationMessagesListResponse,
    ConversationRecord,
    ConversationResponse,
    ConversationTurnRecord,
)
from .repository import (
    ActiveTurnConflictError,
    ConversationNotFoundError,
    PostgresConversationRepository,
)
from ..core.contracts import ChatMessage

logger = logging.getLogger("intellicache.gateway.conversations.service")


class ConversationService:
    """Service orchestrating conversation operations, validation, and repository access."""

    def __init__(self, repository: PostgresConversationRepository):
        self.repo = repository

    async def create_conversation(
        self,
        conversation_id: Optional[str],
        namespace: str,
        owner_principal_id: str,
        title: Optional[str] = None,
    ) -> ConversationResponse:
        record = await self.repo.create_conversation(
            conversation_id=conversation_id,
            namespace=namespace,
            owner_principal_id=owner_principal_id,
            title=title,
        )
        return self._to_response(record)

    async def list_conversations(
        self,
        owner_principal_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> ConversationListResponse:
        records, total = await self.repo.list_conversations(
            owner_principal_id=owner_principal_id,
            limit=limit,
            offset=offset,
        )
        return ConversationListResponse(
            conversations=[self._to_response(r) for r in records],
            total=total,
        )

    async def get_conversation(
        self,
        conversation_id: str,
        owner_principal_id: str,
    ) -> ConversationResponse:
        record = await self.repo.get_conversation(conversation_id, owner_principal_id)
        if not record:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found.")
        return self._to_response(record)

    async def delete_conversation(
        self,
        conversation_id: str,
        owner_principal_id: str,
    ) -> bool:
        deleted = await self.repo.delete_conversation(conversation_id, owner_principal_id)
        if not deleted:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found.")
        return True

    async def get_messages(
        self,
        conversation_id: str,
        owner_principal_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> ConversationMessagesListResponse:
        records, total = await self.repo.get_conversation_messages(
            conversation_id=conversation_id,
            owner_principal_id=owner_principal_id,
            limit=limit,
            offset=offset,
        )
        return ConversationMessagesListResponse(
            conversation_id=conversation_id,
            messages=[
                ConversationMessageResponse(
                    id=m.id,
                    conversation_id=m.conversation_id,
                    turn_id=m.turn_id,
                    sequence_number=m.sequence_number,
                    role=m.role,
                    content=m.content,
                    created_at=m.created_at.isoformat(),
                    metadata=m.metadata,
                )
                for m in records
            ],
            total=total,
        )

    async def get_completed_history(
        self,
        conversation_id: str,
        owner_principal_id: str,
    ) -> List[ChatMessage]:
        return await self.repo.get_completed_history(conversation_id, owner_principal_id)

    async def begin_turn(
        self,
        conversation_id: str,
        owner_principal_id: str,
        idempotency_key: str,
        user_content: str,
    ) -> Tuple[ConversationTurnRecord, Optional[ConversationMessageRecord], bool]:
        return await self.repo.begin_turn(
            conversation_id=conversation_id,
            owner_principal_id=owner_principal_id,
            idempotency_key=idempotency_key,
            user_content=user_content,
        )

    async def complete_turn(
        self,
        turn_id: str,
        assistant_content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ConversationMessageRecord:
        return await self.repo.complete_turn(
            turn_id=turn_id,
            assistant_content=assistant_content,
            metadata=metadata,
        )

    async def fail_turn(self, turn_id: str, error_code: str = "GENERATION_FAILED") -> None:
        await self.repo.fail_turn(turn_id, error_code=error_code)

    async def cancel_turn(self, turn_id: str) -> None:
        await self.repo.cancel_turn(turn_id)

    @staticmethod
    def _to_response(r: ConversationRecord) -> ConversationResponse:
        return ConversationResponse(
            id=r.id,
            title=r.title,
            cache_namespace=r.cache_namespace,
            created_at=r.created_at.isoformat(),
            updated_at=r.updated_at.isoformat(),
            last_message_at=r.last_message_at.isoformat(),
        )
