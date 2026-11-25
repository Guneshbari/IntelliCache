-- Migration 006: Add per-principal privacy scope columns to cache_entries (Stage 3.5.4)
-- Adds explicit visibility and owner_principal_id columns.
-- Legacy rows default to 'unknown' (fail-closed) and are NEVER implicitly treated as 'tenant_shared'.
--
-- Documented Rollback Procedure (manual DBA intervention only):
--   DROP INDEX IF EXISTS idx_cache_entries_scope_expires;
--   DROP INDEX IF EXISTS idx_cache_entries_scope_owner;
--   ALTER TABLE cache_entries DROP COLUMN IF EXISTS owner_principal_id;
--   ALTER TABLE cache_entries DROP COLUMN IF EXISTS visibility;

ALTER TABLE cache_entries
    ADD COLUMN IF NOT EXISTS visibility VARCHAR(32) NOT NULL DEFAULT 'unknown';

ALTER TABLE cache_entries
    ADD COLUMN IF NOT EXISTS owner_principal_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_cache_entries_scope_owner
    ON cache_entries (cache_namespace, visibility, owner_principal_id);

CREATE INDEX IF NOT EXISTS idx_cache_entries_scope_expires
    ON cache_entries (cache_namespace, visibility, expires_at);
