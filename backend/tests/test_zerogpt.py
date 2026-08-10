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

from services.zerogpt import (  # noqa: E402
    ZeroGPTClient,
    ZeroGPTConfigurationError,
    ZeroGPTHTTPError,
    ZeroGPTResponseError,
    ZeroGPTTimeoutError,
    ZeroGPTTransportError,
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
    def __init__(
        self,
        response: StubResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
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


def client_with_key(opener: RecordingOpener, *, timeout: float = 12) -> ZeroGPTClient:
    with patch.dict(
        "os.environ",
        {"ARTICLE_AGENT_ZEROGPT_API_KEY": "unit-test-key"},
        clear=False,
    ):
        return ZeroGPTClient(opener=opener, timeout=timeout)


class ZeroGPTClientTests(unittest.TestCase):
    def test_detect_uses_official_endpoint_header_and_payload(self) -> None:
        opener = RecordingOpener(
            StubResponse(
                {
                    "success": True,
                    "code": 200,
                    "data": {
                        "fakePercentage": "62.5",
                        "aiWords": "25",
                        "textWords": "40",
                    },
                }
            )
        )
        client = client_with_key(opener)

        result = client.detect("  Generated article body.  ")

        self.assertTrue(client.ready)
        self.assertEqual(result.ai_percentage, 62.5)
        self.assertEqual(result.ai_words, 25)
        self.assertEqual(result.text_words, 40)
        self.assertIn("62.5", result.report)
        req, timeout = opener.calls[0]
        self.assertEqual(req.full_url, "https://api.zerogpt.com/api/detect/detectText")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Apikey"), "unit-test-key")
        self.assertEqual(timeout, 12)
        self.assertEqual(
            json.loads(req.data.decode("utf-8")),
            {"input_text": "Generated article body."},
        )

    def test_missing_key_is_not_ready_and_never_calls_opener(self) -> None:
        opener = RecordingOpener(StubResponse({"success": True}))
        with patch.dict("os.environ", {"ARTICLE_AGENT_ZEROGPT_API_KEY": ""}, clear=False):
            client = ZeroGPTClient(opener=opener)

        self.assertFalse(client.ready)
        with self.assertRaisesRegex(
            ZeroGPTConfigurationError,
            "ARTICLE_AGENT_ZEROGPT_API_KEY",
        ):
            client.detect("article")
        self.assertEqual(opener.calls, [])

    def test_invalid_percentage_and_nan_are_rejected(self) -> None:
        for value in ("not-a-number", "NaN", 101, -1):
            with self.subTest(value=value):
                client = client_with_key(
                    RecordingOpener(
                        StubResponse(
                            {
                                "success": True,
                                "data": {"fakePercentage": value},
                            }
                        )
                    )
                )
                with self.assertRaises(ZeroGPTResponseError):
                    client.detect("article")

    def test_http_timeout_and_transport_errors_do_not_expose_key(self) -> None:
        api_error = HTTPError(
            "https://api.zerogpt.com/api/detect/detectText",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b"unit-test-key rejected"),
        )
        self.addCleanup(api_error.close)
        client = client_with_key(RecordingOpener(error=api_error))
        with self.assertRaises(ZeroGPTHTTPError) as raised:
            client.detect("article")
        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("unit-test-key", str(raised.exception))
        self.assertNotIn("rejected", str(raised.exception))

        for error, expected in (
            (TimeoutError(), ZeroGPTTimeoutError),
            (URLError(socket.timeout("timed out")), ZeroGPTTimeoutError),
            (URLError("network unavailable"), ZeroGPTTransportError),
        ):
            with self.subTest(error=type(error).__name__):
                detector = client_with_key(RecordingOpener(error=error), timeout=3)
                with self.assertRaises(expected):
                    detector.detect("article")

    def test_empty_text_is_rejected_before_network_access(self) -> None:
        opener = RecordingOpener(StubResponse({"success": True}))
        client = client_with_key(opener)
        with self.assertRaisesRegex(ValueError, "non-empty"):
            client.detect("   ")
        self.assertEqual(opener.calls, [])


if __name__ == "__main__":
    unittest.main()
