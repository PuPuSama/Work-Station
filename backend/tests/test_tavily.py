from __future__ import annotations

import io
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.tavily import (  # noqa: E402
    TavilyClient,
    TavilyConfigurationError,
    TavilyHTTPError,
    TavilyResponseError,
    TavilyTimeoutError,
    TavilyTransportError,
)


class StubResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class RecordingOpener:
    def __init__(self, response: StubResponse | None = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[object, float]] = []

    def __call__(self, req: object, timeout: float) -> StubResponse:
        self.calls.append((req, timeout))
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("test opener has no response")
        return self.response


def client_with_key(opener: RecordingOpener, *, timeout: float = 12) -> TavilyClient:
    with patch("services.tavily.os.environ.get", return_value="unit-test-token"):
        return TavilyClient(opener=opener, timeout=timeout)


class TavilyClientTests(unittest.TestCase):
    def test_search_is_host_limited_and_uses_fixed_low_cost_options(self) -> None:
        opener = RecordingOpener(
            StubResponse(
                {
                    "query": "injection moulding",
                    "results": [
                        {
                            "title": "Product A",
                            "url": "https://shop.example.com/products/a",
                            "content": "A short result.",
                            "score": "0.91",
                        }
                    ],
                    "response_time": "1.25",
                    "request_id": "request-search",
                }
            )
        )
        client = client_with_key(opener)

        response = client.search(
            "  injection moulding  ",
            "https://Shop.Example.com/products/",
            max_results=7,
        )

        self.assertTrue(client.ready)
        self.assertEqual(response.query, "injection moulding")
        self.assertEqual(response.results[0].title, "Product A")
        self.assertEqual(response.results[0].score, 0.91)
        self.assertEqual(response.to_dict()["results"][0]["url"], "https://shop.example.com/products/a")

        req, timeout = opener.calls[0]
        self.assertEqual(req.full_url, "https://api.tavily.com/search")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer unit-test-token")
        self.assertEqual(timeout, 12)
        self.assertEqual(
            json.loads(req.data.decode("utf-8")),
            {
                "query": "injection moulding",
                "include_domains": ["shop.example.com"],
                "max_results": 7,
                "search_depth": "basic",
                "include_answer": False,
                "include_images": False,
            },
        )

    def test_extract_supports_advanced_depth_images_and_normalizes_failures(self) -> None:
        opener = RecordingOpener(
            StubResponse(
                {
                    "results": [
                        {
                            "url": "https://example.com/a",
                            "raw_content": "# Product A",
                            "images": [
                                "https://example.com/a.jpg",
                                {"url": "https://example.com/b.jpg", "description": "B"},
                            ],
                        }
                    ],
                    "failed_results": [
                        {"url": "https://example.com/b", "error": "Could not extract"}
                    ],
                    "response_time": 2.5,
                    "request_id": "request-extract",
                }
            )
        )
        client = client_with_key(opener)

        response = client.extract(
            ["https://example.com/a", "https://example.com/b"],
            extract_depth="ADVANCED",
        )

        self.assertEqual(
            response.results[0].images,
            ("https://example.com/a.jpg", "https://example.com/b.jpg"),
        )
        self.assertEqual(response.failed_results[0].error, "Could not extract")
        self.assertEqual(response.to_dict()["results"][0]["raw_content"], "# Product A")

        req, _ = opener.calls[0]
        self.assertEqual(req.full_url, "https://api.tavily.com/extract")
        self.assertEqual(
            json.loads(req.data.decode("utf-8")),
            {
                "urls": ["https://example.com/a", "https://example.com/b"],
                "include_images": True,
                "extract_depth": "advanced",
            },
        )

    def test_missing_key_is_not_ready_and_never_calls_opener(self) -> None:
        opener = RecordingOpener(StubResponse({"results": []}))
        with patch("services.tavily.os.environ.get", return_value=""):
            client = TavilyClient(opener=opener)

        self.assertFalse(client.ready)
        with self.assertRaisesRegex(TavilyConfigurationError, "TAVILY_API_KEY"):
            client.search("query", "example.com")
        self.assertEqual(opener.calls, [])

    def test_http_error_is_explicit_and_does_not_expose_body_or_key(self) -> None:
        api_error = HTTPError(
            "https://api.tavily.com/search",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"unit-test-token rejected"}'),
        )
        self.addCleanup(api_error.close)
        opener = RecordingOpener(error=api_error)
        client = client_with_key(opener)

        with self.assertRaises(TavilyHTTPError) as raised:
            client.search("query", "example.com")

        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("unit-test-token", str(raised.exception))
        self.assertNotIn("rejected", str(raised.exception))

    def test_timeout_is_distinct_for_direct_and_wrapped_socket_timeout(self) -> None:
        for error in (TimeoutError(), URLError(socket.timeout("timed out"))):
            with self.subTest(error=type(error).__name__):
                opener = RecordingOpener(error=error)
                client = client_with_key(opener, timeout=3)
                with self.assertRaisesRegex(TavilyTimeoutError, "3 seconds"):
                    client.extract("https://example.com")

    def test_other_url_errors_are_transport_errors(self) -> None:
        opener = RecordingOpener(error=URLError("network unavailable"))
        client = client_with_key(opener)

        with self.assertRaises(TavilyTransportError):
            client.search("query", "example.com")

    def test_limits_and_modes_are_validated_before_network_access(self) -> None:
        opener = RecordingOpener(StubResponse({"results": []}))
        client = client_with_key(opener)

        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            client.search("query", "example.com", max_results=21)
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            client.extract([f"https://example.com/{index}" for index in range(21)])
        with self.assertRaisesRegex(ValueError, "basic.*advanced"):
            client.extract("https://example.com", extract_depth="deep")
        with self.assertRaisesRegex(ValueError, r"HTTP\(S\)"):
            client.extract("ftp://example.com/file")
        self.assertEqual(opener.calls, [])

    def test_malformed_response_has_a_clear_error(self) -> None:
        opener = RecordingOpener(StubResponse({"results": {"not": "a list"}}))
        client = client_with_key(opener)

        with self.assertRaisesRegex(TavilyResponseError, "results.*list"):
            client.search("query", "example.com")


if __name__ == "__main__":
    unittest.main()
