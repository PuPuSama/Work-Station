from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.llm import build_responses_payload  # noqa: E402


class ResponsesPayloadTests(unittest.TestCase):
    def test_every_request_uses_xhigh_reasoning_effort(self) -> None:
        payload = build_responses_payload(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "Write an outline."}],
            temperature=0.3,
            max_tokens=800,
        )
        self.assertEqual(payload["reasoning"], {"effort": "xhigh"})
        self.assertEqual(payload["model"], "gpt-5.5")


if __name__ == "__main__":
    unittest.main()
