-- Migration 008: PostgreSQL-backed distributed SingleFlight coordination leases (Stage 3.9)
-- Coordinates equivalent in-flight cold-cache and semantic lookup requests across independent
-- gateway worker processes using atomic lease claims, unguessable owner tokens, monotonic fencing
-- tokens, and bounded lease expirations.
-- Does NOT store raw prompts, system instructions, credentials, or generated response bodies.
--
-- Documented Rollback Procedure (manual DBA intervention only):
--   DROP INDEX IF EXISTS idx_dsf_leases_status_updated;
--   DROP INDEX IF EXISTS idx_dsf_leases_expires_at;
--   DROP TABLE IF EXISTS distributed_singleflight_leases;

CREATE TABLE IF NOT EXISTS distributed_singleflight_leases (
    coordination_key VARCHAR(64) PRIMARY KEY,
    cache_namespace VARCHAR(128) NOT NULL,
    visibility VARCHAR(32) NOT NULL,
    owner_token VARCHAR(64) NOT NULL,
    fencing_token BIGINT NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    outcome_type VARCHAR(32),
    failure_code VARCHAR(64),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT chk_dsf_status CHECK (status IN ('pending', 'completed', 'failed', 'expired')),
    CONSTRAINT chk_dsf_outcome CHECK (outcome_type IS NULL OR outcome_type IN ('exact', 'semantic')),
    CONSTRAINT chk_dsf_visibility CHECK (visibility IN ('private', 'tenant_shared'))
);

CREATE INDEX IF NOT EXISTS idx_dsf_leases_expires_at
    ON distributed_singleflight_leases (lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_dsf_leases_status_updated
    ON distributed_singleflight_leases (status, updated_at);
