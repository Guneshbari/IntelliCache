-- Migration 005 (Vector Store): Add per-principal privacy scope columns (Stage 3.5.4)
-- Adds explicit visibility and owner_principal_id columns to vector_records,
-- semantic_variant_records, and shadow_vector_records.
-- Legacy rows default to 'unknown' (fail-closed) and are NEVER implicitly treated as 'tenant_shared'.
--
-- Documented Rollback Procedure (manual DBA intervention only):
--   DROP INDEX IF EXISTS idx_shadow_vector_records_scope;
--   DROP INDEX IF EXISTS idx_semantic_variant_records_scope;
--   DROP INDEX IF EXISTS idx_vector_records_scope;
--   ALTER TABLE IF EXISTS shadow_vector_records DROP COLUMN IF EXISTS owner_principal_id;
--   ALTER TABLE IF EXISTS shadow_vector_records DROP COLUMN IF EXISTS visibility;
--   ALTER TABLE IF EXISTS semantic_variant_records DROP COLUMN IF EXISTS owner_principal_id;
--   ALTER TABLE IF EXISTS semantic_variant_records DROP COLUMN IF EXISTS visibility;
--   ALTER TABLE vector_records DROP COLUMN IF EXISTS owner_principal_id;
--   ALTER TABLE vector_records DROP COLUMN IF EXISTS visibility;

ALTER TABLE vector_records
    ADD COLUMN IF NOT EXISTS visibility VARCHAR(32) NOT NULL DEFAULT 'unknown';

ALTER TABLE vector_records
    ADD COLUMN IF NOT EXISTS owner_principal_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_vector_records_scope
    ON vector_records (cache_namespace, model, visibility, owner_principal_id);

ALTER TABLE IF EXISTS semantic_variant_records
    ADD COLUMN IF NOT EXISTS visibility VARCHAR(32) NOT NULL DEFAULT 'unknown';

ALTER TABLE IF EXISTS semantic_variant_records
    ADD COLUMN IF NOT EXISTS owner_principal_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_semantic_variant_records_scope
    ON semantic_variant_records (cache_namespace, model, visibility, owner_principal_id);

ALTER TABLE IF EXISTS shadow_vector_records
    ADD COLUMN IF NOT EXISTS visibility VARCHAR(32) NOT NULL DEFAULT 'unknown';

ALTER TABLE IF EXISTS shadow_vector_records
    ADD COLUMN IF NOT EXISTS owner_principal_id VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_shadow_vector_records_scope
    ON shadow_vector_records (cache_namespace, model, visibility, owner_principal_id);
