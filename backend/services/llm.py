from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError
from urllib import request

from dotenv import load_dotenv

from config import AppConfig, ROOT_DIR


LLM_TIMEOUT_SECONDS = 240


class LLMClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        timeout_seconds: float = LLM_TIMEOUT_SECONDS,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        load_dotenv(ROOT_DIR / ".env")
        load_dotenv(ROOT_DIR / "backend" / ".env")
        self.config = config
        self.api_key = (
            os.environ.get("LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        ).strip()
        self.model = os.environ.get("LLM_MODEL", config.llm_model).strip()
        self.base_url = os.environ.get("LLM_BASE_URL", config.llm_base_url).strip().rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.model)

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 1800,
    ) -> str:
        if not self.ready:
            return ""

        payload = build_responses_payload(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

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
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Responses API request failed: HTTP {exc.code} {detail}") from exc
        return extract_response_text(data)


def build_responses_payload(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """Build the shared Responses API payload with maximum reasoning effort."""

    return {
        "model": model,
        "input": responses_input_from_messages(messages),
        "reasoning": {"effort": "xhigh"},
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }


def responses_input_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate text or multimodal chat messages to Responses API input items.

    Plain string content is deliberately left untouched for compatibility with
    the existing article-generation calls.  Multimodal callers may use native
    Responses API content parts (``input_text``/``input_image``) or the common
    Chat Completions aliases (``text``/``image_url``).
    """

    input_items: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role", "user")
        content = _responses_message_content(message.get("content", ""))
        if role == "system":
            role = "developer"
        elif role not in {"developer", "user", "assistant"}:
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
    return input_items


def _responses_message_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    parts: list[dict[str, Any]] = []
    for raw_part in content:
        if isinstance(raw_part, str):
            parts.append({"type": "input_text", "text": raw_part})
            continue
        if not isinstance(raw_part, dict):
            continue

        part_type = str(raw_part.get("type") or "").strip()
        if part_type in {"input_text", "output_text"}:
            text = raw_part.get("text")
            if isinstance(text, str):
                parts.append({"type": part_type, "text": text})
            continue
        if part_type == "text":
            text = raw_part.get("text")
            if isinstance(text, str):
                parts.append({"type": "input_text", "text": text})
            continue
        if part_type in {"input_image", "image_url"}:
            image_url: Any = raw_part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or not image_url.strip():
                continue
            image_part: dict[str, Any] = {
                "type": "input_image",
                "image_url": image_url,
            }
            detail = raw_part.get("detail")
            if detail is None and isinstance(raw_part.get("image_url"), dict):
                detail = raw_part["image_url"].get("detail")
            if detail in {"auto", "low", "high"}:
                image_part["detail"] = detail
            parts.append(image_part)

    return parts


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
