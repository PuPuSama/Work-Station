from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import load_config  # noqa: E402
from services.llm import LLMClient, build_responses_payload, extract_stream_text  # noqa: E402


class FakeStreamingResponse(BytesIO):
    def __enter__(self) -> FakeStreamingResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class ResponsesPayloadTests(unittest.TestCase):
    def test_request_uses_selected_reasoning_effort(self) -> None:
        payload = build_responses_payload(
            model="gpt-5.6-sol",
            messages=[{"role": "user", "content": "Write an outline."}],
            temperature=0.3,
            max_tokens=800,
            reasoning_effort="medium",
        )
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertIs(payload["stream"], True)

    def test_default_model_is_gpt_5_6_sol(self) -> None:
        self.assertEqual(load_config().llm_model, "gpt-5.6-sol")

    def test_default_reasoning_effort_is_xhigh(self) -> None:
        self.assertEqual(load_config().llm_reasoning_effort, "xhigh")


class ResponsesStreamTests(unittest.TestCase):
    def test_client_requests_and_consumes_an_sse_stream(self) -> None:
        response = FakeStreamingResponse(
            (
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"Streamed text"}\n\n'
                "event: response.completed\n"
                'data: {"type":"response.completed","response":{"output_text":"Streamed text"}}\n\n'
            ).encode("utf-8")
        )
        with (
            patch.dict(
                os.environ,
                {"LLM_API_KEY": "test-key", "LLM_MODEL": "gpt-5.6-sol"},
            ),
            patch("services.llm.request.urlopen", return_value=response) as urlopen,
        ):
            client = LLMClient(load_config())
            result = client.chat([{"role": "user", "content": "Write."}])

        sent_request = urlopen.call_args.args[0]
        payload = json.loads(sent_request.data.decode("utf-8"))
        self.assertEqual(result, "Streamed text")
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["reasoning"], {"effort": "xhigh"})
        self.assertIs(payload["stream"], True)
        self.assertEqual(sent_request.get_header("Accept"), "text/event-stream")

    def test_saved_runtime_selection_overrides_environment_model(self) -> None:
        response = FakeStreamingResponse(
            (
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"Done"}\n\n'
            ).encode("utf-8")
        )
        selected = replace(
            load_config(),
            llm_model="gpt-5.6-terra",
            llm_reasoning_effort="medium",
            llm_runtime_override=True,
        )
        with (
            patch.dict(
                os.environ,
                {
                    "LLM_API_KEY": "test-key",
                    "LLM_MODEL": "gpt-5.6-sol",
                    "LLM_REASONING_EFFORT": "xhigh",
                },
            ),
            patch("services.llm.request.urlopen", return_value=response) as urlopen,
        ):
            result = LLMClient(selected).chat(
                [{"role": "user", "content": "Write."}]
            )

        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(result, "Done")
        self.assertEqual(payload["model"], "gpt-5.6-terra")
        self.assertEqual(payload["reasoning"], {"effort": "medium"})

    def test_collects_output_text_delta_events(self) -> None:
        stream = BytesIO(
            (
                ": keepalive\n\n"
                "event: response.created\n"
                'data: {"type":"response.created"}\n\n'
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"Hello "}\n\n'
                "event: response.output_text.delta\n"
                'data: {"type":"response.output_text.delta","delta":"world"}\n\n'
                "event: response.completed\n"
                'data: {"type":"response.completed","response":{"output_text":"Hello world"}}\n\n'
            ).encode("utf-8")
        )

        self.assertEqual(extract_stream_text(stream), "Hello world")

    def test_uses_completed_response_when_no_delta_was_emitted(self) -> None:
        stream = BytesIO(
            (
                "event: response.completed\n"
                'data: {"type":"response.completed","response":{"output":[{"content":'
                '[{"type":"output_text","text":"Fallback text"}]}]}}\n\n'
            ).encode("utf-8")
        )

        self.assertEqual(extract_stream_text(stream), "Fallback text")

    def test_stream_error_raises_clear_runtime_error(self) -> None:
        stream = BytesIO(
            (
                "event: error\n"
                'data: {"type":"error","code":"rate_limit","message":"Try later"}\n\n'
            ).encode("utf-8")
        )

        with self.assertRaisesRegex(RuntimeError, "rate_limit: Try later"):
            extract_stream_text(stream)


if __name__ == "__main__":
    unittest.main()
