"""
Domain 3: Databases and Vector Search (21 intent groups).
Covers PostgreSQL, pgvector, Redis, SQLite, DynamoDB, Elasticsearch, and Cassandra.
"""

from typing import Any, Dict, List

DB_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 43. PostgreSQL MVCC and Vacuum
    {
        "intent_group_id": "ig_db_postgres_mvcc_vacuum",
        "domain": "databases_and_vector_search",
        "subtopic": "PostgreSQL",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "postgresql"}},
        "canonical_query": "How does PostgreSQL Multi-Version Concurrency Control (MVCC) create dead tuples and why is VACUUM required?",
        "canonical_response": "In PostgreSQL MVCC, an UPDATE or DELETE does not overwrite existing disk data; instead, it writes a new tuple version and sets xmax on the old tuple. Over time, expired dead tuples accumulate, causing table bloat and slower scans. VACUUM scans pages to mark dead tuple space as reusable for future inserts.",
        "positive_paraphrase_1": {
            "query": "Explain how PostgreSQL MVCC row updates generate dead tuples and why autovacuum is essential.",
            "reason": "Direct paraphrase explaining MVCC tuple versioning and the necessity of VACUUM.",
        },
        "positive_paraphrase_2": {
            "query": "Why do frequent UPDATE queries in PostgreSQL cause disk bloat that requires VACUUM maintenance?",
            "reason": "Syntactic variation focusing on UPDATE disk bloat and vacuuming.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you run VACUUM FULL to reclaim physical disk space back to the OS in PostgreSQL?",
            "response": "Run `VACUUM FULL table_name;`, which writes an entirely new copy of the table without bloat and acquires an exclusive ACCESS EXCLUSIVE lock on the table.",
            "reason": "Asks how to execute VACUUM FULL and its locking impact rather than explaining MVCC mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write SQL queries to inspect dead tuple counts and bloat ratio from pg_stat_user_tables.",
            "response": "```sql\nSELECT relname, n_dead_tup, n_live_tup, round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 2) AS dead_pct\nFROM pg_stat_user_tables ORDER BY n_dead_tup DESC;\n```",
            "reason": "Requests specific monitoring SQL queries rather than explanation of MVCC architecture.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does MySQL InnoDB undo log MVCC compare to PostgreSQL append-only dead tuple MVCC?",
            "response": "MySQL InnoDB modifies records in place and writes previous versions to an undo log segment, whereas PostgreSQL writes new row versions directly into the main heap table.",
            "reason": "Asks about MySQL InnoDB undo log architecture vs PostgreSQL.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a long-running transaction or stale replication slot prevents autovacuum from freezing tuples?",
            "response": "Autovacuum cannot clean up tuples visible to the oldest active transaction, leading to unbounded table bloat and eventually transaction ID wraparound emergency shutdown.",
            "reason": "Asks about transaction ID wraparound and bloat from stale slots (failure mode).",
        },
    },

    # 44. PostgreSQL EXPLAIN ANALYZE BUFFERS
    {
        "intent_group_id": "ig_db_postgres_explain_buffers",
        "domain": "databases_and_vector_search",
        "subtopic": "PostgreSQL",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "postgresql"}},
        "canonical_query": "How do you interpret shared hit and read counts in PostgreSQL EXPLAIN (ANALYZE, BUFFERS) query plans?",
        "canonical_response": "In EXPLAIN (ANALYZE, BUFFERS), 'shared hit' indicates 8KB pages retrieved directly from PostgreSQL shared_buffers cache in RAM, while 'shared read' indicates pages not cached in PostgreSQL memory that required operating system disk reads. A high read count indicates I/O bottlenecks and cache misses.",
        "positive_paraphrase_1": {
            "query": "What is the difference between shared hit and shared read metrics in PostgreSQL EXPLAIN BUFFERS output?",
            "reason": "Direct paraphrase asking to contrast shared hit vs shared read metrics.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how buffer metrics in PostgreSQL EXPLAIN reveal whether a query is memory-bound or I/O-bound.",
            "reason": "Syntactic variation focusing on diagnosing memory vs I/O bounds via buffer metrics.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you enable pg_stat_statements in postgresql.conf to track cumulative buffer hits across all queries?",
            "response": "Add `shared_preload_libraries = 'pg_stat_statements'` and `pg_stat_statements.track = all` in postgresql.conf, then execute `CREATE EXTENSION pg_stat_statements;`.",
            "reason": "Asks how to configure the pg_stat_statements extension rather than interpreting EXPLAIN output.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, WAL) query for a complex 3-table join.",
            "response": "```sql\nEXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS, WAL)\nSELECT * FROM orders o JOIN users u ON o.user_id = u.id JOIN items i ON o.item_id = i.id WHERE o.created_at > '2026-01-01';\n```",
            "reason": "Requests specific EXPLAIN query syntax rather than conceptual interpretation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does MySQL EXPLAIN format=TREE and analyze metrics compare to PostgreSQL EXPLAIN ANALYZE?",
            "response": "MySQL format=TREE outputs iterator trees showing iterator costs and actual loop counts, but does not report shared buffer page hits or disk reads like PostgreSQL.",
            "reason": "Asks about MySQL EXPLAIN format=TREE rather than PostgreSQL.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does running EXPLAIN (ANALYZE) on UPDATE or DELETE statements actually modify database data?",
            "response": "Because ANALYZE actually executes the query to measure real runtime and buffer stats; wrapping it in `BEGIN; EXPLAIN ANALYZE ...; ROLLBACK;` is required to avoid persistent changes.",
            "reason": "Asks about the data modification danger of EXPLAIN ANALYZE (execution caveat).",
        },
    },

    # 45. PostgreSQL Connection Pooling
    {
        "intent_group_id": "ig_db_postgres_connection_pooling",
        "domain": "databases_and_vector_search",
        "subtopic": "PostgreSQL",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "postgresql"}},
        "canonical_query": "What is the difference between session pooling and transaction pooling in PgBouncer?",
        "canonical_response": "In session pooling, a client retains a dedicated PostgreSQL backend process for the entire duration of its client connection. In transaction pooling, PgBouncer assigns a server connection only for the duration of a single transaction, returning it to the pool immediately upon COMMIT or ROLLBACK. This supports thousands of clients with few server connections.",
        "positive_paraphrase_1": {
            "query": "How do session pooling and transaction pooling differ in PgBouncer connection multiplexing?",
            "reason": "Direct paraphrase contrasting session and transaction pooling in PgBouncer.",
        },
        "positive_paraphrase_2": {
            "query": "Explain why transaction pooling in PgBouncer achieves much higher client density than session pooling.",
            "reason": "Syntactic variation focusing on client density and connection multiplexing.",
        },
        "hard_negative_diff_operation": {
            "query": "Which PostgreSQL features (like prepared statements or SET commands) break under PgBouncer transaction pooling?",
            "response": "Session-level features break under transaction pooling: unnamed prepared statements, `LISTEN/NOTIFY`, `SET` commands that persist across transactions, and temporary tables.",
            "reason": "Asks which features break under transaction pooling rather than explaining how pooling works.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a pgbouncer.ini configuration file configuring transaction pooling for database app_db on port 6432.",
            "response": "```ini\n[databases]\napp_db = host=127.0.0.1 port=5432 dbname=app_db\n[pgbouncer]\nlisten_port = 6432\npool_mode = transaction\nmax_client_conn = 5000\ndefault_pool_size = 20\n```",
            "reason": "Requests specific pgbouncer.ini config file rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does HikariCP client-side connection pooling in Java compare to PgBouncer server-side pooling?",
            "response": "HikariCP pools connections inside a single Java JVM application process, while PgBouncer is an independent proxy pooling connections across multiple application instances and servers.",
            "reason": "Asks about HikariCP client-side pooling vs PgBouncer.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does setting max_connections too high (e.g., >1000) directly on PostgreSQL severely degrade performance?",
            "response": "PostgreSQL forks a separate OS process for each connection (10MB+ RAM each). Excessive connections saturate CPU caches with process context switching and cause severe lock contention on shared memory.",
            "reason": "Asks about performance degradation from high native max_connections (scaling limitation).",
        },
    },

    # 46. PostgreSQL ON DELETE CASCADE
    {
        "intent_group_id": "ig_db_postgres_foreign_key_cascade",
        "domain": "databases_and_vector_search",
        "subtopic": "PostgreSQL",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "postgresql"}},
        "canonical_query": "How does PostgreSQL enforce referential integrity with ON DELETE CASCADE and why are child indexes critical?",
        "canonical_response": "ON DELETE CASCADE automatically deletes referencing child rows when a parent row is deleted. If the child table's foreign key column lacks an index, PostgreSQL must perform an expensive full sequential scan of the child table for every deleted parent row, causing massive transaction stalls and lock contention.",
        "positive_paraphrase_1": {
            "query": "Why does deleting parent rows with ON DELETE CASCADE cause severe performance degradation without foreign key indexes?",
            "reason": "Direct paraphrase explaining sequential scans and foreign key index requirements.",
        },
        "positive_paraphrase_2": {
            "query": "Explain how PostgreSQL executes cascade deletions and why foreign key columns require dedicated B-tree indexes.",
            "reason": "Syntactic variation focusing on cascade execution and B-tree indexing.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you alter an existing foreign key constraint in PostgreSQL to add ON DELETE CASCADE without dropping the table?",
            "response": "Drop the existing constraint with `ALTER TABLE child DROP CONSTRAINT fk_name;` and re-add it with `ALTER TABLE child ADD CONSTRAINT fk_name FOREIGN KEY (parent_id) REFERENCES parent(id) ON DELETE CASCADE;`.",
            "reason": "Asks for SQL ALTER TABLE syntax rather than explaining execution mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write SQL DDL creating a parent orders table and child order_items table with composite foreign key and cascade delete.",
            "response": "```sql\nCREATE TABLE orders (id INT PRIMARY KEY, org_id INT, UNIQUE(id, org_id));\nCREATE TABLE order_items (item_id INT PRIMARY KEY, order_id INT, org_id INT, FOREIGN KEY (order_id, org_id) REFERENCES orders(id, org_id) ON DELETE CASCADE);\n```",
            "reason": "Requests specific DDL schema creation code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does MongoDB handle cascade deletions compared to relational foreign keys in PostgreSQL?",
            "response": "MongoDB has no native declarative foreign keys or cascade constraints; cascade deletions must be implemented manually in application code or via MongoDB Atlas Triggers.",
            "reason": "Asks about MongoDB deletion handling vs PostgreSQL.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between ON DELETE CASCADE and ON DELETE RESTRICT in PostgreSQL?",
            "response": "ON DELETE CASCADE automatically removes referencing rows in child tables, whereas ON DELETE RESTRICT raises an immediate integrity violation error and prevents parent row deletion.",
            "reason": "Contrasts CASCADE with RESTRICT constraint behavior rather than explaining CASCADE indexing mechanics.",
        },
    },

    # 47. PostgreSQL Declarative Partitioning
    {
        "intent_group_id": "ig_db_postgres_partitioning",
        "domain": "databases_and_vector_search",
        "subtopic": "PostgreSQL",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "postgresql"}},
        "canonical_query": "How does declarative table partitioning in PostgreSQL enable partition pruning during query execution?",
        "canonical_response": "Declarative partitioning divides a large table into distinct partition tables based on range, list, or hash keys. During planning and execution, PostgreSQL uses query WHERE clauses to evaluate partition bounds, automatically skipping (pruning) non-matching partition tables from the query scan tree.",
        "positive_paraphrase_1": {
            "query": "Explain how PostgreSQL uses partition pruning to scan only relevant child tables for parameterized queries.",
            "reason": "Direct paraphrase explaining partition pruning on child tables.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does range and list table partitioning improve query performance and maintenance in PostgreSQL?",
            "reason": "Syntactic variation focusing on range/list partitioning performance benefits.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you detach an old partition in PostgreSQL and convert it to a standalone table using DETACH PARTITION CONCURRENTLY?",
            "response": "Execute `ALTER TABLE parent_table DETACH PARTITION child_table CONCURRENTLY;` to detach the partition without blocking concurrent queries on other partitions.",
            "reason": "Asks how to detach partitions concurrently rather than explaining pruning.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write SQL statements to create a partitioned table orders by range on order_date with partitions for 2026 Q1 and Q2.",
            "response": "```sql\nCREATE TABLE orders (id INT, order_date DATE) PARTITION BY RANGE (order_date);\nCREATE TABLE orders_2026_q1 PARTITION OF orders FOR VALUES FROM ('2026-01-01') TO ('2026-04-01');\n```",
            "reason": "Requests specific partition table DDL statements rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Cassandra partition key token ring distribution compare to PostgreSQL table partitioning?",
            "response": "Cassandra hashes the partition key to place rows onto distinct nodes in a distributed cluster, whereas PostgreSQL partitioning organizes tables on a single database instance.",
            "reason": "Asks about Cassandra token ring distribution vs PostgreSQL partitioning.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a query WHERE clause uses an expression that prevents PostgreSQL from performing partition pruning?",
            "response": "If the query uses unprunable expressions (like volatile functions or non-immutable casts on the partition key), PostgreSQL falls back to scanning every partition table sequentially.",
            "reason": "Asks about pruning failure when non-immutable functions are used in WHERE clauses.",
        },
    },

    # 48. PostgreSQL Index Types
    {
        "intent_group_id": "ig_db_postgres_index_types",
        "domain": "databases_and_vector_search",
        "subtopic": "PostgreSQL",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "postgresql"}},
        "canonical_query": "When should you choose a GIN or GiST index over a standard B-tree index in PostgreSQL?",
        "canonical_response": "Standard B-tree indexes are optimized for scalar comparisons (<, <=, =, >=, >) and sorting. GIN (Generalized Inverted Index) is optimized for composite items with multi-element values (full-text search, JSONB @> containment, array overlap). GiST is optimized for spatial data, range types (overlaps &&), and nearest-neighbor distance searches.",
        "positive_paraphrase_1": {
            "query": "Explain the architectural differences and ideal use cases for B-tree, GIN, and GiST indexes in PostgreSQL.",
            "reason": "Direct paraphrase comparing B-tree, GIN, and GiST indexes.",
        },
        "positive_paraphrase_2": {
            "query": "Why is a GIN index superior to B-tree for querying JSONB documents and full-text search in PostgreSQL?",
            "reason": "Syntactic variation focusing on GIN superiority for JSONB and text search.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you create a partial index in PostgreSQL to index only active, unexpired records?",
            "response": "Execute `CREATE INDEX idx_active ON records (user_id) WHERE expires_at IS NULL OR expires_at > NOW();` to index only matching subsets.",
            "reason": "Asks how to write a partial index definition rather than comparing index types.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the SQL command to create a GIN index on a JSONB column using jsonb_path_ops.",
            "response": "```sql\nCREATE INDEX idx_data_gin ON events USING gin (payload jsonb_path_ops);\n```",
            "reason": "Requests specific GIN index creation command rather than conceptual comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does MongoDB multikey index compare to PostgreSQL GIN indexes for array fields?",
            "response": "MongoDB automatically creates multikey index entries for array fields, while PostgreSQL requires explicit GIN index creation with array operator classes.",
            "reason": "Asks about MongoDB multikey indexes vs PostgreSQL GIN.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why are GIN indexes significantly slower than B-tree indexes for INSERT and UPDATE operations?",
            "response": "GIN indexes must insert entries for every individual token or key in the document; a single row insert with 50 tags requires updating dozens of posting lists in the GIN index.",
            "reason": "Asks about write amplification overhead of GIN indexes (drawback/limitation).",
        },
    },

    # 49. pgvector HNSW Parameters
    {
        "intent_group_id": "ig_db_pgvector_hnsw_construction",
        "domain": "databases_and_vector_search",
        "subtopic": "pgvector",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "pgvector"}},
        "canonical_query": "How do the m and ef_construction parameters in pgvector influence HNSW index quality and build time?",
        "canonical_response": "In pgvector HNSW indexes, 'm' defines the maximum number of bidirectional connections per graph node (higher m increases graph connectivity, recall, and RAM usage), while 'ef_construction' determines the size of the dynamic candidate list evaluated during index construction (higher ef_construction improves graph recall but linearly increases build duration).",
        "positive_paraphrase_1": {
            "query": "Explain how tuning m and ef_construction affects build duration, memory usage, and recall in pgvector HNSW.",
            "reason": "Direct paraphrase explaining tuning trade-offs for m and ef_construction.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do HNSW index parameters m and ef_construction govern vector graph topology in PostgreSQL?",
            "reason": "Syntactic variation addressing vector graph topology and connectivity.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you tune runtime query accuracy in pgvector using SET hnsw.ef_search = 100?",
            "response": "Execute `SET hnsw.ef_search = 100;` before running query vectors. This widens the search beam during query-time graph traversal, trading higher latency for improved recall without rebuilding the index.",
            "reason": "Asks how to set runtime ef_search rather than explaining build parameters m and ef_construction.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the SQL DDL statement to build a cosine distance HNSW index on a 384-dimensional vector column with m=16 and ef_construction=64.",
            "response": "```sql\nCREATE INDEX idx_vector_hnsw ON records USING hnsw (vector vector_cosine_ops) WITH (m = 16, ef_construction = 64);\n```",
            "reason": "Requests specific SQL DDL statement rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Qdrant HNSW indexing implementation compare to pgvector's PostgreSQL HNSW index?",
            "response": "Qdrant maintains an in-memory vector database engine with native payload filtering integrated directly into graph traversal, whereas pgvector operates inside PostgreSQL's shared buffers and executor framework.",
            "reason": "Asks about Qdrant vs pgvector rather than explaining pgvector HNSW parameters.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does setting maintenance_work_mem too low cause HNSW index builds to fail or crawl in pgvector?",
            "response": "HNSW builds construct the graph entirely in memory up to maintenance_work_mem; insufficient memory forces swapping graph nodes to temporary disk files, drastically slowing index construction.",
            "reason": "Asks about maintenance_work_mem resource exhaustion during HNSW builds.",
        },
    },

    # 50. pgvector Distance Operators
    {
        "intent_group_id": "ig_db_pgvector_cosine_vs_l2",
        "domain": "databases_and_vector_search",
        "subtopic": "pgvector",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "pgvector"}},
        "canonical_query": "What is the mathematical difference between cosine distance (<=>) and L2 squared Euclidean distance (<->) in pgvector?",
        "canonical_response": "In pgvector, cosine distance `<=>` is defined as `1.0 - (u . v) / (||u|| * ||v||)`, measuring the angle between vectors independent of their magnitude. L2 distance `<->` computes Euclidean distance `sqrt(sum((u_i - v_i)^2))`, which is sensitive to vector length. For unit-normalized vectors, cosine distance and squared Euclidean distance produce identical rankings.",
        "positive_paraphrase_1": {
            "query": "How do the cosine distance (<=>) and Euclidean distance (<->) operators differ in pgvector?",
            "reason": "Direct paraphrase contrasting cosine and L2 distance operators in pgvector.",
        },
        "positive_paraphrase_2": {
            "query": "Explain the difference between angular distance (<=>) and magnitude-sensitive L2 distance (<->) in vector search.",
            "reason": "Syntactic variation focusing on angular vs magnitude-sensitive distance metrics.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you convert cosine distance returned by pgvector into a 0.0 to 1.0 similarity score in Python?",
            "response": "Compute `similarity = 1.0 - distance`, and clamp the result between -1.0 and 1.0 to guard against floating-point inaccuracies.",
            "reason": "Asks how to convert distance to similarity in Python rather than explaining mathematical operator differences.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a PostgreSQL query that retrieves the top 5 nearest neighbors using the negative inner product (<#>) operator.",
            "response": "```sql\nSELECT id, (vector <#> $1::vector) * -1 AS inner_product FROM vector_records ORDER BY vector <#> $1::vector ASC LIMIT 5;\n```",
            "reason": "Requests specific SQL query using inner product operator rather than explaining cosine vs L2.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does FAISS IndexFlatIP (inner product) compare to pgvector cosine distance indexing?",
            "response": "FAISS IndexFlatIP computes exact dot products across dense arrays in C++, while pgvector integrates cosine distance into PostgreSQL's cost-based query planner and index scans.",
            "reason": "Asks about FAISS IndexFlatIP vs pgvector cosine distance.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why will an HNSW index created with vector_l2_ops NOT accelerate a query using the <=> (cosine) operator in pgvector?",
            "response": "PostgreSQL index selection requires the operator in the ORDER BY clause to match the index operator class; an HNSW index built for L2 (<->) cannot be used for cosine distance (<=>) queries.",
            "reason": "Asks about operator class mismatch preventing index usage.",
        },
    },

    # 51. pgvector Iterative Scan
    {
        "intent_group_id": "ig_db_pgvector_iterative_scan",
        "domain": "databases_and_vector_search",
        "subtopic": "pgvector",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "pgvector"}},
        "canonical_query": "How does pgvector's hnsw.iterative_scan feature resolve candidate starvation caused by post-filtering?",
        "canonical_response": "When a query combines vector search with a restrictive WHERE filter (e.g. namespace = 'tenant_a'), standard HNSW search may exhaust its candidate beam on non-matching rows, returning fewer than top_k items (candidate starvation). With `hnsw.iterative_scan = 'strict_order'` or `'relaxed_order'`, pgvector continues traversing the graph until top_k filtered matches are found.",
        "positive_paraphrase_1": {
            "query": "Explain how iterative scan in pgvector prevents candidate starvation when filtering vector search results.",
            "reason": "Direct paraphrase explaining how iterative scan prevents candidate starvation.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does setting hnsw.iterative_scan ensure that filtered vector queries in PostgreSQL return the requested top_k items?",
            "reason": "Syntactic variation focusing on top_k candidate recovery via iterative scan.",
        },
        "hard_negative_diff_operation": {
            "query": "What is the performance difference between hnsw.iterative_scan = 'strict_order' and 'relaxed_order'?",
            "response": "'strict_order' guarantees exact monotonic distance ordering but requires re-sorting candidate queues during graph traversal; 'relaxed_order' skips re-sorting overhead, providing faster retrieval.",
            "reason": "Asks specifically about strict vs relaxed order trade-offs rather than general iterative scan mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the SQL commands to enable iterative scan within an isolated PostgreSQL transaction block.",
            "response": "```sql\nBEGIN;\nSET LOCAL hnsw.iterative_scan = 'relaxed_order';\nSELECT * FROM vector_records WHERE namespace = 'tenant_a' ORDER BY vector <=> $1 LIMIT 10;\nCOMMIT;\n```",
            "reason": "Requests specific transaction SQL code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Weaviate filtered vector search compare to pgvector iterative scan?",
            "response": "Weaviate maintains inverted index bitmap filters intersected with vector graphs at every step, whereas pgvector uses PostgreSQL index filtering with iterative graph continuation.",
            "reason": "Asks about Weaviate filtered search vs pgvector iterative scan.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why should hnsw.iterative_scan NOT be enabled globally across all connections in postgresql.conf?",
            "response": "Enabling iterative scan globally can cause catastrophic query latency if a filter matches zero or very few rows, forcing pgvector to traverse the entire HNSW graph before returning.",
            "reason": "Asks why global activation is dangerous (worst-case latency trap / limitation).",
        },
    },

    # 52. pgvector halfvec Storage
    {
        "intent_group_id": "ig_db_pgvector_halfvec_storage",
        "domain": "databases_and_vector_search",
        "subtopic": "pgvector",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "pgvector"}},
        "canonical_query": "How does pgvector's halfvec data type reduce RAM and storage requirements for vector embeddings?",
        "canonical_response": "Standard `vector` stores 32-bit single-precision floats (4 bytes per dimension). `halfvec` stores 16-bit half-precision floats (IEEE 754-2008 binary16, 2 bytes per dimension). This cuts index size, shared_buffers footprint, and disk I/O exactly in half with negligible loss in retrieval recall.",
        "positive_paraphrase_1": {
            "query": "Explain how 16-bit halfvec in pgvector achieves a 50% reduction in index size and memory usage.",
            "reason": "Direct paraphrase explaining memory and storage savings with 16-bit halfvec.",
        },
        "positive_paraphrase_2": {
            "query": "Why does migrating from float32 vector to halfvec optimize HNSW index caching in PostgreSQL?",
            "reason": "Syntactic variation focusing on HNSW index cache optimization with halfvec.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you cast a standard 384-dimensional vector column to halfvec in a PostgreSQL migration?",
            "response": "Execute `ALTER TABLE records ALTER COLUMN embedding TYPE halfvec(384) USING embedding::halfvec(384);`.",
            "reason": "Asks for migration SQL DDL syntax rather than explaining storage and RAM mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the SQL DDL to create an HNSW index on a halfvec column with halfvec_cosine_ops in pgvector.",
            "response": "```sql\nCREATE INDEX idx_halfvec_hnsw ON vector_records USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);\n```",
            "reason": "Requests specific index creation DDL rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Milvus scalar quantization (SQ8) compare to pgvector halfvec FP16 precision?",
            "response": "Milvus SQ8 quantizes 32-bit floats into 8-bit integers (4x compression) with some precision degradation, while pgvector halfvec uses 16-bit floats (2x compression) preserving high fidelity.",
            "reason": "Asks about Milvus SQ8 quantization vs pgvector halfvec.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Under what conditions does casting embeddings to halfvec cause numerical underflow in cosine distance calculation?",
            "response": "16-bit floats have an exponent range of ~10^-5 to 10^4; if raw embedding dimensions have extremely tiny magnitudes (<6e-8), values flush to zero, causing precision distortion.",
            "reason": "Asks about FP16 numerical underflow edge cases (numerical boundary limitation).",
        },
    },

    # 53. pgvector Filter Pushdown vs Planner Fallback
    {
        "intent_group_id": "ig_db_pgvector_filter_pushdown",
        "domain": "databases_and_vector_search",
        "subtopic": "pgvector",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "pgvector"}},
        "canonical_query": "Why does the PostgreSQL query planner switch from an HNSW index scan to a sequential scan on highly selective filters?",
        "canonical_response": "PostgreSQL's cost-based query planner estimates the cost of graph traversal versus direct filtering. If a WHERE filter matches only a tiny fraction of rows (e.g. 1% or 20 rows out of 2,000), traversing the global HNSW graph and discarding 99% of nodes is much slower than directly scanning the 20 rows and sorting their distances in memory.",
        "positive_paraphrase_1": {
            "query": "Explain why PostgreSQL chooses sequential scan with distance sort over HNSW when filtering on small partitions.",
            "reason": "Direct paraphrase explaining cost-based fallback on small selective partitions.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does PostgreSQL cost estimation decide between vector index scans and exact filtered sequential scans?",
            "reason": "Syntactic variation addressing cost-based decision between index scans and sequential scans.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you force PostgreSQL to use an HNSW index scan for debugging by disabling sequential scan?",
            "response": "Execute `SET LOCAL enable_seqscan = off;` within the transaction, forcing the planner to evaluate the HNSW index path regardless of estimated cost.",
            "reason": "Asks how to force index scans by toggling planner flags rather than explaining cost decisions.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script that parses EXPLAIN (ANALYZE, FORMAT JSON) to check whether 'Index Scan using idx_vector' was selected.",
            "response": "```python\nplan = json.loads(explain_output)\nused_hnsw = any('idx_vector' in str(node.get('Index Name')) for node in extract_nodes(plan))\n```",
            "reason": "Requests specific EXPLAIN parsing code rather than conceptual planner explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Pinecone metadata filtering compare to PostgreSQL pgvector query planner fallback?",
            "response": "Pinecone uses proprietary hybrid single-stage filtering integrated directly inside its vector index nodes without planner fallback to sequential table scans.",
            "reason": "Asks about Pinecone metadata filtering vs pgvector.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does creating separate partial HNSW indexes for each tenant prevent planner fallback?",
            "response": "A partial HNSW index built specifically `WHERE namespace = 'tenant_a'` contains only matching rows; the planner does not discard candidates during traversal, making index scans cost-optimal.",
            "reason": "Asks how partial indexes eliminate post-filtering overhead.",
        },
    },

    # 54. Redis Pub/Sub vs Streams
    {
        "intent_group_id": "ig_db_redis_pubsub_vs_streams",
        "domain": "databases_and_vector_search",
        "subtopic": "Redis",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "redis"}},
        "canonical_query": "What is the architectural difference between Redis Pub/Sub and Redis Streams for message processing?",
        "canonical_response": "Redis Pub/Sub is an ephemeral fire-and-forget message broker: messages are not persisted, and if a subscriber is disconnected when a message is published, it is lost forever. Redis Streams is an append-only log data structure with disk persistence, consumer groups, message acknowledgement (XACK), and pending entry tracking (XPENDING).",
        "positive_paraphrase_1": {
            "query": "How do Redis Streams provide message persistence and consumer groups compared to ephemeral Pub/Sub?",
            "reason": "Direct paraphrase contrasting Redis Streams persistence and consumer groups with Pub/Sub.",
        },
        "positive_paraphrase_2": {
            "query": "Explain why Redis Streams is suitable for reliable message queues while Redis Pub/Sub is strictly fire-and-forget.",
            "reason": "Syntactic variation focusing on reliable queuing vs fire-and-forget delivery.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you acknowledge and claim pending messages in Redis Streams using XACK and XAUTOCLAIM?",
            "response": "Use `XACK stream_key group_name id` to acknowledge completed messages, and `XAUTOCLAIM stream_key group_name consumer min_idle_time start` to reassign timed-out messages from dead consumers.",
            "reason": "Asks how to use XACK and XAUTOCLAIM rather than contrasting Streams with Pub/Sub.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python Redis Streams worker loop reading from a consumer group with XREADGROUP and XACK.",
            "response": "```python\nwhile True:\n    entries = r.xreadgroup('mygroup', 'worker1', {'mystream': '>'}, count=10)\n    for sid, msgs in entries:\n        for mid, data in msgs:\n            process(data)\n            r.xack('mystream', 'mygroup', mid)\n```",
            "reason": "Requests specific Redis Streams worker code rather than architectural comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Apache Kafka partition log architecture compare to Redis Streams?",
            "response": "Kafka is designed for massive distributed throughput with partition replication across disk clusters, while Redis Streams operates in-memory on a single primary node.",
            "reason": "Asks about Kafka vs Redis Streams.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does capping a Redis Stream with MAXLEN ~ 10000 prevent unbounded memory exhaustion?",
            "response": "Using `XADD mystream MAXLEN ~ 10000 * key val` automatically trims the oldest stream entries when the limit is reached, maintaining constant memory overhead.",
            "reason": "Asks about stream trimming via MAXLEN rather than Pub/Sub vs Streams comparison.",
        },
    },

    # 55. Redis Eviction Policies
    {
        "intent_group_id": "ig_db_redis_eviction_policies",
        "domain": "databases_and_vector_search",
        "subtopic": "Redis",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "redis"}},
        "canonical_query": "How does allkeys-lru differ from volatile-lru when Redis reaches its maxmemory limit?",
        "canonical_response": "When Redis memory exceeds `maxmemory`, `allkeys-lru` evicts the least recently used keys across the entire database, regardless of whether they have a TTL expiration. `volatile-lru` evicts only from keys that have an explicit expiration (TTL) set, raising an OOM error on write commands if no expired keys remain.",
        "positive_paraphrase_1": {
            "query": "Explain the difference between allkeys-lru and volatile-lru eviction behavior in Redis when memory is full.",
            "reason": "Direct paraphrase asking to contrast allkeys-lru and volatile-lru eviction behavior.",
        },
        "positive_paraphrase_2": {
            "query": "Why does selecting allkeys-lru over volatile-lru prevent OOM errors on non-expiring Redis keys?",
            "reason": "Syntactic variation addressing OOM prevention on non-expiring keys.",
        },
        "hard_negative_diff_operation": {
            "query": "When should you choose LFU (Least Frequently Used) eviction over LRU eviction in Redis?",
            "response": "Choose LFU (`allkeys-lfu`) when access frequency matters more than recency, preventing a burst of one-time reads from flushing hot, frequently requested keys from the cache.",
            "reason": "Asks when to choose LFU over LRU rather than contrasting allkeys-lru with volatile-lru.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the redis.conf configuration lines to set maxmemory to 4GB and maxmemory-policy to allkeys-lru.",
            "response": "```text\nmaxmemory 4gb\nmaxmemory-policy allkeys-lru\nmaxmemory-samples 10\n```",
            "reason": "Requests specific redis.conf configuration directives rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Memcached slab allocation and eviction compare to Redis maxmemory policies?",
            "response": "Memcached pre-allocates memory into fixed-size slab classes and evicts within individual slabs, whereas Redis manages variable-sized heap allocations with probabilistic LRU/LFU sampling.",
            "reason": "Asks about Memcached slab allocation vs Redis eviction.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Redis use an approximated LRU algorithm rather than an exact linked-list LRU?",
            "response": "A true LRU linked list requires allocating 16-24 bytes of pointer metadata per key and moving nodes under locks on every read; Redis samples 5-10 random keys to approximate LRU with zero per-read memory mutation.",
            "reason": "Asks why Redis uses approximated LRU sampling rather than exact linked lists.",
        },
    },

    # 56. Redis Cluster Hash Slots
    {
        "intent_group_id": "ig_db_redis_cluster_hash_slots",
        "domain": "databases_and_vector_search",
        "subtopic": "Redis",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "redis"}},
        "canonical_query": "How does Redis Cluster use 16,384 hash slots to distribute keys across master nodes?",
        "canonical_response": "Redis Cluster partitions its key space into 16,384 discrete hash slots. Every key is assigned to a slot using `CRC16(key) mod 16384`. Master nodes each own a contiguous or divided range of these slots. When a client queries a key, the cluster routes the request to the master currently hosting that slot via MOVED redirection.",
        "positive_paraphrase_1": {
            "query": "Explain how CRC16 hashing maps keys to 16,384 hash slots across nodes in Redis Cluster.",
            "reason": "Direct paraphrase explaining CRC16 mapping to 16,384 hash slots.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does hash slot sharding allow Redis Cluster to scale horizontally without global routing tables?",
            "reason": "Syntactic variation focusing on horizontal scaling and hash slot sharding.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you use hash tags ({user_123}) in Redis Cluster to force related keys into the exact same hash slot?",
            "response": "Enclose the sharding key in curly braces: `user:{123}:profile` and `user:{123}:orders`. Redis Cluster hashes only the substring inside `{...}`, ensuring multi-key operations map to the same node.",
            "reason": "Asks how to use hash tags for co-locating keys rather than explaining cluster sharding.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the redis-cli cluster reshard command to transfer 1000 hash slots from node A to node B.",
            "response": "```bash\nredis-cli --cluster reshard 127.0.0.1:7000 --cluster-from <nodeA> --cluster-to <nodeB> --cluster-slots 1000 --cluster-yes\n```",
            "reason": "Requests specific CLI resharding command syntax rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does DynamoDB partition key hashing compare to Redis Cluster hash slots?",
            "response": "DynamoDB uses an MD5 hash ring partitioned across an internal managed fleet, whereas Redis Cluster exposes 16,384 explicit slots managed via client-side topology caching.",
            "reason": "Asks about DynamoDB partition hashing vs Redis Cluster slots.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Redis Cluster reject multi-key operations (MGET, MSET, transactions) across different hash slots with a CROSSSLOT error?",
            "response": "Redis operations on multiple keys are single-threaded on a single server; if keys belong to different hash slots on different nodes, Redis cannot guarantee atomicity and aborts with CROSSSLOT.",
            "reason": "Asks about the CROSSSLOT error cause across multi-node keys.",
        },
    },

    # 57. Redis Pipeline vs Transaction
    {
        "intent_group_id": "ig_db_redis_pipeline_vs_multi",
        "domain": "databases_and_vector_search",
        "subtopic": "Redis",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "redis"}},
        "canonical_query": "What is the difference between Redis pipelining and MULTI/EXEC transactions regarding atomicity?",
        "canonical_response": "Redis pipelining buffers multiple commands on the client and sends them in a single network packet to reduce roundtrip latency; commands execute sequentially but other clients' commands can interleave between them (no atomicity). MULTI/EXEC transactions queue commands on the server and execute them as an isolated, atomic block with zero interleaving.",
        "positive_paraphrase_1": {
            "query": "How do Redis pipelining and MULTI/EXEC transactions differ in terms of network roundtrips and atomic execution?",
            "reason": "Direct paraphrase contrasting network batching with server-side atomicity in Redis.",
        },
        "positive_paraphrase_2": {
            "query": "Explain why pipelining optimizes network latency while MULTI/EXEC provides isolated atomic command execution in Redis.",
            "reason": "Syntactic variation focusing on network latency vs isolated command execution.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you achieve optimistic locking in Redis using the WATCH command before a MULTI/EXEC block?",
            "response": "Call `WATCH key` before MULTI. If any watched key is modified by another client before EXEC runs, the transaction aborts and returns a null response, signaling the client to retry.",
            "reason": "Asks how to use the WATCH command rather than contrasting pipelining with MULTI/EXEC.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script using redis-py pipeline with transaction=False to bulk insert 10,000 keys.",
            "response": "```python\npipe = r.pipeline(transaction=False)\nfor i in range(10000):\n    pipe.set(f'k:{i}', i)\npipe.execute()\n```",
            "reason": "Requests specific bulk insert script code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Lua scripting in Redis compare to MULTI/EXEC transactions for atomic operations?",
            "response": "Redis Lua scripts execute atomically and allow conditional logic based on intermediate query results (e.g., if GET equals X, then SET Y), which MULTI/EXEC cannot do.",
            "reason": "Asks about Lua scripting vs MULTI/EXEC rather than pipelining vs transactions.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Redis MULTI/EXEC NOT support rollback when a command fails with a runtime type error?",
            "response": "Redis transactions do not support rollback; if a command fails at runtime (e.g. calling LPUSH on a string), that command returns an error, but all other queued commands still execute.",
            "reason": "Asks about the lack of rollback support in Redis transactions.",
        },
    },

    # 58. Redis Sorted Sets Leaderboard
    {
        "intent_group_id": "ig_db_redis_sorted_set_leaderboard",
        "domain": "databases_and_vector_search",
        "subtopic": "Redis",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "redis"}},
        "canonical_query": "How does the Redis Sorted Set (ZSET) skiplist and hash table dual data structure enable O(log N) leaderboards?",
        "canonical_response": "Redis Sorted Sets combine a hash table (mapping member names to float scores in O(1)) with a skiplist (ordering members by score in O(log N)). This dual representation allows instantaneous score lookups (ZSCORE) and logarithmic time complexity for adding, updating, and range-querying ranked members (ZADD, ZREVRANGE).",
        "positive_paraphrase_1": {
            "query": "Explain how the skiplist and hash table structure in Redis Sorted Sets supports logarithmic rank and range queries.",
            "reason": "Direct paraphrase explaining skiplist and hash table dual structure in Sorted Sets.",
        },
        "positive_paraphrase_2": {
            "query": "Why is Redis ZSET ideally suited for real-time leaderboards with millions of users?",
            "reason": "Syntactic variation focusing on real-time leaderboard capabilities.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you retrieve the top 10 users with their scores and rank using ZREVRANGE and ZREVRANK in Redis?",
            "response": "Execute `ZREVRANGE leaderboard 0 9 WITHSCORES` to fetch the top 10 members with scores, and `ZREVRANK leaderboard user_123` to obtain a specific user's zero-based rank.",
            "reason": "Asks for specific command syntax rather than explaining the skiplist internal structure.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python Redis leaderboard service supporting update_score(user, score) and get_top_players(n).",
            "response": "```python\nclass Leaderboard:\n    def update(self, u, s): r.zadd('lb', {u: s})\n    def top(self, n): return r.zrevrange('lb', 0, n-1, withscores=True)\n```",
            "reason": "Requests specific leaderboard class code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does an SQL B-tree index on (score DESC) compare to Redis ZSET for real-time ranking queries?",
            "response": "An SQL B-tree requires scanning pages or using window functions (DENSE_RANK()) that scan rows, while Redis Sorted Sets track skiplist node span counts for instant rank lookups.",
            "reason": "Asks about SQL B-tree ranking vs Redis ZSET.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "How does Redis break score ties when two members have the exact same float score in a Sorted Set?",
            "response": "When two members have identical scores, Redis orders them lexicographically in binary byte order using their member string keys as the secondary tie-breaker.",
            "reason": "Asks specifically about secondary tie-breaking rules on identical scores.",
        },
    },

    # 59. SQLite WAL Mode
    {
        "intent_group_id": "ig_db_sqlite_wal_mode",
        "domain": "databases_and_vector_search",
        "subtopic": "SQLite",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "sqlite"}},
        "canonical_query": "How does enabling Write-Ahead Logging (WAL) in SQLite allow concurrent readers while a write transaction is active?",
        "canonical_response": "In traditional rollback journal mode, writing locks the entire database file, blocking all readers. In WAL mode (`PRAGMA journal_mode=WAL`), changes are appended to a separate `-wal` file. Readers read committed pages from the main database file while writers append to the WAL, allowing readers and one writer to operate simultaneously.",
        "positive_paraphrase_1": {
            "query": "Explain how SQLite WAL mode decouples readers from writers and prevents reader lock contention.",
            "reason": "Direct paraphrase explaining reader/writer concurrency in SQLite WAL mode.",
        },
        "positive_paraphrase_2": {
            "query": "Why does switching SQLite to PRAGMA journal_mode=WAL significantly increase read throughput during concurrent writes?",
            "reason": "Syntactic variation focusing on read throughput improvements under WAL mode.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you trigger a manual checkpoint in SQLite to flush the -wal file back into the main database file?",
            "response": "Execute `PRAGMA wal_checkpoint(TRUNCATE);`, which waits for readers to finish, copies all WAL frames into the main database file, and truncates the WAL file to zero bytes.",
            "reason": "Asks how to execute manual checkpoints rather than explaining reader concurrency.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write Python sqlite3 connection code that configures WAL mode, normal synchronous, and a 5-second busy timeout.",
            "response": "```python\nconn = sqlite3.connect('app.db')\nconn.execute('PRAGMA journal_mode=WAL;')\nconn.execute('PRAGMA synchronous=NORMAL;')\nconn.execute('PRAGMA busy_timeout=5000;')\n```",
            "reason": "Requests specific Python configuration code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does SQLite WAL file architecture compare to PostgreSQL Write-Ahead Log (pg_wal)?",
            "response": "SQLite WAL holds actual uncommitted/committed pages that readers access directly, whereas PostgreSQL pg_wal stores redo record logs strictly for crash recovery and replication.",
            "reason": "Asks about SQLite WAL vs PostgreSQL pg_wal.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is SQLite WAL mode NOT supported and dangerous to use over shared network filesystems like NFS?",
            "response": "SQLite WAL requires shared memory primitives (`-shm` file) and POSIX fcntl locks across processes on the same host; network filesystems do not support shared memory and can corrupt the database.",
            "reason": "Asks about NFS file locking incompatibility (environmental limitation).",
        },
    },

    # 60. SQLite Busy Timeout
    {
        "intent_group_id": "ig_db_sqlite_busy_timeout",
        "domain": "databases_and_vector_search",
        "subtopic": "SQLite",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "sqlite"}},
        "canonical_query": "How does setting PRAGMA busy_timeout prevent immediate 'database is locked' errors in SQLite?",
        "canonical_response": "By default, when a SQLite transaction encounters a file lock held by another process, it immediately aborts with `SQLITE_BUSY: database is locked`. Setting `PRAGMA busy_timeout = 5000` instructs SQLite to sleep and retry acquiring the lock with backoff for up to 5,000 milliseconds before failing.",
        "positive_paraphrase_1": {
            "query": "Explain how PRAGMA busy_timeout instructs SQLite to sleep and retry instead of immediately failing on lock contention.",
            "reason": "Direct paraphrase explaining sleep and retry on lock contention.",
        },
        "positive_paraphrase_2": {
            "query": "Why is setting a busy handler or busy_timeout essential for multi-process SQLite applications?",
            "reason": "Syntactic variation focusing on multi-process concurrency and busy timeout.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you use BEGIN IMMEDIATE transactions in SQLite to acquire write locks upfront and prevent deadlocks?",
            "response": "Start transactions with `BEGIN IMMEDIATE;` rather than `BEGIN;`. This acquires the write lock at the start of the transaction, ensuring concurrent writers fail or wait early rather than deadlocking.",
            "reason": "Asks how to use BEGIN IMMEDIATE to avoid deadlocks rather than explaining busy_timeout.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a multithreaded Python script demonstrating 10 threads writing to SQLite with a busy_timeout.",
            "response": "```python\nimport sqlite3, threading\ndef worker():\n    conn = sqlite3.connect('test.db', timeout=10.0)\n    with conn:\n        conn.execute('INSERT INTO log VALUES (?)', (1,))\n```",
            "reason": "Requests specific multithreaded test code rather than explanation of busy_timeout mechanics.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does PostgreSQL lock_timeout and statement_timeout compare to SQLite busy_timeout?",
            "response": "PostgreSQL queues blocked transactions by default and uses lock_timeout to cancel transactions if a lock is not acquired in time, whereas SQLite aborts immediately unless busy_timeout is set.",
            "reason": "Asks about PostgreSQL lock timeouts vs SQLite busy timeout.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why will busy_timeout NOT prevent a deadlock if two transactions upgrade deferred locks simultaneously?",
            "response": "If two transactions begin with deferred locks, read data, and both try to write, each waits for the other to release its read lock; busy_timeout cannot resolve the circular wait and times out.",
            "reason": "Asks about the deferred lock upgrade deadlock trap.",
        },
    },

    # 61. DynamoDB Single Table Design
    {
        "intent_group_id": "ig_db_dynamodb_single_table",
        "domain": "databases_and_vector_search",
        "subtopic": "DynamoDB",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "dynamodb"}},
        "canonical_query": "How does DynamoDB single-table design use partition keys (PK) and sort keys (SK) to model one-to-many relationships?",
        "canonical_response": "Single-table design co-locates multiple entity types (e.g. Orders and OrderItems) into a single physical table. By setting PK to a shared entity identifier (e.g. `ORDER#123`) and SK to prefixed item IDs (e.g. `METADATA` for the order and `ITEM#456` for line items), a single Query on PK retrieves the order and all its items in one network roundtrip without joins.",
        "positive_paraphrase_1": {
            "query": "Explain how generic PK and SK key overloading in DynamoDB single-table design enables pre-joined queries.",
            "reason": "Direct paraphrase explaining key overloading and pre-joined queries in DynamoDB.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does modeling relational data in a single DynamoDB table eliminate the need for SQL joins?",
            "reason": "Syntactic variation focusing on eliminating SQL joins via composite partition keys.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure a Global Secondary Index (GSI) in DynamoDB single-table design to enable inverted lookups?",
            "response": "Create a GSI with GSI1PK set to the child entity attribute and GSI1SK set to the parent entity attribute, allowing queries from the child back to parent entities.",
            "reason": "Asks how to configure a GSI for inverted lookups rather than explaining one-to-many PK/SK modeling.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python boto3 script that performs a query on a DynamoDB table for PK='USER#123' and SK begins_with('ORDER#').",
            "response": "```python\nresp = table.query(\n    KeyConditionExpression=Key('PK').eq('USER#123') & Key('SK').begins_with('ORDER#')\n)\n```",
            "reason": "Requests specific boto3 query code rather than conceptual data modeling explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does relational normalization (3NF) in PostgreSQL compare to DynamoDB single-table denormalization?",
            "response": "Relational 3NF minimizes data redundancy by separating entities into isolated tables connected by foreign keys, whereas DynamoDB prioritizes query-driven denormalization for single-digit millisecond latency.",
            "reason": "Asks about relational normalization vs DynamoDB denormalization.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is single-table design in DynamoDB notoriously rigid and difficult to adapt to ad-hoc query requirements?",
            "response": "Single-table designs are heavily optimized for pre-determined access patterns; querying by an attribute that was not planned into PK, SK, or a GSI requires an expensive full-table Scan.",
            "reason": "Asks about the rigidity and ad-hoc query limitations of single-table design.",
        },
    },

    # 62. Elasticsearch Inverted Index and BM25
    {
        "intent_group_id": "ig_db_elasticsearch_inverted_index",
        "domain": "databases_and_vector_search",
        "subtopic": "Elasticsearch",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "elasticsearch"}},
        "canonical_query": "How does Elasticsearch utilize inverted indexes and the BM25 algorithm to score full-text relevance?",
        "canonical_response": "Elasticsearch analyzes text into normalized tokens and builds an inverted index mapping each term to a posting list of matching document IDs. When a search query arrives, the BM25 algorithm calculates relevance scores based on Term Frequency (TF, with saturation preventing keyword stuffing) and Inverse Document Frequency (IDF, penalizing common terms).",
        "positive_paraphrase_1": {
            "query": "Explain how inverted index posting lists and BM25 term weighting rank search documents in Elasticsearch.",
            "reason": "Direct paraphrase explaining posting lists and BM25 ranking.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the BM25 scoring algorithm in Elasticsearch balance term frequency and document frequency?",
            "reason": "Syntactic variation focusing on term frequency saturation and document frequency.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you combine dense vector search with BM25 keyword search using Reciprocal Rank Fusion (RRF) in Elasticsearch?",
            "response": "Configure a hybrid search request with both a `knn` vector query and a `query` BM25 block, specifying `rank: { rrf: {} }` to merge and re-rank candidate rankings.",
            "reason": "Asks how to implement hybrid search with RRF rather than explaining BM25 inverted index mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write an Elasticsearch JSON index mapping defining a custom edge_ngram analyzer for autocomplete search.",
            "response": "```json\n{\n  \"settings\": { \"analysis\": { \"tokenizer\": { \"my_edge\": { \"type\": \"edge_ngram\", \"min_gram\": 2 } } } }\n}\n```",
            "reason": "Requests specific JSON analyzer mapping rather than explaining BM25 scoring.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does PostgreSQL tsvector and tsquery full-text search compare to Elasticsearch BM25 scoring?",
            "response": "PostgreSQL full-text search uses tf-idf like ranking (ts_rank) with GIN indexes, but lacks BM25 term frequency saturation, distributed sharding, and fuzzy text analyzers.",
            "reason": "Asks about PostgreSQL full-text search vs Elasticsearch.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does BM25 score calculation produce slightly inconsistent scores across different Elasticsearch shards?",
            "response": "By default, each shard calculates IDF based only on its local document frequencies; if documents are not uniformly distributed, scores vary unless `dfs_query_then_fetch` is enabled.",
            "reason": "Asks about local vs global shard IDF score inconsistency.",
        },
    },

    # 63. Cassandra Partition Keys and Clustering Columns
    {
        "intent_group_id": "ig_db_cassandra_partition_keys",
        "domain": "databases_and_vector_search",
        "subtopic": "Cassandra",
        "task_kind": "explanation",
        "dimensions": {"database": {"status": "known", "value": "cassandra"}},
        "canonical_query": "What is the difference between a partition key and a clustering column in Apache Cassandra primary keys?",
        "canonical_response": "In Cassandra, the partition key determines which cluster node stores the data via Murmur3 token hashing. Clustering columns determine the on-disk sorted order of rows within that specific partition. Together, they form the primary key, enabling fast sequential reads within a single node partition.",
        "positive_paraphrase_1": {
            "query": "Explain how partition keys control node placement while clustering columns control on-disk row sorting in Cassandra.",
            "reason": "Direct paraphrase explaining node placement vs on-disk sorting in Cassandra.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do composite primary keys in Cassandra determine cluster data distribution and query sorting?",
            "reason": "Syntactic variation focusing on data distribution and sorting via composite keys.",
        },
        "hard_negative_diff_operation": {
            "query": "Why does executing an ALLOW FILTERING query in Cassandra cause severe performance degradation on production clusters?",
            "response": "ALLOW FILTERING forces Cassandra to scan all partitions across all cluster nodes and filter rows in memory, bypassing token ring routing and resulting in massive distributed I/O.",
            "reason": "Asks about ALLOW FILTERING performance degradation rather than explaining partition vs clustering keys.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a CQL table definition for time-series sensor data with sensor_id as partition key and timestamp DESC as clustering key.",
            "response": "```sql\nCREATE TABLE sensor_readings (\n  sensor_id UUID,\n  reading_time TIMESTAMP,\n  value DOUBLE,\n  PRIMARY KEY ((sensor_id), reading_time)\n) WITH CLUSTERING ORDER BY (reading_time DESC);\n```",
            "reason": "Requests specific CQL table DDL code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does ScyllaDB C++ seastar thread-per-core architecture compare to Apache Cassandra Java architecture?",
            "response": "ScyllaDB rewrites Cassandra in C++ using an asynchronous thread-per-core architecture, eliminating Java JVM garbage collection pauses and achieving higher throughput per node.",
            "reason": "Asks about ScyllaDB C++ vs Apache Cassandra Java.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What danger arises if a Cassandra partition grows too large (hot wide partition exceeding 100MB)?",
            "response": "Wide partitions cause JVM heap pressure, slow compactions, and dropped read timeouts during repair operations, as individual partitions cannot be split across multiple nodes.",
            "reason": "Asks about wide partition scaling risks (anti-pattern / size limitation).",
        },
    },
]
