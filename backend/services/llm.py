from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError
from urllib import request

from dotenv import load_dotenv

from config import AppConfig, ROOT_DIR


class LLMClient:
    def __init__(self, config: AppConfig):
        load_dotenv(ROOT_DIR / ".env")
        load_dotenv(ROOT_DIR / "backend" / ".env")
        self.config = config
        self.api_key = (
            os.environ.get("LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        ).strip()
        self.model = os.environ.get("LLM_MODEL", config.llm_model).strip()
        self.base_url = os.environ.get("LLM_BASE_URL", config.llm_base_url).strip().rstrip("/")

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.model)

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.7, max_tokens: int = 1800) -> str:
        if not self.ready:
            return ""

        instructions, input_items = responses_input_from_messages(messages)
        payload = {
            "model": self.model,
            "input": input_items,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "store": False,
            "truncation": "auto",
        }
        if instructions:
            payload["instructions"] = instructions

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/responses",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=90) as response:
                data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Responses API request failed: HTTP {exc.code} {detail}") from exc
        return extract_response_text(data)


def responses_input_from_messages(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    instruction_parts: list[str] = []
    input_items: list[dict[str, str]] = []

    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role in {"system", "developer"}:
            instruction_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        input_items.append(
            {
                "type": "message",
                "role": role,
                "content": content,
            }
        )

    if not input_items:
        input_items.append({"type": "message", "role": "user", "content": ""})
    return "\n\n".join(part for part in instruction_parts if part), input_items


def extract_response_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        content_items = item.get("content", [])
        if not isinstance(content_items, list):
            continue
        for content in content_items:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
            elif content.get("type") == "refusal" and isinstance(content.get("refusal"), str):
                parts.append(content["refusal"])

    return "\n".join(part.strip() for part in parts if part.strip()).strip()
