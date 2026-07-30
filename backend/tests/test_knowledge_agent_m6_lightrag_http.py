from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.lightrag_retriever import (  # noqa: E402
    LightRAGHttpCandidateProvider,
    LightRAGProviderError,
    lightrag_document_path,
)


class KnowledgeAgentM6LightRAGHttpTests(unittest.TestCase):
    def test_query_data_maps_only_pinned_project_document_paths(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "message": "ok",
                    "data": {
                        "chunks": [
                            {
                                "chunk_id": "external-a",
                                "file_path": lightrag_document_path(
                                    "example.com",
                                    "source:snapshot:0",
                                ),
                                "content": "untrusted and ignored",
                            },
                            {
                                "chunk_id": "external-b",
                                "file_path": lightrag_document_path(
                                    "other.example",
                                    "cross-project",
                                ),
                            },
                            {
                                "chunk_id": "external-c",
                                "file_path": "/unmapped/customer-file.pdf",
                            },
                            {
                                "chunk_id": "external-d",
                                "file_path": lightrag_document_path(
                                    "example.com",
                                    "source:snapshot:2",
                                ),
                            },
                        ]
                    },
                    "metadata": {},
                },
            )

        provider = LightRAGHttpCandidateProvider(
            project_id="example.com",
            base_url="http://lightrag.test/api",
            api_key="do-not-log-this-key",
            transport=httpx.MockTransport(handler),
        )
        try:
            candidates = provider.search(
                project_id="example.com",
                text="product facts",
                limit=3,
            )
        finally:
            provider.close()

        self.assertEqual(
            [candidate.chunk_id for candidate in candidates],
            ["source:snapshot:0", "source:snapshot:2"],
        )
        self.assertEqual(
            [candidate.score for candidate in candidates],
            [1.0, 0.25],
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0].url,
            httpx.URL("http://lightrag.test/api/query/data"),
        )
        self.assertEqual(
            requests[0].headers["X-API-Key"],
            "do-not-log-this-key",
        )
        self.assertEqual(
            json.loads(requests[0].content),
            {
                "query": "product facts",
                "mode": "mix",
                "chunk_top_k": 3,
                "include_references": True,
                "include_chunk_content": False,
            },
        )

    def test_provider_is_project_pinned_before_network_call(self) -> None:
        provider = LightRAGHttpCandidateProvider(
            project_id="example.com",
            base_url="http://lightrag.test",
            transport=httpx.MockTransport(
                lambda _request: self.fail("network must not be called")
            ),
        )
        with self.assertRaisesRegex(LightRAGProviderError, "different project"):
            provider.search(
                project_id="other.example",
                text="product facts",
                limit=5,
            )
        provider.close()

    def test_http_failure_does_not_expose_api_key_or_response_body(self) -> None:
        secret = "sensitive-lightrag-key"
        provider = LightRAGHttpCandidateProvider(
            project_id="example.com",
            base_url="http://lightrag.test",
            api_key=secret,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    500,
                    text=f"provider echoed {secret}",
                )
            ),
        )
        try:
            with self.assertRaises(LightRAGProviderError) as caught:
                provider.search(
                    project_id="example.com",
                    text="product facts",
                    limit=5,
                )
        finally:
            provider.close()
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(
            str(caught.exception),
            "LightRAG query failed with HTTP 500",
        )


if __name__ == "__main__":
    unittest.main()
