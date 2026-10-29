"""
Unit tests for the LLM Provider abstraction, Groq Adapter, and Provider Factory.
All provider interactions are mocked; no real network requests are performed.
"""

import asyncio
from typing import AsyncGenerator, List, Optional
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import groq
import httpx

from app.core.contracts import ChatMessage, GenerationRequest, StreamChunk, UsageMetrics
from app.core.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderRateLimitError,
    ProviderUpstreamError,
)
from app.providers.factory import ProviderFactory
from app.providers.groq_adapter import GroqProvider
from app.providers.interfaces import LLMProvider


class DummyCustomProvider(LLMProvider):
    """Custom provider for factory registration test."""
    async def generate(self, request: GenerationRequest):
        pass

    async def stream(self, request: GenerationRequest) -> AsyncGenerator[StreamChunk, None]:
        if False:
            yield StreamChunk()

    async def close(self) -> None:
        pass


class TestProviderFactory(unittest.TestCase):
    """Tests the ProviderFactory registry and instantiation."""

    def test_default_groq_registration(self):
        cls = ProviderFactory.get_provider_class("groq")
        self.assertIs(cls, GroqProvider)

    def test_custom_provider_registration(self):
        ProviderFactory.register("custom_dummy", DummyCustomProvider)
        provider = ProviderFactory.create("custom_dummy")
        self.assertIsInstance(provider, DummyCustomProvider)

    def test_unknown_provider_raises_configuration_error(self):
        with self.assertRaises(ProviderConfigurationError) as ctx:
            ProviderFactory.create("non_existent_provider")
        self.assertIn("non_existent_provider", str(ctx.exception))


class TestGroqAdapter(unittest.IsolatedAsyncioTestCase):
    """Tests the GroqProvider adapter with mock AsyncGroq."""

    def test_lazy_initialization_no_client_created_on_init(self):
        provider = GroqProvider(api_key="valid-key")
        self.assertIsNone(provider._cached_client)

    def test_missing_api_key_raises_configuration_error(self):
        provider = GroqProvider(api_key="")
        with self.assertRaises(ProviderConfigurationError):
            provider._create_client()

    def test_placeholder_api_key_raises_configuration_error(self):
        provider = GroqProvider(api_key="replace_with_your_key")
        with self.assertRaises(ProviderConfigurationError):
            provider._create_client()

    async def test_successful_generation_and_client_closure(self):
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Groq response text"
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 15
        mock_response.usage.completion_tokens = 25
        mock_response.usage.total_tokens = 40

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_client.close = AsyncMock()

        provider = GroqProvider(
            api_key="test-key",
            client_factory=lambda: mock_client,
        )

        req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="hello")],
            temperature=0.7,
            max_tokens=100,
        )

        result = await provider.generate(req)
        self.assertEqual(result.text, "Groq response text")
        self.assertEqual(result.model, "qwen/qwen3.8-27b")
        self.assertEqual(result.usage.prompt_tokens, 15)
        self.assertEqual(result.usage.completion_tokens, 25)
        self.assertEqual(result.usage.total_tokens, 40)

        # Assert client was closed after generation
        mock_client.close.assert_awaited_once()

    async def test_authentication_error_translation(self):
        mock_client = AsyncMock()
        req_obj = httpx.Request("POST", "https://api.groq.com")
        mock_client.chat.completions.create = AsyncMock(
            side_effect=groq.AuthenticationError(
                message="Invalid API Key",
                response=httpx.Response(status_code=401, request=req_obj),
                body=None,
            )
        )
        mock_client.close = AsyncMock()

        provider = GroqProvider(api_key="test-key", client_factory=lambda: mock_client)
        req = GenerationRequest(model="test", messages=[ChatMessage(role="user", content="hi")])

        with self.assertRaises(ProviderAuthenticationError):
            await provider.generate(req)

        mock_client.close.assert_awaited_once()

    async def test_rate_limit_error_translation(self):
        mock_client = AsyncMock()
        req_obj = httpx.Request("POST", "https://api.groq.com")
        mock_client.chat.completions.create = AsyncMock(
            side_effect=groq.RateLimitError(
                message="Quota exceeded",
                response=httpx.Response(status_code=429, request=req_obj),
                body=None,
            )
        )
        mock_client.close = AsyncMock()

        provider = GroqProvider(api_key="test-key", client_factory=lambda: mock_client)
        req = GenerationRequest(model="test", messages=[ChatMessage(role="user", content="hi")])

        with self.assertRaises(ProviderRateLimitError):
            await provider.generate(req)

        mock_client.close.assert_awaited_once()

    async def test_connection_error_translation(self):
        mock_client = AsyncMock()
        req_obj = httpx.Request("POST", "https://api.groq.com")
        mock_client.chat.completions.create = AsyncMock(
            side_effect=groq.APIConnectionError(request=req_obj, message="Connection refused")
        )
        mock_client.close = AsyncMock()

        provider = GroqProvider(api_key="test-key", client_factory=lambda: mock_client)
        req = GenerationRequest(model="test", messages=[ChatMessage(role="user", content="hi")])

        with self.assertRaises(ProviderConnectionError):
            await provider.generate(req)

        mock_client.close.assert_awaited_once()

    async def test_upstream_status_error_translation(self):
        mock_client = AsyncMock()
        req_obj = httpx.Request("POST", "https://api.groq.com")
        mock_client.chat.completions.create = AsyncMock(
            side_effect=groq.APIStatusError(
                message="Bad Gateway",
                response=httpx.Response(status_code=502, request=req_obj),
                body=None,
            )
        )
        mock_client.close = AsyncMock()

        provider = GroqProvider(api_key="test-key", client_factory=lambda: mock_client)
        req = GenerationRequest(model="test", messages=[ChatMessage(role="user", content="hi")])

        with self.assertRaises(ProviderUpstreamError) as ctx:
            await provider.generate(req)

        self.assertEqual(ctx.exception.status_code, 502)
        mock_client.close.assert_awaited_once()

    async def test_streaming_chunks_and_closure(self):
        async def mock_stream_gen():
            c1 = MagicMock()
            c1.choices = [MagicMock(delta=MagicMock(content="Token 1 "))]
            yield c1
            c2 = MagicMock()
            c2.choices = [MagicMock(delta=MagicMock(content="Token 2"))]
            yield c2

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_stream_gen())
        mock_client.close = AsyncMock()

        provider = GroqProvider(api_key="test-key", client_factory=lambda: mock_client)
        req = GenerationRequest(model="test", messages=[ChatMessage(role="user", content="hi")], stream=True)

        chunks: List[StreamChunk] = []
        async for chunk in provider.stream(req):
            chunks.append(chunk)

        self.assertEqual(len(chunks), 3)  # 2 delta chunks + 1 final completion chunk
        self.assertEqual(chunks[0].delta, "Token 1 ")
        self.assertEqual(chunks[1].delta, "Token 2")
        self.assertTrue(chunks[2].done)
        mock_client.close.assert_awaited_once()
