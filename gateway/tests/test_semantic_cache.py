"""
Unit and integration tests for IntelliCache Semantic Caching.
Tests deterministic policy, eligibility rules, candidate verification, ambiguity thresholds,
core pipeline execution order, single-flight coordination with semantic hits, safe failure fallbacks,
and end-to-end PostgreSQL + pgvector integration.
"""

import ast
import asyncio
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import asyncpg

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.config import Settings
from app.core.contracts import (
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
)
from app.core.errors import ProviderUpstreamError
from app.core.service import IntelliCacheCore
from app.core.singleflight import SingleFlightCoordinator
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.errors import EmbeddingTimeoutError
from app.embeddings.interfaces import EmbeddingProvider
from app.providers.interfaces import LLMProvider
from app.semantic_cache.contracts import EligibilityResult, SemanticHit
from app.semantic_cache.interfaces import BaseSemanticCache
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.errors import VectorStoreError
from app.vector_store.interfaces import BaseVectorStore
from app.vector_store.migration import run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore


class MockLLMProvider(LLMProvider):
    """Mock LLM provider tracking calls and returning canned responses."""

    def __init__(self, canned_response: str = "Mocked LLM generation"):
        self.canned_response = canned_response
        self.generate_calls: list[GenerationRequest] = []
        self.stream_calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.generate_calls.append(request)
        return GenerationResult(
            text=self.canned_response,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=15, completion_tokens=25, total_tokens=40),
            latency_ms=120.0,
            cached=False,
            fingerprint="",
        )

    async def stream(self, request: GenerationRequest):
        self.stream_calls.append(request)
        yield StreamChunk(delta=self.canned_response, done=False, cached=False)
        yield StreamChunk(delta="", done=True, cached=False, model=request.model)

    async def close(self) -> None:
        pass


class MockEmbeddingProvider(EmbeddingProvider):
    """Deterministic mock embedding provider."""

    def __init__(self, vector_map: dict[str, list[float]] | None = None, default_dim: int = 768):
        self.vector_map = vector_map or {}
        self.default_dim = default_dim
        self.embed_calls: list[tuple[str, str]] = []

    async def embed(self, text: str, task: EmbeddingTask | str = EmbeddingTask.QUERY) -> EmbeddingResult:
        task_str = task.value if isinstance(task, EmbeddingTask) else str(task)
        self.embed_calls.append((text, task_str))
        vec = self.vector_map.get(text)
        if not vec:
            # Deterministic pseudo-vector based on string hash
            h = abs(hash(text))
            vec = [0.0] * self.default_dim
            vec[0] = ((h % 100) / 100.0)
            vec[1] = (((h // 100) % 100) / 100.0)
        return EmbeddingResult(vector=vec, model="nomic-embed-text", dimensions=self.default_dim)

    async def close(self) -> None:
        pass


class TestSemanticCachePolicy(unittest.TestCase):
    """Tests deterministic query eligibility and candidate compatibility rules."""

    def setUp(self):
        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
        )

    def test_single_turn_technical_query_is_eligible(self):
        """Single-turn user technical/programming query is eligible."""
        req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How do I implement binary search in Python?")],
        )
        res = self.policy.check_eligibility(req)
        self.assertTrue(res.eligible)

    def test_code_snippet_query_is_eligible(self):
        """Query containing code syntax markers is eligible."""
        req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="def factorial(n):\n    return 1 if n <= 1 else n * factorial(n-1)")],
        )
        res = self.policy.check_eligibility(req)
        self.assertTrue(res.eligible)

    def test_multiturn_query_is_ineligible(self):
        """Multi-turn conversation is rejected."""
        req = GenerationRequest(
            model="llama-3",
            messages=[
                ChatMessage(role="user", content="How do I sort a list in Python?"),
                ChatMessage(role="assistant", content="Use list.sort()"),
                ChatMessage(role="user", content="Can you show an example?"),
            ],
        )
        res = self.policy.check_eligibility(req)
        self.assertFalse(res.eligible)
        self.assertIn("single-turn", res.reason.lower())

    def test_system_prompt_is_ineligible(self):
        """System prompt in messages or metadata is rejected."""
        req1 = GenerationRequest(
            model="llama-3",
            messages=[
                ChatMessage(role="system", content="You are a helpful coding assistant."),
                ChatMessage(role="user", content="Write a python function"),
            ],
        )
        self.assertFalse(self.policy.check_eligibility(req1).eligible)

        req2 = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="Write a python function")],
            metadata={"system_prompt": "You are a code assistant."},
        )
        self.assertFalse(self.policy.check_eligibility(req2).eligible)

    def test_streaming_and_bypass_are_ineligible(self):
        """Streaming or bypass_cache requests are rejected."""
        req_stream = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="Write a python function")],
            stream=True,
        )
        self.assertFalse(self.policy.check_eligibility(req_stream).eligible)

        req_bypass = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="Write a python function")],
            bypass_cache=True,
        )
        self.assertFalse(self.policy.check_eligibility(req_bypass).eligible)

    def test_conversation_id_is_ineligible(self):
        """Request tied to a conversation ID is rejected."""
        req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="Write a python function")],
            conversation_id="conv-abc-123",
        )
        self.assertFalse(self.policy.check_eligibility(req).eligible)

    def test_sensitive_credentials_are_ineligible(self):
        """Requests containing secret patterns are rejected."""
        samples = [
            "Here is my key: gsk_1234567890abcdef1234567890, help me debug",
            "sk-1234567890abcdef1234567890123456 is not working",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDc",
            "password = my_super_secret_password_123",
            "postgres://admin:secretpass@localhost:5432/mydb",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...",
        ]
        for s in samples:
            req = GenerationRequest(
                model="llama-3",
                messages=[ChatMessage(role="user", content=s)],
            )
            res = self.policy.check_eligibility(req)
            self.assertFalse(res.eligible, f"Failed to reject sensitive pattern: {s}")
            self.assertIn("credentials", res.reason.lower())

    def test_non_technical_query_is_ineligible(self):
        """General non-technical queries are rejected."""
        non_tech = [
            "What is the capital of France?",
            "Can you write a poem about autumn leaves?",
            "Who won the 1998 football world cup?",
        ]
        for q in non_tech:
            req = GenerationRequest(
                model="llama-3",
                messages=[ChatMessage(role="user", content=q)],
            )
            res = self.policy.check_eligibility(req)
            self.assertFalse(res.eligible, f"Expected non-technical query to be rejected: {q}")

    def test_candidate_compatibility_validation(self):
        """Validates all candidate metadata compatibility checks."""
        req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How to sort in Python?")],
            temperature=0.7,
            max_tokens=500,
            provider="groq",
            namespace="default",
        )

        valid_cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_1",
            query_text="How to sort in Python?",
            model="nomic-embed-text",
            distance=0.02,
            similarity=0.98,
            cached_response="Use sorted()",
            metadata={
                "llm_provider": "groq",
                "llm_model": "llama-3",
                "temperature": 0.7,
                "max_tokens": 500,
            },
        )
        compat, reason = self.policy.is_candidate_compatible(valid_cand, req)
        self.assertTrue(compat)

        # Provider mismatch
        bad_prov = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_1",
            query_text="q",
            model="nomic-embed-text",
            distance=0.02,
            similarity=0.98,
            cached_response="resp",
            metadata={"llm_provider": "openai", "llm_model": "llama-3", "temperature": 0.7, "max_tokens": 500},
        )
        self.assertFalse(self.policy.is_candidate_compatible(bad_prov, req)[0])

        # Model mismatch
        bad_model = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_1",
            query_text="q",
            model="nomic-embed-text",
            distance=0.02,
            similarity=0.98,
            cached_response="resp",
            metadata={"llm_provider": "groq", "llm_model": "mistral-7b", "temperature": 0.7, "max_tokens": 500},
        )
        self.assertFalse(self.policy.is_candidate_compatible(bad_model, req)[0])

        # Temperature mismatch
        bad_temp = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_1",
            query_text="q",
            model="nomic-embed-text",
            distance=0.02,
            similarity=0.98,
            cached_response="resp",
            metadata={"llm_provider": "groq", "llm_model": "llama-3", "temperature": 0.2, "max_tokens": 500},
        )
        self.assertFalse(self.policy.is_candidate_compatible(bad_temp, req)[0])

        # Missing metadata
        no_meta = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_1",
            query_text="q",
            model="nomic-embed-text",
            distance=0.02,
            similarity=0.98,
            cached_response="resp",
            metadata={},
        )
        self.assertFalse(self.policy.is_candidate_compatible(no_meta, req)[0])

        # Missing or empty response
        no_resp = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_1",
            query_text="q",
            model="nomic-embed-text",
            distance=0.02,
            similarity=0.98,
            cached_response="",
            metadata={"llm_provider": "groq", "llm_model": "llama-3", "temperature": 0.7, "max_tokens": 500},
        )
        self.assertFalse(self.policy.is_candidate_compatible(no_resp, req)[0])

    def test_similarity_threshold_and_ambiguity_margin(self):
        """Verifies minimum similarity threshold and ambiguity margin behavior."""
        req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How to sort in Python?")],
            temperature=0.7,
            provider="groq",
            namespace="default",
        )
        meta = {"llm_provider": "groq", "llm_model": "llama-3", "temperature": 0.7, "max_tokens": None}

        # Case 1: Below minimum similarity (0.89 < 0.92) -> Rejected
        cand_low = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_low",
            query_text="q",
            model="nomic-embed-text",
            distance=0.11,
            similarity=0.89,
            cached_response="ans1",
            metadata=meta,
        )
        winner, reason = self.policy.evaluate_candidates([cand_low], req)
        self.assertIsNone(winner)
        self.assertIn("below threshold", reason.lower())

        # Case 2: Above threshold and unique -> Accepted
        cand_good = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_good",
            query_text="q",
            model="nomic-embed-text",
            distance=0.04,
            similarity=0.96,
            cached_response="Good Answer",
            metadata=meta,
        )
        winner, reason = self.policy.evaluate_candidates([cand_good], req)
        self.assertEqual(winner, cand_good)
        self.assertIsNone(reason)

        # Case 3: Ambiguous candidates with different answers within margin
        # Top = 0.95, Alternative = 0.94 -> margin = 0.01 (< 0.03) -> Rejected!
        cand_alt_diff = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_alt",
            query_text="q_alt",
            model="nomic-embed-text",
            distance=0.06,
            similarity=0.94,
            cached_response="Contradictory Answer",
            metadata=meta,
        )
        winner, reason = self.policy.evaluate_candidates([cand_good, cand_alt_diff], req)
        self.assertIsNone(winner)
        self.assertIn("ambiguous candidate", reason.lower())

        # Case 4: Near candidates but identical answers -> Not ambiguous!
        cand_alt_same = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_alt2",
            query_text="q_alt2",
            model="nomic-embed-text",
            distance=0.05,
            similarity=0.95,
            cached_response="Good Answer",
            metadata=meta,
        )
        winner, reason = self.policy.evaluate_candidates([cand_good, cand_alt_same], req)
        self.assertEqual(winner, cand_good)
        self.assertIsNone(reason)


class TestCoreSemanticCachePipeline(unittest.IsolatedAsyncioTestCase):
    """
    Tests IntelliCacheCore request path orchestration: exact cache first,
    semantic cache second, LLM provider fallback, single-flight coordination,
    and safe post-generation indexing.
    """

    async def asyncSetUp(self):
        self.cache = ExactCache()
        self.mock_provider = MockLLMProvider("Live Generated LLM Response")
        self.mock_embedder = MockEmbeddingProvider(default_dim=768)

        # In-memory vector store mock
        self.mock_vector_store = MagicMock(spec=BaseVectorStore)
        self.mock_vector_store.search = AsyncMock(return_value=[])
        self.mock_vector_store.upsert = AsyncMock(return_value=None)
        self.mock_vector_store.close = AsyncMock(return_value=None)

        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
        )
        self.semantic_service = SemanticCacheService(
            embedding_provider=self.mock_embedder,
            vector_store=self.mock_vector_store,
            policy=self.policy,
            default_namespace="default",
        )
        self.core = IntelliCacheCore(
            cache=self.cache,
            cache_enabled=True,
            semantic_cache=self.semantic_service,
        )

        self.eligible_request = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How do I write a binary search function in Python?")],
            temperature=0.7,
            max_tokens=None,
            provider="groq",
            namespace="default",
        )

    async def test_exact_hit_bypasses_semantic_lookup(self):
        """Exact cache hit returns immediately; does not call semantic lookup or provider."""
        # Prime exact cache
        from app.core.fingerprinting import generate_fingerprint
        fp = generate_fingerprint(
            messages=self.eligible_request.messages,
            model=self.eligible_request.model,
            temperature=self.eligible_request.temperature,
            max_tokens=self.eligible_request.max_tokens,
            provider=self.eligible_request.provider,
        )
        await self.cache.set(fp, CacheEntry(response="Exact Cached Text", model="llama-3", usage=UsageMetrics(), latency_ms=5.0))

        result = await self.core.execute_query(self.eligible_request, self.mock_provider)
        self.assertTrue(result.cached)
        self.assertEqual(result.cache_type, "exact")
        self.assertEqual(result.text, "Exact Cached Text")
        self.assertEqual(len(self.mock_provider.generate_calls), 0)
        self.assertEqual(len(self.mock_embedder.embed_calls), 0)

    async def test_semantic_hit_returns_cached_response_without_llm(self):
        """Semantic hit returns candidate response with cache_type='semantic' without calling LLM."""
        meta = {
            "llm_provider": "groq",
            "llm_model": "llama-3",
            "temperature": 0.7,
            "max_tokens": None,
        }
        cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_semantic_source",
            query_text="How to do binary search in Python?",
            model="nomic-embed-text",
            distance=0.03,
            similarity=0.97,
            cached_response="Semantic Cached Answer",
            metadata=meta,
        )
        self.mock_vector_store.search.return_value = [cand]

        result = await self.core.execute_query(self.eligible_request, self.mock_provider)
        self.assertTrue(result.cached)
        self.assertEqual(result.cache_type, "semantic")
        self.assertEqual(result.text, "Semantic Cached Answer")
        self.assertEqual(len(self.mock_provider.generate_calls), 0)

        # Semantic hits are not recursively indexed under a new fingerprint
        self.assertEqual(self.mock_vector_store.upsert.call_count, 0)

        # Verify stats reflect semantic hit
        stats = self.semantic_service.get_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["lookups"], 1)

    async def test_semantic_miss_calls_llm_and_indexes_result(self):
        """Semantic miss falls back to provider and indexes successful response."""
        self.mock_vector_store.search.return_value = []

        result = await self.core.execute_query(self.eligible_request, self.mock_provider)
        self.assertFalse(result.cached)
        self.assertEqual(result.cache_type, "none")
        self.assertEqual(result.text, "Live Generated LLM Response")
        self.assertEqual(len(self.mock_provider.generate_calls), 1)

        # Verify vector store upsert was called with indexed result
        self.assertEqual(self.mock_vector_store.upsert.call_count, 1)
        upserted_record: VectorRecord = self.mock_vector_store.upsert.call_args[0][0]
        self.assertEqual(upserted_record.fingerprint, result.fingerprint)
        self.assertEqual(upserted_record.query_text, "How do I write a binary search function in Python?")
        self.assertEqual(upserted_record.metadata["llm_provider"], "groq")

        stats = self.semantic_service.get_stats()
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["indexing_successes"], 1)

    async def test_failed_llm_generation_is_not_indexed(self):
        """If LLM generation fails, vector record is never created or indexed."""
        failing_provider = MagicMock(spec=LLMProvider)
        failing_provider.generate = AsyncMock(side_effect=ProviderUpstreamError("Upstream exploded", status_code=500))

        with self.assertRaises(ProviderUpstreamError):
            await self.core.execute_query(self.eligible_request, failing_provider)

        self.assertEqual(self.mock_vector_store.upsert.call_count, 0)
        self.assertEqual(self.semantic_service.get_stats()["indexing_successes"], 0)

    async def test_vector_indexing_failure_does_not_fail_query(self):
        """Vector indexing failure fails safely without raising error to the caller."""
        self.mock_vector_store.search.return_value = []
        self.mock_vector_store.upsert.side_effect = VectorStoreError("DB insert failed")

        result = await self.core.execute_query(self.eligible_request, self.mock_provider)
        self.assertEqual(result.text, "Live Generated LLM Response")
        self.assertEqual(result.cache_type, "none")

        stats = self.semantic_service.get_stats()
        self.assertEqual(stats["indexing_failures"], 1)

    async def test_embedding_failure_fails_open_to_llm(self):
        """Embedding timeout fails open to LLM provider generation."""
        failing_embedder = MagicMock(spec=EmbeddingProvider)
        failing_embedder.embed = AsyncMock(side_effect=EmbeddingTimeoutError("Timeout"))

        self.semantic_service.embedding_provider = failing_embedder

        result = await self.core.execute_query(self.eligible_request, self.mock_provider)
        self.assertEqual(result.text, "Live Generated LLM Response")
        self.assertEqual(len(self.mock_provider.generate_calls), 1)

        stats = self.semantic_service.get_stats()
        self.assertEqual(stats["embedding_failures"], 1)

    async def test_vector_store_failure_fails_open_to_llm(self):
        """Vector store retrieval error fails open to LLM provider generation."""
        self.mock_vector_store.search.side_effect = VectorStoreError("Connection refused")

        result = await self.core.execute_query(self.eligible_request, self.mock_provider)
        self.assertEqual(result.text, "Live Generated LLM Response")
        self.assertEqual(len(self.mock_provider.generate_calls), 1)

        stats = self.semantic_service.get_stats()
        self.assertEqual(stats["vector_store_failures"], 1)

    async def test_single_flight_follower_receives_semantic_hit(self):
        """Single-flight follower receives the leader's semantic hit correctly."""
        meta = {
            "llm_provider": "groq",
            "llm_model": "llama-3",
            "temperature": 0.7,
            "max_tokens": None,
        }
        cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_shared",
            query_text="How to do binary search in Python?",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="Shared Semantic Hit",
            metadata=meta,
        )
        self.mock_vector_store.search.return_value = [cand]

        # Execute two concurrent identical queries
        res1, res2 = await asyncio.gather(
            self.core.execute_query(self.eligible_request, self.mock_provider),
            self.core.execute_query(self.eligible_request, self.mock_provider),
        )

        self.assertEqual(res1.text, "Shared Semantic Hit")
        self.assertEqual(res2.text, "Shared Semantic Hit")
        self.assertEqual(res1.cache_type, "semantic")
        self.assertEqual(res2.cache_type, "semantic")
        self.assertEqual(len(self.mock_provider.generate_calls), 0)

    async def test_streaming_does_not_use_semantic_cache(self):
        """Streaming requests never invoke semantic lookup or indexing."""
        stream_req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How do I sort in Python?")],
            stream=True,
        )
        chunks = []
        async for chunk in self.core.execute_stream(stream_req, self.mock_provider):
            chunks.append(chunk)

        self.assertEqual(len(self.mock_provider.stream_calls), 1)
        self.assertEqual(len(self.mock_embedder.embed_calls), 0)
        self.assertEqual(self.mock_vector_store.search.call_count, 0)
        self.assertEqual(self.mock_vector_store.upsert.call_count, 0)

    def test_ast_import_isolation(self):
        """app/semantic_cache and app/core must not import FastAPI, Groq, Ollama, or PostgreSQL."""
        forbidden = ["fastapi", "starlette", "groq", "ollama", "asyncpg"]
        packages = [
            Path(__file__).resolve().parent.parent / "app" / "semantic_cache",
            Path(__file__).resolve().parent.parent / "app" / "core",
        ]

        for pkg in packages:
            for py_path in pkg.glob("*.py"):
                tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            top = alias.name.split(".")[0].lower()
                            self.assertNotIn(
                                top,
                                forbidden,
                                f"Forbidden import '{top}' detected in {py_path.name}",
                            )
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            top = node.module.split(".")[0].lower()
                            self.assertNotIn(
                                top,
                                forbidden,
                                f"Forbidden from-import '{top}' detected in {py_path.name}",
                            )


class TestPostgresSemanticCacheIntegration(unittest.IsolatedAsyncioTestCase):
    """
    End-to-end integration tests with real PostgreSQL exact cache and pgvector vector store.
    """

    async def asyncSetUp(self):
        explicit_url = os.getenv("TEST_DATABASE_URL")
        self.is_explicit_url = explicit_url is not None
        self.db_url = explicit_url or "postgresql://gnx@127.0.0.1:5433/intellicache_test"
        safe_url = self.db_url
        parsed = urlsplit(self.db_url)
        if parsed.password:
            safe_url = self.db_url.replace(f":{parsed.password}@", ":***@")

        db_name = (parsed.path or "").lstrip("/")
        if "test" not in db_name.lower():
            raise ValueError(f"Safety check failed: database '{db_name}' must contain 'test'.")

        try:
            self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=768)

            self.exact_cache = PostgresCache(
                pool=self.pool,
                default_namespace="test_sem_ns",
                ttl_seconds=3600,
            )
            self.vector_store = PostgresVectorStore(
                pool=self.pool,
                default_namespace="test_sem_ns",
                default_model="nomic-embed-text",
                dimensions=768,
            )
            await self.exact_cache.clear()
            await self.vector_store.clear()

            # Pre-compute close vectors for integration test queries
            # Vector 1: query A
            vec_a = [0.0] * 768
            vec_a[0] = 1.0
            # Vector 2: semantically similar query B (cosine sim ≈ 0.96)
            vec_b = [0.0] * 768
            vec_b[0] = 0.98
            vec_b[1] = 0.1989

            self.mock_embedder = MockEmbeddingProvider(
                vector_map={
                    "How do I write quicksort in Python?": vec_a,
                    "How to implement quicksort in Python?": vec_b,
                },
                default_dim=768,
            )
            self.policy = SemanticCachePolicy(
                min_similarity=0.92,
                min_margin=0.03,
                top_k=5,
                configured_model="nomic-embed-text",
            )
            self.semantic_service = SemanticCacheService(
                embedding_provider=self.mock_embedder,
                vector_store=self.vector_store,
                policy=self.policy,
                default_namespace="test_sem_ns",
            )
            self.core = IntelliCacheCore(
                cache=self.exact_cache,
                cache_enabled=True,
                semantic_cache=self.semantic_service,
            )
            self.mock_provider = MockLLMProvider("def quicksort(arr): ...")
        except Exception as e:
            if self.is_explicit_url:
                self.fail(f"Integration setup failed for explicit URL ({safe_url}): {e}")
            else:
                self.skipTest(f"pgvector test database unavailable at {safe_url}: {e}")

    async def asyncTearDown(self):
        if hasattr(self, "exact_cache"):
            try:
                await self.exact_cache.clear()
                await self.vector_store.clear()
            except Exception:
                pass
        if hasattr(self, "pool"):
            await self.pool.close()

    async def test_end_to_end_semantic_reuse(self):
        """
        Step 1: Query A executes -> exact miss, semantic miss, calls provider, persists exact cache & vector store.
        Step 2: Query A executes -> exact cache hit.
        Step 3: Query B (semantically similar) executes -> exact miss, semantic cache hit! Calls provider 0 times.
        """
        req_a = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How do I write quicksort in Python?")],
            temperature=0.7,
            provider="groq",
            namespace="test_sem_ns",
        )
        req_b = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How to implement quicksort in Python?")],
            temperature=0.7,
            provider="groq",
            namespace="test_sem_ns",
        )

        # 1. First execution of Query A: miss -> provider generation + indexing
        res1 = await self.core.execute_query(req_a, self.mock_provider)
        self.assertFalse(res1.cached)
        self.assertEqual(res1.cache_type, "none")
        self.assertEqual(len(self.mock_provider.generate_calls), 1)

        # 2. Second execution of Query A: exact match hit
        res2 = await self.core.execute_query(req_a, self.mock_provider)
        self.assertTrue(res2.cached)
        self.assertEqual(res2.cache_type, "exact")
        self.assertEqual(len(self.mock_provider.generate_calls), 1)

        # 3. Execution of Query B (different text, same semantic intent): semantic hit!
        res3 = await self.core.execute_query(req_b, self.mock_provider)
        self.assertTrue(res3.cached)
        self.assertEqual(res3.cache_type, "semantic")
        self.assertEqual(res3.text, "def quicksort(arr): ...")
        # Provider still called only once across all 3 queries!
        self.assertEqual(len(self.mock_provider.generate_calls), 1)

        stats = self.semantic_service.get_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["indexing_successes"], 1)
