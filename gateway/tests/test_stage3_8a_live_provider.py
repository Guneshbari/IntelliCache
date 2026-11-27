"""
Stage 3.8A Verification Tests:
1. Offline configuration, opt-in guard, dataset schema, and defaults tests.
2. GroqProvider timeout, bounded retries, streaming usage/TTFT, and cancellation tests.
3. Budget enforcement, redaction, fault-injection recovery, and FastAPI HTTP error/stream tests.
4. Opt-in real Groq + PG evaluation test (skipped unless explicitly configured).
"""

import asyncio
import json
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
    LiveEvalBudgetExceededError,
    LiveEvalBudgetGuard,
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
            max_cost=0.001,
            input_cost_per_million=100.0,
            output_cost_per_million=100.0,
            pricing_source="unit_test_rate",
        )
        await g_cost.reserve_request()
        await g_cost.record_usage(UsageMetrics(prompt_tokens=10, completion_tokens=10, total_tokens=20), usage_available=True)
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
