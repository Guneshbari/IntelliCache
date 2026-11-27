"""
Stage 3.8A Verification Tests:
1. Offline configuration, opt-in guard, dataset schema, and defaults tests.
2. GroqProvider timeout, bounded retries, streaming usage/TTFT, and cancellation tests.
3. Budget enforcement, redaction, fault-injection recovery, and FastAPI HTTP error/stream tests.
4. Opt-in real Groq + PG evaluation test (skipped unless explicitly configured).
"""

import asyncio
import json
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import groq
import httpx
from fastapi.testclient import TestClient

GATEWAY_ROOT = Path(__file__).resolve().parent.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.cache.exact_cache import ExactCache
from app.config import Settings, reload_settings
from app.core.contracts import ChatMessage, GenerationRequest, UsageMetrics
from app.core.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.core.service import IntelliCacheCore
from app.main import app
from app.metrics import RuntimeMetricsCollector, get_metrics_collector
from app.providers.groq_adapter import GroqProvider
from app.routes.query import get_groq_client_factory, get_query_cache, set_semantic_cache
from scripts.evaluate_stage3_5_5_operational import verify_no_sensitive_leaks
from scripts.evaluate_stage3_8a_live_provider import (
    DATASET_PATH,
    BudgetedProviderProxy,
    LiveEvalBudgetExceededError,
    LiveEvalBudgetGuard,
    PhysicalRequestReservation,
    render_stage38a_markdown,
    run_fault_injection_matrix,
    run_live_provider_evaluation,
    sanitize_report_payload,
    validate_live_eval_profile,
)


class TestStage38AConfigAndSecurityContracts(unittest.TestCase):
    """Validates Stage 3.8A opt-in configuration, safe defaults, and dataset structure."""

    def test_defaults_and_live_eval_off_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = Settings()
        self.assertFalse(cfg.live_provider_evaluation_enabled)
        self.assertEqual(cfg.live_provider_name, "groq")
        self.assertEqual(cfg.live_provider_model, "")
        self.assertFalse(cfg.semantic_cache_enabled)
        self.assertFalse(cfg.semantic_cache_serving_enabled)
        self.assertFalse(cfg.shadow_evaluation_enabled)
        self.assertEqual(cfg.semantic_cache_default_visibility, "private")
        self.assertEqual(cfg.semantic_cache_min_similarity, 0.92)
        self.assertEqual(cfg.semantic_cache_min_margin, 0.03)
        self.assertEqual(cfg.semantic_cache_top_k, 5)
        self.assertEqual(cfg.semantic_cache_max_retrieval_candidates, 25)
        self.assertFalse(hasattr(cfg, "redis_url"))

    def test_live_eval_requires_explicit_credentials_and_model(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LIVE_PROVIDER_EVALUATION_ENABLED": "true",
                "LIVE_PROVIDER_MODEL": "llama-3.3-70b-versatile",
                "GROQ_API_KEY": "replace_with_your_key",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings()

        with patch.dict(
            os.environ,
            {
                "LIVE_PROVIDER_EVALUATION_ENABLED": "true",
                "LIVE_PROVIDER_MODEL": "",
                "GROQ_API_KEY": "gsk_validplaceholderfortestonly",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings()

        with patch.dict(
            os.environ,
            {
                "LIVE_PROVIDER_EVALUATION_ENABLED": "true",
                "LIVE_PROVIDER_MODEL": "llama-3.3-70b-versatile",
                "LIVE_PROVIDER_MAX_REQUESTS": "0",
                "GROQ_API_KEY": "gsk_validplaceholderfortestonly",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings()

    def test_validate_live_eval_profile_no_silent_mock_fallback(self) -> None:
        with self.assertRaises(ProviderConfigurationError):
            validate_live_eval_profile(
                confirm_live=False,
                live_enabled=True,
                model_name="llama-3.3-70b-versatile",
                api_key="gsk_validkey",
            )
        with self.assertRaises(ProviderConfigurationError):
            validate_live_eval_profile(
                confirm_live=True,
                live_enabled=False,
                model_name="llama-3.3-70b-versatile",
                api_key="gsk_validkey",
            )
        with self.assertRaises(ProviderConfigurationError):
            validate_live_eval_profile(
                confirm_live=True,
                live_enabled=True,
                provider_name="mock",
                model_name="mock-qwen-27b",
                api_key="gsk_validkey",
            )
        profile = validate_live_eval_profile(
            confirm_live=True,
            live_enabled=True,
            provider_name="groq",
            model_name="llama-3.3-70b-versatile",
            api_key="gsk_testkeyonly123",
            max_requests=25,
        )
        self.assertEqual(profile["provider"], "groq")
        self.assertEqual(profile["max_requests"], 25)

    def test_stage38a_dataset_integrity(self) -> None:
        with open(DATASET_PATH, "r", encoding="utf-8") as fh:
            ds = json.load(fh)
        cases = ds["cases"]
        ids = [c["case_id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(cases), 20)
        for case in cases:
            self.assertIn("expected_compatibility", case)
            self.assertIn("expected_context_class", case)
            self.assertIn("expected_privacy_scope", case)
            self.assertIn("reason", case)
        for step in ds["benchmark_sequence"]["steps"]:
            self.assertIn(step["case_id"], ids)


class TestStage38AGroqAdapterRetryTimeoutStreamingUsage(unittest.IsolatedAsyncioTestCase):
    """Verifies bounded timeout/retries, streaming usage/TTFT, cancellation, and no semantic streaming."""

    async def test_asyncgroq_receives_bounded_timeout_and_zero_sdk_retries(self) -> None:
        provider = GroqProvider(api_key="gsk_dummy_test", timeout_seconds=12.5, max_retries=2)
        with patch("app.providers.groq_adapter.AsyncGroq") as mock_cls:
            provider._create_client()
            mock_cls.assert_called_once_with(api_key="gsk_dummy_test", timeout=12.5, max_retries=0)

    async def test_retry_after_header_bounds_backoff_on_rate_limit(self) -> None:
        provider = GroqProvider(api_key="gsk_dummy", retry_backoff_seconds=0.1)
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        rl_err = groq.RateLimitError(
            message="429",
            response=httpx.Response(429, headers={"Retry-After": "2.5"}, request=req_obj),
            body=None,
        )
        with patch("app.providers.groq_adapter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await provider._sleep_backoff(0, rl_err)
            mock_sleep.assert_awaited_once()
            waited = mock_sleep.await_args.args[0]
            self.assertGreaterEqual(waited, 2.5)
            self.assertLessEqual(waited, 10.0)

    async def test_timeout_rate_limit_and_missing_usage_metrics(self) -> None:
        collector = RuntimeMetricsCollector()
        cache = ExactCache(max_entries=20)
        core = IntelliCacheCore(cache=cache, metrics_collector=collector)
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")

        # 1. Timeout retries once then raises ProviderTimeoutError (subclass of ProviderConnectionError)
        c_to = AsyncMock()
        c_to.chat.completions.create = AsyncMock(side_effect=groq.APITimeoutError(request=req_obj))
        c_to.close = AsyncMock()
        p_to = GroqProvider(api_key="gsk_test", client_factory=lambda: c_to, max_retries=1, retry_backoff_seconds=0.0)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Explain TLS 1.3.")],
            namespace="s38a_unit_ns",
            visibility="tenant_shared",
        )
        with self.assertRaises(ProviderTimeoutError) as err_ctx:
            await core.execute_query(req, p_to)
        self.assertIsInstance(err_ctx.exception, ProviderConnectionError)
        self.assertEqual(c_to.chat.completions.create.await_count, 2)

        # 2. Missing usage metadata is reported accurately via counter without inventing tokens
        resp_no_usage = MagicMock()
        resp_no_usage.choices = [MagicMock(message=MagicMock(content="Concise TLS answer."), finish_reason="stop")]
        resp_no_usage.usage = None
        c_nu = AsyncMock()
        c_nu.chat.completions.create = AsyncMock(return_value=resp_no_usage)
        c_nu.close = AsyncMock()
        p_nu = GroqProvider(api_key="gsk_test", client_factory=lambda: c_nu)
        res = await core.execute_query(req, p_nu)
        self.assertEqual(res.usage.total_tokens, 0)

        snap = collector.get_namespace_snapshot("s38a_unit_ns")
        self.assertEqual(snap.provider_and_savings.provider_timeouts_total, 1)
        self.assertEqual(snap.provider_and_savings.provider_retries_total, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 1)
        self.assertEqual(snap.provider_and_savings.provider_usage_metadata_missing_total, 1)

    async def test_streaming_extracts_x_groq_usage_records_ttft_and_skips_semantic_cache(self) -> None:
        collector = RuntimeMetricsCollector()
        cache = ExactCache(max_entries=20)
        mock_sem = MagicMock()
        mock_sem.lookup = AsyncMock()
        mock_sem.index = AsyncMock()
        core = IntelliCacheCore(
            cache=cache,
            semantic_cache=mock_sem,
            metrics_collector=collector,
            semantic_cache_startup_enabled=True,
        )

        async def _stream_with_x_groq_usage():
            c1 = MagicMock()
            c1.choices = [MagicMock(delta=MagicMock(content="Hello "), finish_reason=None)]
            c1.usage = None
            c1.x_groq = None
            yield c1

            c2 = MagicMock()
            c2.choices = [MagicMock(delta=MagicMock(content="Groq!"), finish_reason="stop")]
            c2.usage = None
            c2.x_groq = MagicMock(usage=UsageMetrics(prompt_tokens=9, completion_tokens=3, total_tokens=12))
            yield c2

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=_stream_with_x_groq_usage())
        client.close = AsyncMock()
        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Say hello cleanly.")],
            stream=True,
            namespace="s38a_stream_ns",
            visibility="tenant_shared",
        )
        chunks = [c async for c in core.execute_stream(req, provider)]
        self.assertEqual("".join(c.delta for c in chunks if not c.done), "Hello Groq!")
        self.assertTrue(chunks[-1].done)
        self.assertEqual(chunks[-1].finish_reason, "stop")
        self.assertEqual(chunks[-1].usage.total_tokens, 12)

        # Semantic cache must NOT run on streaming requests
        mock_sem.lookup.assert_not_awaited()
        mock_sem.index.assert_not_awaited()

        snap = collector.get_namespace_snapshot("s38a_stream_ns")
        self.assertEqual(snap.latencies.stream_time_to_first_token_ms.sample_count, 1)
        self.assertEqual(snap.latencies.stream_completion_latency_ms.sample_count, 1)
        self.assertEqual(snap.provider_and_savings.provider_total_tokens_used, 12)


class TestStage38ABudgetRedactionAndFaultRecovery(unittest.IsolatedAsyncioTestCase):
    """Tests budget limits, report redaction, fault matrix, and FastAPI HTTP error/stream contracts."""

    async def test_budget_guard_request_and_cost_ceilings(self) -> None:
        g_req = LiveEvalBudgetGuard(max_requests=2)
        await g_req.reserve_request()
        await g_req.reserve_request()
        with self.assertRaises(LiveEvalBudgetExceededError):
            await g_req.reserve_request()
        self.assertEqual(g_req.summary()["cost_status"], "unavailable")

        g_cost = LiveEvalBudgetGuard(
            max_requests=10,
            max_cost=0.01,
            input_cost_per_million=100.0,
            output_cost_per_million=100.0,
            pricing_source="unit_test_rate",
            pricing_effective_date="2026-11-27",
        )
        await g_cost.reserve_request()
        await g_cost.record_usage(UsageMetrics(prompt_tokens=100, completion_tokens=100, total_tokens=200), usage_available=True)
        with self.assertRaises(LiveEvalBudgetExceededError):
            await g_cost.reserve_request()
        self.assertEqual(g_cost.summary()["cost_status"], "calculated_from_usage")
        self.assertEqual(g_cost.summary()["pricing_source"], "unit_test_rate")

    async def test_report_redaction_strips_all_secrets(self) -> None:
        secret_key = "gsk_supersecretkey99887766"
        raw_payload = {
            "provider": "groq",
            "api_key": secret_key,
            "nested": {
                "authorization": "Bearer secret_bearer_token_abc",
                "err": f"Auth failed with key {secret_key} for /home/gnx/secret/file at 10.2.3.4",
            },
        }
        cleaned = sanitize_report_payload(raw_payload, extra_secrets=[secret_key])
        dumped = json.dumps(cleaned)
        self.assertNotIn(secret_key, dumped)
        self.assertNotIn("secret_bearer_token_abc", dumped)
        verify_no_sensitive_leaks(cleaned, [secret_key, "secret_bearer_token_abc"], "unit_redaction")

    async def test_controlled_fault_injection_matrix_passes(self) -> None:
        results = await run_fault_injection_matrix("postgresql://unused:unused@127.0.0.1:5433/unused")
        self.assertGreaterEqual(len(results), 10)
        for item in results:
            self.assertTrue(item["passed"], f"Fault scenario failed: {item}")

    def test_http_query_error_mapping_and_metrics_exposure(self) -> None:
        get_metrics_collector().reset()
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(
            side_effect=groq.APITimeoutError(request=req_obj)
        )
        mock_groq.close = AsyncMock()

        env_vars = {
            "ALLOW_UNAUTHENTICATED_LOOPBACK": "true",
            "ADMIN_API_TOKEN": "s38a_test_admin_tok_1234",
            "GROQ_API_KEY": "gsk_test_only",
            "CACHE_BACKEND": "memory",
            "VECTOR_STORE_BACKEND": "memory",
            "SEMANTIC_CACHE_ENABLED": "false",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            reload_settings()
            set_semantic_cache(None)
            app.dependency_overrides[get_groq_client_factory] = lambda: (lambda: mock_groq)
            try:
                client = TestClient(app)
                asyncio.run(get_query_cache().clear())
                resp = client.post("/query", json={"prompt": "Timeout test query"})
                self.assertEqual(resp.status_code, 503)

                admin_metrics = client.get("/admin/metrics", headers={"X-Admin-Token": "s38a_test_admin_tok_1234"})
                self.assertEqual(admin_metrics.status_code, 200)
                ps = admin_metrics.json()["aggregate"]["provider_and_savings"]
                self.assertEqual(ps["provider_timeouts_total"], 1)
                self.assertEqual(ps["provider_calls_failed"], 1)
            finally:
                app.dependency_overrides.clear()
                reload_settings()


class TestStage38APhysicalRequestAccounting(unittest.IsolatedAsyncioTestCase):
    """Verifies that physical provider request attempts are strictly accounted."""

    async def test_single_generation_consumes_one_physical_request(self) -> None:
        budget = LiveEvalBudgetGuard(max_requests=5)
        client = AsyncMock()
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content="Hello"), finish_reason="stop")]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        client.chat.completions.create = AsyncMock(return_value=resp)
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client)
        proxy = BudgetedProviderProxy(inner=provider, budget=budget)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Test")],
        )
        res = await proxy.generate(req)
        self.assertEqual(res.text, "Hello")
        self.assertEqual(budget.physical_requests_dispatched, 1)
        self.assertEqual(budget.logical_operations, 1)
        self.assertEqual(budget.successful_responses, 1)
        self.assertEqual(budget.failed_attempts, 0)
        self.assertEqual(budget.retry_attempts, 0)

    async def test_single_failure_consumes_one_physical_request(self) -> None:
        budget = LiveEvalBudgetGuard(max_requests=5)
        client = AsyncMock()
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        client.chat.completions.create = AsyncMock(
            side_effect=groq.AuthenticationError(
                message="Invalid API Key",
                response=httpx.Response(401, request=req_obj),
                body=None,
            )
        )
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_bad", client_factory=lambda: client, max_retries=2)
        proxy = BudgetedProviderProxy(inner=provider, budget=budget)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Test")],
        )
        with self.assertRaises(ProviderAuthenticationError):
            await proxy.generate(req)

        self.assertEqual(budget.physical_requests_dispatched, 1)
        self.assertEqual(budget.logical_operations, 1)
        self.assertEqual(budget.failed_attempts, 1)
        self.assertEqual(budget.successful_responses, 0)

    async def test_retries_consume_physical_requests_individually(self) -> None:
        budget = LiveEvalBudgetGuard(max_requests=5)
        client = AsyncMock()
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content="Retried answer"), finish_reason="stop")]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)

        # 2 timeouts followed by 1 success -> 3 physical dispatches
        client.chat.completions.create = AsyncMock(
            side_effect=[
                groq.APITimeoutError(request=req_obj),
                groq.APITimeoutError(request=req_obj),
                resp,
            ]
        )
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client, max_retries=2, retry_backoff_seconds=0.0)
        proxy = BudgetedProviderProxy(inner=provider, budget=budget)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Test retry")],
        )
        res = await proxy.generate(req)
        self.assertEqual(res.text, "Retried answer")
        self.assertEqual(client.chat.completions.create.await_count, 3)
        self.assertEqual(budget.physical_requests_dispatched, 3)
        self.assertEqual(budget.logical_operations, 1)
        self.assertEqual(budget.retry_attempts, 2)
        self.assertEqual(budget.successful_responses, 1)
        self.assertEqual(budget.failed_attempts, 2)

    async def test_retry_blocked_before_dispatch_when_budget_exhausted(self) -> None:
        # Cap is strictly 2 physical requests. Provider has max_retries=3.
        budget = LiveEvalBudgetGuard(max_requests=2)
        client = AsyncMock()
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        client.chat.completions.create = AsyncMock(side_effect=groq.APITimeoutError(request=req_obj))
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client, max_retries=3, retry_backoff_seconds=0.0)
        proxy = BudgetedProviderProxy(inner=provider, budget=budget)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Test exhaustion")],
        )
        with self.assertRaises(LiveEvalBudgetExceededError):
            await proxy.generate(req)

        # Attempt 0: dispatched (count=1, failed)
        # Attempt 1 (retry 1): dispatched (count=2, failed)
        # Attempt 2 (retry 2): reserve_attempt raises LiveEvalBudgetExceededError BEFORE dispatch!
        self.assertEqual(client.chat.completions.create.await_count, 2)
        self.assertEqual(budget.physical_requests_dispatched, 2)
        self.assertGreaterEqual(budget.budget_rejections, 1)

    async def test_streaming_physical_attempts_and_chunk_safety(self) -> None:
        budget = LiveEvalBudgetGuard(max_requests=5)

        async def _stream_generator():
            for i in range(50):
                c = MagicMock()
                c.choices = [MagicMock(delta=MagicMock(content=f"word{i} "), finish_reason=None)]
                c.usage = None
                c.x_groq = None
                yield c
            end_chunk = MagicMock()
            end_chunk.choices = [MagicMock(delta=MagicMock(content="done"), finish_reason="stop")]
            end_chunk.usage = MagicMock(prompt_tokens=20, completion_tokens=50, total_tokens=70)
            end_chunk.x_groq = None
            yield end_chunk

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=_stream_generator())
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client)
        proxy = BudgetedProviderProxy(inner=provider, budget=budget)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Stream test")],
            stream=True,
        )
        chunks = [c async for c in proxy.stream(req)]
        self.assertGreaterEqual(len(chunks), 50)
        # 50 delta chunks must consume exactly 1 physical request unit!
        self.assertEqual(budget.physical_requests_dispatched, 1)
        self.assertEqual(budget.successful_responses, 1)

    async def test_streaming_connection_retry_consumes_physical_requests(self) -> None:
        budget = LiveEvalBudgetGuard(max_requests=5)
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")

        async def _stream_generator():
            c = MagicMock()
            c.choices = [MagicMock(delta=MagicMock(content="ok"), finish_reason="stop")]
            c.usage = MagicMock(prompt_tokens=5, completion_tokens=2, total_tokens=7)
            c.x_groq = None
            yield c

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[
                groq.APITimeoutError(request=req_obj),
                _stream_generator(),
            ]
        )
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client, max_retries=1, retry_backoff_seconds=0.0)
        proxy = BudgetedProviderProxy(inner=provider, budget=budget)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Stream retry")],
            stream=True,
        )
        chunks = [c async for c in proxy.stream(req)]
        self.assertTrue(chunks[-1].done)
        self.assertEqual(client.chat.completions.create.await_count, 2)
        self.assertEqual(budget.physical_requests_dispatched, 2)
        self.assertEqual(budget.retry_attempts, 1)

    async def test_concurrent_physical_request_budget_enforcement(self) -> None:
        budget = LiveEvalBudgetGuard(max_requests=5)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Concurrent test")],
        )

        async def _worker():
            try:
                res = await budget.reserve_attempt(req, attempt=0, is_streaming=False)
                await res.record_success(UsageMetrics(prompt_tokens=10, completion_tokens=10, total_tokens=20), usage_available=True)
                return True
            except LiveEvalBudgetExceededError:
                return False

        results = await asyncio.gather(*[_worker() for _ in range(25)])
        successes = sum(1 for r in results if r is True)
        rejections = sum(1 for r in results if r is False)

        self.assertEqual(successes, 5)
        self.assertEqual(rejections, 20)
        self.assertEqual(budget.physical_requests_dispatched, 5)
        self.assertEqual(budget.budget_rejections, 20)

    async def test_cancellation_retains_uncertain_spend_when_dispatched(self) -> None:
        budget = LiveEvalBudgetGuard(
            max_requests=5,
            max_cost=1.0,
            input_cost_per_million=10.0,
            output_cost_per_million=10.0,
            pricing_source="unit_test",
            pricing_effective_date="2026-11-27",
        )
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Cancel test")],
            max_tokens=50,
        )
        res = await budget.reserve_attempt(req, attempt=0, is_streaming=False)
        reserved_amt = res.reserved_cost
        self.assertGreater(budget.active_reserved_cost, 0.0)

        await res.record_failure(asyncio.CancelledError(), dispatched=True, partial_output=False)
        self.assertEqual(budget.active_reserved_cost, 0.0)
        self.assertEqual(budget.failed_attempts, 1)
        self.assertEqual(budget.uncertain_failures_count, 1)
        self.assertAlmostEqual(budget.uncertain_estimated_cost, reserved_amt, places=9)
        self.assertAlmostEqual(budget.accumulated_cost, reserved_amt, places=9)

    async def test_client_initialization_failure_consumes_zero_requests_or_cost(self) -> None:
        budget = LiveEvalBudgetGuard(
            max_requests=5,
            max_cost=1.0,
            input_cost_per_million=10.0,
            output_cost_per_million=10.0,
            pricing_source="unit_test",
            pricing_effective_date="2026-11-27",
        )
        # 1. Non-streaming with placeholder API key -> _create_client fails before reserve_attempt
        provider_bad_key = GroqProvider(api_key="replace_with_your_key")
        proxy_bad = BudgetedProviderProxy(inner=provider_bad_key, budget=budget)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Init fail")],
            max_tokens=50,
        )
        with self.assertRaises(ProviderConfigurationError):
            await proxy_bad.generate(req)

        # 2. Streaming with failing custom client_factory -> also consumes 0 units
        def _failing_factory():
            raise ProviderConfigurationError("Factory initialization failed")

        provider_bad_factory = GroqProvider(api_key="gsk_test", client_factory=_failing_factory)
        proxy_bad_stream = BudgetedProviderProxy(inner=provider_bad_factory, budget=budget)
        with self.assertRaises(ProviderConfigurationError):
            async for _ in proxy_bad_stream.stream(req):
                pass

        self.assertEqual(budget.physical_requests_dispatched, 0)
        self.assertEqual(budget.active_reserved_cost, 0.0)
        self.assertEqual(budget.accumulated_reserved_cost, 0.0)
        self.assertEqual(budget.uncertain_estimated_cost, 0.0)
        self.assertEqual(budget.failed_attempts, 0)

    async def test_cache_hits_consume_zero_provider_budget(self) -> None:
        cache = ExactCache(max_entries=10)
        core = IntelliCacheCore(cache=cache)
        budget = LiveEvalBudgetGuard(max_requests=5)

        client = AsyncMock()
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content="Cached response"), finish_reason="stop")]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        client.chat.completions.create = AsyncMock(return_value=resp)
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client)
        proxy = BudgetedProviderProxy(inner=provider, budget=budget)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Cache me")],
            visibility="tenant_shared",
            temperature=0.0,
        )

        # 1st call: Cold miss (consumes 1)
        r1 = await core.execute_query(req, proxy)
        self.assertFalse(r1.cached)
        self.assertEqual(budget.physical_requests_dispatched, 1)

        # 2nd call: Warm exact hit (consumes 0)
        r2 = await core.execute_query(req, proxy)
        self.assertTrue(r2.cached)
        self.assertEqual(budget.physical_requests_dispatched, 1)
        self.assertEqual(client.chat.completions.create.await_count, 1)

    async def test_production_groq_provider_works_with_no_guard(self) -> None:
        client = AsyncMock()
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content="Prod answer"), finish_reason="stop")]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        client.chat.completions.create = AsyncMock(return_value=resp)
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client, attempt_guard=None)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Prod test")],
        )
        res = await provider.generate(req)
        self.assertEqual(res.text, "Prod answer")

    async def test_sensitive_bypass_clears_stale_admission_and_records_dispatch_ledger(self) -> None:
        cache = ExactCache(max_entries=10)
        core = IntelliCacheCore(cache=cache)
        mock_sem = MagicMock()
        mock_sem.last_admission_decision = MagicMock(status="ADMIT", reason_code="ADMISSION_ADMITTED")
        core.semantic_cache = mock_sem

        budget = LiveEvalBudgetGuard(
            max_requests=5,
            max_cost=1.0,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="unit_test",
            pricing_effective_date="2026-11-27",
        )
        client = AsyncMock()
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content="Sensitive URI ok"), finish_reason="stop")]
        resp.usage = MagicMock(prompt_tokens=15, completion_tokens=5, total_tokens=20)
        client.chat.completions.create = AsyncMock(return_value=resp)
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client)
        proxy = BudgetedProviderProxy(inner=provider, budget=budget)
        proxy.set_dispatch_context("gateway_workload", "LPE-019")

        req_sensitive = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Check postgresql://user:Pass123@localhost:5432/db")],
            max_tokens=50,
            visibility="tenant_shared",
            temperature=0.0,
        )
        res = await core.execute_query(req_sensitive, proxy)
        self.assertFalse(res.cached)
        # Sensitive bypass must clear stale last_admission_decision so runners never misattribute previous decision
        self.assertIsNone(mock_sem.last_admission_decision)
        self.assertEqual(len(budget.dispatch_ledger), 1)
        entry = budget.dispatch_ledger[0]
        self.assertEqual(entry["phase"], "gateway_workload")
        self.assertEqual(entry["case_id"], "LPE-019")
        self.assertEqual(entry["status"], "succeeded")
        self.assertEqual(entry["prompt_tokens"], 15)
        self.assertEqual(entry["completion_tokens"], 5)
        self.assertNotIn("Pass123", json.dumps(entry))


class TestStage38AMonetaryCostEnforcement(unittest.IsolatedAsyncioTestCase):
    """Verifies strict monetary cost-budget enforcement, conservative estimation, and reconciliation."""

    def test_pricing_config_validation_rules(self) -> None:
        # 1. max_cost set without pricing rates fails validation
        with patch.dict(os.environ, {"LIVE_PROVIDER_MAX_COST": "0.50"}, clear=True):
            with self.assertRaises(ValueError):
                Settings()

        # 2. negative pricing rate fails validation
        with patch.dict(
            os.environ,
            {
                "LIVE_PROVIDER_INPUT_COST_PER_MILLION": "-0.05",
                "LIVE_PROVIDER_OUTPUT_COST_PER_MILLION": "0.08",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings()

        # 3. validate_live_eval_profile fails closed on max_cost without full pricing
        with self.assertRaises(ProviderConfigurationError):
            validate_live_eval_profile(
                confirm_live=True,
                live_enabled=True,
                model_name="llama-3.3-70b-versatile",
                api_key="gsk_test123",
                max_cost=0.50,
                input_cost_per_million=None,
                output_cost_per_million=0.08,
                pricing_source="groq",
                pricing_effective_date="2026-11-27",
            )

        # 4. validate_live_eval_profile fails on invalid currency
        with self.assertRaises(ProviderConfigurationError):
            validate_live_eval_profile(
                confirm_live=True,
                live_enabled=True,
                model_name="llama-3.3-70b-versatile",
                api_key="gsk_test123",
                max_cost=0.50,
                input_cost_per_million=0.05,
                output_cost_per_million=0.08,
                currency="INVALID_CURRENCY",
                pricing_source="groq",
                pricing_effective_date="2026-11-27",
            )

    def test_conservative_token_estimation_formula(self) -> None:
        guard = LiveEvalBudgetGuard(max_requests=10)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[
                ChatMessage(role="system", content="You are a helpful assistant."),
                ChatMessage(role="user", content="Hello world!"),
            ],
            max_tokens=150,
        )
        total_chars = sum(len(m.role or "") + len(m.content or "") for m in req.messages)
        expected_prompt = int(math.ceil((total_chars / 3.0) * 1.5)) + 32
        prompt_est, comp_est = guard.estimate_tokens(req)
        self.assertEqual(prompt_est, expected_prompt)
        self.assertEqual(comp_est, 150)

        # Default completion tokens when max_tokens is None
        req_no_max = GenerationRequest(model="llama-3.3-70b-versatile", messages=[ChatMessage(role="user", content="Hi")])
        _, comp_default = guard.estimate_tokens(req_no_max)
        self.assertEqual(comp_default, 2048)

    async def test_pre_dispatch_cost_cap_blocks_excessive_request(self) -> None:
        guard = LiveEvalBudgetGuard(
            max_requests=10,
            max_cost=0.00005,
            input_cost_per_million=10.0,
            output_cost_per_million=100.0,
            pricing_source="test_source",
            pricing_effective_date="2026-11-27",
        )
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Hello")],
            max_tokens=2048,
        )
        with self.assertRaises(LiveEvalBudgetExceededError) as ctx:
            await guard.reserve_attempt(req, attempt=0, is_streaming=False)

        self.assertIn("cost ceiling reached", str(ctx.exception).lower())
        self.assertEqual(guard.physical_requests_dispatched, 0)
        self.assertEqual(guard.budget_rejections, 1)

    async def test_fitting_request_reconciled_with_actual_usage_and_tracks_overrun(self) -> None:
        guard = LiveEvalBudgetGuard(
            max_requests=10,
            max_cost=1.00,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
        )
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Short prompt")],
            max_tokens=50,
        )
        # 1. Reserve attempt
        res = await guard.reserve_attempt(req, attempt=0, is_streaming=False)
        self.assertGreater(guard.active_reserved_cost, 0.0)
        self.assertEqual(guard.accumulated_cost, 0.0)

        # 2. Response returns actual usage: 100 prompt tokens, 40 completion tokens
        # Actual cost: (100 / 1M * 10) + (40 / 1M * 20) = 0.001 + 0.0008 = 0.0018
        await res.record_success(
            UsageMetrics(prompt_tokens=100, completion_tokens=40, total_tokens=140),
            usage_available=True,
        )
        self.assertEqual(guard.active_reserved_cost, 0.0)
        self.assertAlmostEqual(guard.accumulated_cost, 0.0018, places=6)
        self.assertEqual(guard.successful_responses, 1)
        self.assertEqual(guard.summary()["cost_status"], "calculated_from_usage")

        # Overrun check: prompt estimation for "user" (4) + "Short prompt" (12) = 16 chars:
        # prompt_est = ceil(16 / 3 * 1.5) + 32 = 8 + 32 = 40.
        # comp_est = 50.
        # Reserved cost = (40/1M * 10) + (50/1M * 20) = 0.0004 + 0.0010 = 0.0014.
        # Actual cost was 0.0018 > 0.0014, so overrun is 0.0004!
        self.assertEqual(guard.reservation_overrun_count, 1)
        self.assertAlmostEqual(guard.reservation_overrun_cost, 0.0004, places=6)

    async def test_missing_usage_tracking_and_cost_status(self) -> None:
        guard = LiveEvalBudgetGuard(
            max_requests=10,
            max_cost=1.00,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
        )
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Hi")],
            max_tokens=50,
        )
        res = await guard.reserve_attempt(req, attempt=0, is_streaming=False)
        await res.record_success(None, usage_available=False)

        self.assertEqual(guard.usage_missing_calls, 1)
        self.assertEqual(guard.usage_reported_calls, 0)
        self.assertEqual(guard.actual_reported_cost, 0.0)
        self.assertGreater(guard.uncertain_estimated_cost, 0.0)
        self.assertEqual(guard.summary()["cost_status"], "no_usage_reported")
        self.assertEqual(guard.summary()["cost_estimation_method"], "heuristic_char_ratio_v1")
        self.assertEqual(guard.summary()["cost_guarantee"], "heuristic_reservation_only_not_billing_guarantee")

    async def test_concurrent_cost_reservation_invariants(self) -> None:
        # Cap is $0.005. Each request reserves approx $0.0014.
        # Exactly 3 requests can fit under the ceiling (3 * 0.0014 = 0.0042 <= 0.005, 4 * 0.0014 = 0.0056 > 0.005).
        guard = LiveEvalBudgetGuard(
            max_requests=20,
            max_cost=0.005,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
        )
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Concurrency cost test")],
            max_tokens=50,
        )

        async def _worker():
            try:
                res = await guard.reserve_attempt(req, attempt=0, is_streaming=False)
                # Keep active_reserved_cost held during concurrency test
                await asyncio.sleep(0.01)
                await res.record_success(UsageMetrics(prompt_tokens=10, completion_tokens=10, total_tokens=20), usage_available=True)
                return True
            except LiveEvalBudgetExceededError:
                return False

        results = await asyncio.gather(*[_worker() for _ in range(10)])
        successes = sum(1 for r in results if r is True)
        rejections = sum(1 for r in results if r is False)

        self.assertEqual(successes, 3)
        self.assertEqual(rejections, 7)
        self.assertLessEqual(guard.accumulated_cost, 0.005)

    async def test_timeout_after_acceptance_retains_uncertain_spend_and_blocks_next(self) -> None:
        # Cap allows ~1 request ($0.002 ceiling, each request reserves ~$0.0014).
        guard = LiveEvalBudgetGuard(
            max_requests=10,
            max_cost=0.002,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
        )
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=groq.APITimeoutError(request=req_obj))
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client, max_retries=0)
        proxy = BudgetedProviderProxy(inner=provider, budget=guard)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Short prompt")],
            max_tokens=50,
        )

        with self.assertRaises(ProviderTimeoutError):
            await proxy.generate(req)

        # Ambiguous timeout must NOT release spend back to $0 — must retain reserved_cost ($0.0014)
        self.assertEqual(guard.uncertain_failures_count, 1)
        self.assertEqual(guard.unprocessed_failures_count, 0)
        self.assertAlmostEqual(guard.uncertain_estimated_cost, 0.0014, places=6)
        self.assertEqual(guard.actual_reported_cost, 0.0)

        # Second request reserves $0.0014 -> 0.0014 + 0.0014 = 0.0028 > $0.002 -> must block before dispatch!
        with self.assertRaises(LiveEvalBudgetExceededError):
            await proxy.generate(req)
        self.assertEqual(guard.physical_requests_dispatched, 1)
        self.assertEqual(client.chat.completions.create.await_count, 1)

    async def test_unprocessed_failures_release_reservation_cleanly(self) -> None:
        guard = LiveEvalBudgetGuard(
            max_requests=10,
            max_cost=0.002,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
        )
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        resp_ok = MagicMock()
        resp_ok.choices = [MagicMock(message=MagicMock(content="OK"), finish_reason="stop")]
        resp_ok.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=[
                groq.RateLimitError(message="429", response=httpx.Response(429, request=req_obj), body=None),
                resp_ok,
            ]
        )
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client, max_retries=1, retry_backoff_seconds=0.0)
        proxy = BudgetedProviderProxy(inner=provider, budget=guard)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Short prompt")],
            max_tokens=50,
        )

        # 1st attempt: 429 unprocessed rejection releases cost -> 2nd attempt succeeds under $0.002 ceiling!
        res = await proxy.generate(req)
        self.assertEqual(res.text, "OK")
        self.assertEqual(guard.physical_requests_dispatched, 2)
        self.assertEqual(guard.unprocessed_failures_count, 1)
        self.assertEqual(guard.uncertain_failures_count, 0)
        self.assertGreater(guard.released_unprocessed_cost, 0.0)
        self.assertEqual(guard.uncertain_estimated_cost, 0.0)

    async def test_partial_streaming_cancellation_and_midstream_drop_retain_uncertain_spend(self) -> None:
        guard = LiveEvalBudgetGuard(
            max_requests=10,
            max_cost=1.0,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
        )
        req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")

        async def _partial_then_drop():
            c1 = MagicMock()
            c1.choices = [MagicMock(delta=MagicMock(content="Partial token "), finish_reason=None)]
            c1.usage = None
            c1.x_groq = None
            yield c1
            raise groq.APIConnectionError(request=req_obj, message="Mid-stream drop")

        async def _infinite_partial():
            for i in range(10):
                c = MagicMock()
                c.choices = [MagicMock(delta=MagicMock(content=f"tok{i} "), finish_reason=None)]
                c.usage = None
                c.x_groq = None
                yield c

        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=[_partial_then_drop(), _infinite_partial()])
        client.close = AsyncMock()

        provider = GroqProvider(api_key="gsk_test", client_factory=lambda: client)
        proxy = BudgetedProviderProxy(inner=provider, budget=guard)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Stream partial")],
            max_tokens=50,
            stream=True,
        )

        # Case 1: Mid-stream connection failure after yielding partial output
        with self.assertRaises(ProviderConnectionError):
            async for _ in proxy.stream(req):
                pass
        self.assertEqual(guard.partial_stream_failures_count, 1)
        self.assertEqual(guard.uncertain_failures_count, 1)
        self.assertGreater(guard.uncertain_estimated_cost, 0.0)
        self.assertEqual(guard.active_reserved_cost, 0.0)

        # Case 2: Consumer closes stream early (GeneratorExit via aclose) after 1st chunk
        prev_uncertain = guard.uncertain_estimated_cost
        stream_gen = proxy.stream(req)
        first_chunk = await stream_gen.__anext__()
        self.assertEqual(first_chunk.delta, "tok0 ")
        await stream_gen.aclose()

        self.assertEqual(guard.partial_stream_failures_count, 2)
        self.assertEqual(guard.uncertain_failures_count, 2)
        self.assertGreater(guard.uncertain_estimated_cost, prev_uncertain)
        self.assertEqual(guard.active_reserved_cost, 0.0)

    async def test_report_only_mode_contract_and_strict_separation(self) -> None:
        # report_only: NEVER blocks on max_cost, reports actual/uncertain/reserved separately, STILL enforces max_requests!
        guard = LiveEvalBudgetGuard(
            max_requests=3,
            max_cost=0.0001,  # tiny cap that every call exceeds
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
            cost_mode="report_only",
        )
        req_unbounded = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Report only unbounded")],
            max_tokens=None,
        )
        # Call 1: succeeds with actual usage even though cost exceeds max_cost!
        r1 = await guard.reserve_attempt(req_unbounded, attempt=0, is_streaming=False)
        await r1.record_success(UsageMetrics(prompt_tokens=100, completion_tokens=50, total_tokens=150), usage_available=True)

        # Call 2: succeeds with missing usage (recorded in uncertain_estimated_cost, NOT dropped or mixed into actual)
        r2 = await guard.reserve_attempt(req_unbounded, attempt=0, is_streaming=False)
        await r2.record_success(None, usage_available=False)

        # Call 3: succeeds with ambiguous failure
        r3 = await guard.reserve_attempt(req_unbounded, attempt=0, is_streaming=False)
        await r3.record_failure(TimeoutError("timeout"), dispatched=True, partial_output=False)

        # Call 4: MUST still block on physical max_requests=3!
        with self.assertRaises(LiveEvalBudgetExceededError):
            await guard.reserve_attempt(req_unbounded, attempt=0, is_streaming=False)

        snap = guard.summary()
        self.assertEqual(snap["physical_requests_dispatched"], 3)
        self.assertTrue(snap["cost_ceiling_exceeded_in_report_only"])
        self.assertGreater(snap["actual_reported_cost_usd"], 0.0)
        self.assertGreater(snap["uncertain_estimated_cost_usd"], 0.0)
        self.assertGreater(snap["accumulated_reserved_cost"], 0.0)
        self.assertAlmostEqual(snap["accumulated_cost_usd"], snap["actual_reported_cost_usd"], places=6)

    async def test_input_and_output_token_ceilings_enforced_before_dispatch(self) -> None:
        # 1. Input ceiling wired through validate_live_eval_profile and enforced in LiveEvalBudgetGuard
        profile = validate_live_eval_profile(
            confirm_live=True,
            live_enabled=True,
            model_name="llama-3.3-70b-versatile",
            api_key="gsk_testkey123",
            max_input_tokens=45,
            max_output_tokens=100,
        )
        self.assertEqual(profile["max_input_tokens"], 45)
        self.assertEqual(profile["max_output_tokens"], 100)

        guard = LiveEvalBudgetGuard(
            max_requests=5,
            max_cost=1.0,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
            max_input_tokens=45,
            max_output_tokens=100,
        )
        # Small prompt ("user" + "Hi" = 6 chars -> ceil(6/3*1.5)+32 = 35 <= 45) succeeds
        ok_req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Hi")],
            max_tokens=50,
        )
        res = await guard.reserve_attempt(ok_req, attempt=0, is_streaming=False)
        self.assertEqual(guard.physical_requests_dispatched, 1)
        await res.record_success(UsageMetrics(prompt_tokens=10, completion_tokens=10, total_tokens=20), usage_available=True)

        # Oversized prompt ("user" + 100 chars -> ceil(104/3*1.5)+32 = 84 > 45) blocked BEFORE dispatch!
        big_in_req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="x" * 100)],
            max_tokens=50,
        )
        with self.assertRaises(LiveEvalBudgetExceededError):
            await guard.reserve_attempt(big_in_req, attempt=0, is_streaming=False)
        self.assertEqual(guard.physical_requests_dispatched, 1)

        # Oversized output max_tokens > 100 blocked BEFORE dispatch
        big_out_req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Hi")],
            max_tokens=250,
        )
        with self.assertRaises(LiveEvalBudgetExceededError):
            await guard.reserve_attempt(big_out_req, attempt=0, is_streaming=False)
        self.assertEqual(guard.physical_requests_dispatched, 1)

        # Unbounded request (max_tokens=None) rejected in strict monetary mode!
        guard_strict_no_out = LiveEvalBudgetGuard(
            max_requests=5,
            max_cost=1.0,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
            cost_mode="strict",
        )
        unbounded_req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Hi")],
            max_tokens=None,
        )
        with self.assertRaises(LiveEvalBudgetExceededError):
            await guard_strict_no_out.reserve_attempt(unbounded_req, attempt=0, is_streaming=False)
        self.assertEqual(guard_strict_no_out.physical_requests_dispatched, 0)

    async def test_concurrent_retries_and_overbudget_reconciliation(self) -> None:
        # Ceiling is $0.003. Two concurrent operations run retries and overrun reservations.
        guard = LiveEvalBudgetGuard(
            max_requests=10,
            max_cost=0.003,
            input_cost_per_million=10.0,
            output_cost_per_million=20.0,
            pricing_source="test_rates",
            pricing_effective_date="2026-11-27",
        )
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Short")],
            max_tokens=20,
        )
        # Heuristic reservation is ~($0.00037 + $0.0004 = $0.00077)
        # Actual reported usage on success is huge (400 prompt + 50 comp -> $0.004 + $0.001 = $0.005) > $0.003!
        res = await guard.reserve_attempt(req, attempt=0, is_streaming=False)
        await res.record_success(
            UsageMetrics(prompt_tokens=400, completion_tokens=50, total_tokens=450),
            usage_available=True,
        )
        self.assertEqual(guard.reservation_overrun_count, 1)
        self.assertGreater(guard.reservation_overrun_cost, 0.004)
        self.assertGreater(guard.actual_reported_cost, 0.003)

        # Any further retry or new request MUST be blocked immediately because overrun exceeded max_cost!
        with self.assertRaises(LiveEvalBudgetExceededError):
            await guard.reserve_attempt(req, attempt=1, is_streaming=False)
        self.assertEqual(guard.physical_requests_dispatched, 1)


class TestStage38AReportProvenanceAndSanitization(unittest.IsolatedAsyncioTestCase):
    """Verifies that generated evaluation reports include complete provenance and zero leaks."""

    def test_markdown_report_includes_provenance_and_accounting(self) -> None:
        report = {
            "stage": "3.8A.2",
            "generated_at": "2026-11-27T12:00:00Z",
            "commit_sha": "ddd7bd43ce2e36898c472f2bccbdfed596b9dbd8",
            "dataset": {
                "name": "stage3_8a_live_provider_dataset",
                "version": "1.0.0",
                "sha256": "abcdef1234567890",
            },
            "production_readiness": False,
            "provider": "groq",
            "model": "llama-3.3-70b-versatile",
            "environment": {"streaming_semantic_cache_supported": False},
            "budget": {
                "max_requests": 40,
                "physical_requests_dispatched": 25,
                "logical_operations": 20,
                "retry_attempts": 5,
                "successful_responses": 20,
                "failed_attempts": 5,
                "unprocessed_failures_count": 3,
                "uncertain_failures_count": 2,
                "partial_stream_failures_count": 1,
                "budget_rejections": 0,
                "max_cost": 0.50,
                "max_input_tokens": 4096,
                "max_output_tokens": 512,
                "currency": "USD",
                "cost_mode": "strict",
                "cost_estimation_method": "heuristic_char_ratio_v1",
                "cost_guarantee": "heuristic_reservation_only_not_billing_guarantee",
                "accumulated_cost_usd": 0.045,
                "actual_reported_cost_usd": 0.042,
                "uncertain_estimated_cost_usd": 0.003,
                "released_unprocessed_cost_usd": 0.004,
                "accumulated_reserved_cost": 0.050,
                "cost_status": "calculated_from_usage",
                "pricing_source": "https://groq.com/pricing",
                "pricing_effective_date": "2026-11-27",
                "reservation_overrun_count": 0,
                "reservation_overrun_cost": 0.0,
                "total_tokens_reported": 12500,
                "prompt_tokens_reported": 8000,
                "completion_tokens_reported": 4500,
                "usage_reported_calls": 20,
                "usage_missing_calls": 0,
            },
            "mode_a_upstream_only": {"latency_summary": "p50: 250ms"},
            "failure_recovery_matrix": [
                {"id": "F-01", "scenario": "Rate limit", "source": "groq", "passed": True}
            ],
            "limitations": ["Live evaluation opt-in only; safe production defaults remain disabled."],
        }
        md = render_stage38a_markdown(report)
        self.assertIn("ddd7bd43ce2e36898c472f2bccbdfed596b9dbd8", md)
        self.assertIn("abcdef1234567890", md)
        self.assertIn("Physical Groq Dispatches", md)
        self.assertIn("Cost Ceiling", md)
        self.assertIn("Spend Breakdown", md)
        self.assertIn("heuristic_char_ratio_v1", md)
        self.assertIn("heuristic_reservation_only_not_billing_guarantee", md)
        self.assertIn("Token Ceilings", md)
        self.assertIn("Limitations and Operational Scope", md)

        secret_sample = "gsk_testsecretsample12345"
        verify_no_sensitive_leaks(md, [secret_sample], "markdown_disclosure_test")


@unittest.skipUnless(
    os.getenv("LIVE_PROVIDER_EVALUATION_ENABLED", "false").lower() in ("true", "1", "yes")
    and os.getenv("LIVE_PROVIDER_CONFIRM_SPEND", "false").lower() in ("true", "1", "yes"),
    "Opt-in Stage 3.8A live Groq test skipped unless LIVE_PROVIDER_EVALUATION_ENABLED=true and LIVE_PROVIDER_CONFIRM_SPEND=true.",
)
class TestStage38AOptInLiveGroqEvaluation(unittest.IsolatedAsyncioTestCase):
    """Manual opt-in real Groq + PG + FastEmbed test; never executes automatically in CI."""

    async def test_live_provider_smoke_evaluation(self) -> None:
        report = await run_live_provider_evaluation(
            confirm_live=True,
            max_requests_override=10,
            case_ids=("LPE-001", "LPE-007", "LPE-002", "LPE-008"),
            skip_mode_comparison=True,
        )
        self.assertEqual(report["provider"], "groq")
        self.assertGreater(report["budget"]["requests_made"], 0)
        self.assertTrue(all(item["passed"] for item in report["failure_recovery_matrix"]))
