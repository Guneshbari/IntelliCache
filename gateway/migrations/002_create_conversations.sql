-- Migration 002: Create persistent conversation, turn, and message tables
-- Authoritative server-side conversation history storage for IntelliCache

CREATE TABLE IF NOT EXISTS conversations (
    id VARCHAR(64) PRIMARY KEY,
    cache_namespace VARCHAR(128) NOT NULL DEFAULT 'default',
    owner_principal_id VARCHAR(64) NOT NULL,
    title VARCHAR(255) NOT NULL DEFAULT 'New Conversation',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_owner 
    ON conversations (owner_principal_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversations_ns_owner 
    ON conversations (cache_namespace, owner_principal_id);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id VARCHAR(64) PRIMARY KEY,
    conversation_id VARCHAR(64) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error_code VARCHAR(64),
    CONSTRAINT uq_conversation_turn_idempotency UNIQUE (conversation_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_conv 
    ON conversation_turns (conversation_id, created_at DESC);

-- Enforce at most one active (pending) turn per conversation at database level
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_active_turn 
    ON conversation_turns (conversation_id) 
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS conversation_messages (
    id VARCHAR(64) PRIMARY KEY,
    conversation_id VARCHAR(64) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_id VARCHAR(64) NOT NULL REFERENCES conversation_turns(id) ON DELETE CASCADE,
    sequence_number INT NOT NULL,
    role VARCHAR(32) NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_conversation_message_sequence UNIQUE (conversation_id, sequence_number)
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_lookup 
    ON conversation_messages (conversation_id, sequence_number ASC);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_turn 
    ON conversation_messages (turn_id);
