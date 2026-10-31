-- Migration 002 (Vector Store): Create HNSW index for cosine distance nearest-neighbour search
-- Provides approximate nearest-neighbour (ANN) retrieval using pgvector cosine distance operator (<=>)

CREATE INDEX IF NOT EXISTS idx_vector_records_embedding_hnsw
ON vector_records
USING hnsw (vector vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
