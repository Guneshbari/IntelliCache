-- Migration 007: PostgreSQL-backed shared runtime semantic serving control state (Stage 3.8B)
-- Coordinates the runtime semantic-serving kill switch and per-namespace overrides
-- across multiple gateway workers without relying on process-local state alone.
-- Preserves fail-closed safe defaults (runtime_enabled = FALSE).
--
-- Documented Rollback Procedure (manual DBA intervention only):
--   DROP TABLE IF EXISTS semantic_runtime_namespace_overrides;
--   DROP TABLE IF EXISTS semantic_runtime_control;

CREATE TABLE IF NOT EXISTS semantic_runtime_control (
    control_key VARCHAR(64) PRIMARY KEY,
    runtime_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(64) NOT NULL DEFAULT 'startup',
    last_reason VARCHAR(256)
);

CREATE TABLE IF NOT EXISTS semantic_runtime_namespace_overrides (
    namespace VARCHAR(128) PRIMARY KEY,
    runtime_enabled BOOLEAN NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(64) NOT NULL DEFAULT 'admin',
    reason VARCHAR(256)
);
