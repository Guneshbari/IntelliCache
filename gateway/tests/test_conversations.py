"""
Integration and unit tests for persistent conversation management, identity isolation,
bounded context construction, and conversational query execution in IntelliCache Gateway.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import asyncpg
from fastapi.testclient import TestClient
import httpx

from app.cache.migration import run_migrations
from app.config import Settings, get_settings, reload_settings
from app.context.builder import ContextBuilder
from app.context.contracts import ContextStatus
from app.context.policy import ContextPolicy
from app.conversations.identity import (
    AuthenticatedClient,
    _is_loopback_address,
    compute_turn_request_hash,
    derive_principal_id,
    verify_conversation_client,
)
from app.conversations.models import ConversationRecord
from app.conversations.repository import (
    ActiveTurnConflictError,
    ConversationNotFoundError,
    IdempotencyConflictError,
    PostgresConversationRepository,
)
from app.conversations.service import ConversationService
from app.core.contracts import ChatMessage
from app.main import app
from app.api.routes import TurnLeaseHeartbeat
from app.routes.query import (
    get_groq_client_factory,
    get_query_cache,
    set_query_cache,
)

APPROVED_TEST_DATABASES: frozenset = frozenset({"intellicache_test"})


class MockChoice:
    def __init__(self, content: str):
        self.message = type("Msg", (), {"content": content})


class MockUsage:
    def __init__(self, prompt_tokens: int = 10, completion_tokens: int = 20, total_tokens: int = 30):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class MockCompletion:
    def __init__(self, content: str = "Test response from Groq"):
        self.choices = [MockChoice(content)]
        self.usage = MockUsage()


class TestConversationIdentity(unittest.TestCase):
    """Unit tests for principal derivation and client authentication."""

    def test_derive_principal_id_determinism(self):
        token = "secret-client-token-123"
        secret = "system-identity-secret-456"
        p1 = derive_principal_id(token, secret)
        p2 = derive_principal_id(token, secret)
        self.assertEqual(p1, p2)
        self.assertEqual(len(p1), 64)  # SHA-256 hex digest length

    def test_derive_principal_id_different_tokens(self):
        secret = "system-identity-secret"
        p1 = derive_principal_id("token-alpha", secret)
        p2 = derive_principal_id("token-beta", secret)
        self.assertNotEqual(p1, p2)

    def test_derive_principal_id_different_secrets(self):
        token = "token-alpha"
        p1 = derive_principal_id(token, "secret-one-123456789")
        p2 = derive_principal_id(token, "secret-two-123456789")
        self.assertNotEqual(p1, p2)

    def test_derive_principal_id_invalid_inputs(self):
        with self.assertRaises(ValueError):
            derive_principal_id("", "secret")
        with self.assertRaises(ValueError):
            derive_principal_id("   ", "secret")
        with self.assertRaises(ValueError):
            derive_principal_id("token", "")
        with self.assertRaises(ValueError):
            derive_principal_id("token", "   ")

    def test_is_loopback_address(self):
        self.assertTrue(_is_loopback_address("127.0.0.1"))
        self.assertTrue(_is_loopback_address("127.0.1.1"))
        self.assertTrue(_is_loopback_address("::1"))
        self.assertTrue(_is_loopback_address("::ffff:127.0.0.1"))
        self.assertTrue(_is_loopback_address("localhost"))

        self.assertFalse(_is_loopback_address("192.168.1.100"))
        self.assertFalse(_is_loopback_address("10.0.0.1"))
        self.assertFalse(_is_loopback_address("172.16.0.1"))
        self.assertFalse(_is_loopback_address("8.8.8.8"))
        self.assertFalse(_is_loopback_address(""))
        self.assertFalse(_is_loopback_address("unknown-host"))

    def test_insecure_secret_rejected_at_startup(self):
        """Verifies that known placeholder and insecure identity secrets fail startup validation."""
        insecure_secrets = [
            "intellicache-default-identity-secret-change-in-production",
            "replace_with_a_secure_random_hex_or_passphrase_at_least_16_chars",
            "replace_with_your_key",
            "short-secret",
        ]
        for sec in insecure_secrets:
            with self.assertRaises(ValueError):
                Settings(
                    conversation_identity_secret=sec,
                    client_api_token="test_client_token",
                )

    def test_stateless_query_operational_when_secret_unset(self):
        """Verifies that stateless queries work when CONVERSATION_IDENTITY_SECRET is unset."""
        with patch.dict(
            os.environ,
            {
                "CLIENT_API_TOKENS": json.dumps({"test_stateless_token": "default"}),
                "CONVERSATION_IDENTITY_SECRET": "",
            },
            clear=False,
        ):
            reload_settings()
            try:
                client = TestClient(app)
                # Stateless query without conversation_id works
                resp = client.post(
                    "/query",
                    json={"prompt": "Hello stateless"},
                    headers={"X-Client-Token": "test_stateless_token"},
                )
                # Should not be 503 from missing identity secret
                self.assertNotEqual(resp.status_code, 503)

                # Persistent conversation creation returns 503
                c_resp = client.post(
                    "/conversations",
                    headers={"X-Client-Token": "test_stateless_token"},
                )
                self.assertEqual(c_resp.status_code, 503)
                self.assertIn("CONVERSATION_IDENTITY_SECRET must be set", c_resp.json()["detail"])
            finally:
                reload_settings()

    def test_compute_turn_request_hash_conservative_whitespace(self):
        """Verifies conservative whitespace handling preserves multiline indentation and structure."""
        p1 = "def solve():\n    x = 10\n    return x"
        p2 = "def solve():\n  x = 10\n  return x"
        p3 = "def solve(): x = 10; return x"
        p4 = "  def solve():\n    x = 10\n    return x  \n\n"
        p5 = "def solve():\r\n    x = 10\r\n    return x"

        h1 = compute_turn_request_hash(prompt=p1, model="m1", temperature=0.7)
        h2 = compute_turn_request_hash(prompt=p2, model="m1", temperature=0.7)
        h3 = compute_turn_request_hash(prompt=p3, model="m1", temperature=0.7)
        h4 = compute_turn_request_hash(prompt=p4, model="m1", temperature=0.7)
        h5 = compute_turn_request_hash(prompt=p5, model="m1", temperature=0.7)

        # Trimming outer whitespace preserves identical core hash
        self.assertEqual(h1, h4)
        # Windows CRLF normalizes to Unix LF
        self.assertEqual(h1, h5)
        # Internal indentation difference is preserved and changes hash
        self.assertNotEqual(h1, h2)
        # Inlined/collapsed structure changes hash
        self.assertNotEqual(h1, h3)

    def test_compute_turn_request_hash_float_precision(self):
        """Verifies no lossy rounding on temperature."""
        h1 = compute_turn_request_hash(prompt="prompt", model="m1", temperature=0.70001)
        h2 = compute_turn_request_hash(prompt="prompt", model="m1", temperature=0.70002)
        self.assertNotEqual(h1, h2)

    def test_compute_turn_request_hash_provider_and_bypass_cache(self):
        """Verifies provider and bypass_cache are bound to request hash."""
        h_groq = compute_turn_request_hash(prompt="prompt", model="m1", temperature=0.7, provider="groq", bypass_cache=False)
        h_ollama = compute_turn_request_hash(prompt="prompt", model="m1", temperature=0.7, provider="ollama", bypass_cache=False)
        h_bypass = compute_turn_request_hash(prompt="prompt", model="m1", temperature=0.7, provider="groq", bypass_cache=True)

        self.assertNotEqual(h_groq, h_ollama)
        self.assertNotEqual(h_groq, h_bypass)


class TestTurnLeaseHeartbeat(unittest.IsolatedAsyncioTestCase):
    """Unit tests for TurnLeaseHeartbeat lifecycle and lease loss detection."""

    async def test_heartbeat_periodically_renews_lease(self):
        conv_service = AsyncMock()
        conv_service.renew_turn_lease.return_value = True

        heartbeat = TurnLeaseHeartbeat(
            conv_service=conv_service,
            turn_id="turn_hb_1",
            lease_seconds=30,
            interval=0.03,
        )
        heartbeat.start()
        await asyncio.sleep(0.08)
        await heartbeat.stop()

        self.assertGreaterEqual(conv_service.renew_turn_lease.call_count, 2)
        self.assertFalse(heartbeat.lease_lost)

    async def test_heartbeat_detects_lost_lease(self):
        conv_service = AsyncMock()
        # Returns False simulating turn recovered or cancelled
        conv_service.renew_turn_lease.return_value = False

        heartbeat = TurnLeaseHeartbeat(
            conv_service=conv_service,
            turn_id="turn_hb_lost",
            lease_seconds=30,
            interval=0.02,
        )
        heartbeat.start()
        await asyncio.sleep(0.06)
        await heartbeat.stop()

        self.assertTrue(heartbeat.lease_lost)


class TestConversationsFallbackWithoutDB(unittest.TestCase):
    """Tests ensuring graceful degradation when PostgreSQL is not configured."""

    def setUp(self):
        self.token = "valid-test-fallback-token-123"
        tokens_json = json.dumps({self.token: "default"})
        self.env_patcher = patch.dict(
            os.environ,
            {
                "CLIENT_API_TOKENS": tokens_json,
                "CONVERSATION_IDENTITY_SECRET": "fallback-test-secret-12345678",
            },
            clear=False,
        )
        self.env_patcher.start()
        reload_settings()

        self.client = TestClient(app)
        self._saved_pool = getattr(app.state, "db_pool", None)
        app.state.db_pool = None

    def tearDown(self):
        self.env_patcher.stop()
        reload_settings()
        app.state.db_pool = self._saved_pool

    def test_conversations_endpoint_returns_503_without_db(self):
        resp = self.client.get("/conversations", headers={"X-Client-Token": self.token})
        self.assertEqual(resp.status_code, 503)
        data = resp.json()
        self.assertIn("detail", data)
        self.assertIn("requires a PostgreSQL backend", data["detail"])

    def test_query_with_conversation_id_missing_idempotency_key_returns_400(self):
        resp = self.client.post(
            "/query",
            json={"prompt": "Hello", "conversation_id": "conv_123"},
            headers={"X-Client-Token": self.token},
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertIn("Idempotency-Key header is required", data["detail"])

    def test_query_with_conversation_id_returns_503_without_db(self):
        resp = self.client.post(
            "/query",
            json={"prompt": "Hello", "conversation_id": "conv_123"},
            headers={
                "X-Client-Token": self.token,
                "Idempotency-Key": "test-key-123",
            },
        )
        self.assertEqual(resp.status_code, 503)
        data = resp.json()
        self.assertIn("requires a PostgreSQL backend", data["detail"])


class TestPostgresConversationsIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration tests with live PostgreSQL database."""

    async def asyncSetUp(self):
        explicit_url = os.getenv("TEST_DATABASE_URL")
        if not explicit_url:
            self.skipTest("TEST_DATABASE_URL environment variable is not set.")

        self.db_url = explicit_url
        parsed = urlsplit(self.db_url)
        db_name = (parsed.path or "").lstrip("/")
        if db_name not in APPROVED_TEST_DATABASES:
            self.fail(
                f"Safety check failed: Database '{db_name}' is not in the approved test database allowlist: "
                f"{sorted(APPROVED_TEST_DATABASES)}."
            )

        self.token_a = "token_conv_user_alpha"
        self.token_b = "token_conv_user_beta"
        self.ns = "conv_test_ns"

        # Capture app state
        self._saved_dependency_overrides = dict(app.dependency_overrides)
        self._saved_query_cache = get_query_cache()
        self._saved_db_pool = getattr(app.state, "db_pool", None)

        self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
        self.addAsyncCleanup(self._cleanup_database_and_pool)
        self.addAsyncCleanup(self._restore_app_state)

        await run_migrations(self.pool)

        # Clean existing test data
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM conversation_messages")
            await conn.execute("DELETE FROM conversation_turns")
            await conn.execute("DELETE FROM conversations")
            await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)

        # Set up repository and service
        from app.cache.postgres_cache import PostgresCache
        self.repo = PostgresConversationRepository(self.pool)
        self.service = ConversationService(self.repo)
        app.state.db_pool = self.pool
        set_query_cache(PostgresCache(self.pool))

        # Mock Groq provider
        self.provider_call_count = 0
        self.last_provider_messages = []
        self.mock_groq = AsyncMock()

        def side_effect(*args, **kwargs):
            self.provider_call_count += 1
            if "messages" in kwargs:
                self.last_provider_messages = kwargs["messages"]
            return MockCompletion(f"Assistant Response #{self.provider_call_count}")

        self.mock_groq.chat.completions.create.side_effect = side_effect
        self.mock_groq.close = AsyncMock()
        app.dependency_overrides[get_groq_client_factory] = lambda: lambda: self.mock_groq

        # Configure environment
        tokens_json = json.dumps({
            self.token_a: self.ns,
            self.token_b: self.ns,
        })
        self.env_patcher = patch.dict(os.environ, {
            "CLIENT_API_TOKENS": tokens_json,
            "CACHE_BACKEND": "postgres",
            "DATABASE_URL": self.db_url,
            "CACHE_ENABLED": "true",
            "CONVERSATION_IDENTITY_SECRET": "test-conv-secret-key-999",
            "CONVERSATION_MAX_CONTEXT_MESSAGES": "50",
            "CONVERSATION_MAX_CONTEXT_CHARS": "65536",
        }, clear=False)
        self.env_patcher.start()
        reload_settings()

    async def _cleanup_database_and_pool(self):
        if hasattr(self, "pool") and self.pool is not None:
            try:
                if not self.pool.is_closing():
                    async with self.pool.acquire() as conn:
                        await conn.execute("DELETE FROM conversation_messages")
                        await conn.execute("DELETE FROM conversation_turns")
                        await conn.execute("DELETE FROM conversations")
                        await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)
            finally:
                await self.pool.close()

    async def _restore_app_state(self):
        if hasattr(self, "env_patcher"):
            self.env_patcher.stop()
        if hasattr(self, "_saved_dependency_overrides"):
            app.dependency_overrides.clear()
            app.dependency_overrides.update(self._saved_dependency_overrides)
        reload_settings()
        if hasattr(self, "_saved_query_cache"):
            set_query_cache(self._saved_query_cache)
        app.state.db_pool = self._saved_db_pool

    async def test_conversation_crud_and_isolation(self):
        """Test conversation creation, listing, detail retrieval, and isolation between clients."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b:

            # 1. Client A creates a conversation
            create_resp = await client_a.post(
                "/conversations",
                json={"title": "Client A Chat"},
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(create_resp.status_code, 201)
            conv_data = create_resp.json()
            conv_id = conv_data["id"]
            self.assertEqual(conv_data["title"], "Client A Chat")
            self.assertEqual(conv_data["cache_namespace"], self.ns)

            # 2. Client A lists conversations -> sees the created conversation
            list_resp_a = await client_a.get(
                "/conversations",
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(list_resp_a.status_code, 200)
            list_data_a = list_resp_a.json()
            self.assertEqual(list_data_a["total"], 1)
            self.assertEqual(list_data_a["conversations"][0]["id"], conv_id)

            # 3. Client B (same namespace, different token -> different principal) lists conversations
            list_resp_b = await client_b.get(
                "/conversations",
                headers={"X-Client-Token": self.token_b},
            )
            self.assertEqual(list_resp_b.status_code, 200)
            list_data_b = list_resp_b.json()
            self.assertEqual(list_data_b["total"], 0)
            self.assertEqual(len(list_data_b["conversations"]), 0)

            # 4. Client B attempts to fetch Client A's conversation -> 404
            get_resp_b = await client_b.get(
                f"/conversations/{conv_id}",
                headers={"X-Client-Token": self.token_b},
            )
            self.assertEqual(get_resp_b.status_code, 404)

            # 5. Client B attempts to delete Client A's conversation -> 404
            del_resp_b = await client_b.delete(
                f"/conversations/{conv_id}",
                headers={"X-Client-Token": self.token_b},
            )
            self.assertEqual(del_resp_b.status_code, 404)

            # 6. Client A deletes conversation -> 200 OK
            del_resp_a = await client_a.delete(
                f"/conversations/{conv_id}",
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(del_resp_a.status_code, 200)
            self.assertTrue(del_resp_a.json()["deleted"])

            # 7. Verification: it's gone
            get_resp_a = await client_a.get(
                f"/conversations/{conv_id}",
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(get_resp_a.status_code, 404)

    async def test_server_managed_query_multi_turn_and_idempotency(self):
        """Test multi-turn conversational query flow and idempotent replay."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Create conversation
            c_resp = await client.post(
                "/conversations",
                json={"title": "Multi Turn Conversation"},
                headers={"X-Client-Token": self.token_a},
            )
            conv_id = c_resp.json()["id"]

            # Turn 1
            resp1 = await client.post(
                "/query",
                json={"prompt": "Turn 1 question", "conversation_id": conv_id},
                headers={
                    "X-Client-Token": self.token_a,
                    "Idempotency-Key": "idemp-turn-1",
                },
            )
            self.assertEqual(resp1.status_code, 200)
            data1 = resp1.json()
            self.assertEqual(data1["response"], "Assistant Response #1")
            self.assertFalse(data1["cached"])
            self.assertEqual(data1["cache_type"], "none")
            self.assertEqual(data1["context_status"], "complete")
            self.assertIsNotNone(data1["turn_id"])
            turn1_id = data1["turn_id"]

            # Verify messages in conversation
            msgs_resp1 = await client.get(
                f"/conversations/{conv_id}/messages",
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(msgs_resp1.status_code, 200)
            msgs1 = msgs_resp1.json()["messages"]
            self.assertEqual(len(msgs1), 2)
            self.assertEqual(msgs1[0]["role"], "user")
            self.assertEqual(msgs1[0]["content"], "Turn 1 question")
            self.assertEqual(msgs1[1]["role"], "assistant")
            self.assertEqual(msgs1[1]["content"], "Assistant Response #1")

            # Turn 1 Idempotent Replay (same Idempotency-Key)
            resp1_replay = await client.post(
                "/query",
                json={"prompt": "Turn 1 question", "conversation_id": conv_id},
                headers={
                    "X-Client-Token": self.token_a,
                    "Idempotency-Key": "idemp-turn-1",
                },
            )
            self.assertEqual(resp1_replay.status_code, 200)
            data1_replay = resp1_replay.json()
            self.assertEqual(data1_replay["response"], "Assistant Response #1")
            self.assertFalse(data1_replay["cached"])
            self.assertEqual(data1_replay["cache_type"], "none")
            self.assertTrue(data1_replay.get("idempotent_replay", False))
            self.assertEqual(data1_replay["cache_hit_count"], 0)
            self.assertEqual(data1_replay["fingerprint"], "idempotent_replay")
            self.assertEqual(data1_replay["turn_id"], turn1_id)
            # Upstream provider was NOT called again
            self.assertEqual(self.provider_call_count, 1)

            # Turn 2
            resp2 = await client.post(
                "/query",
                json={"prompt": "Turn 2 question", "conversation_id": conv_id},
                headers={
                    "X-Client-Token": self.token_a,
                    "Idempotency-Key": "idemp-turn-2",
                },
            )
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.json()
            self.assertEqual(data2["response"], "Assistant Response #2")
            self.assertEqual(self.provider_call_count, 2)

            # Verify provider received the reconstructed multi-turn history:
            # [user 1, assistant 1, user 2]
            self.assertEqual(len(self.last_provider_messages), 3)
            self.assertEqual(self.last_provider_messages[0]["role"], "user")
            self.assertEqual(self.last_provider_messages[0]["content"], "Turn 1 question")
            self.assertEqual(self.last_provider_messages[1]["role"], "assistant")
            self.assertEqual(self.last_provider_messages[1]["content"], "Assistant Response #1")
            self.assertEqual(self.last_provider_messages[2]["role"], "user")
            self.assertEqual(self.last_provider_messages[2]["content"], "Turn 2 question")

            # Verify total messages stored is 4 (2 turns * 2 messages)
            msgs_resp2 = await client.get(
                f"/conversations/{conv_id}/messages",
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(msgs_resp2.status_code, 200)
            msgs2 = msgs_resp2.json()["messages"]
            self.assertEqual(len(msgs2), 4)

    async def test_concurrent_active_turn_conflict_409(self):
        """Test that attempting two active turns on the same conversation returns 409 Conflict."""
        # Create conversation directly in repo
        principal = derive_principal_id(self.token_a, "test-conv-secret-key-999")
        conv = await self.repo.create_conversation(None, self.ns, principal, "Conflict Test")

        # Manually begin a turn so it's in 'pending' state
        await self.repo.begin_turn(conv.id, principal, "key-first", "First prompt")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/query",
                json={"prompt": "Second concurrent prompt", "conversation_id": conv.id},
                headers={
                    "X-Client-Token": self.token_a,
                    "Idempotency-Key": "key-second",
                },
            )
            self.assertEqual(resp.status_code, 409)
            data = resp.json()
            self.assertIn("detail", data)
            self.assertIn("turn is currently in progress", data["detail"])

    async def test_context_budget_compacted_bypasses_cache(self):
        """Test that when ContextBuilder truncates messages, status is COMPACTED and exact cache is bypassed."""
        # Set max context messages to 2 (only last turn fits)
        settings = get_settings()
        old_max_msgs = settings.conversation_max_context_messages
        settings.conversation_max_context_messages = 2

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                c_resp = await client.post(
                    "/conversations",
                    json={"title": "Compaction Test"},
                    headers={"X-Client-Token": self.token_a},
                )
                conv_id = c_resp.json()["id"]

                # Turn 1
                await client.post(
                    "/query",
                    json={"prompt": "Question 1", "conversation_id": conv_id},
                    headers={"X-Client-Token": self.token_a, "Idempotency-Key": "t1"},
                )

                # Turn 2
                await client.post(
                    "/query",
                    json={"prompt": "Question 2", "conversation_id": conv_id},
                    headers={"X-Client-Token": self.token_a, "Idempotency-Key": "t2"},
                )

                # Turn 3 -> History has 4 messages, limit is 2 -> Compaction occurs!
                resp3 = await client.post(
                    "/query",
                    json={"prompt": "Question 3", "conversation_id": conv_id},
                    headers={"X-Client-Token": self.token_a, "Idempotency-Key": "t3"},
                )
                self.assertEqual(resp3.status_code, 200)
                data3 = resp3.json()
                self.assertEqual(data3["context_status"], "compacted")
                # When compacted, cache is bypassed
                self.assertFalse(data3["cached"])
                self.assertEqual(data3["cache_type"], "none")
        finally:
            settings.conversation_max_context_messages = old_max_msgs

    async def test_prompt_exceeding_max_context_chars_413(self):
        """Test that sending a prompt exceeding CONVERSATION_MAX_CONTEXT_CHARS returns 413."""
        settings = get_settings()
        old_max_chars = settings.conversation_max_context_chars
        settings.conversation_max_context_chars = 100

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                c_resp = await client.post(
                    "/conversations",
                    headers={"X-Client-Token": self.token_a},
                )
                conv_id = c_resp.json()["id"]

                long_prompt = "A" * 150
                resp = await client.post(
                    "/query",
                    json={"prompt": long_prompt, "conversation_id": conv_id},
                    headers={
                        "X-Client-Token": self.token_a,
                        "Idempotency-Key": "idemp-too-large",
                    },
                )
                self.assertEqual(resp.status_code, 413)
                data = resp.json()
                self.assertIn("exceed maximum context character budget", data["detail"])
        finally:
            settings.conversation_max_context_chars = old_max_chars

    async def test_idempotency_key_payload_conflict_returns_409(self):
        """Reusing the same Idempotency-Key with a different prompt/model payload returns HTTP 409 Conflict."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            c_resp = await client.post(
                "/conversations",
                headers={"X-Client-Token": self.token_a},
            )
            conv_id = c_resp.json()["id"]

            # First turn with key "key-dup-test" and prompt "Original Prompt"
            resp1 = await client.post(
                "/query",
                json={"prompt": "Original Prompt", "conversation_id": conv_id},
                headers={"X-Client-Token": self.token_a, "Idempotency-Key": "key-dup-test"},
            )
            self.assertEqual(resp1.status_code, 200)

            # Reusing same key with DIFFERENT prompt payload
            resp2 = await client.post(
                "/query",
                json={"prompt": "Different Question Entirely", "conversation_id": conv_id},
                headers={"X-Client-Token": self.token_a, "Idempotency-Key": "key-dup-test"},
            )
            self.assertEqual(resp2.status_code, 409)
            data2 = resp2.json()
            self.assertIn("detail", data2)
            self.assertIn("previously used with a different request payload", data2["detail"])

    async def test_expired_pending_turn_crash_recovery(self):
        """Simulates a crashed server leaving a pending turn; verifies lease timeout recovery unblocks conversation."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            c_resp = await client.post(
                "/conversations",
                headers={"X-Client-Token": self.token_a},
            )
            conv_id = c_resp.json()["id"]

            # Artificially insert an expired pending turn in the database (simulating a crash)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO conversation_turns (
                        id, conversation_id, idempotency_key, status, created_at, lease_expires_at
                    ) VALUES (
                        'crashed-turn-id-123', $1, 'crashed-turn-key', 'pending',
                        NOW() - INTERVAL '300 seconds', NOW() - INTERVAL '60 seconds'
                    )
                    """,
                    conv_id,
                )

            # A new query arrives. It should recover the expired pending turn and succeed
            resp = await client.post(
                "/query",
                json={"prompt": "Query after crash", "conversation_id": conv_id},
                headers={"X-Client-Token": self.token_a, "Idempotency-Key": "fresh-turn-after-crash"},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["cached"])

            # Verify the crashed turn in DB was transitioned to failed with LEASE_TIMEOUT_RECOVERED
            async with self.pool.acquire() as conn:
                crashed_status = await conn.fetchrow(
                    "SELECT status, error_code, lease_expires_at FROM conversation_turns WHERE id = 'crashed-turn-id-123'"
                )
                self.assertEqual(crashed_status["status"], "failed")
                self.assertEqual(crashed_status["error_code"], "LEASE_TIMEOUT_RECOVERED")
                self.assertIsNone(crashed_status["lease_expires_at"])

    async def test_history_sequence_continuity_gap_detected_and_bypasses_cache(self):
        """Verifies that sequence gaps or malformed turn pairs flag history as incomplete and bypass cache."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            c_resp = await client.post(
                "/conversations",
                headers={"X-Client-Token": self.token_a},
            )
            conv_id = c_resp.json()["id"]

            # Turn 1
            await client.post(
                "/query",
                json={"prompt": "Q1", "conversation_id": conv_id},
                headers={"X-Client-Token": self.token_a, "Idempotency-Key": "turn-gap-1"},
            )

            # Artificially corrupt sequence number of next completed turn (inject sequence gap: 1, 2, then 5)
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO conversation_turns (id, conversation_id, idempotency_key, status, created_at, completed_at)
                    VALUES ('corrupted-turn', $1, 'corrupted-key', 'completed', NOW(), NOW())
                    """,
                    conv_id,
                )
                # Sequence 5 introduces gap (missing 3 and 4)
                await conn.execute(
                    """
                    INSERT INTO conversation_messages (id, conversation_id, turn_id, sequence_number, role, content, created_at)
                    VALUES ('msg-u2', $1, 'corrupted-turn', 5, 'user', 'Broken seq', NOW())
                    """,
                    conv_id,
                )
                await conn.execute(
                    """
                    INSERT INTO conversation_messages (id, conversation_id, turn_id, sequence_number, role, content, created_at)
                    VALUES ('msg-a2', $1, 'corrupted-turn', 6, 'assistant', 'Broken resp', NOW())
                    """,
                    conv_id,
                )

            # Query should detect sequence gap, mark context incomplete, and bypass cache
            resp = await client.post(
                "/query",
                json={"prompt": "Next Query", "conversation_id": conv_id},
                headers={"X-Client-Token": self.token_a, "Idempotency-Key": "turn-gap-3"},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["context_status"], "incomplete")
            self.assertFalse(data["cached"])
            self.assertEqual(data["cache_type"], "none")

    async def test_legacy_null_request_hash_rejected_on_replay(self):
        """Verifies that idempotency key replay against legacy turns with request_hash=NULL raises conflict."""
        principal = derive_principal_id(self.token_a, "test-conv-secret-key-999")
        conv = await self.repo.create_conversation(None, self.ns, principal, "Legacy Test")

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO conversation_turns (
                    id, conversation_id, idempotency_key, request_hash, status, created_at, completed_at
                ) VALUES (
                    'legacy-turn-1', $1, 'legacy-key-1', NULL, 'completed', NOW(), NOW()
                )
                """,
                conv.id,
            )

        with self.assertRaises(IdempotencyConflictError) as ctx:
            await self.repo.begin_turn(
                conversation_id=conv.id,
                owner_principal_id=principal,
                idempotency_key="legacy-key-1",
                user_content="Any prompt",
                request_hash="new-hash-value",
            )
        self.assertIn("unverified legacy turn", str(ctx.exception))

    async def test_complete_turn_guarded_against_non_pending(self):
        """Verifies that complete_turn raises ActiveTurnConflictError if the turn is no longer pending."""
        principal = derive_principal_id(self.token_a, "test-conv-secret-key-999")
        conv = await self.repo.create_conversation(None, self.ns, principal, "Guarded Completion")

        turn, _, _ = await self.repo.begin_turn(
            conversation_id=conv.id,
            owner_principal_id=principal,
            idempotency_key="guard-key-1",
            user_content="Prompt 1",
            request_hash="hash-1",
        )

        # Transition the turn to failed (simulating lease timeout recovery or abort)
        await self.repo.fail_turn(turn.id, "LEASE_TIMEOUT_RECOVERED")

        # Now attempting to complete the turn should be rejected by the status guard
        with self.assertRaises(ActiveTurnConflictError) as ctx:
            await self.repo.complete_turn(
                turn_id=turn.id,
                assistant_content="Late response arrived after recovery",
            )
        self.assertIn("expected 'pending'", str(ctx.exception))

    async def test_failed_turn_in_history_does_not_break_subsequent_history_and_context(self):
        """
        Verifies that legitimate failed turns with persisted user messages do not break
        history sequence validation or cause subsequent turns to be marked incomplete.
        """
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            c_resp = await client.post(
                "/conversations",
                headers={"X-Client-Token": self.token_a},
            )
            conv_id = c_resp.json()["id"]

            # Turn 1: Completes successfully (seq 1 user, seq 2 assistant)
            resp1 = await client.post(
                "/query",
                json={"prompt": "Unique Failed Turn Test Q1", "conversation_id": conv_id},
                headers={"X-Client-Token": self.token_a, "Idempotency-Key": "turn-fail-flow-1"},
            )
            self.assertEqual(resp1.status_code, 200)

            # Turn 2: Fails during generation (user message seq 3 created, but turn fails)
            principal = derive_principal_id(self.token_a, "test-conv-secret-key-999")
            turn2, _, _ = await self.repo.begin_turn(
                conversation_id=conv_id,
                owner_principal_id=principal,
                idempotency_key="turn-fail-flow-2-fail",
                user_content="Turn 2 question that fails",
                request_hash="hash-turn-2",
            )
            await self.repo.fail_turn(turn2.id, "PROVIDER_OVERLOADED")

            # Turn 3: Completes successfully (seq 4 user, seq 5 assistant)
            resp3 = await client.post(
                "/query",
                json={"prompt": "Unique Failed Turn Test Q3", "conversation_id": conv_id},
                headers={"X-Client-Token": self.token_a, "Idempotency-Key": "turn-fail-flow-3"},
            )
            self.assertEqual(resp3.status_code, 200)
            data3 = resp3.json()
            # Context must be complete despite the failed turn in the ledger
            self.assertEqual(data3["context_status"], "complete")

            # Validate history via repository
            history, is_incomplete = await self.repo.get_validated_completed_history(conv_id, principal)
            self.assertFalse(is_incomplete)
            # Should have exactly 4 messages (Turn 1 user/assistant, Turn 3 user/assistant)
            self.assertEqual(len(history), 4)
            self.assertEqual(history[0].role, "user")
            self.assertEqual(history[0].content, "Unique Failed Turn Test Q1")
            self.assertEqual(history[1].role, "assistant")
            self.assertEqual(history[2].role, "user")
            self.assertEqual(history[2].content, "Unique Failed Turn Test Q3")
            self.assertEqual(history[3].role, "assistant")


if __name__ == "__main__":
    unittest.main()
