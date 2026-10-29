-- Migration 001: Create cache_entries table and performance indexes
-- Centralized exact-cache storage for IntelliCache

CREATE TABLE IF NOT EXISTS cache_entries (
    id BIGSERIAL PRIMARY KEY,
    cache_namespace VARCHAR(128) NOT NULL DEFAULT 'default',
    fingerprint VARCHAR(64) NOT NULL,
    response TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    provider VARCHAR(64) NOT NULL DEFAULT 'groq',
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens INT NOT NULL DEFAULT 0,
    latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    hit_count INT NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    CONSTRAINT uq_namespace_fingerprint UNIQUE (cache_namespace, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_cache_lookup 
    ON cache_entries (cache_namespace, fingerprint);

CREATE INDEX IF NOT EXISTS idx_cache_expires_at 
    ON cache_entries (expires_at);

CREATE INDEX IF NOT EXISTS idx_cache_last_accessed 
    ON cache_entries (cache_namespace, last_accessed);
