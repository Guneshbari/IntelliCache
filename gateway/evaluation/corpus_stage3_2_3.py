"""
IntelliCache Stage 3.2.3 Technical Corpus and Query Generator.

Generates a deterministic 2,000-document technical corpus across 5 core domains:
1. Programming languages (Rust, Python, Go, TypeScript)
2. Frameworks and libraries (FastAPI, React, PyTorch, Tokio)
3. Databases and vector search (PostgreSQL, pgvector, SQLite, DistributedStorage)
4. Concurrency and distributed systems (Raft, SingleFlight, DistributedLocks, EventDriven)
5. Protocols and networking (HTTP/2, WebSockets, SSE, TLS 1.3)

Includes 50 hard negatives and 20 distinct technical queries with known target intents.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List


def generate_stage3_2_3_corpus(total: int = 2000) -> List[Dict[str, Any]]:
    """
    Generates a deterministic technical corpus of 2,000 documents with diverse technical vocabulary,
    semantically related concepts, cross-domain technical explanations, and controlled hard negatives.
    """
    categories: Dict[str, Dict[str, List[str]]] = {
        "programming_languages": {
            "Rust": [
                "ownership and affine type system preventing use-after-free errors",
                "borrow checker enforcing single mutable reference xor multiple shared references",
                "lifetimes elision rules and explicit lifetime parameters in structs",
                "Send and Sync marker traits for fearless multi-threading concurrency",
                "zero-cost abstractions through monomorphization of generic functions",
                "pattern matching with exhaustive match expressions on Option and Result",
                "RAII resource acquisition is initialization with Drop trait destructor",
                "smart pointers comparison Arc Rc Box and RefCell interior mutability",
                "unsafe Rust guidelines for raw pointer dereferencing and FFI bindings",
                "Tokio work-stealing async task executor and non-blocking IO loop",
            ],
            "Python": [
                "asyncio event loop cooperative multitasking and task cancellation",
                "GIL global interpreter lock constraints on multi-core CPU parallelism",
                "memory management reference counting combined with generational garbage collector",
                "descriptors protocol dunder get dunder set and property implementation",
                "metaclasses type creation customization and abstract base classes",
                "generators coroutines yield from and memory-efficient streaming pipelines",
                "dataclasses Pydantic models serialization and runtime type validation",
                "multiprocessing shared memory IPC and process pool executors",
                "context managers dunder enter dunder exit and resource cleanup",
                "CPython bytecode compilation opcode dispatch and eval loop evaluation",
            ],
            "Go": [
                "goroutines lightweight stack allocation and M-to-N work stealing scheduler",
                "channels synchronization buffered vs unbuffered communication and select",
                "interfaces implicit structural satisfaction and interface table dispatch",
                "low-latency concurrent tri-color mark-and-sweep garbage collection",
                "context propagation with deadline timeout and hierarchical cancellation",
                "sync Pool object reuse reducing GC pressure in high throughput servers",
                "sync Mutex vs sync RWMutex contention trade-offs under high read loads",
                "defer statement execution LIFO stack unwinding and panic recovery",
                "memory escape analysis determining heap allocation vs stack allocation",
                "netpoll runtime integrated non-blocking network polling with epoll kqueue",
            ],
            "TypeScript": [
                "discriminated unions tagged unions and exhaustive type narrowing with never",
                "generics conditional types infer keyword and template literal string types",
                "structural typing subtyping compatibility vs nominal type systems",
                "mapped types utility types Partial Pick Omit Record and keyof operators",
                "strict null checks optional chaining nullish coalescing operator semantics",
                "module declaration merging ambient namespaces and global type augmentation",
                "const assertions as const preserving literal types and readonly tuples",
                "type guards user-defined type predicates with is operator validation",
                "decorator metadata reflection and dependency injection container design",
                "covariance contravariance and bivariance in function argument type checking",
            ],
        },
        "frameworks_and_libraries": {
            "FastAPI": [
                "asynchronous request handling with ASGI standard and Starlette internals",
                "dependency injection system using Depends decorator and yield dependencies",
                "automatic OpenAPI and Swagger documentation generation from type hints",
                "background tasks execution after returning response to client",
                "custom middleware stack for request authentication CORS and request logging",
                "request validation and error formatting with Pydantic v2 integration",
                "WebSocket endpoints bidirectional streaming and connection state handling",
                "sub-applications mounting and APIRouter modular routing configuration",
                "testing asynchronous endpoints using httpx AsyncClient and pytest-asyncio",
                "database session lifecycle management with async SQLAlchemy or Asyncpg",
            ],
            "React": [
                "Server Components architecture streaming SSR and client-side hydration",
                "concurrent rendering transitions with startTransition and useDeferredValue",
                "custom hooks composition dependency arrays and useEffect cleanup functions",
                "Fiber reconciliation algorithm priority lanes and virtual DOM diffing",
                "Context API performance optimization memoization with useMemo and useCallback",
                "Suspense boundaries asynchronous data fetching and fallback rendering",
                "Error boundaries catch componentDidCatch and declarative UI recovery",
                "state management comparison Redux Toolkit Zustand and React Query",
                "portals rendering children into external DOM nodes for modals and tooltips",
                "synthetic events system event delegation and event pooling mechanics",
            ],
            "PyTorch": [
                "autograd dynamic computation graph generation and backward propagation",
                "tensor operations GPU CUDA acceleration and memory management",
                "custom neural network modules inheriting from torch nn Module",
                "DistributedDataParallel DDP multi-GPU all-reduce synchronization across nodes",
                "DataLoader multiprocessing pinned memory and prefetching optimizations",
                "mixed precision training with torch cuda amp GradScaler",
                "custom autograd Function implementing forward and backward passes",
                "TorchScript JIT tracing scripting and model deployment optimizations",
                "learning rate schedulers warmups and optimizer state dictionaries",
                "profiler execution analyzing CPU GPU operator bottlenecks and kernel launch overhead",
            ],
            "Tokio": [
                "multi-threaded work-stealing async task executor in Rust systems",
                "epoll and kqueue non-blocking socket I/O driver event loop polling",
                "hashed timing wheel for high-resolution asynchronous timer delays",
                "cooperative scheduling with task budget yielding preventing starvation",
                "synchronization channels broadcast watch mpsc and oneshot implementations",
                "asynchronous file filesystem operations using blocking threadpool pool",
                "graceful shutdown coordination with CancellationToken and tracker guards",
                "spawn blocking offloading CPU-intensive computation from async threads",
                "tracing integration distributed span propagation and async instrumentation",
                "framed transport codecs splitting byte streams into typed messages",
            ],
        },
        "databases_and_vector_search": {
            "PostgreSQL": [
                "multi-version concurrency control MVCC and tuple visibility row versions",
                "write-ahead logging WAL crash recovery and log sequence numbers checkpoints",
                "B-tree index page split mechanisms GiST and GIN indexing internals",
                "autovacuum daemon worker processes table bloat reduction and freeze maps",
                "connection pooling transaction pooling vs session pooling with PgBouncer",
                "query planner cost estimation statistics histogram and join algorithms",
                "table partitioning declarative range list and hash partition pruning",
                "read replicas streaming replication synchronous vs asynchronous commit",
                "logical replication change data capture CDC using pgoutput decoder",
                "lock modes row-level locks advisory locks and deadlock detection engine",
            ],
            "pgvector": [
                "HNSW hierarchical navigable small world graph index construction",
                "hnsw ef_search tuning balancing recall and query latency trade-offs",
                "hnsw iterative index scan strict order avoiding candidate starvation",
                "IVFFlat inverted file index clustering with k-means Voronoi cells",
                "halfvec 16-bit half precision float vector storage and quantization",
                "vector cosine distance operator L2 Euclidean distance and inner product",
                "index filtering predicates pushdown vs post-index scan filtering",
                "memory consumption maintenance_work_mem and hnsw graph RAM requirements",
                "hybrid search combining BM25 full-text tsvector and vector similarity",
                "parallel index creation and multi-worker vector distance calculations",
            ],
            "SQLite": [
                "write-ahead logging WAL mode single-writer multiple-concurrent-readers",
                "zero-configuration embedded relational engine architecture and b-tree pages",
                "prepared statements bytecode compilation and virtual database engine VDBE",
                "in-memory database shared cache mode and temporary tables storage",
                "busy handler timeout retry logic for handling database locked errors",
                "full-text search FTS5 extension tokenizers and inverted index tables",
                "user-defined SQL functions and aggregate functions written in C and Python",
                "vacuum command page reorganization and database file defragmentation",
                "atomic commit rollback journal vs write-ahead log crash durability",
                "savepoints nested transactions and partial rollback capabilities",
            ],
            "DistributedStorage": [
                "LSM-tree log-structured merge tree memtable and SSTable compaction strategies",
                "consistent hashing with virtual nodes for distributed data partitioning",
                "vector clocks and version vectors for tracking causal event ordering",
                "quorum consensus read and write repair in Dynamo-style distributed systems",
                "Raft log compaction state machine snapshots and cluster membership changes",
                "distributed transaction coordination with two-phase commit 2PC protocols",
                "conflict-free replicated data types CRDTs state-based and operation-based",
                "sharding keys range-based vs hash-based partitioning hot-spot mitigation",
                "erasure coding Reed-Solomon codes vs multi-replica storage efficiency",
                "split-brain avoidance fencing tokens and lease-based distributed locks",
            ],
        },
        "concurrency_and_distributed_systems": {
            "Raft": [
                "leader election with randomized heartbeat election timeouts",
                "log replication entry matching property and committed index advancement",
                "safety invariants state machine safety and term progression validation",
                "cluster membership changes joint consensus and configuration transitions",
                "linearizable read operations read index and lease read optimizations",
                "snapshotting log compaction and installing snapshot RPCs on followers",
                "pre-vote protocol preventing disrupted partitioned nodes from incrementing terms",
                "persistent state durability writing currentTerm and votedFor to stable storage",
                "pipeline log replication overlapping append entries requests for throughput",
                "witness nodes non-voting learners and geographically distributed quorums",
            ],
            "SingleFlight": [
                "duplicate function call suppression and concurrent request deduplication",
                "shared result channel synchronization across multiple waiting callers",
                "mutex synchronization preventing cache stampede and thundering herd",
                "context cancellation propagation to active in-flight function calls",
                "panic recovery and re-panicking across duplicate waiting callers",
                "forget key method allowing immediate retries after transient upstream failures",
                "distributed singleflight coordination across multi-node gateway clusters",
                "benchmarking singleflight latency overhead vs raw backend request latency",
                "caching wrapper combining singleflight with short-lived memory caches",
                "rate limiting integration and admission control on high concurrency spikes",
            ],
            "DistributedLocks": [
                "Redlock algorithm multi-master Redis consensus and clock drift tolerance",
                "ZooKeeper ephemeral sequential znode lock recipe and watch notifications",
                "fencing tokens monotonic counters preventing stale write split-brain",
                "heartbeat keepalive background thread extending lease expiration safely",
                "lock acquisition timeout and exponential jittered backoff retry strategies",
                "etcd distributed lock implementation using leases and transactional revisions",
                "Consul sessions health check integration and key-value distributed locking",
                "reentrant distributed lock semantics and lock re-acquisition patterns",
                "fair distributed locking FIFO queueing with ticket locks and wait queues",
                "handling network partitions and split-brain scenarios in lock managers",
            ],
            "EventDriven": [
                "event loop phase ordering timers poll check close and microtasks dispatch",
                "Apache Kafka partition consumer groups offset commit and partition rebalancing",
                "idempotent consumer processing with deduplication tables and unique message IDs",
                "dead letter queues DLQ retry topics and poison pill message isolation",
                "saga pattern orchestration with centralized orchestrator state machine",
                "saga pattern choreography with decentralized event choreographing",
                "outbox pattern transactional messaging guaranteeing at-least-once delivery",
                "event sourcing immutable event logs and aggregate root state reconstruction",
                "schema registry message serialization with Protocol Buffers and Avro",
                "backpressure flow control reactive streams and consumer lag monitoring",
            ],
        },
        "protocols_and_networking": {
            "HTTP2": [
                "binary framing layer multiplexing bidirectional streams over single connection",
                "HPACK header compression static table dynamic table and Huffman encoding",
                "stream dependency prioritization and weight-based bandwidth allocation",
                "flow control window update frames preventing buffer overflow at endpoints",
                "server push stream promise frames and client RST_STREAM rejection",
                "connection lifecycle SETTINGS frame negotiation and GOAWAY graceful shutdown",
                "head-of-line blocking elimination at application layer vs TCP transport layer",
                "ping frames latency measurement and connection keepalive verification",
                "error handling stream errors vs connection-level PROTOCOL_ERROR codes",
                "HTTP2 server performance tuning connection concurrency and stream limits",
            ],
            "WebSockets": [
                "HTTP 101 Switching Protocols upgrade handshake and Sec-WebSocket-Key validation",
                "masked client frames and unmasked server frames masking key XOR decoding",
                "bidirectional full-duplex TCP communication streaming continuous messages",
                "control frames ping pong keepalive heartbeats and close frame status codes",
                "subprotocol negotiation Sec-WebSocket-Protocol and application framing",
                "message fragmentation FIN bit and continuation frames for large payloads",
                "horizontal scaling WebSocket servers with Redis pub-sub message backplanes",
                "SSL TLS termination WSS secure connections and proxy buffer configurations",
                "connection drop detection heartbeat timeouts and client reconnection backoff",
                "compression extension permessage-deflate sliding window memory trade-offs",
            ],
            "SSE": [
                "Server-Sent Events text-event-stream format and unidirectional push",
                "automatic client reconnection browser EventSource and Last-Event-ID header",
                "chunked transfer encoding over single persistent HTTP connection",
                "custom event types event field data field and multi-line message blocks",
                "low overhead server-to-browser notifications compared to WebSocket overhead",
                "HTTP2 multiplexing benefits for SSE overcoming browser connection limits",
                "proxy buffering issues X-Accel-Buffering no headers in Nginx and reverse proxies",
                "heartbeat comment lines keeping idle connections alive through NAT gateways",
                "state synchronization replay missed events from server buffer using event ID",
                "security CORS authorization headers and EventSource polyfills for tokens",
            ],
            "TLS13": [
                "1-RTT cryptographic handshake using ephemeral Diffie-Hellman key exchange",
                "0-RTT early data resumption replay attack vulnerability and mitigation",
                "forward secrecy elimination of static RSA key exchange in cipher suites",
                "AEAD authenticated encryption ChaCha20-Poly1305 and AES-GCM cipher suites",
                "certificate verification SAN subject alternative names and OCSP stapling",
                "KeyShare extension HelloRetryRequest and supported groups negotiation",
                "encrypted handshake extensions hiding server certificate from passive observers",
                "session tickets PSK pre-shared key resumption and ticket encryption keys",
                "TLS false start reducing application data latency on initial connection",
                "post-quantum hybrid key exchange integration Kyber with X25519 algorithms",
            ],
        },
    }

    angles = [
        "In-depth architectural analysis and core invariants of {sub} regarding {concept}.",
        "Production debugging and failure recovery techniques in {sub} when implementing {concept}.",
        "High-throughput optimization and CPU cache efficiency in {sub} systems utilizing {concept}.",
        "Memory profiling, heap allocation benchmarks, and leak detection in {sub} involving {concept}.",
        "Concurrency pitfalls, deadlocks, and race condition prevention in {sub} using {concept}.",
        "Static analysis, compiler guarantees, and type system verification of {concept} in {sub}.",
        "Comparative evaluation between alternative architectural patterns and {concept} in {sub}.",
        "Network I/O scalability, connection multiplexing, and backpressure handling for {concept} in {sub}.",
        "Data serialization, binary encoding, and wire format considerations for {concept} in {sub}.",
        "Real-world incident post-mortem analysis and operational resilience best practices for {concept} in {sub}.",
    ]

    docs: List[Dict[str, Any]] = []
    doc_id = 0
    for cat_name, subtopics in categories.items():
        for sub_name, concepts in subtopics.items():
            for concept in concepts:
                for angle in angles:
                    doc_id += 1
                    text = angle.format(sub=sub_name, concept=concept)
                    docs.append({
                        "id": f"doc_{doc_id:04d}",
                        "category": cat_name,
                        "subtopic": sub_name,
                        "concept": concept,
                        "text": text,
                        "is_negative": False,
                    })

    # Hard negatives (50 documents)
    negatives = [
        ("programming_languages", "Rust", "How to remove rust stains from garden tools using white vinegar and baking soda paste."),
        ("databases_and_vector_search", "pgvector", "Vector graphic design tips for creating scalable logos in Adobe Illustrator using bezier curves."),
        ("frameworks_and_libraries", "FastAPI", "Fast API speed in making espresso coffee using a manual lever espresso machine."),
        ("concurrency_and_distributed_systems", "SingleFlight", "Booking a single flight ticket with carry-on luggage for an international vacation."),
        ("protocols_and_networking", "WebSockets", "Replacing electrical wall sockets and installing child-safety wall plate covers."),
    ]
    for n_idx, (cat, sub, neg_text) in enumerate(negatives):
        for rep in range(10):
            idx_to_replace = (n_idx * 10 + rep) * 40
            if idx_to_replace < len(docs):
                original_id = docs[idx_to_replace]["id"]
                docs[idx_to_replace] = {
                    "id": original_id,
                    "category": cat,
                    "subtopic": sub,
                    "concept": "hard_negative_distractor",
                    "text": f"{neg_text} (Reference #{rep + 1})",
                    "is_negative": True,
                }

    return docs[:total]


def get_stage3_2_3_queries() -> List[Dict[str, Any]]:
    """
    Returns 20 distinct technical queries with explicit technical intent and categories.
    """
    return [
        {
            "query_id": 1,
            "category": "programming_languages",
            "subtopic": "Rust",
            "query": "How does Rust manage memory ownership and borrowing without a garbage collector?",
            "intent": "Rust ownership, borrow checker and lifetimes",
        },
        {
            "query_id": 2,
            "category": "programming_languages",
            "subtopic": "Python",
            "query": "Explain Python asyncio event loop tasks and non-blocking I/O operations.",
            "intent": "Python asyncio event loop and tasks",
        },
        {
            "query_id": 3,
            "category": "programming_languages",
            "subtopic": "Go",
            "query": "How do Go channels and goroutines implement concurrent communication?",
            "intent": "Go goroutines, channels and select synchronization",
        },
        {
            "query_id": 4,
            "category": "programming_languages",
            "subtopic": "TypeScript",
            "query": "How to use TypeScript discriminated unions for exhaustive type checking?",
            "intent": "TypeScript discriminated unions and narrowing",
        },
        {
            "query_id": 5,
            "category": "frameworks_and_libraries",
            "subtopic": "FastAPI",
            "query": "FastAPI dependency injection with Depends and asynchronous path operations.",
            "intent": "FastAPI dependency injection and async handlers",
        },
        {
            "query_id": 6,
            "category": "frameworks_and_libraries",
            "subtopic": "React",
            "query": "How does React concurrent rendering and Server Components streaming work?",
            "intent": "React Server Components and concurrent rendering",
        },
        {
            "query_id": 7,
            "category": "frameworks_and_libraries",
            "subtopic": "PyTorch",
            "query": "PyTorch autograd dynamic computation graph and tensor backward pass.",
            "intent": "PyTorch autograd and tensor backward propagation",
        },
        {
            "query_id": 8,
            "category": "frameworks_and_libraries",
            "subtopic": "Tokio",
            "query": "How does Tokio work-stealing runtime schedule asynchronous tasks in Rust?",
            "intent": "Tokio work-stealing async runtime scheduler",
        },
        {
            "query_id": 9,
            "category": "databases_and_vector_search",
            "subtopic": "PostgreSQL",
            "query": "PostgreSQL write-ahead logging WAL crash recovery and checkpoint mechanism.",
            "intent": "PostgreSQL WAL and LSN crash recovery",
        },
        {
            "query_id": 10,
            "category": "databases_and_vector_search",
            "subtopic": "pgvector",
            "query": "pgvector HNSW index tuning with ef_search and m parameters.",
            "intent": "pgvector HNSW index configuration and tuning",
        },
        {
            "query_id": 11,
            "category": "databases_and_vector_search",
            "subtopic": "SQLite",
            "query": "How does SQLite write-ahead logging handle concurrent readers and writers?",
            "intent": "SQLite WAL concurrent readers and single writer",
        },
        {
            "query_id": 12,
            "category": "databases_and_vector_search",
            "subtopic": "DistributedStorage",
            "query": "LSM-tree compaction and SSTable storage in distributed databases.",
            "intent": "LSM-tree compaction and SSTables in storage engines",
        },
        {
            "query_id": 13,
            "category": "concurrency_and_distributed_systems",
            "subtopic": "Raft",
            "query": "Raft consensus leader election randomized timers and log entry replication.",
            "intent": "Raft leader election and log entry replication",
        },
        {
            "query_id": 14,
            "category": "concurrency_and_distributed_systems",
            "subtopic": "SingleFlight",
            "query": "SingleFlight pattern for suppressing duplicate concurrent function executions.",
            "intent": "SingleFlight duplicate call suppression and cache stampede prevention",
        },
        {
            "query_id": 15,
            "category": "concurrency_and_distributed_systems",
            "subtopic": "DistributedLocks",
            "query": "Distributed lock implementation using Redlock vs ZooKeeper ephemeral nodes.",
            "intent": "Distributed locks with Redis Redlock and ZooKeeper recipes",
        },
        {
            "query_id": 16,
            "category": "concurrency_and_distributed_systems",
            "subtopic": "EventDriven",
            "query": "Event-driven architecture with Kafka message ordering and consumer group rebalancing.",
            "intent": "Kafka partitioning, consumer groups and event-driven architectures",
        },
        {
            "query_id": 17,
            "category": "protocols_and_networking",
            "subtopic": "HTTP2",
            "query": "HTTP/2 multiplexed streams and HPACK header compression.",
            "intent": "HTTP/2 binary framing, multiplexing and HPACK",
        },
        {
            "query_id": 18,
            "category": "protocols_and_networking",
            "subtopic": "WebSockets",
            "query": "WebSockets bidirectional full-duplex TCP communication and frame masking.",
            "intent": "WebSockets 101 Switching Protocols and duplex frame masking",
        },
        {
            "query_id": 19,
            "category": "protocols_and_networking",
            "subtopic": "SSE",
            "query": "Server-Sent Events SSE unidirectional streaming with Last-Event-ID reconnection.",
            "intent": "Server-Sent Events text-event-stream and reconnection",
        },
        {
            "query_id": 20,
            "category": "protocols_and_networking",
            "subtopic": "TLS13",
            "query": "TLS 1.3 cryptographic handshake latency reduction and forward secrecy.",
            "intent": "TLS 1.3 1-RTT handshake and forward secrecy",
        },
    ]


def get_stage3_2_3_corpus_hash() -> str:
    """Computes a SHA-256 fingerprint over the canonical corpus representation."""
    docs = generate_stage3_2_3_corpus()
    canonical = json.dumps(
        [{"id": d["id"], "text": d["text"], "category": d["category"]} for d in docs],
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
