"""
Domain 4: Concurrency and Distributed Systems (21 intent groups).
Covers Raft, Redlock, Fencing Tokens, Paxos, SingleFlight, 2PC, Sagas, Locking, and Event Systems.
"""

from typing import Any, Dict, List

DS_INTENT_GROUPS: List[Dict[str, Any]] = [
    # 64. Raft Leader Election
    {
        "intent_group_id": "ig_ds_raft_leader_election",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "Raft",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "raft"}},
        "canonical_query": "How do randomized election timeouts in the Raft consensus algorithm prevent split votes during leader election?",
        "canonical_response": "Raft nodes transition from Follower to Candidate if they do not receive heartbeats before an election timeout expires. By randomizing election timeouts across nodes (e.g. 150-300ms), Raft ensures one node's timer almost always expires first. This node requests votes and establishes a majority quorum before competitors start elections, avoiding repeated split-vote ties.",
        "positive_paraphrase_1": {
            "query": "Explain how Raft avoids perpetual split-vote ties during leader election using randomized election timers.",
            "reason": "Direct paraphrase explaining how randomized timers prevent split votes in Raft.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does randomized timer staggering guarantee fast leader election in Raft consensus?",
            "reason": "Syntactic variation focusing on timer staggering and leader election speed.",
        },
        "hard_negative_diff_operation": {
            "query": "How does a Raft node handle incoming AppendEntries RPCs from a leader with a stale term number?",
            "response": "When a node receives an AppendEntries RPC with a term less than its currentTerm, it rejects the RPC immediately and returns false, informing the caller to step down as leader.",
            "reason": "Asks how stale AppendEntries RPCs are rejected rather than explaining election timeouts.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python class modeling a Raft node state machine with states Follower, Candidate, and Leader.",
            "response": "```python\nclass RaftNode:\n    def __init__(self, id): self.id = id; self.state = 'Follower'; self.term = 0\n    def start_election(self): self.state = 'Candidate'; self.term += 1\n```",
            "reason": "Requests specific state machine code implementation rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Multi-Paxos leader election compare to Raft's single-leader election mechanism?",
            "response": "Multi-Paxos allows any proposer to attempt leadership by executing Phase 1 prepare rounds, whereas Raft enforces a single strong leader with append-only log synchronization.",
            "reason": "Asks about Multi-Paxos leader election vs Raft.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if network latency exceeds the Raft election timeout duration across a cluster?",
            "response": "If network latency exceeds election timeouts, heartbeats arrive late, causing nodes to constantly time out, start spurious elections, increment terms, and disrupt cluster availability.",
            "reason": "Asks about election instability when network latency exceeds timeout (configuration failure).",
        },
    },

    # 65. Raft Log Replication
    {
        "intent_group_id": "ig_ds_raft_log_replication",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "Raft",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "raft"}},
        "canonical_query": "How does the Raft log matching property ensure replicated state machines remain strictly consistent across followers?",
        "canonical_response": "Raft enforces the Log Matching Property: if two entries in different logs have the same index and term, they store the same command, and their logs are identical in all preceding entries. Leaders enforce this by including prevLogIndex and prevLogTerm in AppendEntries; if a follower finds a mismatch, it rejects the entry until the leader backtracks and overwrites divergent follower entries.",
        "positive_paraphrase_1": {
            "query": "Explain how the Raft Log Matching Property and AppendEntries consistency checks synchronize follower logs.",
            "reason": "Direct paraphrase explaining Log Matching Property and AppendEntries consistency checks.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does Raft detect and overwrite conflicting uncommitted log entries on partitioned followers?",
            "reason": "Syntactic variation focusing on detecting and overwriting conflicting uncommitted entries.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Raft take log snapshots (compaction) to truncate committed log entries and free disk space?",
            "response": "Each node independently takes a snapshot of its state machine state up to a specific committed index, discarding all preceding log entries, and uses InstallSnapshot RPC to bring lagging nodes up to date.",
            "reason": "Asks how log snapshot compaction works rather than explaining the Log Matching Property.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write the Go struct definitions for Raft AppendEntriesArgs and AppendEntriesReply RPC payloads.",
            "response": "```go\ntype AppendEntriesArgs struct {\n    Term, LeaderId, PrevLogIndex, PrevLogTerm int\n    Entries []LogEntry\n    LeaderCommit int\n}\n```",
            "reason": "Requests specific Go RPC struct definitions rather than conceptual consistency explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Zab (ZooKeeper Atomic Broadcast) log replication protocol compare to Raft log replication?",
            "response": "Zab uses a two-phase commit-like broadcast with epoch IDs and separate recovery modes, whereas Raft unifies leader election, log replication, and safety into a single consensus model.",
            "reason": "Asks about ZooKeeper Zab protocol vs Raft.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Can a Raft leader commit a log entry from a previous term solely by counting replica majority votes?",
            "response": "No. Raft explicitly forbids committing entries from previous terms by counting replicas; a leader must commit an entry from its current term by majority, which indirectly commits earlier entries.",
            "reason": "Asks about the subtle safety rule regarding committing previous term entries (safety invariant).",
        },
    },

    # 66. Redis Redlock Algorithm
    {
        "intent_group_id": "ig_ds_redis_redlock_algorithm",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "DistributedLocks",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "redis_lock"}},
        "canonical_query": "How does the Redlock algorithm coordinate distributed mutual exclusion across N independent Redis master nodes?",
        "canonical_response": "Redlock attempts to acquire a lock by requesting a unique random token with a TTL across N independent Redis masters sequentially with a short timeout. The lock is successfully acquired if a majority (N/2 + 1) of nodes respond with success and the total time elapsed is less than the lock validity time, ensuring fault tolerance without replication.",
        "positive_paraphrase_1": {
            "query": "Explain how the Redlock algorithm uses majority quorum across independent Redis instances for distributed locking.",
            "reason": "Direct paraphrase explaining Redlock majority quorum across independent masters.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does Redlock guarantee safety and liveness across multiple Redis nodes without master-replica failover lag?",
            "reason": "Syntactic variation focusing on safety and independence from replication failover lag.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you release a Redis distributed lock safely using a Lua script to ensure token ownership?",
            "response": "```lua\nif redis.call('get', KEYS[1]) == ARGV[1] then\n    return redis.call('del', KEYS[1])\nelse\n    return 0\nend\n```",
            "reason": "Asks for the Lua release script rather than explaining the multi-node Redlock consensus protocol.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python implementation of the Redlock acquire and release protocol using redis-py across 3 clusters.",
            "response": "```python\nclass Redlock:\n    def acquire(self, resource, ttl):\n        # acquire on 3 nodes and verify elapsed < ttl\n```",
            "reason": "Requests specific Python code implementation rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Apache Curator Zookeeper recipes for InterProcessMutex compare to Redis Redlock?",
            "response": "ZooKeeper uses ephemeral sequential znodes with watches for strict, consensus-backed distributed locks that release automatically upon session loss without clock skew vulnerabilities.",
            "reason": "Asks about ZooKeeper Curator vs Redis Redlock.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why did Martin Kleppmann critique the Redlock algorithm regarding unsynchronized system clocks and GC pauses?",
            "response": "Kleppmann proved that Redlock relies on bounded clock drift assumptions; if a node experiences an NTP clock jump or long GC pause, the lock lease expires while the client still thinks it owns the lock.",
            "reason": "Asks about theoretical critiques and clock-drift vulnerability in Redlock.",
        },
    },

    # 67. Fencing Tokens
    {
        "intent_group_id": "ig_ds_fencing_tokens_locks",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "DistributedLocks",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "fencing_tokens"}},
        "canonical_query": "How do monotonically increasing fencing tokens prevent split-brain writes caused by expired distributed lock leases?",
        "canonical_response": "When a client acquires a distributed lock, the lock service issues an incrementing number (fencing token, e.g. 34, 35). When writing to the storage resource (database or file), the client includes this token. The storage system rejects any write whose token is less than or equal to the highest token it has already processed, blocking stale writes from slow clients whose leases expired.",
        "positive_paraphrase_1": {
            "query": "Explain how fencing tokens stop slow or paused clients from corrupting storage after a distributed lock timeout.",
            "reason": "Direct paraphrase explaining how incrementing fencing tokens protect storage after lock timeouts.",
        },
        "positive_paraphrase_2": {
            "query": "Why does verifying an incrementing token at the storage layer solve the distributed lock lease expiration problem?",
            "reason": "Syntactic variation focusing on storage-layer token verification.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement an optimistic concurrency version check in PostgreSQL using UPDATE WHERE version = X?",
            "response": "Execute `UPDATE accounts SET balance = $1, version = version + 1 WHERE id = $2 AND version = $3;`. If rowcount is 0, another transaction updated the record first, triggering a retry.",
            "reason": "Asks how to write SQL version check statements rather than explaining distributed lock fencing tokens.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python storage service that enforces fencing tokens on file write operations.",
            "response": "```python\nclass FencedStore:\n    def __init__(self): self.max_token = 0\n    def write(self, token, data):\n        if token <= self.max_token: raise StaleTokenError()\n        self.max_token = token; self.data = data\n```",
            "reason": "Requests specific Python class implementation rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does AWS DynamoDB Conditional Writes compare to fencing token validation?",
            "response": "DynamoDB conditional writes evaluate expressions (attribute_exists, version = :expected) at the database layer before writing, acting as a built-in optimistic locking or fencing check.",
            "reason": "Asks about DynamoDB conditional writes vs fencing tokens.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can distributed locks NEVER guarantee mutual exclusion without server-side validation or fencing tokens?",
            "response": "Because of asynchronous network delays, OS thread pauses, and GC stalls; a client can be paused after acquiring the lock and wake up after the lease expired, blindly issuing an unsafe write unless fenced.",
            "reason": "Asks about the fundamental theoretical impossibility of safe locking without fencing.",
        },
    },

    # 68. Paxos Consensus
    {
        "intent_group_id": "ig_ds_paxos_consensus",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "Paxos",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "paxos"}},
        "canonical_query": "What occurs during Phase 1 (Prepare/Promise) and Phase 2 (Accept/Accepted) in classic Paxos consensus?",
        "canonical_response": "In Phase 1, a Proposer sends `Prepare(n)` with proposal number n to Acceptors; Acceptors reply `Promise(n)` agreeing not to accept proposals < n and returning the highest proposal already accepted. In Phase 2, the Proposer sends `Accept(n, v)` with the value v from the highest promised proposal (or its own value); if Acceptors receive it without seeing a higher proposal, they reply `Accepted(n, v)` to establish consensus.",
        "positive_paraphrase_1": {
            "query": "Explain the message flow and voting rules of Phase 1 and Phase 2 in classic Paxos.",
            "reason": "Direct paraphrase explaining Prepare/Promise and Accept/Accepted phases in Paxos.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do Paxos proposal numbers and promise guarantees prevent split consensus values?",
            "reason": "Syntactic variation focusing on proposal numbers and promise guarantees.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Multi-Paxos optimize classic Paxos by executing Phase 1 only once during leader election?",
            "response": "Multi-Paxos elects a stable leader that executes Phase 1 once for all subsequent log instances; steady-state log replication then requires only Phase 2 (one roundtrip per commit).",
            "reason": "Asks how Multi-Paxos optimizes roundtrips rather than explaining classic two-phase Paxos.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write Python dataclasses representing Paxos messages: Prepare, Promise, Accept, Accepted, and Nack.",
            "response": "```python\n@dataclass\nclass Prepare: n: int\n@dataclass\nclass Promise: n: int; accepted_n: Optional[int]; accepted_v: Any\n```",
            "reason": "Requests specific message dataclasses rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "Why is Raft considered more understandable and implementable than classic Paxos?",
            "response": "Raft decomposes consensus into explicit, independent sub-problems (leader election, log replication, membership safety) with strong invariants, whereas Paxos is abstract and omits cluster membership mechanics.",
            "reason": "Asks why Raft is simpler than Paxos rather than explaining Paxos protocol phases.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "How can two competing Proposers cause a live-lock (dueling proposers) in classic Paxos?",
            "response": "If Proposer A sends Prepare(n1), and Proposer B sends Prepare(n2 > n1) before A sends Accept, A's Accept is rejected; A then sends Prepare(n3 > n2), repeatedly preempting each other without progress.",
            "reason": "Asks about the dueling proposers livelock failure scenario in Paxos.",
        },
    },

    # 69. Split-Brain Protection
    {
        "intent_group_id": "ig_ds_split_brain_protection",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "QuorumConsensus",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "quorum"}},
        "canonical_query": "Why does requiring a strict majority quorum (N/2 + 1) prevent split-brain in distributed systems?",
        "canonical_response": "In an N-node cluster, a strict majority requires at least floor(N/2) + 1 votes. If a network partition divides the cluster into multiple isolated components, at most one component can possibly contain a majority of nodes. Because any two majorities must overlap by at least one node, conflicting decisions cannot be committed concurrently.",
        "positive_paraphrase_1": {
            "query": "Explain how quorum intersection (floor(N/2) + 1) mathematically eliminates split-brain partitions.",
            "reason": "Direct paraphrase explaining quorum intersection mathematics preventing split brain.",
        },
        "positive_paraphrase_2": {
            "query": "Why can an even number of nodes (like 4 or 6) fail to make progress during equal network splits?",
            "reason": "Syntactic variation focusing on 50/50 split ties with even node counts.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure an arbiter or witness node in MongoDB replica sets to provide a tie-breaking vote?",
            "response": "Add a lightweight arbiter node (`rs.addArbiter('host:port')`) that holds no dataset data and participates strictly in election voting to break ties in even-numbered clusters.",
            "reason": "Asks how to add a MongoDB arbiter rather than explaining mathematical quorum intersection.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python function that calculates whether an array of node responses forms a valid majority quorum.",
            "response": "```python\ndef has_quorum(responses, total_cluster_nodes):\n    return len([r for r in responses if r.ok]) > total_cluster_nodes // 2\n```",
            "reason": "Requests specific Python code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Elasticsearch cluster master election deal with split-brain via discovery.zen.minimum_master_nodes?",
            "response": "Historically, Elasticsearch required setting `minimum_master_nodes = (N/2) + 1` to prevent isolated sub-clusters from electing competing masters; modern ES uses Raft-like cluster state consensus.",
            "reason": "Asks about legacy Elasticsearch config rather than general quorum mathematics.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a network partition splits a 5-node cluster into partitions of 2 and 3 nodes?",
            "response": "The 3-node partition holds a majority and continues processing reads and writes normally; the 2-node partition lacks a majority and must reject write requests or step down to read-only.",
            "reason": "Asks about specific partition scenario outcomes (2 vs 3 nodes) rather than general mathematical proof.",
        },
    },

    # 70. SingleFlight Deduplication
    {
        "intent_group_id": "ig_ds_singleflight_deduplication",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "ConcurrencyPatterns",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "singleflight"}},
        "canonical_query": "How does the SingleFlight concurrency pattern suppress duplicate downstream requests and prevent cache stampedes?",
        "canonical_response": "SingleFlight maintains an internal map of active in-flight function calls keyed by request identifier. When multiple concurrent requests arrive for the exact same key during a cache miss (thundering herd), only the first caller executes the downstream query; all subsequent callers subscribe to the in-flight future and share the exact same returned response.",
        "positive_paraphrase_1": {
            "query": "Explain how SingleFlight deduplicates simultaneous requests to protect databases from thundering herd cache stampedes.",
            "reason": "Direct paraphrase explaining call deduplication and cache stampede protection via SingleFlight.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does in-flight request coalescing in SingleFlight ensure only one upstream call is executed?",
            "reason": "Syntactic variation focusing on in-flight request coalescing.",
        },
        "hard_negative_diff_operation": {
            "query": "How does probabilistic early expiration (XFetch algorithm) prevent cache stampedes compared to SingleFlight?",
            "response": "XFetch recalculates cached values before they expire with a probability proportional to read rate and remaining TTL, whereas SingleFlight coordinates concurrent requests after a miss occurs.",
            "reason": "Asks about probabilistic early expiration (XFetch) vs SingleFlight.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a thread-safe Python SingleFlight class using asyncio.Event and asyncio.Lock.",
            "response": "```python\nclass SingleFlight:\n    def __init__(self): self.calls = {}; self.lock = asyncio.Lock()\n    async def do(self, key, coro):\n        async with self.lock:\n            if key in self.calls: return await self.calls[key]\n            # register and execute\n```",
            "reason": "Requests specific Python implementation code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Go golang.org/x/sync/singleflight package implementation compare to Python asyncio locks?",
            "response": "Go singleflight uses sync.WaitGroup and sync.Mutex to block waiting goroutines without allocations, whereas Python asyncio uses Future objects and cooperative event loop scheduling.",
            "reason": "Asks about Go singleflight internals vs Python.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a downstream call hangs indefinitely inside a SingleFlight group without a timeout?",
            "response": "All current and future callers requesting that key will block indefinitely waiting for the hung future, causing request backpressure and exhausting client connection pools.",
            "reason": "Asks about catastrophic hanging failure mode when timeouts are omitted in SingleFlight.",
        },
    },

    # 71. Cache-Aside Pattern
    {
        "intent_group_id": "ig_ds_cache_aside_pattern",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "CachingPatterns",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "cache_aside"}},
        "canonical_query": "How does the cache-aside (lazy loading) pattern manage reads, writes, and cache invalidation?",
        "canonical_response": "In cache-aside, the application coordinates both cache and database. On read, the application checks cache; on hit, it returns data; on miss, it reads from the database, populates the cache with a TTL, and returns. On write, the application updates the database directly and invalidates (deletes) the matching key in cache.",
        "positive_paraphrase_1": {
            "query": "Explain the read-through and write-invalidation lifecycle of the cache-aside pattern in web architectures.",
            "reason": "Direct paraphrase explaining read, write, and invalidation in cache-aside.",
        },
        "positive_paraphrase_2": {
            "query": "Why does the cache-aside pattern delete cached keys upon database updates instead of updating cache values directly?",
            "reason": "Syntactic variation focusing on invalidation vs update-in-place on write.",
        },
        "hard_negative_diff_operation": {
            "query": "What is the difference between cache-aside and write-through caching?",
            "response": "In write-through caching, the application writes only to the cache; the cache layer is responsible for synchronously writing data to the underlying database before acknowledging success.",
            "reason": "Contrasts cache-aside with write-through rather than explaining cache-aside mechanics.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python async service function get_user(id) implementing cache-aside with Redis and PostgreSQL.",
            "response": "```python\nasync def get_user(uid):\n    cached = await redis.get(f'user:{uid}')\n    if cached: return json.loads(cached)\n    u = await db.fetch_user(uid)\n    await redis.setex(f'user:{uid}', 3600, json.dumps(u))\n    return u\n```",
            "reason": "Requests specific Python code implementation rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Hibernate second-level cache compare to application-level cache-aside with Redis?",
            "response": "Hibernate second-level cache intercepts entity queries transparently inside the ORM layer, whereas cache-aside is explicitly orchestrated in business application code.",
            "reason": "Asks about Hibernate second-level cache vs cache-aside.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "How can concurrent writes and reads cause stale data in cache-aside without transactional locking?",
            "response": "If a reader encounters a cache miss and reads from the DB, a concurrent writer updates the DB and evicts the cache, and the slow reader then writes its stale value into cache with a fresh TTL.",
            "reason": "Asks about the race condition leading to stale cache entries.",
        },
    },

    # 72. Two-Phase Commit (2PC)
    {
        "intent_group_id": "ig_ds_two_phase_commit",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "DistributedTransactions",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "2pc"}},
        "canonical_query": "What happens during the Prepare and Commit phases of Two-Phase Commit (2PC) and why is coordinator failure blocking?",
        "canonical_response": "In Phase 1 (Prepare), coordinator asks all participants if they can commit; participants vote YES (locking resources and writing undo/redo logs) or NO. In Phase 2, if all voted YES, coordinator writes COMMIT log and sends Commit; otherwise it sends Rollback. If coordinator crashes after participants vote YES, participants remain blocked holding locks indefinitely.",
        "positive_paraphrase_1": {
            "query": "Explain how 2PC guarantees atomic distributed transactions and why it is vulnerable to coordinator crashes.",
            "reason": "Direct paraphrase explaining 2PC phases and coordinator blocking failure.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Two-Phase Commit protocol coordinate multi-database transaction commits and rollbacks?",
            "reason": "Syntactic variation focusing on multi-database atomic coordination.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Three-Phase Commit (3PC) add a PreCommit phase to avoid non-blocking coordinator crashes?",
            "response": "3PC splits the commit decision into PreCommit and DoCommit with timeouts, allowing participants to time out and commit or abort safely if the coordinator disappears.",
            "reason": "Asks how 3PC adds PreCommit rather than explaining 2PC phases and blocking.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python simulation of a 2PC coordinator handling participant voting timeouts.",
            "response": "```python\nclass Coordinator:\n    def execute_tx(self, nodes):\n        votes = [n.prepare() for n in nodes]\n        if all(v == 'YES' for v in votes): [n.commit() for n in nodes]\n        else: [n.abort() for n in nodes]\n```",
            "reason": "Requests specific simulation code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does the Saga pattern compare to 2PC for modern microservices transactions?",
            "response": "2PC provides strict ACID atomicity with synchronous resource locks, while Sagas provide eventual consistency using asynchronous events and compensating actions without distributed locking.",
            "reason": "Asks about Saga pattern vs 2PC.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does Two-Phase Commit severely limit transaction throughput in high-scale distributed systems?",
            "response": "2PC holds exclusive database row and table locks across multiple network roundtrips until all participants reply, causing massive lock contention, high latency, and vulnerability to network partitions.",
            "reason": "Asks about throughput and latency bottlenecks in 2PC.",
        },
    },

    # 73. Saga Pattern
    {
        "intent_group_id": "ig_ds_saga_pattern_distributed",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "DistributedTransactions",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "saga"}},
        "canonical_query": "What is the difference between choreography and orchestration in Saga distributed transaction management?",
        "canonical_response": "In choreography, each microservice executes its local transaction and publishes domain events; other services listen to events and trigger their local transactions or compensating actions without central control. In orchestration, a central Saga Orchestrator tells participants which local transactions to execute and explicitly coordinates compensating actions on failure.",
        "positive_paraphrase_1": {
            "query": "How do choreographed Sagas differ from orchestrated Sagas in microservice transaction coordination?",
            "reason": "Direct paraphrase contrasting choreography with orchestration in Sagas.",
        },
        "positive_paraphrase_2": {
            "query": "Explain the trade-offs between event-driven choreography and centralized orchestrators in Saga architectures.",
            "reason": "Syntactic variation focusing on trade-offs between choreography and orchestration.",
        },
        "hard_negative_diff_operation": {
            "query": "How do compensating transactions in a Saga roll back partial changes without database-level rollback?",
            "response": "Compensating transactions execute semantic inverses of previous operations (e.g. issuing a refund transaction to undo a charge, or restocking inventory) rather than database log rollbacks.",
            "reason": "Asks how compensating transactions work rather than comparing choreography vs orchestration.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python Temporal workflow definition implementing an order checkout Saga with compensation activities.",
            "response": "```python\n@workflow.defn\nclass OrderSaga:\n    @workflow.run\n    async def run(self, order):\n        try: await workflow.execute_activity(charge_card)\n        except Exception: await workflow.execute_activity(cancel_order)\n```",
            "reason": "Requests specific Temporal workflow code rather than architectural comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does AWS Step Functions state machine execution compare to custom Saga orchestrators?",
            "response": "AWS Step Functions is a managed visual orchestrator handling retries, timeouts, and compensation steps via JSON state machine definitions, reducing custom orchestrator codebase maintenance.",
            "reason": "Asks about AWS Step Functions vs custom orchestrators.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does the Saga pattern NOT guarantee isolation (the 'I' in ACID) across participating services?",
            "response": "Sagas commit local transactions immediately, making intermediate states visible to concurrent transactions before the entire Saga finishes, which can lead to dirty reads without semantic countermeasures.",
            "reason": "Asks about the lack of isolation in Sagas (ACID trade-off).",
        },
    },

    # 74. Consistent Hashing
    {
        "intent_group_id": "ig_ds_consistent_hashing",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "DistributedStorage",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "consistent_hashing"}},
        "canonical_query": "How does consistent hashing with virtual nodes prevent massive key remapping when nodes are added or removed?",
        "canonical_response": "Consistent hashing maps both servers and keys onto a circular hash ring (0 to 2^32-1). A key is assigned to the first server found clockwise on the ring. Adding or removing a node affects only its immediate neighbors (remapping K/N keys instead of all keys). Virtual nodes assign multiple ring tokens per physical server, ensuring uniform load distribution.",
        "positive_paraphrase_1": {
            "query": "Explain how a consistent hashing ring and virtual nodes minimize cache remapping during cluster resizing.",
            "reason": "Direct paraphrase explaining ring topology and virtual nodes minimizing key remapping.",
        },
        "positive_paraphrase_2": {
            "query": "Why does consistent hashing remap only K/N keys compared to modulo hashing when nodes change?",
            "reason": "Syntactic variation focusing on K/N key remapping vs modulo hashing.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Maglev hashing algorithm compare to standard consistent hashing for network load balancers?",
            "response": "Google Maglev uses lookup tables built from permutation tables to achieve near-optimal load distribution and constant-time lookup without binary searching ring tokens.",
            "reason": "Asks about Google Maglev hashing vs standard consistent hashing.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python ConsistentHashRing class implementing add_node, remove_node, and get_node with bisect.",
            "response": "```python\nclass HashRing:\n    def __init__(self, vnodes=100):\n        self.ring = {}; self.keys = []\n    def get_node(self, k):\n        h = hash(k); idx = bisect.bisect(self.keys, h)\n        return self.ring[self.keys[idx % len(self.keys)]]\n```",
            "reason": "Requests specific Python code implementation rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Cassandra token ring architecture implement consistent hashing across replicas?",
            "response": "Cassandra partitions its token range across nodes using Murmur3Partitioner, placing primary replicas on the first clockwise node and secondary replicas on subsequent physical nodes.",
            "reason": "Asks about Cassandra token ring implementation vs general consistent hashing.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes severe hotspotting in consistent hashing if virtual nodes (vnodes) are omitted?",
            "response": "Without virtual nodes, randomly positioned physical servers create highly non-uniform ring intervals, causing one server to be assigned a massive segment and receive 5-10x more traffic than others.",
            "reason": "Asks about hotspots resulting from omitting virtual nodes.",
        },
    },

    # 75. Optimistic vs Pessimistic Locking
    {
        "intent_group_id": "ig_ds_optimistic_vs_pessimistic_locking",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "ConcurrencyControl",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "locking"}},
        "canonical_query": "What are the trade-offs between optimistic concurrency control (OCC) and pessimistic locking in database design?",
        "canonical_response": "Pessimistic locking uses database locks (SELECT FOR UPDATE) to prevent concurrent modifications, guaranteeing safety at the expense of blocking readers/writers and risking deadlocks. Optimistic concurrency control (OCC) does not lock records during reads; it checks a version column at update time, succeeding without lock overhead under low contention but requiring retries under high contention.",
        "positive_paraphrase_1": {
            "query": "Compare optimistic locking version checking with pessimistic SELECT FOR UPDATE for concurrent transactions.",
            "reason": "Direct paraphrase comparing OCC versioning with pessimistic locking.",
        },
        "positive_paraphrase_2": {
            "query": "When is optimistic concurrency control superior to pessimistic row-level locking in web APIs?",
            "reason": "Syntactic variation focusing on when OCC outperforms pessimistic locking.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you handle OptimisticLockException retries in JPA / Hibernate using Spring @Retryable?",
            "response": "Annotate the service method with `@Retryable(retryFor = OptimisticLockException.class, maxAttempts = 3, backoff = @Backoff(delay = 100))` to automatically retry on version conflicts.",
            "reason": "Asks how to configure Spring @Retryable for JPA exceptions rather than explaining OCC vs pessimistic locking.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write SQL queries showing a complete SELECT FOR UPDATE pessimistic locking bank transfer transaction.",
            "response": "```sql\nBEGIN;\nSELECT balance FROM accounts WHERE id = 1 FOR UPDATE;\nSELECT balance FROM accounts WHERE id = 2 FOR UPDATE;\nUPDATE accounts SET balance = balance - 100 WHERE id = 1;\nUPDATE accounts SET balance = balance + 100 WHERE id = 2;\nCOMMIT;\n```",
            "reason": "Requests specific bank transfer SQL transaction code rather than conceptual comparison.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Google Spanner TrueTime API achieve lock-free read transactions with external consistency?",
            "response": "Google Spanner uses TrueTime GPS and atomic clocks to generate monotonically increasing timestamps with bounded uncertainty, allowing read transactions to read snapshots without locks or conflicts.",
            "reason": "Asks about Google Spanner TrueTime snapshots vs standard locking.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does optimistic concurrency control collapse and cause thrashing under extremely high write contention?",
            "response": "Under extreme contention, almost every update encounters a version conflict, forcing all concurrent clients to roll back and retry in loops, wasting CPU and network bandwidth without progress.",
            "reason": "Asks about OCC thrashing collapse under high contention.",
        },
    },

    # 76. Read-Write Locks
    {
        "intent_group_id": "ig_ds_read_write_locks",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "ConcurrencyPrimitives",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "rwlock"}},
        "canonical_query": "How do Read-Write Locks (RwLock) optimize performance for read-heavy workloads compared to standard Mutexes?",
        "canonical_response": "A standard Mutex enforces exclusive access, allowing only one thread to access data even for pure reads. A Read-Write Lock (RwLock) allows any number of concurrent readers to hold read locks simultaneously as long as no writer is active, granting exclusive access only when a thread acquires a write lock.",
        "positive_paraphrase_1": {
            "query": "Explain why RwLock improves throughput over Mutex when reads vastly outnumber writes in multithreaded programs.",
            "reason": "Direct paraphrase explaining RwLock throughput benefits on read-heavy workloads.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does separating shared read locks from exclusive write locks reduce thread contention?",
            "reason": "Syntactic variation focusing on shared read vs exclusive write separation.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you prevent writer starvation in reader-preference RwLock implementations?",
            "response": "Use a writer-preference RwLock (or fair RwLock). When a thread requests a write lock, new incoming readers are queued behind the waiting writer rather than being granted immediate read locks.",
            "reason": "Asks how to solve writer starvation rather than explaining general RwLock vs Mutex benefits.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a thread-safe Go cache struct using sync.RWMutex with Get (RLock) and Set (Lock) methods.",
            "response": "```go\ntype Cache struct { mu sync.RWMutex; items map[string]string }\nfunc (c *Cache) Get(k string) (string, bool) { c.mu.RLock(); defer c.mu.RUnlock(); v, ok := c.items[k]; return v, ok }\nfunc (c *Cache) Set(k, v string) { c.mu.Lock(); defer c.mu.Unlock(); c.items[k] = v }\n```",
            "reason": "Requests specific Go code implementation rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Read-Copy-Update (RCU) in the Linux kernel compare to RwLock for read-mostly data?",
            "response": "RCU allows readers to traverse data structures without acquiring any locks or atomic increments; writers modify a copy and atomic-swap pointers, waiting for a grace period before reclaiming old memory.",
            "reason": "Asks about Linux RCU vs RwLock.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can an RwLock be SLOWER than a simple Mutex if write frequency is high or critical sections are tiny?",
            "response": "RwLock maintains internal reader counters with atomic operations; on frequent writes or tiny critical sections, the atomic counter contention and lock acquisition overhead exceeds that of a simple fast mutex.",
            "reason": "Asks when RwLock performs worse than a mutex (performance inversion).",
        },
    },

    # 77. Compare-and-Swap (CAS)
    {
        "intent_group_id": "ig_ds_atomic_compare_and_swap",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "LockFreeConcurrency",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "cas"}},
        "canonical_query": "How does hardware atomic Compare-And-Swap (CAS) enable lock-free concurrent data structures?",
        "canonical_response": "CAS is a single CPU atomic instruction (like CMPXCHG on x86) that compares memory contents at an address with an expected value, updating to a new value only if they match. Lock-free data structures read current state, compute a new state, and execute CAS in a loop until it succeeds, eliminating operating system thread blocking and priority inversion.",
        "positive_paraphrase_1": {
            "query": "Explain how the CPU Compare-And-Swap instruction implements optimistic lock-free atomic updates.",
            "reason": "Direct paraphrase explaining CPU CAS instruction and lock-free update loops.",
        },
        "positive_paraphrase_2": {
            "query": "In what way do atomic CAS loops eliminate mutex lock contention in multithreaded algorithms?",
            "reason": "Syntactic variation focusing on eliminating mutex contention via CAS loops.",
        },
        "hard_negative_diff_operation": {
            "query": "What is the ABA problem in lock-free CAS data structures and how do tagged pointers or version counters resolve it?",
            "response": "The ABA problem occurs when a memory location changes from A to B and back to A; CAS succeeds thinking nothing changed, but structural pointers may have been invalidated. Tagged pointers attach an incrementing version counter to the pointer so A1 != A2.",
            "reason": "Asks specifically about the ABA problem and tagged pointers rather than general CAS operation.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a lock-free Treiber stack implementation in Rust using std::sync::atomic::AtomicPtr and compare_exchange_weak.",
            "response": "```rust\nstruct Stack<T> { head: AtomicPtr<Node<T>> }\n// implement push using compare_exchange loop\n```",
            "reason": "Requests specific lock-free stack Rust code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Load-Linked / Store-Conditional (LL/SC) on ARM compare to Compare-And-Swap on x86?",
            "response": "LL/SC uses two instructions: LL reads and sets a hardware reservation monitor, and SC stores only if the reservation was not touched by another core, inherently immune to the ABA problem.",
            "reason": "Asks about ARM LL/SC architecture vs x86 CAS.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does excessive CAS looping cause severe memory bus saturation on multi-socket NUMA servers?",
            "response": "Failed CAS instructions issue cache invalidation broadcasts (MESI cache protocol bus traffic), saturating the interconnect bus and degrading performance across all CPU cores.",
            "reason": "Asks about hardware bus saturation from failed CAS loops.",
        },
    },

    # 78. Coffman Deadlock Conditions
    {
        "intent_group_id": "ig_ds_deadlock_coffman_conditions",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "Deadlocks",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "deadlocks"}},
        "canonical_query": "What are the four Coffman conditions required for a deadlock to occur in concurrent systems?",
        "canonical_response": "The four Coffman conditions are: 1. Mutual Exclusion (resources cannot be shared); 2. Hold and Wait (processes holding resources can request new ones); 3. No Preemption (resources cannot be forcibly taken); 4. Circular Wait (a closed chain of processes each waiting for a resource held by the next). All four must be present simultaneously for a deadlock to occur.",
        "positive_paraphrase_1": {
            "query": "Explain the four necessary and sufficient Coffman conditions that produce deadlocks in operating systems.",
            "reason": "Direct paraphrase asking to explain the four Coffman conditions.",
        },
        "positive_paraphrase_2": {
            "query": "Why does breaking any one of the four Coffman conditions prevent system deadlocks?",
            "reason": "Syntactic variation focusing on eliminating deadlocks by breaking individual conditions.",
        },
        "hard_negative_diff_operation": {
            "query": "How does enforcing a global lock acquisition hierarchy (lock ordering) break the Circular Wait condition?",
            "response": "By assigning a strict total order to all lockable resources and requiring all threads to acquire locks in ascending order, circular dependency chains cannot form, mathematically preventing deadlocks.",
            "reason": "Asks specifically how lock ordering breaks circular wait rather than explaining all four conditions.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python script demonstrating a classic deadlock between two threads acquiring Lock A and Lock B in reverse order.",
            "response": "```python\ndef t1(): lockA.acquire(); time.sleep(0.01); lockB.acquire()\ndef t2(): lockB.acquire(); time.sleep(0.01); lockA.acquire()\n```",
            "reason": "Requests specific deadlock reproduction code rather than explaining the Coffman conditions.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Dijkstra's Banker's Algorithm detect and avoid unsafe deadlock states?",
            "response": "The Banker's Algorithm simulates resource allocation for maximum possible claims, verifying that at least one safe execution sequence exists before granting resource requests.",
            "reason": "Asks about Dijkstra's Banker's algorithm vs Coffman conditions.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What is the difference between a deadlock and a livelock in concurrent software?",
            "response": "In a deadlock, processes are permanently blocked in a wait state doing zero CPU work; in a livelock, processes actively change their internal state and consume CPU but make zero progress.",
            "reason": "Contrasts deadlock with livelock rather than explaining Coffman conditions.",
        },
    },

    # 79. Counting Semaphore vs Mutex
    {
        "intent_group_id": "ig_ds_semaphore_vs_mutex",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "ConcurrencyPrimitives",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "semaphore"}},
        "canonical_query": "What is the conceptual difference between a counting semaphore and a mutual exclusion lock (Mutex)?",
        "canonical_response": "A Mutex enforces strict binary mutual exclusion with ownership: only the thread that acquired the mutex can release it. A counting semaphore manages a pool of N shared permits: threads acquire permits to access resources and release them to restore permits, and any thread can release a permit without ownership restrictions.",
        "positive_paraphrase_1": {
            "query": "How do counting semaphores and mutexes differ regarding thread ownership and resource permit capacity?",
            "reason": "Direct paraphrase contrasting semaphores and mutexes on ownership and capacity.",
        },
        "positive_paraphrase_2": {
            "query": "Explain why a binary semaphore is NOT identical to a Mutex due to thread ownership rules.",
            "reason": "Syntactic variation addressing thread ownership distinction between binary semaphores and mutexes.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement a rate-limiting concurrency gate in Python asyncio using asyncio.Semaphore(10)?",
            "response": "```python\nsem = asyncio.Semaphore(10)\nasync def safe_fetch(url):\n    async with sem:\n        return await fetch(url)\n```",
            "reason": "Asks how to write an asyncio semaphore rate limiter rather than explaining conceptual differences.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a C program using POSIX sem_init, sem_wait, and sem_post to solve the producer-consumer bounded buffer problem.",
            "response": "```c\nsem_t empty, full; pthread_mutex_t mutex;\n// implement producer consumer\n```",
            "reason": "Requests specific C producer-consumer code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Java ReentrantLock with Condition variables compare to counting semaphores?",
            "response": "ReentrantLock allows the holding thread to re-enter locks and uses Condition variables (await/signal) for complex conditional predicates, unlike simple integer permit counters in semaphores.",
            "reason": "Asks about Java ReentrantLock vs counting semaphores.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why can releasing a Semaphore more times than acquired cause unexpected permit inflation in Go or Python?",
            "response": "Semaphores simply increment an internal counter on release; calling release() without an acquire() inflates the counter above initial capacity unless bounded semaphore types are used.",
            "reason": "Asks about permit counter inflation bugs (unintended behavior).",
        },
    },

    # 80. Event Sourcing Pattern
    {
        "intent_group_id": "ig_ds_event_sourcing_pattern",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "EventDrivenArchitecture",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "event_sourcing"}},
        "canonical_query": "How does the Event Sourcing pattern represent application state as an append-only log of immutable events?",
        "canonical_response": "Instead of updating database rows to store current entity state, Event Sourcing records every state transition as an immutable domain event (e.g., OrderCreated, ItemAdded, OrderPaid) in an append-only event store. Current state is computed by replaying events from genesis, providing a complete audit trail and time-travel debugging.",
        "positive_paraphrase_1": {
            "query": "Explain how Event Sourcing derives entity state by replaying an immutable sequence of historical events.",
            "reason": "Direct paraphrase explaining event replay and append-only state derivation.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does storing state changes as append-only event streams differ from traditional CRUD databases?",
            "reason": "Syntactic variation contrasting append-only event streams with mutable CRUD storage.",
        },
        "hard_negative_diff_operation": {
            "query": "How do snapshots optimize read performance in Event Sourcing systems with thousands of events per aggregate?",
            "response": "Snapshots serialize the aggregate state periodically (e.g. every 100 events); to reconstitute state, the system loads the latest snapshot and replays only subsequent events, avoiding full log replays.",
            "reason": "Asks how snapshotting optimizes replay rather than explaining Event Sourcing fundamentals.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python BankAccount aggregate class that handles DepositMoney and WithdrawMoney using event sourcing.",
            "response": "```python\nclass BankAccount:\n    def __init__(self): self.balance = 0; self.events = []\n    def apply(self, event): ...\n```",
            "reason": "Requests specific Python event sourcing class code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does EventStoreDB specialized event store compare to storing events in PostgreSQL tables?",
            "response": "EventStoreDB is purpose-built for event streams with native projection engines and stream tombstoning, whereas PostgreSQL uses standard indexed tables with append-only triggers.",
            "reason": "Asks about EventStoreDB vs PostgreSQL event storage.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does schema evolution pose severe complexity challenges in long-term Event Sourcing systems?",
            "response": "Because past events are immutable and cannot be rewritten in place; modifying event structures requires complex upcasters, version adapters, or multiple serialization schemas over time.",
            "reason": "Asks about schema evolution difficulties in event stores (architectural drawback).",
        },
    },

    # 81. CQRS Pattern
    {
        "intent_group_id": "ig_ds_cqrs_pattern",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "EventDrivenArchitecture",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "cqrs"}},
        "canonical_query": "What is the architectural motivation for Command Query Responsibility Segregation (CQRS)?",
        "canonical_response": "CQRS segregates data modification operations (Commands) from data retrieval operations (Queries) into separate models. Commands focus strictly on business validation, transactional consistency, and domain logic. Queries use denormalized, read-optimized views (e.g. Elasticsearch or Redis) to deliver high-performance reads without complex joins.",
        "positive_paraphrase_1": {
            "query": "Explain how separating read models from write models in CQRS optimizes performance and scalability.",
            "reason": "Direct paraphrase explaining read and write model separation in CQRS.",
        },
        "positive_paraphrase_2": {
            "query": "Why does CQRS decouple command validation logic from denormalized query representations?",
            "reason": "Syntactic variation focusing on decoupling command validation from query views.",
        },
        "hard_negative_diff_operation": {
            "query": "How do asynchronous projection workers synchronize the write database to read-model databases in CQRS?",
            "response": "Write models emit domain events to a message bus (like Kafka); projection consumer workers consume events and update read database tables or search indexes asynchronously.",
            "reason": "Asks how projection workers sync read databases rather than explaining CQRS motivation.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a C# MediatR handler separating CreateUserCommand handler from GetUserByIdQuery handler.",
            "response": "```csharp\npublic class CreateUserHandler : IRequestHandler<CreateUserCommand, Guid> { ... }\npublic class GetUserHandler : IRequestHandler<GetUserQuery, UserDTO> { ... }\n```",
            "reason": "Requests specific C# MediatR code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Axon Framework integrate CQRS and Event Sourcing in Java applications?",
            "response": "Axon provides built-in CommandGateway, EventGateway, and QueryGateway abstractions with automated event bus routing and projection handling in the Spring ecosystem.",
            "reason": "Asks about Axon Framework Java implementation vs general CQRS concept.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why does CQRS introduce eventual consistency lag between command completion and query updates?",
            "response": "Because read projections are updated asynchronously via message queues after the command transaction commits; queries executed immediately after a command may return stale data.",
            "reason": "Asks about eventual consistency lag in CQRS (read/write desynchronization trade-off).",
        },
    },

    # 82. Kafka Consumer Groups
    {
        "intent_group_id": "ig_ds_kafka_consumer_groups",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "MessageBrokers",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "kafka"}},
        "canonical_query": "How do Kafka consumer groups coordinate partition assignment and scale parallel message consumption?",
        "canonical_response": "A Kafka consumer group allows multiple worker instances to divide topic partitions among themselves. Each partition is assigned to exactly one consumer within the group at any time, guaranteeing ordered processing per partition. When consumers join or leave, a rebalance assigns partitions across active members.",
        "positive_paraphrase_1": {
            "query": "Explain how Apache Kafka assigns topic partitions across consumer group instances for parallel processing.",
            "reason": "Direct paraphrase explaining partition assignment across consumer group instances.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does Kafka consumer group rebalancing dynamically adapt to consumer scaling and failures?",
            "reason": "Syntactic variation focusing on dynamic consumer rebalancing and scaling.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you configure cooperative sticky partition assignment in Kafka to minimize rebalance stop-the-world pauses?",
            "response": "Set `partition.assignment.strategy = org.apache.kafka.clients.consumer.CooperativeStickyAssignor`, allowing consumers to retain existing partitions while migrating only reassigned ones.",
            "reason": "Asks how to configure the cooperative sticky assignor rather than explaining general partition division.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python Kafka consumer using kafka-python with group_id='billing_workers' and manual commit.",
            "response": "```python\nconsumer = KafkaConsumer('orders', group_id='billing_workers', enable_auto_commit=False)\nfor msg in consumer:\n    process(msg); consumer.commit()\n```",
            "reason": "Requests specific Python Kafka consumer code rather than architectural explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does RabbitMQ competing consumers on shared queues compare to Kafka partition-based consumption?",
            "response": "RabbitMQ distributes individual messages across competing workers in round-robin fashion, whereas Kafka assigns entire partitions, constraining maximum concurrency to partition count.",
            "reason": "Asks about RabbitMQ shared queues vs Kafka partition consumption.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What happens if a Kafka consumer group contains MORE consumers than the topic has partitions?",
            "response": "Excess consumers remain idle and receive zero messages because each partition can only be assigned to a single consumer in a group at any time.",
            "reason": "Asks about idle consumer overflow when consumer count exceeds partition count.",
        },
    },

    # 83. Idempotent Consumer Pattern
    {
        "intent_group_id": "ig_ds_idempotent_consumer_pattern",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "MessageProcessing",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "idempotency"}},
        "canonical_query": "How does the Idempotent Consumer pattern achieve exactly-once processing semantics over at-least-once message queues?",
        "canonical_response": "Message brokers guarantee at-least-once delivery, meaning duplicate messages can arrive during network retries. The Idempotent Consumer pattern attaches a unique message_id to each payload. When receiving a message, the consumer checks if the ID was already recorded in an atomic deduplication store; if present, it ignores processing.",
        "positive_paraphrase_1": {
            "query": "Explain how unique message identifiers and deduplication stores convert at-least-once queues into exactly-once processing.",
            "reason": "Direct paraphrase explaining how unique IDs and deduplication stores achieve effective exactly-once semantics.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the idempotent consumer pattern protect against duplicate message retries in event systems?",
            "reason": "Syntactic variation focusing on duplicate message protection via idempotency.",
        },
        "hard_negative_diff_operation": {
            "query": "How do you implement an transactional outbox pattern to guarantee messages are published only if the database commits?",
            "response": "Write the business data and outgoing message to an 'outbox' table in the same local database transaction; a separate CDC relay process (like Debezium) publishes outbox records to the message broker.",
            "reason": "Asks about the transactional outbox publisher pattern rather than the consumer deduplication pattern.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a Python message processor that checks an SQLite table for processed message_id before executing billing.",
            "response": "```python\ndef process_msg(msg_id, amount):\n    with db:\n        if db.execute('SELECT 1 FROM processed WHERE id=?', (msg_id,)).fetchone(): return\n        charge(amount)\n        db.execute('INSERT INTO processed VALUES (?)', (msg_id,))\n```",
            "reason": "Requests specific Python deduplication code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Apache Flink checkpointing achieve true end-to-end exactly-once stream processing?",
            "response": "Flink uses Chandy-Lamport barrier snapshots and two-phase commit sinks to atomically commit state and output sinks synchronously with checkpoint boundaries.",
            "reason": "Asks about Apache Flink stream checkpointing vs consumer deduplication.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "Why is recording the message ID AFTER executing the business operation vulnerable to duplicate execution?",
            "response": "If the process crashes or network fails between executing the operation and saving the message ID, the retried message will not find the ID and will execute the operation a second time.",
            "reason": "Asks about the failure window when deduplication is recorded after processing.",
        },
    },

    # 84. Actor Model Concurrency
    {
        "intent_group_id": "ig_ds_actor_model_concurrency",
        "domain": "concurrency_and_distributed_systems",
        "subtopic": "ConcurrencyModels",
        "task_kind": "explanation",
        "dimensions": {"distributed_system": {"status": "known", "value": "actor_model"}},
        "canonical_query": "How does the Actor Model eliminate shared mutable state and lock contention in concurrent systems?",
        "canonical_response": "In the Actor Model, actors are autonomous computational entities that encapsulate private state. Actors cannot access or mutate each other's memory directly; they communicate strictly by passing immutable messages to each other's mailboxes. Each actor processes incoming messages sequentially from its mailbox, eliminating locks and race conditions.",
        "positive_paraphrase_1": {
            "query": "Explain how message-passing mailboxes and isolated actor state prevent race conditions without thread locks.",
            "reason": "Direct paraphrase explaining isolated state and message-passing mailboxes in the Actor Model.",
        },
        "positive_paraphrase_2": {
            "query": "In what way does the Actor Model achieve high concurrency through shared-nothing message passing?",
            "reason": "Syntactic variation focusing on shared-nothing message passing in the Actor Model.",
        },
        "hard_negative_diff_operation": {
            "query": "How does Erlang/OTP supervision tree strategy 'one_for_all' differ from 'one_for_one' on actor crashes?",
            "response": "'one_for_one' restarts only the crashed child actor; 'one_for_all' restarts all sibling actors supervised under that tree if any single child actor crashes.",
            "reason": "Asks about Erlang supervisor restart strategies rather than explaining actor model isolation.",
        },
        "hard_negative_expl_vs_impl": {
            "query": "Write a complete Akka / Pekko Scala actor class managing an account balance responding to Deposit and GetBalance messages.",
            "response": "```scala\nclass AccountActor extends Actor {\n  var balance = 0\n  def receive = {\n    case Deposit(amount) => balance += amount\n    case GetBalance => sender() ! balance\n  }\n}\n```",
            "reason": "Requests specific Akka Scala actor code rather than conceptual explanation.",
        },
        "hard_negative_diff_lang_or_framework": {
            "query": "How does Go CSP (Communicating Sequential Processes) channel communication compare to the Actor Model?",
            "response": "In CSP (Go), channels are first-class anonymous conduits that processes read/write to; in the Actor Model, messages are addressed to specific named actor mailboxes with identity.",
            "reason": "Asks about Go CSP vs Actor Model.",
        },
        "hard_negative_parameter_or_negation_shift": {
            "query": "What causes an actor mailbox to experience unbounded queue growth and memory exhaustion?",
            "response": "If incoming messages arrive faster than the actor's sequential processing loop can handle them, the mailbox buffer grows continuously until the process exhausts memory.",
            "reason": "Asks about mailbox overflow and consumer lag (failure mode).",
        },
    },
]
