-- Migration 004 (Vector Store): Create semantic_variant_records table for atomic variant coexistence
-- Enables multiple distinct compatibility/schema variants sharing the same exact-cache fingerprint
-- to coexist atomically with independent response snapshots and vector embeddings, while preserving
-- ON DELETE CASCADE cleanup from cache_entries and backwards compatibility for vector_records.

CREATE TABLE IF NOT EXISTS semantic_variant_records (
    id BIGSERIAL PRIMARY KEY,
    cache_namespace VARCHAR(128) NOT NULL DEFAULT 'default',
    fingerprint VARCHAR(64) NOT NULL,
    variant_key VARCHAR(80) NOT NULL,
    query_text TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    vector vector({{VECTOR_DIMENSIONS}}) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    cached_response TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_semantic_variant_ns_fp_model_vk UNIQUE (cache_namespace, fingerprint, model, variant_key),
    CONSTRAINT fk_semantic_variant_cache_entry FOREIGN KEY (cache_namespace, fingerprint)
        REFERENCES cache_entries(cache_namespace, fingerprint)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_semantic_variant_records_ns_model
    ON semantic_variant_records (cache_namespace, model);

CREATE INDEX IF NOT EXISTS idx_semantic_variant_records_lookup
    ON semantic_variant_records (cache_namespace, fingerprint, variant_key);

CREATE INDEX IF NOT EXISTS idx_semantic_variant_records_hnsw
    ON semantic_variant_records
    USING hnsw (vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
