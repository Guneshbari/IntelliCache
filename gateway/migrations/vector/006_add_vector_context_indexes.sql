-- Migration 006 (Vector Store): Add JSONB context-class and context-fingerprint indexes
-- Optimizes Stage 3.6 context-aware semantic retrieval on vector_records and semantic_variant_records
-- so multi-conversation / tenant-scoped lookups avoid brute-force metadata scans.

CREATE INDEX IF NOT EXISTS idx_vector_records_context_scope
    ON vector_records (
        cache_namespace,
        model,
        visibility,
        (metadata->'context_contract'->>'context_class'),
        (metadata->'context_contract'->>'context_fingerprint')
    );

CREATE INDEX IF NOT EXISTS idx_vector_records_private_context_scope
    ON vector_records (
        cache_namespace,
        model,
        owner_principal_id,
        (metadata->'context_contract'->>'context_class'),
        (metadata->'context_contract'->>'context_fingerprint')
    )
    WHERE visibility = 'private';

CREATE INDEX IF NOT EXISTS idx_semantic_variant_records_context_scope
    ON semantic_variant_records (
        cache_namespace,
        model,
        visibility,
        (metadata->'context_contract'->>'context_class'),
        (metadata->'context_contract'->>'context_fingerprint')
    );

CREATE INDEX IF NOT EXISTS idx_semantic_variant_records_private_context_scope
    ON semantic_variant_records (
        cache_namespace,
        model,
        owner_principal_id,
        (metadata->'context_contract'->>'context_class'),
        (metadata->'context_contract'->>'context_fingerprint')
    )
    WHERE visibility = 'private';
