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

    def test_unsupported_provider_raises_configuration_error(self):
        """Unsupported provider name raises EmbeddingConfigurationError with supported options."""
        with self.assertRaises(EmbeddingConfigurationError) as ctx:
            EmbeddingProviderFactory.create("openai-embeddings")
        self.assertIn("Unsupported embedding provider 'openai-embeddings'", str(ctx.exception))
        self.assertIn("ollama", str(ctx.exception))


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


if __name__ == "__main__":
    unittest.main()
