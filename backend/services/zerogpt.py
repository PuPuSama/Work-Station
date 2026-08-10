from __future__ import annotations

import json
import math
import os
import socket
from dataclasses import dataclass
from typing import Any, Mapping
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from dotenv import load_dotenv

from config import ROOT_DIR


ZEROGPT_API_BASE_URL = "https://api.zerogpt.com"
ZEROGPT_API_KEY_ENV = "ARTICLE_AGENT_ZEROGPT_API_KEY"
ZEROGPT_API_KEY_COMPAT_ENV = "ZEROGPT_API_KEY"
ZEROGPT_BASE_URL_ENV = "ARTICLE_AGENT_ZEROGPT_BASE_URL"
ZEROGPT_DETECT_PATH = "/api/detect/detectText"
ZEROGPT_TIMEOUT_SECONDS = 30.0


class ZeroGPTError(RuntimeError):
    """Base class for safe ZeroGPT integration failures."""


class ZeroGPTConfigurationError(ZeroGPTError):
    """Raised when the detector has not been configured."""


class ZeroGPTRequestError(ZeroGPTError):
    """Base class for network or HTTP failures."""


class ZeroGPTHTTPError(ZeroGPTRequestError):
    """Raised when ZeroGPT returns a non-successful HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"ZeroGPT detection request failed with HTTP {status_code}.")


class ZeroGPTTimeoutError(ZeroGPTRequestError):
    """Raised when the ZeroGPT request exceeds its timeout."""


class ZeroGPTTransportError(ZeroGPTRequestError):
    """Raised when the ZeroGPT API cannot be reached."""


class ZeroGPTResponseError(ZeroGPTError):
    """Raised when ZeroGPT returns an unexpected response."""


@dataclass(frozen=True, slots=True)
class ZeroGPTDetectionResult:
    """The small, provider-neutral result persisted on an article Task."""

    ai_percentage: float
    ai_words: int | None = None
    text_words: int | None = None

    @property
    def report(self) -> str:
        details = f"ZeroGPT 自动检测：AI 内容占比 {self.ai_percentage:g}%。"
        if self.ai_words is not None and self.text_words is not None:
            details += f" AI 字数 {self.ai_words}/{self.text_words}。"
        return details


class ZeroGPTClient:
    """Minimal client for the ZeroGPT Business API text detector.

    The business API uses ``ApiKey`` (not a Bearer token) and accepts one
    ``input_text`` JSON field at ``/api/detect/detectText``.  The key is read
    from the server environment only; response bodies and exception details
    are deliberately not surfaced because they can contain provider data.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = ZEROGPT_TIMEOUT_SECONDS,
        opener: Any | None = None,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")

        load_dotenv(ROOT_DIR / ".env")
        load_dotenv(ROOT_DIR / "backend" / ".env")
        configured_base_url = base_url or os.environ.get(
            ZEROGPT_BASE_URL_ENV,
            ZEROGPT_API_BASE_URL,
        )
        normalized_base_url = str(configured_base_url or "").strip().rstrip("/")
        parsed_base_url = urlsplit(normalized_base_url)
        if (
            parsed_base_url.scheme not in {"http", "https"}
            or not parsed_base_url.netloc
            or parsed_base_url.username
            or parsed_base_url.password
        ):
            raise ValueError("base_url must be an absolute HTTP(S) URL without credentials")

        selected_opener = opener if opener is not None else request.urlopen
        open_method = getattr(selected_opener, "open", selected_opener)
        if not callable(open_method):
            raise TypeError("opener must be callable or expose an open method")

        self._api_key = (
            os.environ.get(ZEROGPT_API_KEY_ENV)
            or os.environ.get(ZEROGPT_API_KEY_COMPAT_ENV)
            or ""
        ).strip()
        self._base_url = normalized_base_url
        self._timeout = float(timeout)
        self._opener = selected_opener

    @property
    def ready(self) -> bool:
        return bool(self._api_key)

    def detect(self, text: str) -> ZeroGPTDetectionResult:
        normalized_text = _required_text(text, "text")
        response_payload = self._post({"input_text": normalized_text})
        return _normalize_detection_response(response_payload)

    def _post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.ready:
            raise ZeroGPTConfigurationError(
                f"{ZEROGPT_API_KEY_ENV} is not configured; ZeroGPT requests are disabled."
            )

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        req = request.Request(
            f"{self._base_url}{ZEROGPT_DETECT_PATH}",
            data=body,
            method="POST",
            headers={
                "ApiKey": self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "article-agent/1.0",
            },
        )
        open_method = getattr(self._opener, "open", self._opener)
        try:
            with open_method(req, timeout=self._timeout) as response:
                status = _response_status(response)
                if status is not None and not 200 <= status < 300:
                    raise ZeroGPTHTTPError(status)
                raw_body = response.read()
        except HTTPError as exc:
            raise ZeroGPTHTTPError(int(exc.code)) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ZeroGPTTimeoutError(
                f"ZeroGPT detection request timed out after {self._timeout:g} seconds."
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ZeroGPTTimeoutError(
                    f"ZeroGPT detection request timed out after {self._timeout:g} seconds."
                ) from exc
            raise ZeroGPTTransportError(
                "ZeroGPT detection request could not reach the API."
            ) from exc

        decoded_body = (
            raw_body.decode("utf-8", errors="replace")
            if isinstance(raw_body, bytes)
            else str(raw_body)
        )
        try:
            data = json.loads(decoded_body)
        except (TypeError, ValueError) as exc:
            raise ZeroGPTResponseError("ZeroGPT returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise ZeroGPTResponseError(
                "ZeroGPT returned a JSON value other than an object."
            )
        return data


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _response_status(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _normalize_detection_response(payload: Mapping[str, Any]) -> ZeroGPTDetectionResult:
    if payload.get("success") is False:
        raise ZeroGPTResponseError("ZeroGPT rejected the detection request.")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ZeroGPTResponseError("ZeroGPT response did not contain detection data.")

    score = _finite_percentage(data.get("fakePercentage"))
    if score is None:
        raise ZeroGPTResponseError("ZeroGPT response did not contain a valid AI percentage.")
    return ZeroGPTDetectionResult(
        ai_percentage=score,
        ai_words=_optional_int(data.get("aiWords")),
        text_words=_optional_int(data.get("textWords")),
    )


def _finite_percentage(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or not 0 <= score <= 100:
        return None
    return score


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


__all__ = [
    "ZEROGPT_API_BASE_URL",
    "ZEROGPT_API_KEY_COMPAT_ENV",
    "ZEROGPT_API_KEY_ENV",
    "ZEROGPT_BASE_URL_ENV",
    "ZEROGPT_DETECT_PATH",
    "ZeroGPTClient",
    "ZeroGPTConfigurationError",
    "ZeroGPTDetectionResult",
    "ZeroGPTError",
    "ZeroGPTHTTPError",
    "ZeroGPTRequestError",
    "ZeroGPTResponseError",
    "ZeroGPTTimeoutError",
    "ZeroGPTTransportError",
]
