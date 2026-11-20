-- Migration 003 (Vector Store): Create shadow_vector_records table for isolated shadow candidate ingestion
-- Enables shadow evaluation mode to ingest and retrieve semantic candidates without mutating production vector_records or exact cache_entries.

CREATE TABLE IF NOT EXISTS shadow_vector_records (
    id BIGSERIAL PRIMARY KEY,
    cache_namespace VARCHAR(128) NOT NULL DEFAULT 'default',
    fingerprint VARCHAR(64) NOT NULL,
    query_text TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    vector vector({{VECTOR_DIMENSIONS}}) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shadow_vector_ns_fp_model UNIQUE (cache_namespace, fingerprint, model)
);

CREATE INDEX IF NOT EXISTS idx_shadow_vector_records_ns_model
    ON shadow_vector_records (cache_namespace, model);

CREATE INDEX IF NOT EXISTS idx_shadow_vector_records_lookup
    ON shadow_vector_records (cache_namespace, fingerprint);

CREATE INDEX IF NOT EXISTS idx_shadow_vector_records_hnsw
    ON shadow_vector_records
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
