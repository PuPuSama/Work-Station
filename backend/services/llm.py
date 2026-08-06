from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.error import HTTPError
from urllib import request

from dotenv import load_dotenv

from config import AppConfig, ROOT_DIR


LLM_TIMEOUT_SECONDS = 240
LLM_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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
        if config.llm_runtime_override:
            self.model = config.llm_model.strip()
            self.reasoning_effort = config.llm_reasoning_effort.strip()
        else:
            self.model = os.environ.get("LLM_MODEL", config.llm_model).strip()
            self.reasoning_effort = os.environ.get(
                "LLM_REASONING_EFFORT",
                config.llm_reasoning_effort,
            ).strip()
        self.base_url = os.environ.get(
            "LLM_BASE_URL",
            config.llm_base_url,
        ).strip().rstrip("/")
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
            max_tokens=max_tokens,
            reasoning_effort=self.reasoning_effort,
        )

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/responses",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": LLM_USER_AGENT,
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return extract_stream_text(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Responses API request failed: HTTP {exc.code} {detail}") from exc


def build_responses_payload(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    reasoning_effort: str = "xhigh",
) -> dict[str, Any]:
    """Build a Responses API payload accepted by reasoning-model gateways."""

    return {
        "model": model,
        "input": responses_input_from_messages(messages),
        "reasoning": {"effort": reasoning_effort},
        "max_output_tokens": max_tokens,
        "stream": True,
    }


def iter_responses_sse_events(
    stream: Iterable[bytes | str],
) -> Iterator[dict[str, Any]]:
    """Parse Responses API server-sent events from an HTTP response stream."""

    event_name = ""
    data_lines: list[str] = []

    def decode_event() -> dict[str, Any] | None:
        nonlocal event_name, data_lines
        raw_data = "\n".join(data_lines).strip()
        current_event_name = event_name
        event_name = ""
        data_lines = []
        if not raw_data or raw_data == "[DONE]":
            return None
        try:
            event = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Responses API returned an invalid streaming event.") from exc
        if not isinstance(event, dict):
            raise RuntimeError("Responses API returned a non-object streaming event.")
        if current_event_name and not event.get("type"):
            event["type"] = current_event_name
        return event

    for raw_line in stream:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8")
        else:
            line = str(raw_line)
        line = line.rstrip("\r\n")

        if not line:
            event = decode_event()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if not separator:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    event = decode_event()
    if event is not None:
        yield event


def extract_stream_text(stream: Iterable[bytes | str]) -> str:
    """Collect visible text from a streamed Responses API request."""

    deltas: list[str] = []
    done_text: list[str] = []
    completed_text = ""

    for event in iter_responses_sse_events(stream):
        event_type = str(event.get("type") or "")
        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                done_text.append(text)
            continue
        if event_type == "response.completed":
            response_data = event.get("response")
            if isinstance(response_data, dict):
                completed_text = extract_response_text(response_data)
            continue
        if event_type in {"response.failed", "error"}:
            raise RuntimeError(
                f"Responses API stream failed: {_stream_error_detail(event)}"
            )

    if deltas:
        return "".join(deltas).strip()
    if done_text:
        return "\n".join(part.strip() for part in done_text if part.strip()).strip()
    return completed_text.strip()


def _stream_error_detail(event: dict[str, Any]) -> str:
    error: Any = event.get("error")
    response_data = event.get("response")
    if not isinstance(error, dict) and isinstance(response_data, dict):
        error = response_data.get("error")
    if not isinstance(error, dict):
        error = event

    code = error.get("code")
    message = error.get("message")
    if code and message:
        return f"{code}: {message}"
    if message:
        return str(message)
    if code:
        return str(code)
    return "unknown streaming error"


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
