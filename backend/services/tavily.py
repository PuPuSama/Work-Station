from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit


TAVILY_API_BASE_URL = "https://api.tavily.com"
TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RESULTS = 20
MAX_EXTRACT_URLS = 20


class TavilyError(RuntimeError):
    """Base class for Tavily client failures."""


class TavilyConfigurationError(TavilyError):
    """Raised when the Tavily client cannot be used as configured."""


class TavilyRequestError(TavilyError):
    """Base class for Tavily request failures."""


class TavilyTimeoutError(TavilyRequestError):
    """Raised when a Tavily request exceeds the configured timeout."""


class TavilyHTTPError(TavilyRequestError):
    """Raised when Tavily returns a non-successful HTTP status."""

    def __init__(self, endpoint: str, status_code: int) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        # Do not include response bodies here. They are not needed by callers and
        # could contain request details that should not be surfaced or logged.
        super().__init__(f"Tavily {endpoint} request failed with HTTP {status_code}.")


class TavilyTransportError(TavilyRequestError):
    """Raised when the Tavily API cannot be reached."""


class TavilyResponseError(TavilyError):
    """Raised when Tavily returns malformed or unexpected JSON."""


@dataclass(frozen=True, slots=True)
class TavilySearchResult:
    title: str
    url: str
    content: str
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class TavilySearchResponse:
    query: str
    results: tuple[TavilySearchResult, ...]
    response_time: float | None = None
    request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": [result.to_dict() for result in self.results],
            "response_time": self.response_time,
            "request_id": self.request_id,
        }


@dataclass(frozen=True, slots=True)
class TavilyExtractResult:
    url: str
    raw_content: str
    images: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "raw_content": self.raw_content,
            "images": list(self.images),
        }


@dataclass(frozen=True, slots=True)
class TavilyExtractFailure:
    url: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "error": self.error}


@dataclass(frozen=True, slots=True)
class TavilyExtractResponse:
    results: tuple[TavilyExtractResult, ...]
    failed_results: tuple[TavilyExtractFailure, ...] = ()
    response_time: float | None = None
    request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [result.to_dict() for result in self.results],
            "failed_results": [failure.to_dict() for failure in self.failed_results],
            "response_time": self.response_time,
            "request_id": self.request_id,
        }


class TavilyClient:
    """Small REST client for the Tavily Search and Extract endpoints.

    The API key is read only from ``TAVILY_API_KEY``. Instantiating the client
    without a key is side-effect free; ``ready`` is false and the first API call
    raises ``TavilyConfigurationError`` before the injected opener is invoked.
    """

    def __init__(
        self,
        *,
        base_url: str = TAVILY_API_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Any | None = None,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")

        normalized_base_url = str(base_url or "").strip().rstrip("/")
        parsed_base_url = urlsplit(normalized_base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")

        selected_opener = opener if opener is not None else request.urlopen
        open_method = getattr(selected_opener, "open", selected_opener)
        if not callable(open_method):
            raise TypeError("opener must be callable or expose an open method")

        self._api_key = (os.environ.get(TAVILY_API_KEY_ENV) or "").strip()
        self._base_url = normalized_base_url
        self._timeout = float(timeout)
        self._opener = selected_opener

    @property
    def ready(self) -> bool:
        return bool(self._api_key)

    def search(
        self,
        query: str,
        host: str,
        max_results: int = 5,
    ) -> TavilySearchResponse:
        """Search within one host using Tavily's basic search mode."""

        normalized_query = _required_text(query, "query")
        normalized_host = _normalize_host(host)
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= MAX_RESULTS
        ):
            raise ValueError(f"max_results must be an integer between 1 and {MAX_RESULTS}")

        payload = {
            "query": normalized_query,
            "include_domains": [normalized_host],
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_images": False,
        }
        data = self._post("search", payload)
        return _normalize_search_response(data, fallback_query=normalized_query)

    def extract(
        self,
        urls: str | Iterable[str],
        *,
        extract_depth: str = "basic",
    ) -> TavilyExtractResponse:
        """Extract up to 20 HTTP(S) URLs and always request their images."""

        normalized_urls = _normalize_urls(urls)
        depth = str(extract_depth or "").strip().lower()
        if depth not in {"basic", "advanced"}:
            raise ValueError("extract_depth must be 'basic' or 'advanced'")

        payload = {
            "urls": normalized_urls,
            "include_images": True,
            "extract_depth": depth,
        }
        data = self._post("extract", payload)
        return _normalize_extract_response(data)

    def _post(self, endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.ready:
            raise TavilyConfigurationError(
                f"{TAVILY_API_KEY_ENV} is not configured; Tavily requests are disabled."
            )

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            f"{self._base_url}/{endpoint}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        open_method = getattr(self._opener, "open", self._opener)

        try:
            with open_method(req, timeout=self._timeout) as response:
                status = _response_status(response)
                if status is not None and not 200 <= status < 300:
                    raise TavilyHTTPError(endpoint, status)
                raw_body = response.read()
        except HTTPError as exc:
            raise TavilyHTTPError(endpoint, int(exc.code)) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise TavilyTimeoutError(
                f"Tavily {endpoint} request timed out after {self._timeout:g} seconds."
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TavilyTimeoutError(
                    f"Tavily {endpoint} request timed out after {self._timeout:g} seconds."
                ) from exc
            raise TavilyTransportError(
                f"Tavily {endpoint} request could not reach the API."
            ) from exc

        if isinstance(raw_body, bytes):
            decoded_body = raw_body.decode("utf-8", errors="replace")
        else:
            decoded_body = str(raw_body)
        try:
            data = json.loads(decoded_body)
        except (TypeError, ValueError) as exc:
            raise TavilyResponseError(
                f"Tavily {endpoint} returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise TavilyResponseError(
                f"Tavily {endpoint} returned a JSON value other than an object."
            )
        return data


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalize_host(host: str) -> str:
    raw_host = _required_text(host, "host")
    parsed = urlsplit(raw_host if "://" in raw_host else f"//{raw_host}")
    if parsed.username or parsed.password:
        raise ValueError("host must not include credentials")
    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not hostname or any(character.isspace() for character in hostname):
        raise ValueError("host must contain a valid hostname")
    try:
        return hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("host must contain a valid hostname") from exc


def _normalize_urls(urls: str | Iterable[str]) -> list[str]:
    if isinstance(urls, str):
        candidates = [urls]
    else:
        try:
            candidates = list(urls)
        except TypeError as exc:
            raise ValueError("urls must be a URL or an iterable of URLs") from exc

    if not 1 <= len(candidates) <= MAX_EXTRACT_URLS:
        raise ValueError(f"urls must contain between 1 and {MAX_EXTRACT_URLS} URLs")

    normalized: list[str] = []
    for candidate in candidates:
        url = _required_text(candidate, "url")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("each URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("URLs must not include credentials")
        normalized.append(url)
    return normalized


def _response_status(response: Any) -> int | None:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            status = getcode()
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _required_list(data: Mapping[str, Any], field: str, endpoint: str) -> list[Any]:
    value = data.get(field)
    if not isinstance(value, list):
        raise TavilyResponseError(
            f"Tavily {endpoint} response field '{field}' must be a list."
        )
    return value


def _optional_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _normalize_search_response(
    data: Mapping[str, Any],
    *,
    fallback_query: str,
) -> TavilySearchResponse:
    results: list[TavilySearchResult] = []
    for item in _required_list(data, "results", "search"):
        if not isinstance(item, Mapping):
            raise TavilyResponseError("Tavily search response contains an invalid result.")
        results.append(
            TavilySearchResult(
                title=_text(item.get("title")),
                url=_text(item.get("url")),
                content=_text(item.get("content")),
                score=_optional_number(item.get("score")),
            )
        )

    return TavilySearchResponse(
        query=_text(data.get("query")) or fallback_query,
        results=tuple(results),
        response_time=_optional_number(data.get("response_time")),
        request_id=_text(data.get("request_id")),
    )


def _normalize_images(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    images: list[str] = []
    for item in value:
        if isinstance(item, str):
            image_url = item.strip()
        elif isinstance(item, Mapping):
            image_url = _text(item.get("url")).strip()
        else:
            image_url = ""
        if image_url:
            images.append(image_url)
    return tuple(images)


def _normalize_extract_response(data: Mapping[str, Any]) -> TavilyExtractResponse:
    results: list[TavilyExtractResult] = []
    for item in _required_list(data, "results", "extract"):
        if not isinstance(item, Mapping):
            raise TavilyResponseError("Tavily extract response contains an invalid result.")
        results.append(
            TavilyExtractResult(
                url=_text(item.get("url")),
                raw_content=_text(item.get("raw_content")),
                images=_normalize_images(item.get("images")),
            )
        )

    raw_failures = data.get("failed_results", [])
    if not isinstance(raw_failures, list):
        raise TavilyResponseError(
            "Tavily extract response field 'failed_results' must be a list."
        )
    failures: list[TavilyExtractFailure] = []
    for item in raw_failures:
        if not isinstance(item, Mapping):
            raise TavilyResponseError("Tavily extract response contains an invalid failure.")
        failures.append(
            TavilyExtractFailure(
                url=_text(item.get("url")),
                error=_text(item.get("error")) or _text(item.get("message")),
            )
        )

    return TavilyExtractResponse(
        results=tuple(results),
        failed_results=tuple(failures),
        response_time=_optional_number(data.get("response_time")),
        request_id=_text(data.get("request_id")),
    )
