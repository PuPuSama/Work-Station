from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent import (  # noqa: E402
    EMBEDDING_DIMENSIONS,
    EmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingResponseError,
    OpenAICompatibleEmbeddingProvider,
)
from knowledge_agent.embedding import parse_embeddings_response  # noqa: E402


def vector(first_value: float) -> list[float]:
    return [first_value] + [0.0] * (EMBEDDING_DIMENSIONS - 1)


class OpenAICompatibleEmbeddingProviderTests(unittest.TestCase):
    def test_sends_dedicated_authenticated_request_and_restores_input_order(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("Authorization")
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": 1, "embedding": vector(2.0)},
                        {"object": "embedding", "index": 0, "embedding": vector(1.0)},
                    ],
                    "model": "text-embedding-3-small",
                    "usage": {"prompt_tokens": 4, "total_tokens": 4},
                },
            )

        with OpenAICompatibleEmbeddingProvider(
            base_url="https://gateway.example/v1/",
            api_key="unit-test-key",
            model_id="text-embedding-3-small",
            transport=httpx.MockTransport(handler),
        ) as provider:
            result = provider.embed(["first", "second"])

        self.assertIsInstance(provider, EmbeddingProvider)
        self.assertEqual(captured["url"], "https://gateway.example/v1/embeddings")
        self.assertEqual(captured["authorization"], "Bearer unit-test-key")
        self.assertEqual(
            captured["payload"],
            {
                "model": "text-embedding-3-small",
                "input": ["first", "second"],
                "encoding_format": "float",
                "dimensions": EMBEDDING_DIMENSIONS,
            },
        )
        self.assertEqual(result.count, 2)
        self.assertEqual(result.model_id, "text-embedding-3-small")
        self.assertEqual(result.dimensions, EMBEDDING_DIMENSIONS)
        self.assertEqual(result.vectors[0][0], 1.0)
        self.assertEqual(result.vectors[1][0], 2.0)
        self.assertEqual(result.prompt_tokens, 4)

    def test_accepts_an_injected_httpx_client(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": vector(1.0)}],
                    "model": "text-embedding-3-small",
                },
            )
        )
        with httpx.Client(transport=transport) as client:
            provider = OpenAICompatibleEmbeddingProvider(
                base_url="https://gateway.example/v1",
                api_key="unit-test-key",
                model_id="text-embedding-3-small",
                client=client,
            )
            self.assertEqual(provider.embed(["text"]).count, 1)
            provider.close()
            self.assertFalse(client.is_closed)

    def test_rejects_empty_or_blank_input_before_network_access(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://gateway.example/v1",
            api_key="unit-test-key",
            model_id="text-embedding-3-small",
            transport=httpx.MockTransport(handler),
        )
        self.addCleanup(provider.close)

        for texts in ([], ["valid", " "]):
            with self.subTest(texts=texts):
                with self.assertRaises(ValueError):
                    provider.embed(texts)
        self.assertEqual(calls, 0)

    def test_rejects_count_index_dimension_nonfinite_and_zero_vector_failures(
        self,
    ) -> None:
        valid = vector(1.0)
        cases = {
            "count": {"data": [], "model": "text-embedding-3-small"},
            "duplicate-index": {
                "data": [
                    {"index": 0, "embedding": valid},
                    {"index": 0, "embedding": valid},
                ],
                "model": "text-embedding-3-small",
            },
            "wrong-dimension": {
                "data": [{"index": 0, "embedding": [1.0]}],
                "model": "text-embedding-3-small",
            },
            "nan": {
                "data": [{"index": 0, "embedding": vector(float("nan"))}],
                "model": "text-embedding-3-small",
            },
            "infinity": {
                "data": [{"index": 0, "embedding": vector(float("inf"))}],
                "model": "text-embedding-3-small",
            },
            "zero": {
                "data": [
                    {
                        "index": 0,
                        "embedding": [0.0] * EMBEDDING_DIMENSIONS,
                    }
                ],
                "model": "text-embedding-3-small",
            },
        }

        for name, payload in cases.items():
            expected_count = 2 if name == "duplicate-index" else 1
            with self.subTest(name=name):
                with self.assertRaises(EmbeddingResponseError):
                    parse_embeddings_response(
                        payload,
                        expected_count=expected_count,
                        model_id="text-embedding-3-small",
                    )

    def test_rejects_missing_or_mismatched_response_model(self) -> None:
        for payload in (
            {"data": [{"index": 0, "embedding": vector(1.0)}]},
            {
                "data": [{"index": 0, "embedding": vector(1.0)}],
                "model": "different-embedding-model",
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    EmbeddingResponseError,
                    "model does not match",
                ):
                    parse_embeddings_response(
                        payload,
                        expected_count=1,
                        model_id="text-embedding-3-small",
                    )

    def test_http_failure_does_not_expose_the_api_key_or_response_body(self) -> None:
        api_key = "super-sensitive-unit-test-key"
        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://gateway.example/v1",
            api_key=api_key,
            model_id="text-embedding-3-small",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    401,
                    json={"error": f"invalid credential {api_key}"},
                )
            ),
        )
        self.addCleanup(provider.close)

        with self.assertRaises(EmbeddingProviderError) as raised:
            provider.embed(["text"])

        message = str(raised.exception)
        self.assertNotIn(api_key, message)
        self.assertNotIn("invalid credential", message)
        self.assertEqual(message, "embedding request failed with HTTP 401")

    def test_request_failure_does_not_expose_authorization_headers(self) -> None:
        api_key = "super-sensitive-unit-test-key"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                f"request failed with {request.headers['Authorization']}",
                request=request,
            )

        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://gateway.example/v1",
            api_key=api_key,
            model_id="text-embedding-3-small",
            transport=httpx.MockTransport(handler),
        )
        self.addCleanup(provider.close)

        with self.assertRaises(EmbeddingProviderError) as raised:
            provider.embed(["text"])

        self.assertNotIn(api_key, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
