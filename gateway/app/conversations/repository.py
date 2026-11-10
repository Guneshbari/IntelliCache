"""
PostgreSQL-backed repository for persistent conversations, turns, and messages.
Uses asyncpg connection pool with strict transaction boundaries and row-level locking.
"""

from datetime import datetime, timezone, timedelta
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

import asyncpg

from .models import (
    ConversationMessageRecord,
    ConversationRecord,
    ConversationTurnRecord,
)
from ..core.contracts import ChatMessage

logger = logging.getLogger("intellicache.gateway.conversations.repository")


class ConversationNotFoundError(Exception):
    """Raised when a requested conversation does not exist or caller is unauthorized."""
    pass


class ActiveTurnConflictError(Exception):
    """Raised when another generation turn is currently active in the same conversation."""
    pass


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused with a different request payload."""
    pass


def format_conversation_title(text: str, max_length: int = 40) -> str:
    """Derives a clean summary title from the first prompt."""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return "New Conversation"
    if len(cleaned) <= max_length:
        return cleaned
    return f"{cleaned[:max_length].rstrip()}..."


class PostgresConversationRepository:
    """
    Authoritative database repository for conversations, turns, and messages.
    Enforces tenant principal isolation, idempotency, and turn state transitions.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create_conversation(
        self,
        conversation_id: Optional[str],
        namespace: str,
        owner_principal_id: str,
        title: Optional[str] = None,
    ) -> ConversationRecord:
        """Creates a new conversation record owned by the authenticated principal."""
        cid = (conversation_id or str(uuid.uuid4())).strip()
        conv_title = (title or "New Conversation").strip() or "New Conversation"

        query = """
            INSERT INTO conversations (
                id, cache_namespace, owner_principal_id, title,
                created_at, updated_at, last_message_at
            ) VALUES ($1, $2, $3, $4, NOW(), NOW(), NOW())
            RETURNING id, cache_namespace, owner_principal_id, title,
                      created_at, updated_at, last_message_at
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, cid, namespace, owner_principal_id, conv_title)
            return ConversationRecord(
                id=row["id"],
                cache_namespace=row["cache_namespace"],
                owner_principal_id=row["owner_principal_id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                last_message_at=row["last_message_at"],
            )

    async def list_conversations(
        self,
        owner_principal_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[ConversationRecord], int]:
        """Lists conversations belonging to the authenticated principal, ordered newest first."""
        list_query = """
            SELECT id, cache_namespace, owner_principal_id, title,
                   created_at, updated_at, last_message_at
            FROM conversations
            WHERE owner_principal_id = $1
            ORDER BY updated_at DESC
            LIMIT $2 OFFSET $3
        """
        count_query = "SELECT COUNT(*) FROM conversations WHERE owner_principal_id = $1"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(list_query, owner_principal_id, limit, offset)
            total = await conn.fetchval(count_query, owner_principal_id)

            records = [
                ConversationRecord(
                    id=r["id"],
                    cache_namespace=r["cache_namespace"],
                    owner_principal_id=r["owner_principal_id"],
                    title=r["title"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    last_message_at=r["last_message_at"],
                )
                for r in rows
            ]
            return records, int(total or 0)

    async def get_conversation(
        self,
        conversation_id: str,
        owner_principal_id: str,
    ) -> Optional[ConversationRecord]:
        """Retrieves conversation metadata if accessible to the authenticated principal."""
        query = """
            SELECT id, cache_namespace, owner_principal_id, title,
                   created_at, updated_at, last_message_at
            FROM conversations
            WHERE id = $1 AND owner_principal_id = $2
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, conversation_id, owner_principal_id)
            if not row:
                return None
            return ConversationRecord(
                id=row["id"],
                cache_namespace=row["cache_namespace"],
                owner_principal_id=row["owner_principal_id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                last_message_at=row["last_message_at"],
            )

    async def delete_conversation(
        self,
        conversation_id: str,
        owner_principal_id: str,
    ) -> bool:
        """Deletes a conversation and cascades turns and messages. Validates ownership."""
        query = "DELETE FROM conversations WHERE id = $1 AND owner_principal_id = $2"
        async with self.pool.acquire() as conn:
            result = await conn.execute(query, conversation_id, owner_principal_id)
            return "DELETE 1" in result

    async def get_conversation_messages(
        self,
        conversation_id: str,
        owner_principal_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[ConversationMessageRecord], int]:
        """Retrieves ordered messages for an authenticated conversation with pagination."""
        # Validate ownership first
        conv = await self.get_conversation(conversation_id, owner_principal_id)
        if not conv:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found.")

        list_query = """
            SELECT id, conversation_id, turn_id, sequence_number, role,
                   content, created_at, metadata
            FROM conversation_messages
            WHERE conversation_id = $1
            ORDER BY sequence_number ASC
            LIMIT $2 OFFSET $3
        """
        count_query = "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id = $1"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(list_query, conversation_id, limit, offset)
            total = await conn.fetchval(count_query, conversation_id)

            messages = [
                ConversationMessageRecord(
                    id=r["id"],
                    conversation_id=r["conversation_id"],
                    turn_id=r["turn_id"],
                    sequence_number=r["sequence_number"],
                    role=r["role"],
                    content=r["content"],
                    created_at=r["created_at"],
                    metadata=json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {}),
                )
                for r in rows
            ]
            return messages, int(total or 0)

    async def get_validated_completed_history(
        self,
        conversation_id: str,
        owner_principal_id: str,
    ) -> Tuple[List[ChatMessage], bool]:
        """
        Retrieves all messages belonging to completed turns in chronological order,
        verifying message sequence continuity across all persisted messages and completed
        turn pair completeness and strict adjacency.
        Returns: (messages, is_history_incomplete)
        """
        # Query ALL persisted messages for this conversation across all turn statuses
        query = """
            SELECT m.sequence_number, m.turn_id, m.role, m.content, t.status AS turn_status
            FROM conversation_messages m
            JOIN conversation_turns t ON m.turn_id = t.id
            JOIN conversations c ON m.conversation_id = c.id
            WHERE m.conversation_id = $1
              AND c.owner_principal_id = $2
            ORDER BY m.sequence_number ASC
        """
        async with self.pool.acquire() as conn:
            all_rows = await conn.fetch(query, conversation_id, owner_principal_id)

        if not all_rows:
            return [], False

        # 1. Contiguous sequence validation across ALL persisted messages (1, 2, 3...)
        expected_seq = 1
        for r in all_rows:
            if r["sequence_number"] != expected_seq:
                logger.warning(
                    "Conversation '%s' sequence continuity gap in message ledger: expected %d, got %d",
                    conversation_id,
                    expected_seq,
                    r["sequence_number"],
                )
                return [], True
            expected_seq += 1

        # 2. Complete turn validation: for each completed turn, enforce strictly adjacent pairs (user, assistant)
        completed_turns_dict: Dict[str, List[Dict[str, Any]]] = {}
        for r in all_rows:
            if r["turn_status"] == "completed":
                completed_turns_dict.setdefault(r["turn_id"], []).append(r)

        for tid, t_msgs in completed_turns_dict.items():
            if len(t_msgs) != 2:
                logger.warning(
                    "Conversation '%s' completed turn '%s' has %d messages, expected 2",
                    conversation_id,
                    tid,
                    len(t_msgs),
                )
                return [], True
            u_msg, a_msg = t_msgs[0], t_msgs[1]
            if u_msg["role"] != "user" or a_msg["role"] != "assistant":
                logger.warning(
                    "Conversation '%s' completed turn '%s' malformed roles: user=%s, assistant=%s",
                    conversation_id,
                    tid,
                    u_msg["role"],
                    a_msg["role"],
                )
                return [], True
            if a_msg["sequence_number"] != u_msg["sequence_number"] + 1:
                logger.warning(
                    "Conversation '%s' completed turn '%s' non-adjacent sequences: user=%d, assistant=%d",
                    conversation_id,
                    tid,
                    u_msg["sequence_number"],
                    a_msg["sequence_number"],
                )
                return [], True

        # 3. Construct LLM context messages exclusively from completed turns in chronological order
        chat_messages = [
            ChatMessage(role=r["role"], content=r["content"])
            for r in all_rows
            if r["turn_status"] == "completed"
        ]
        return chat_messages, False

    async def get_completed_history(
        self,
        conversation_id: str,
        owner_principal_id: str,
    ) -> List[ChatMessage]:
        """
        Retrieves all messages belonging to completed turns in chronological order.
        Strictly excludes pending, failed, or cancelled turns from context reconstruction.
        """
        messages, _ = await self.get_validated_completed_history(conversation_id, owner_principal_id)
        return messages

    async def begin_turn(
        self,
        conversation_id: str,
        owner_principal_id: str,
        idempotency_key: str,
        user_content: str,
        request_hash: Optional[str] = None,
        lease_duration_seconds: int = 120,
    ) -> Tuple[ConversationTurnRecord, Optional[ConversationMessageRecord], bool]:
        """
        Begins a new conversation turn transaction.
        1. Locks conversation row for update.
        2. Checks for existing idempotency key.
           - If already completed: returns (turn, assistant_message, is_replay=True).
           - If pending and lease unexpired: raises ActiveTurnConflictError (HTTP 409).
           - If pending and lease expired: marks failed (LEASE_TIMEOUT_RECOVERED) and retries.
           - Validates request_hash match on reuse (raises IdempotencyConflictError if mismatched).
        3. Enforces that no other turn in this conversation is currently pending (recovers expired leases).
        4. Inserts pending turn and user message record in sequence with lease.
        5. Updates conversation last_message_at and auto-sets title on first message.
        6. Returns (turn, user_message, is_replay=False).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 1. Authorize and lock conversation
                conv_row = await conn.fetchrow(
                    "SELECT id, title FROM conversations WHERE id = $1 AND owner_principal_id = $2 FOR UPDATE",
                    conversation_id,
                    owner_principal_id,
                )
                if not conv_row:
                    raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found.")

                now_utc = datetime.now(timezone.utc)

                # 2. Check for matching idempotency key in this conversation
                existing_turn = await conn.fetchrow(
                    "SELECT id, conversation_id, idempotency_key, status, created_at, completed_at, error_code, request_hash, lease_expires_at "
                    "FROM conversation_turns WHERE conversation_id = $1 AND idempotency_key = $2",
                    conversation_id,
                    idempotency_key,
                )

                if existing_turn:
                    # Enforce request fingerprint binding on idempotency key reuse
                    if existing_turn["request_hash"] is None:
                        raise IdempotencyConflictError(
                            f"Idempotency key '{idempotency_key}' refers to an unverified legacy turn without request hash."
                        )
                    if (
                        request_hash
                        and existing_turn["request_hash"] != request_hash
                    ):
                        raise IdempotencyConflictError(
                            f"Idempotency key '{idempotency_key}' was previously used with a different request payload."
                        )

                    t_status = existing_turn["status"]
                    if t_status == "completed":
                        # Retrieve completed assistant message for idempotent return
                        asst_row = await conn.fetchrow(
                            "SELECT id, conversation_id, turn_id, sequence_number, role, content, created_at, metadata "
                            "FROM conversation_messages WHERE turn_id = $1 AND role = 'assistant'",
                            existing_turn["id"],
                        )
                        asst_msg = None
                        if asst_row:
                            asst_msg = ConversationMessageRecord(
                                id=asst_row["id"],
                                conversation_id=asst_row["conversation_id"],
                                turn_id=asst_row["turn_id"],
                                sequence_number=asst_row["sequence_number"],
                                role=asst_row["role"],
                                content=asst_row["content"],
                                created_at=asst_row["created_at"],
                                metadata=json.loads(asst_row["metadata"]) if isinstance(asst_row["metadata"], str) else (asst_row["metadata"] or {}),
                            )
                        turn_rec = ConversationTurnRecord(
                            id=existing_turn["id"],
                            conversation_id=existing_turn["conversation_id"],
                            idempotency_key=existing_turn["idempotency_key"],
                            status=existing_turn["status"],
                            created_at=existing_turn["created_at"],
                            completed_at=existing_turn["completed_at"],
                            error_code=existing_turn["error_code"],
                            request_hash=existing_turn["request_hash"],
                            lease_expires_at=existing_turn["lease_expires_at"],
                        )
                        return turn_rec, asst_msg, True

                    elif t_status == "pending":
                        # Check lease expiry for pending turn recovery
                        is_lease_expired = (
                            existing_turn["lease_expires_at"] is not None
                            and existing_turn["lease_expires_at"] <= now_utc
                        )
                        if is_lease_expired:
                            logger.warning(
                                "Recovering expired pending turn '%s' (idempotency_key='%s') in conversation '%s'",
                                existing_turn["id"],
                                idempotency_key,
                                conversation_id,
                            )
                            await conn.execute("DELETE FROM conversation_messages WHERE turn_id = $1", existing_turn["id"])
                            await conn.execute("DELETE FROM conversation_turns WHERE id = $1", existing_turn["id"])
                        else:
                            raise ActiveTurnConflictError(
                                "An active turn with this idempotency key is already in progress."
                            )
                    else:
                        # If failed or cancelled, delete existing turn and messages to retry cleanly
                        await conn.execute("DELETE FROM conversation_messages WHERE turn_id = $1", existing_turn["id"])
                        await conn.execute("DELETE FROM conversation_turns WHERE id = $1", existing_turn["id"])

                # 3. Verify no OTHER turn is currently active in this conversation, recovering any expired leases
                pending_turns = await conn.fetch(
                    "SELECT id, lease_expires_at FROM conversation_turns WHERE conversation_id = $1 AND status = 'pending'",
                    conversation_id,
                )
                for p_turn in pending_turns:
                    is_p_expired = (
                        p_turn["lease_expires_at"] is not None
                        and p_turn["lease_expires_at"] <= now_utc
                    )
                    if is_p_expired:
                        logger.warning(
                            "Recovering expired pending turn '%s' in conversation '%s'",
                            p_turn["id"],
                            conversation_id,
                        )
                        await conn.execute(
                            "UPDATE conversation_turns SET status = 'failed', completed_at = NOW(), "
                            "error_code = 'LEASE_TIMEOUT_RECOVERED', lease_expires_at = NULL WHERE id = $1",
                            p_turn["id"],
                        )
                    else:
                        raise ActiveTurnConflictError(
                            "Another turn is currently in progress for this conversation."
                        )

                # 4. Allocate next message sequence number atomically
                next_seq = await conn.fetchval(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM conversation_messages WHERE conversation_id = $1",
                    conversation_id,
                )

                turn_id = str(uuid.uuid4())
                await conn.execute(
                    """
                    INSERT INTO conversation_turns (
                        id, conversation_id, idempotency_key, status, created_at, request_hash, lease_expires_at
                    ) VALUES ($1, $2, $3, 'pending', NOW(), $4, NOW() + ($5 * INTERVAL '1 second'))
                    """,
                    turn_id,
                    conversation_id,
                    idempotency_key,
                    request_hash,
                    lease_duration_seconds,
                )

                user_msg_id = str(uuid.uuid4())
                user_row = await conn.fetchrow(
                    "INSERT INTO conversation_messages (id, conversation_id, turn_id, sequence_number, role, content, created_at) "
                    "VALUES ($1, $2, $3, $4, 'user', $5, NOW()) "
                    "RETURNING id, conversation_id, turn_id, sequence_number, role, content, created_at, metadata",
                    user_msg_id,
                    conversation_id,
                    turn_id,
                    next_seq,
                    user_content,
                )

                # 5. Update conversation timestamps and title if first user message
                if conv_row["title"] in ("New Conversation", "New Chat", ""):
                    new_title = format_conversation_title(user_content)
                    await conn.execute(
                        "UPDATE conversations SET title = $1, updated_at = NOW(), last_message_at = NOW() WHERE id = $2",
                        new_title,
                        conversation_id,
                    )
                else:
                    await conn.execute(
                        "UPDATE conversations SET updated_at = NOW(), last_message_at = NOW() WHERE id = $1",
                        conversation_id,
                    )

                turn_record = ConversationTurnRecord(
                    id=turn_id,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                    status="pending",
                    created_at=datetime.now(timezone.utc),
                    request_hash=request_hash,
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_duration_seconds),
                )
                user_msg_record = ConversationMessageRecord(
                    id=user_row["id"],
                    conversation_id=user_row["conversation_id"],
                    turn_id=user_row["turn_id"],
                    sequence_number=user_row["sequence_number"],
                    role=user_row["role"],
                    content=user_row["content"],
                    created_at=user_row["created_at"],
                    metadata={},
                )
                return turn_record, user_msg_record, False

    async def complete_turn(
        self,
        turn_id: str,
        assistant_content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ConversationMessageRecord:
        """
        Marks an active turn completed and persists the assistant response atomically.
        Guards against completing turns that are no longer in pending status (e.g. lease expired or recovered).
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                turn_row = await conn.fetchrow(
                    "SELECT id, conversation_id, status FROM conversation_turns WHERE id = $1 FOR UPDATE",
                    turn_id,
                )
                if not turn_row:
                    raise ConversationNotFoundError(f"Turn '{turn_id}' not found.")

                if turn_row["status"] != "pending":
                    raise ActiveTurnConflictError(
                        f"Cannot complete turn '{turn_id}': status is '{turn_row['status']}', expected 'pending'."
                    )

                conv_id = turn_row["conversation_id"]

                next_seq = await conn.fetchval(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM conversation_messages WHERE conversation_id = $1",
                    conv_id,
                )

                meta_json = json.dumps(metadata or {})
                asst_msg_id = str(uuid.uuid4())
                msg_row = await conn.fetchrow(
                    "INSERT INTO conversation_messages ("
                    "  id, conversation_id, turn_id, sequence_number, role, content, created_at, metadata"
                    ") VALUES ($1, $2, $3, $4, 'assistant', $5, NOW(), $6::jsonb) "
                    "RETURNING id, conversation_id, turn_id, sequence_number, role, content, created_at, metadata",
                    asst_msg_id,
                    conv_id,
                    turn_id,
                    next_seq,
                    assistant_content,
                    meta_json,
                )

                res = await conn.execute(
                    "UPDATE conversation_turns SET status = 'completed', completed_at = NOW(), error_code = NULL, lease_expires_at = NULL "
                    "WHERE id = $1 AND status = 'pending'",
                    turn_id,
                )
                if "UPDATE 1" not in res:
                    raise ActiveTurnConflictError(f"Turn '{turn_id}' was not in pending status during completion.")

                await conn.execute(
                    "UPDATE conversations SET updated_at = NOW(), last_message_at = NOW() WHERE id = $1",
                    conv_id,
                )

                return ConversationMessageRecord(
                    id=msg_row["id"],
                    conversation_id=msg_row["conversation_id"],
                    turn_id=msg_row["turn_id"],
                    sequence_number=msg_row["sequence_number"],
                    role=msg_row["role"],
                    content=msg_row["content"],
                    created_at=msg_row["created_at"],
                    metadata=metadata or {},
                )

    async def fail_turn(self, turn_id: str, error_code: str = "GENERATION_FAILED") -> None:
        """Marks an active turn as failed and releases the active turn lock."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversation_turns SET status = 'failed', completed_at = NOW(), error_code = $2, lease_expires_at = NULL "
                "WHERE id = $1 AND status = 'pending'",
                turn_id,
                error_code[:64],
            )

    async def cancel_turn(self, turn_id: str) -> None:
        """Marks an active turn as cancelled and releases the active turn lock."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE conversation_turns SET status = 'cancelled', completed_at = NOW(), lease_expires_at = NULL "
                "WHERE id = $1 AND status = 'pending'",
                turn_id,
            )

    async def renew_turn_lease(self, turn_id: str, additional_seconds: int = 120) -> bool:
        """Extends the lease expiration of an active pending turn."""
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE conversation_turns SET lease_expires_at = NOW() + ($2 * INTERVAL '1 second') "
                "WHERE id = $1 AND status = 'pending'",
                turn_id,
                additional_seconds,
            )
            return "UPDATE 1" in res
