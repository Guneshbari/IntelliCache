"""
Unit and boundary tests for the IntelliCache Embedding Layer.
Tests the provider-independent embedding contracts, Ollama adapter, prefixes,
error handling, factory, resource cleanup, and architecture boundaries.
Requires no network access to Ollama.
"""

import ast
import json
import math
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.errors import (
    EmbeddingConfigurationError,
    EmbeddingConnectionError,
    EmbeddingError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
)
from app.embeddings.factory import EmbeddingProviderFactory
from app.embeddings.interfaces import EmbeddingProvider
from app.embeddings.ollama_adapter import OllamaEmbeddingProvider


class TestEmbeddingContracts(unittest.TestCase):
    """Tests data validation and contract enforcement in EmbeddingResult."""

    def test_valid_embedding_result(self):
        """Valid vector, model, and dimensions produce an immutable EmbeddingResult."""
        result = EmbeddingResult(
            vector=[0.1, -0.2, 0.35],
            model="nomic-embed-text",
            dimensions=3,
        )
        self.assertEqual(result.model, "nomic-embed-text")
        self.assertEqual(result.dimensions, 3)
        self.assertEqual(result.to_list(), [0.1, -0.2, 0.35])

    def test_empty_vector_rejected(self):
        """Empty vector raises EmbeddingResponseError."""
        with self.assertRaises(EmbeddingResponseError) as ctx:
            EmbeddingResult(vector=[], model="nomic-embed-text", dimensions=0)
        self.assertIn("cannot be empty", str(ctx.exception))

    def test_empty_model_name_rejected(self):
        """Empty model string raises EmbeddingResponseError."""
        with self.assertRaises(EmbeddingResponseError) as ctx:
            EmbeddingResult(vector=[0.1], model="   ", dimensions=1)
        self.assertIn("model identity cannot be empty", str(ctx.exception))

    def test_dimension_mismatch_rejected(self):
        """Declared dimensions not matching vector length raises EmbeddingResponseError."""
        with self.assertRaises(EmbeddingResponseError) as ctx:
            EmbeddingResult(vector=[0.1, 0.2], model="nomic-embed-text", dimensions=3)
        self.assertIn("does not match declared dimensions", str(ctx.exception))

    def test_nan_values_rejected(self):
        """Vector containing NaN raises EmbeddingResponseError."""
        with self.assertRaises(EmbeddingResponseError) as ctx:
            EmbeddingResult(vector=[0.1, float("nan"), 0.3], model="m", dimensions=3)
        self.assertIn("non-finite", str(ctx.exception))

    def test_infinite_values_rejected(self):
        """Vector containing +Inf or -Inf raises EmbeddingResponseError."""
        with self.assertRaises(EmbeddingResponseError) as ctx:
            EmbeddingResult(vector=[0.1, float("inf"), 0.3], model="m", dimensions=3)
        self.assertIn("non-finite", str(ctx.exception))

        with self.assertRaises(EmbeddingResponseError) as ctx:
            EmbeddingResult(vector=[float("-inf"), 0.2, 0.3], model="m", dimensions=3)
        self.assertIn("non-finite", str(ctx.exception))

    def test_boolean_in_vector_rejected(self):
        """Boolean values disguised as numeric types raise EmbeddingResponseError."""
        with self.assertRaises(EmbeddingResponseError):
            EmbeddingResult(vector=[True, 0.2, 0.3], model="m", dimensions=3)

    def test_input_mutation_does_not_mutate_result(self):
        """Mutating the original input list does not mutate the frozen EmbeddingResult vector."""
        input_vec = [0.1, 0.2, 0.3]
        result = EmbeddingResult(
            vector=input_vec,
            model="nomic-embed-text",
            dimensions=3,
        )
        # Mutate the source list
        input_vec[0] = 999.9
        input_vec.append(888.8)

        # Assert result vector remains unchanged and is an immutable tuple
        self.assertEqual(result.vector[0], 0.1)
        self.assertEqual(len(result.vector), 3)
        self.assertIsInstance(result.vector, tuple)
        with self.assertRaises(TypeError):
            result.vector[0] = 5.0  # type: ignore


class TestOllamaEmbeddingProvider(unittest.IsolatedAsyncioTestCase):
    """Tests Ollama adapter with mock HTTP transport."""

    async def test_successful_embedding_generation(self):
        """Generates embedding with correct vector and dimensions from mocked response."""
        expected_vector = [0.01007, -0.00175, 0.05007]

        mock_transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "model": "nomic-embed-text",
                    "embeddings": [expected_vector],
                    "total_duration": 14000,
                },
            )
        )
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://mock-ollama") as client:
            provider = OllamaEmbeddingProvider(
                base_url="http://mock-ollama",
                model="nomic-embed-text",
                timeout_seconds=5.0,
                client=client,
            )
            result = await provider.embed("What is semantic caching?", task=EmbeddingTask.QUERY)

            self.assertEqual(result.model, "nomic-embed-text")
            self.assertEqual(result.dimensions, 3)
            self.assertEqual(result.to_list(), expected_vector)

    async def test_correct_endpoint_and_request_payload(self):
        """Verifies HTTP method is POST, path is /api/embed, and payload structure is correct."""
        captured_request = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_request["method"] = request.method
            captured_request["url"] = str(request.url)
            captured_request["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"model": "nomic-embed-text", "embeddings": [[0.1, 0.2]]},
            )

        mock_transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:11434") as client:
            provider = OllamaEmbeddingProvider(
                base_url="http://127.0.0.1:11434",
                model="nomic-embed-text",
                client=client,
            )
            await provider.embed("Sample query text", task=EmbeddingTask.QUERY)

            self.assertEqual(captured_request["method"], "POST")
            self.assertEqual(captured_request["url"], "http://127.0.0.1:11434/api/embed")
            self.assertEqual(captured_request["body"]["model"], "nomic-embed-text")
            self.assertEqual(captured_request["body"]["input"], "search_query: Sample query text")

    async def test_task_prefixes_for_nomic_models(self):
        """Verifies task prefixes: search_query: for query and search_document: for document."""
        queries_sent = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            queries_sent.append(body["input"])
            return httpx.Response(
                200,
                json={"model": "nomic-embed-text-v1.5", "embeddings": [[0.5, -0.5]]},
            )

        mock_transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:11434") as client:
            provider = OllamaEmbeddingProvider(
                base_url="http://127.0.0.1:11434",
                model="nomic-embed-text-v1.5",
                client=client,
            )

            # 1. Query task
            await provider.embed("User question", task=EmbeddingTask.QUERY)
            self.assertEqual(queries_sent[-1], "search_query: User question")

            # String task "query"
            await provider.embed("User question 2", task="query")
            self.assertEqual(queries_sent[-1], "search_query: User question 2")

            # 2. Document task
            await provider.embed("Documentation text", task=EmbeddingTask.DOCUMENT)
            self.assertEqual(queries_sent[-1], "search_document: Documentation text")

            # String task "document"
            await provider.embed("Documentation text 2", task="document")
            self.assertEqual(queries_sent[-1], "search_document: Documentation text 2")

    async def test_non_nomic_model_no_prefix(self):
        """Models without 'nomic' in their name do not receive search_query/search_document prefixes."""
        inputs_sent = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            inputs_sent.append(body["input"])
            return httpx.Response(
                200,
                json={"model": "all-minilm", "embeddings": [[0.1, 0.2]]},
            )

        mock_transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:11434") as client:
            provider = OllamaEmbeddingProvider(
                model="all-minilm",
                client=client,
            )
            await provider.embed("Pure text", task="query")
            self.assertEqual(inputs_sent[-1], "Pure text")

    async def test_empty_or_blank_input_rejected(self):
        """Empty input text raises EmbeddingResponseError before making network calls."""
        provider = OllamaEmbeddingProvider()
        with self.assertRaises(EmbeddingResponseError) as ctx:
            await provider.embed("")
        self.assertIn("cannot be empty or blank", str(ctx.exception))

        with self.assertRaises(EmbeddingResponseError) as ctx:
            await provider.embed("    \n\t  ")
        self.assertIn("cannot be empty or blank", str(ctx.exception))

    async def test_invalid_task_rejected(self):
        """Invalid task string raises EmbeddingResponseError."""
        provider = OllamaEmbeddingProvider()
        with self.assertRaises(EmbeddingResponseError) as ctx:
            await provider.embed("Valid text", task="unsupported_task")
        self.assertIn("Invalid embedding task", str(ctx.exception))

    async def test_empty_or_missing_embeddings_response(self):
        """Missing or empty embeddings field raises EmbeddingResponseError."""
        # Missing embeddings key
        mock_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "m"}))
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://test") as client:
            provider = OllamaEmbeddingProvider(client=client)
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed("Hello")
            self.assertIn("missing 'embeddings' array", str(ctx.exception))

        # Empty embeddings array
        mock_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"model": "m", "embeddings": []}))
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://test") as client:
            provider = OllamaEmbeddingProvider(client=client)
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed("Hello")
            self.assertIn("missing 'embeddings' array or array is empty", str(ctx.exception))

    async def test_multiple_vectors_returned_rejected(self):
        """Receiving multiple vectors for a single-text request raises EmbeddingResponseError."""
        mock_transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"model": "m", "embeddings": [[0.1, 0.2], [0.3, 0.4]]})
        )
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://test") as client:
            provider = OllamaEmbeddingProvider(client=client)
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed("Hello")
            self.assertIn("Expected exactly 1 vector", str(ctx.exception))

    async def test_malformed_json_response(self):
        """Non-JSON response raises EmbeddingResponseError."""
        mock_transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<!DOCTYPE html>Gateway Error"))
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://test") as client:
            provider = OllamaEmbeddingProvider(client=client)
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed("Hello")
            self.assertIn("Malformed JSON response", str(ctx.exception))

    async def test_http_error_responses(self):
        """HTTP 404 and 500 responses raise EmbeddingResponseError without fabricating embeddings."""
        mock_transport_404 = httpx.MockTransport(
            lambda req: httpx.Response(404, json={"error": "model 'nomic-embed-text' not found"})
        )
        async with httpx.AsyncClient(transport=mock_transport_404, base_url="http://test") as client:
            provider = OllamaEmbeddingProvider(client=client)
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed("Hello")
            self.assertIn("HTTP 404", str(ctx.exception))

        mock_transport_500 = httpx.MockTransport(
            lambda req: httpx.Response(500, text="Internal Ollama Server Error")
        )
        async with httpx.AsyncClient(transport=mock_transport_500, base_url="http://test") as client:
            provider = OllamaEmbeddingProvider(client=client)
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed("Hello")
            self.assertIn("HTTP 500", str(ctx.exception))

    async def test_connection_failure(self):
        """Connection failure raises EmbeddingConnectionError."""
        def raise_connect_error(req):
            raise httpx.ConnectError("Failed to establish a new connection: [Errno 111] Connection refused")

        mock_transport = httpx.MockTransport(raise_connect_error)
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:11434") as client:
            provider = OllamaEmbeddingProvider(client=client)
            with self.assertRaises(EmbeddingConnectionError) as ctx:
                await provider.embed("Hello")
            self.assertIn("Failed to connect to Ollama", str(ctx.exception))

    async def test_request_timeout(self):
        """Timeout raises EmbeddingTimeoutError."""
        def raise_timeout(req):
            raise httpx.ReadTimeout("Request read timed out after 5.0 seconds")

        mock_transport = httpx.MockTransport(raise_timeout)
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:11434") as client:
            provider = OllamaEmbeddingProvider(timeout_seconds=5.0, client=client)
            with self.assertRaises(EmbeddingTimeoutError) as ctx:
                await provider.embed("Hello")
            self.assertIn("timed out after 5.0s", str(ctx.exception))

    async def test_http_resource_cleanup(self):
        """Calling close() closes the underlying HTTP client cleanly."""
        provider = OllamaEmbeddingProvider(base_url="http://127.0.0.1:11434")
        client = provider._get_client()
        self.assertFalse(client.is_closed)

        await provider.close()
        self.assertTrue(client.is_closed)
        self.assertIsNone(provider._client)


class TestEmbeddingProviderFactory(unittest.TestCase):
    """Tests factory registration and provider creation."""

    def test_create_ollama_provider(self):
        """Factory successfully constructs OllamaEmbeddingProvider with given parameters."""
        provider = EmbeddingProviderFactory.create(
            "ollama",
            base_url="http://localhost:11434",
            model="nomic-embed-text",
            timeout_seconds=15.0,
        )
        self.assertIsInstance(provider, OllamaEmbeddingProvider)
        self.assertEqual(provider.base_url, "http://localhost:11434")
        self.assertEqual(provider.model, "nomic-embed-text")
        self.assertEqual(provider.timeout_seconds, 15.0)

    def test_create_fastembed_provider(self):
        """Factory creates FastEmbedProvider accepting either provider_name or name kwargs."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider

        p1 = EmbeddingProviderFactory.create(
            provider_name="fastembed",
            model="BAAI/bge-small-en-v1.5",
            dimensions=384,
        )
        self.assertIsInstance(p1, FastEmbedProvider)
        self.assertEqual(p1.model, "BAAI/bge-small-en-v1.5")
        self.assertEqual(p1.dimensions, 384)

        p2 = EmbeddingProviderFactory.create(
            name="fastembed",
        )
        self.assertIsInstance(p2, FastEmbedProvider)
        self.assertEqual(p2.model, "BAAI/bge-small-en-v1.5")
        self.assertEqual(p2.dimensions, 384)

    def test_unsupported_provider_raises_configuration_error(self):
        """Unsupported provider name raises EmbeddingConfigurationError with supported options."""
        with self.assertRaises(EmbeddingConfigurationError) as ctx:
            EmbeddingProviderFactory.create("openai-embeddings")
        self.assertIn("Unsupported embedding provider 'openai-embeddings'", str(ctx.exception))
        self.assertIn("ollama", str(ctx.exception))
        self.assertIn("fastembed", str(ctx.exception))


class TestFastEmbedProviderMocked(unittest.IsolatedAsyncioTestCase):
    """Unit tests for FastEmbedProvider with mocked TextEmbedding (no network/weight downloads)."""

    async def test_embed_query_routing(self):
        """EmbeddingTask.QUERY routes to model.query_embed."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider
        from unittest.mock import MagicMock

        provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5", dimensions=4)
        mock_model = MagicMock()
        mock_model.query_embed.return_value = [[0.1, 0.2, 0.3, 0.4]]

        with patch.object(provider, "_get_model", return_value=mock_model):
            res = await provider.embed("What is Rust?", task=EmbeddingTask.QUERY)
            self.assertEqual(res.dimensions, 4)
            self.assertEqual(res.to_list(), [0.1, 0.2, 0.3, 0.4])
            mock_model.query_embed.assert_called_once_with("What is Rust?")
            mock_model.passage_embed.assert_not_called()
        await provider.close()

    async def test_embed_document_routing(self):
        """EmbeddingTask.DOCUMENT routes to model.passage_embed."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider
        from unittest.mock import MagicMock

        provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5", dimensions=4)
        mock_model = MagicMock()
        mock_model.passage_embed.return_value = [[0.5, 0.6, 0.7, 0.8]]

        with patch.object(provider, "_get_model", return_value=mock_model):
            res = await provider.embed("Documentation about Rust", task=EmbeddingTask.DOCUMENT)
            self.assertEqual(res.dimensions, 4)
            self.assertEqual(res.to_list(), [0.5, 0.6, 0.7, 0.8])
            mock_model.passage_embed.assert_called_once_with(["Documentation about Rust"])
            mock_model.query_embed.assert_not_called()
        await provider.close()

    async def test_dimension_validation_failure(self):
        """When vector length does not match declared dimensions, raises EmbeddingResponseError."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider
        from app.embeddings.errors import EmbeddingResponseError
        from unittest.mock import MagicMock

        provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5", dimensions=384)
        mock_model = MagicMock()
        # Model returns 4 dimensions while 384 declared
        mock_model.query_embed.return_value = [[0.1, 0.2, 0.3, 0.4]]

        with patch.object(provider, "_get_model", return_value=mock_model):
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed("Test", task=EmbeddingTask.QUERY)
            self.assertIn("expected 384", str(ctx.exception))
        await provider.close()

    async def test_uninstalled_fastembed_raises_configuration_error(self):
        """When fastembed package is not installed, clear EmbeddingConfigurationError is raised."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider
        import builtins

        provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5")
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "fastembed":
                raise ImportError("No module named 'fastembed'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(EmbeddingConfigurationError) as ctx:
                provider._get_model()
            self.assertIn("FastEmbed is not installed", str(ctx.exception))
            self.assertIn("requirements-fastembed.txt", str(ctx.exception))


class TestEmbeddingConfiguration(unittest.TestCase):
    """Tests configuration schema validation for embedding settings."""

    def test_default_embedding_settings(self):
        """Verify secure and sensible defaults for embedding configuration."""
        settings = Settings()
        self.assertEqual(settings.embedding_provider, "ollama")
        self.assertEqual(settings.embedding_model, "nomic-embed-text")
        self.assertEqual(settings.embedding_base_url, "http://127.0.0.1:11434")
        self.assertEqual(settings.embedding_timeout_seconds, 30.0)

    @patch.dict(os.environ, {
        "EMBEDDING_PROVIDER": "ollama",
        "EMBEDDING_MODEL": "custom-embed-model",
        "EMBEDDING_BASE_URL": "http://10.0.0.5:11434/",
        "EMBEDDING_TIMEOUT_SECONDS": "45.5",
    }, clear=False)
    def test_embedding_env_overrides(self):
        """Verify environment variables correctly override embedding defaults."""
        settings = Settings()
        self.assertEqual(settings.embedding_provider, "ollama")
        self.assertEqual(settings.embedding_model, "custom-embed-model")
        self.assertEqual(settings.embedding_base_url, "http://10.0.0.5:11434")
        self.assertEqual(settings.embedding_timeout_seconds, 45.5)

    def test_negative_timeout_rejected(self):
        """Non-positive timeout values are rejected by validation."""
        with patch.dict(os.environ, {"EMBEDDING_TIMEOUT_SECONDS": "-5.0"}, clear=False):
            with self.assertRaises(ValueError) as ctx:
                Settings()
            self.assertIn("EMBEDDING_TIMEOUT_SECONDS must be positive", str(ctx.exception))

        with patch.dict(os.environ, {"EMBEDDING_TIMEOUT_SECONDS": "0"}, clear=False):
            with self.assertRaises(ValueError) as ctx:
                Settings()
            self.assertIn("EMBEDDING_TIMEOUT_SECONDS must be positive", str(ctx.exception))

    def test_invalid_base_url_rejected(self):
        """Base URLs missing http:// or https:// are rejected by validation."""
        with patch.dict(os.environ, {"EMBEDDING_BASE_URL": "ftp://localhost:11434"}, clear=False):
            with self.assertRaises(ValueError) as ctx:
                Settings()
            self.assertIn("EMBEDDING_BASE_URL must be a valid HTTP or HTTPS URL", str(ctx.exception))


class TestEmbeddingArchitectureBoundaries(unittest.TestCase):
    """Verifies that gateway/app/embeddings remains independent of FastAPI, Groq SDK, and DB."""

    def test_embeddings_ast_imports_have_no_forbidden_dependencies(self):
        embeddings_dir = Path(__file__).resolve().parent.parent / "app" / "embeddings"
        forbidden_roots = {"fastapi", "starlette", "groq", "asyncpg", "postgres"}

        for py_file in embeddings_dir.glob("*.py"):
            with open(py_file, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=str(py_file))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root_mod = alias.name.split(".")[0]
                        self.assertNotIn(
                            root_mod,
                            forbidden_roots,
                            f"Module {py_file.name} directly imports forbidden dependency '{root_mod}'",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        root_mod = node.module.split(".")[0]
                        self.assertNotIn(
                            root_mod,
                            forbidden_roots,
                            f"Module {py_file.name} imports from forbidden dependency '{root_mod}'",
                        )


class TestEmbeddingProviderBaseBatch(unittest.IsolatedAsyncioTestCase):
    """Tests default sequential embed_batch implementation on Base EmbeddingProvider."""

    class SimpleMockProvider(EmbeddingProvider):
        def __init__(self):
            self.embed_calls = []

        async def embed(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> EmbeddingResult:
            self.embed_calls.append((text, task))
            if text == "fail_me":
                raise EmbeddingResponseError("Simulated failure")
            return EmbeddingResult(vector=[0.1, 0.2], model="simple-model", dimensions=2)

        async def close(self) -> None:
            pass

    async def test_embed_batch_empty_input(self):
        """Empty input list returns an empty list immediately without calling embed."""
        provider = self.SimpleMockProvider()
        res = await provider.embed_batch([])
        self.assertEqual(res, [])
        self.assertEqual(len(provider.embed_calls), 0)

    async def test_embed_batch_sequential_delegation_and_ordering(self):
        """Default embed_batch calls embed() sequentially and preserves order."""
        provider = self.SimpleMockProvider()
        inputs = ["first", "second", "third"]
        res = await provider.embed_batch(inputs, task=EmbeddingTask.DOCUMENT)
        self.assertEqual(len(res), 3)
        self.assertEqual(len(provider.embed_calls), 3)
        self.assertEqual(provider.embed_calls[0], ("first", EmbeddingTask.DOCUMENT))
        self.assertEqual(provider.embed_calls[1], ("second", EmbeddingTask.DOCUMENT))
        self.assertEqual(provider.embed_calls[2], ("third", EmbeddingTask.DOCUMENT))
        for r in res:
            self.assertEqual(r.dimensions, 2)
            self.assertEqual(r.model, "simple-model")

    async def test_embed_batch_error_propagation(self):
        """Errors in embed() during sequential batch are propagated cleanly."""
        provider = self.SimpleMockProvider()
        with self.assertRaises(EmbeddingResponseError) as ctx:
            await provider.embed_batch(["ok", "fail_me", "after"])
        self.assertIn("Simulated failure", str(ctx.exception))


class TestFastEmbedProviderBatch(unittest.IsolatedAsyncioTestCase):
    """Tests FastEmbedProvider native batch execution."""

    async def test_fastembed_batch_empty_input(self):
        """Empty input sequence returns empty list immediately."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider

        provider = FastEmbedProvider(model="custom-test", dimensions=2)
        res = await provider.embed_batch([])
        self.assertEqual(res, [])
        await provider.close()

    async def test_fastembed_batch_empty_text_rejected(self):
        """Batch containing empty or whitespace-only text raises EmbeddingResponseError."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider

        provider = FastEmbedProvider(model="custom-test", dimensions=2)
        with self.assertRaises(EmbeddingResponseError) as ctx:
            await provider.embed_batch(["valid", "   ", "another"])
        self.assertIn("Input text at index 1 in batch cannot be empty or blank", str(ctx.exception))
        await provider.close()

    async def test_fastembed_batch_query_routing_and_ordering(self):
        """Batch query task routes to model.query_embed and preserves ordering."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider
        from unittest.mock import MagicMock

        provider = FastEmbedProvider(model="custom-test", dimensions=3)
        mock_model = MagicMock()
        mock_model.query_embed.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]

        with patch.object(provider, "_get_model", return_value=mock_model):
            res = await provider.embed_batch(["query 1", "query 2"], task=EmbeddingTask.QUERY)
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0].to_list(), [0.1, 0.2, 0.3])
            self.assertEqual(res[1].to_list(), [0.4, 0.5, 0.6])
            mock_model.query_embed.assert_called_once_with(["query 1", "query 2"])
            mock_model.passage_embed.assert_not_called()
        await provider.close()

    async def test_fastembed_batch_passage_routing(self):
        """Batch document task routes to model.passage_embed."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider
        from unittest.mock import MagicMock

        provider = FastEmbedProvider(model="custom-test", dimensions=2)
        mock_model = MagicMock()
        mock_model.passage_embed.return_value = [
            [0.7, 0.8],
            [0.9, 1.0],
        ]

        with patch.object(provider, "_get_model", return_value=mock_model):
            res = await provider.embed_batch(["doc 1", "doc 2"], task=EmbeddingTask.DOCUMENT)
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0].to_list(), [0.7, 0.8])
            self.assertEqual(res[1].to_list(), [0.9, 1.0])
            mock_model.passage_embed.assert_called_once_with(["doc 1", "doc 2"])
            mock_model.query_embed.assert_not_called()
        await provider.close()

    async def test_fastembed_batch_cardinality_mismatch(self):
        """When FastEmbed returns unexpected number of vectors, raises EmbeddingResponseError."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider
        from unittest.mock import MagicMock

        provider = FastEmbedProvider(model="custom-test", dimensions=2)
        mock_model = MagicMock()
        # Returns 1 vector for 2 inputs
        mock_model.query_embed.return_value = [[0.1, 0.2]]

        with patch.object(provider, "_get_model", return_value=mock_model):
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed_batch(["q1", "q2"])
            self.assertIn("vectors for chunk of 2 inputs", str(ctx.exception))
        await provider.close()

    async def test_fastembed_batch_chunking(self):
        """Batches larger than chunk_size (64) are processed in chunks and stitched in order."""
        from app.embeddings.fastembed_adapter import FastEmbedProvider
        from unittest.mock import MagicMock

        provider = FastEmbedProvider(model="custom-test", dimensions=2)
        mock_model = MagicMock()

        # 70 inputs: chunk 1 has 64, chunk 2 has 6
        def fake_query_embed(chunk):
            return [[float(i), float(i)] for i in range(len(chunk))]

        mock_model.query_embed.side_effect = fake_query_embed

        inputs = [f"text_{i}" for i in range(70)]
        with patch.object(provider, "_get_model", return_value=mock_model):
            res = await provider.embed_batch(inputs)
            self.assertEqual(len(res), 70)
            self.assertEqual(mock_model.query_embed.call_count, 2)
            self.assertEqual(len(mock_model.query_embed.call_args_list[0][0][0]), 64)
            self.assertEqual(len(mock_model.query_embed.call_args_list[1][0][0]), 6)
        await provider.close()


class TestOllamaEmbeddingProviderBatch(unittest.IsolatedAsyncioTestCase):
    """Tests OllamaEmbeddingProvider native batch execution."""

    async def test_ollama_batch_empty_input(self):
        """Empty input sequence returns empty list immediately."""
        provider = OllamaEmbeddingProvider(model="nomic-embed-text")
        res = await provider.embed_batch([])
        self.assertEqual(res, [])
        await provider.close()

    async def test_ollama_batch_request_payload_and_ordering(self):
        """Batch embed sends formatted inputs in single request and preserves order."""
        captured_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payload["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "model": "nomic-embed-text",
                    "embeddings": [
                        [0.11, 0.22],
                        [0.33, 0.44],
                    ],
                },
            )

        mock_transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:11434") as client:
            provider = OllamaEmbeddingProvider(
                model="nomic-embed-text",
                client=client,
            )
            res = await provider.embed_batch(["apple", "banana"], task=EmbeddingTask.QUERY)
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0].to_list(), [0.11, 0.22])
            self.assertEqual(res[1].to_list(), [0.33, 0.44])

            # Check Nomic query prefix was applied to both items
            self.assertEqual(
                captured_payload["body"]["input"],
                ["search_query: apple", "search_query: banana"],
            )
            await provider.close()

    async def test_ollama_batch_document_prefix(self):
        """Batch document task applies search_document: prefix to every input."""
        captured_payload = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payload["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "model": "nomic-embed-text",
                    "embeddings": [[0.1, 0.2]],
                },
            )

        mock_transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:11434") as client:
            provider = OllamaEmbeddingProvider(
                model="nomic-embed-text",
                client=client,
            )
            res = await provider.embed_batch(["my doc"], task=EmbeddingTask.DOCUMENT)
            self.assertEqual(len(res), 1)
            self.assertEqual(
                captured_payload["body"]["input"],
                ["search_document: my doc"],
            )
            await provider.close()

    async def test_ollama_batch_cardinality_mismatch(self):
        """When Ollama returns fewer vectors than requested in batch, raises EmbeddingResponseError."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "nomic-embed-text",
                    "embeddings": [[0.1, 0.2]],  # only 1 vector for 2 inputs
                },
            )

        mock_transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:11434") as client:
            provider = OllamaEmbeddingProvider(
                model="nomic-embed-text",
                client=client,
            )
            with self.assertRaises(EmbeddingResponseError) as ctx:
                await provider.embed_batch(["in1", "in2"])
            self.assertIn("Expected exactly 2 vectors", str(ctx.exception))
            await provider.close()


class TestEmbeddingDimensionValidation(unittest.TestCase):
    """Tests dimension conflict rejection in Settings and EmbeddingProviderFactory."""

    def test_factory_rejects_conflicting_dimensions_for_fastembed(self):
        """EmbeddingProviderFactory rejects explicit dimensions conflicting with FastEmbed BGE-small."""
        with self.assertRaises(EmbeddingConfigurationError) as ctx:
            EmbeddingProviderFactory.create(provider_name="fastembed", dimensions=768)
        self.assertIn("requires 384 dimensions", str(ctx.exception))

    def test_factory_rejects_conflicting_dimensions_for_ollama_nomic(self):
        """EmbeddingProviderFactory rejects explicit dimensions conflicting with Ollama nomic."""
        with self.assertRaises(EmbeddingConfigurationError) as ctx:
            EmbeddingProviderFactory.create(provider_name="ollama", dimensions=384)
        self.assertIn("requires 768 dimensions", str(ctx.exception))

    def test_factory_resolves_coherent_defaults(self):
        """EmbeddingProviderFactory resolves provider-aware defaults when dimensions are omitted."""
        fe_p = EmbeddingProviderFactory.create(provider_name="fastembed")
        self.assertEqual(fe_p.dimensions, 384)
        self.assertEqual(fe_p.model_name, "BAAI/bge-small-en-v1.5")

        ol_p = EmbeddingProviderFactory.create(provider_name="ollama")
        self.assertEqual(ol_p.dimensions, 768)
        self.assertEqual(ol_p.model_name, "nomic-embed-text")

    def test_settings_rejects_conflicting_dimensions_for_fastembed(self):
        """Settings schema rejects explicit VECTOR_DIMENSIONS conflicting with FastEmbed."""
        with self.assertRaises(ValueError) as ctx:
            Settings(
                embedding_provider="fastembed",
                vector_dimensions=768,
            )
        self.assertIn("conflicts with FastEmbed model", str(ctx.exception))

    def test_settings_rejects_conflicting_dimensions_for_ollama(self):
        """Settings schema rejects explicit VECTOR_DIMENSIONS conflicting with Ollama nomic."""
        with self.assertRaises(ValueError) as ctx:
            Settings(
                embedding_provider="ollama",
                vector_dimensions=384,
            )
        self.assertIn("conflicts with Ollama model", str(ctx.exception))

    def test_settings_resolves_authoritative_defaults(self):
        """Settings resolves single authoritative dimension for FastEmbed and Ollama."""
        s_fe = Settings(embedding_provider="fastembed")
        self.assertEqual(s_fe.vector_dimensions, 384)
        self.assertEqual(s_fe.embedding_model, "BAAI/bge-small-en-v1.5")

        s_ol = Settings(embedding_provider="ollama")
        self.assertEqual(s_ol.vector_dimensions, 768)
        self.assertEqual(s_ol.embedding_model, "nomic-embed-text")


if __name__ == "__main__":
    unittest.main()
