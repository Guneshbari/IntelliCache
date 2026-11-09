"""
FastAPI router for persistent conversation management endpoints.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..conversations.identity import AuthenticatedClient, verify_conversation_client
from ..conversations.models import (
    ConversationListResponse,
    ConversationMessagesListResponse,
    ConversationResponse,
    CreateConversationRequest,
)
from ..conversations.repository import (
    ConversationNotFoundError,
    PostgresConversationRepository,
)
from ..conversations.service import ConversationService

logger = logging.getLogger("intellicache.gateway.api.conversations")
router = APIRouter(prefix="/conversations", tags=["Conversations"])

_conversation_service_singleton: Optional[ConversationService] = None


def set_conversation_service(service: Optional[ConversationService]) -> None:
    """Sets the conversation service singleton (used by tests or lifecycle)."""
    global _conversation_service_singleton
    _conversation_service_singleton = service


def get_conversation_service(request: Request) -> ConversationService:
    """Dependency provider returning the ConversationService."""
    global _conversation_service_singleton
    if _conversation_service_singleton is not None:
        return _conversation_service_singleton

    pool = getattr(request.app.state, "db_pool", None)
    if pool is None and hasattr(request.app.state, "cache"):
        pool = getattr(request.app.state.cache, "pool", None)

    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Persistent conversation management requires a PostgreSQL backend. "
                "Ensure CACHE_BACKEND='postgres' and DATABASE_URL is configured."
            ),
        )

    repo = PostgresConversationRepository(pool)
    return ConversationService(repo)


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Conversation",
)
@router.post(
    "/",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def create_conversation(
    payload: Optional[CreateConversationRequest] = None,
    client: AuthenticatedClient = Depends(verify_conversation_client),
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationResponse:
    """Creates a new conversation record owned by the authenticated caller."""
    title = payload.title if payload else None
    return await service.create_conversation(
        conversation_id=None,
        namespace=client.namespace,
        owner_principal_id=client.principal_id,
        title=title,
    )


@router.get(
    "",
    response_model=ConversationListResponse,
    summary="List Conversations",
)
@router.get(
    "/",
    response_model=ConversationListResponse,
    include_in_schema=False,
)
async def list_conversations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    client: AuthenticatedClient = Depends(verify_conversation_client),
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationListResponse:
    """Lists conversations owned by the authenticated principal, newest first."""
    return await service.list_conversations(
        owner_principal_id=client.principal_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
    summary="Get Conversation Metadata",
)
async def get_conversation(
    conversation_id: str,
    client: AuthenticatedClient = Depends(verify_conversation_client),
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationResponse:
    """Retrieves conversation metadata if accessible to the caller."""
    try:
        return await service.get_conversation(
            conversation_id=conversation_id,
            owner_principal_id=client.principal_id,
        )
    except ConversationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' was not found or is not accessible.",
        )


@router.get(
    "/{conversation_id}/messages",
    response_model=ConversationMessagesListResponse,
    summary="Get Conversation Messages",
)
async def get_conversation_messages(
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    client: AuthenticatedClient = Depends(verify_conversation_client),
    service: ConversationService = Depends(get_conversation_service),
) -> ConversationMessagesListResponse:
    """Retrieves chronologically ordered messages for an authenticated conversation."""
    try:
        return await service.get_messages(
            conversation_id=conversation_id,
            owner_principal_id=client.principal_id,
            limit=limit,
            offset=offset,
        )
    except ConversationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' was not found or is not accessible.",
        )


@router.delete(
    "/{conversation_id}",
    summary="Delete Conversation",
)
async def delete_conversation(
    conversation_id: str,
    client: AuthenticatedClient = Depends(verify_conversation_client),
    service: ConversationService = Depends(get_conversation_service),
) -> Dict[str, Any]:
    """Deletes a conversation and its turns and messages."""
    try:
        await service.delete_conversation(
            conversation_id=conversation_id,
            owner_principal_id=client.principal_id,
        )
        return {
            "status": "ok",
            "deleted": True,
            "conversation_id": conversation_id,
        }
    except ConversationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation '{conversation_id}' was not found or is not accessible.",
        )
