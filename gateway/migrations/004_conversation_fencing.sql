-- Migration 004: Conversation turns lease fencing token
-- Adds lease_token to conversation_turns to fence execution attempts and prevent stale completions

ALTER TABLE conversation_turns
    ADD COLUMN IF NOT EXISTS lease_token VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_conversation_turns_fencing
    ON conversation_turns (id, lease_token)
    WHERE status = 'pending';
