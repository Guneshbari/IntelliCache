-- Stage 3.3: Semantic Cache Shadow Evaluations Storage
-- Stores observed production queries and their hypothetical semantic cache outcomes
-- without mutating production cache entries or vector records.

CREATE TABLE IF NOT EXISTS shadow_evaluations (
    id BIGSERIAL PRIMARY KEY,
    request_id VARCHAR(64) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_hash VARCHAR(64) NOT NULL,
    cache_namespace VARCHAR(128) NOT NULL DEFAULT 'default',
    provider VARCHAR(64) NOT NULL,
    requested_model VARCHAR(128) NOT NULL,
    task_kind VARCHAR(64) NOT NULL DEFAULT 'generation',
    eligibility_status VARCHAR(32) NOT NULL,
    eligibility_rejection_reason VARCHAR(64),
    embedding_model VARCHAR(128),
    retrieval_status VARCHAR(32) NOT NULL,
    candidate_count INT NOT NULL DEFAULT 0,
    top_similarity DOUBLE PRECISION,
    runner_up_similarity DOUBLE PRECISION,
    similarity_margin DOUBLE PRECISION,
    compatibility_validation_result VARCHAR(32),
    compatibility_rejection_reason VARCHAR(64),
    ambiguity_decision VARCHAR(32),
    hypothetical_cache_outcome VARCHAR(64) NOT NULL,
    actual_response_source VARCHAR(32) NOT NULL DEFAULT 'none',
    candidate_response_matched BOOLEAN,
    shadow_execution_duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    embedding_duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    retrieval_duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    policy_evaluation_duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    failure_category VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Integrity & Enumerable Constraints
    CONSTRAINT chk_shadow_candidate_count CHECK (candidate_count >= 0),
    CONSTRAINT chk_shadow_exec_duration CHECK (shadow_execution_duration_ms >= 0.0),
    CONSTRAINT chk_shadow_embed_duration CHECK (embedding_duration_ms >= 0.0),
    CONSTRAINT chk_shadow_retrieval_duration CHECK (retrieval_duration_ms >= 0.0),
    CONSTRAINT chk_shadow_policy_duration CHECK (policy_evaluation_duration_ms >= 0.0),
    CONSTRAINT chk_shadow_outcome CHECK (
        hypothetical_cache_outcome IN (
            'eligible_candidate_hit',
            'eligible_no_candidate',
            'ineligible_request',
            'threshold_rejection',
            'ambiguity_rejection',
            'compatibility_rejection',
            'embedding_failure',
            'vector_store_failure',
            'shadow_timeout',
            'shadow_execution_failure'
        )
    ),
    CONSTRAINT chk_shadow_response_source CHECK (
        actual_response_source IN (
            'exact_cache',
            'upstream_provider',
            'bypass',
            'failed',
            'none'
        )
    )
);

-- Indices for namespace-scoped querying, operational analysis, and bounded retention cleanup
CREATE INDEX IF NOT EXISTS idx_shadow_eval_ns_timestamp
    ON shadow_evaluations (cache_namespace, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_shadow_eval_outcome
    ON shadow_evaluations (cache_namespace, hypothetical_cache_outcome);

CREATE INDEX IF NOT EXISTS idx_shadow_eval_request_hash
    ON shadow_evaluations (cache_namespace, request_hash);

CREATE INDEX IF NOT EXISTS idx_shadow_eval_retention
    ON shadow_evaluations (timestamp);
