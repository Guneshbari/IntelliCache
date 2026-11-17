-- Migration 001 (Vector Store): Create vector_records table with pgvector support
-- Persists vector embeddings linked to exact cache entries with ON DELETE CASCADE
-- NOTE: Vector dimension defaults to 768 (nomic-embed-text) in raw SQL.
-- When applied via the IntelliCache migration runner (run_vector_migrations),
-- the vector(768) type is dynamically replaced with vector({dimensions}) matching
-- the configured embedding model (e.g. vector(384) for FastEmbed).
-- For manual psql execution with non-768 models, replace vector(768) with vector({dimensions}).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS vector_records (
    id BIGSERIAL PRIMARY KEY,
    cache_namespace VARCHAR(128) NOT NULL DEFAULT 'default',
    fingerprint VARCHAR(64) NOT NULL,
    query_text TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    vector vector(768) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_vector_ns_fp_model UNIQUE (cache_namespace, fingerprint, model),
    CONSTRAINT fk_vector_cache_entry FOREIGN KEY (cache_namespace, fingerprint)
        REFERENCES cache_entries(cache_namespace, fingerprint)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_vector_records_ns_model
    ON vector_records (cache_namespace, model);

CREATE INDEX IF NOT EXISTS idx_vector_records_lookup
    ON vector_records (cache_namespace, fingerprint);
