-- Migration 003: Conversation turns hardening (request_hash and lease_expires_at)
-- Ensures idempotency keys are bound to request payloads and pending turns have crash recovery leases

ALTER TABLE conversation_turns
    ADD COLUMN IF NOT EXISTS request_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_conversation_turns_lease
    ON conversation_turns (conversation_id, lease_expires_at)
    WHERE status = 'pending';
