from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from .contracts import EMBEDDING_DIMENSIONS, EmbeddingBatch, Vector
from .settings import KnowledgeAgentSettings


class EmbeddingProviderError(RuntimeError):
    """Base error for safe-to-log embedding provider failures."""


class EmbeddingResponseError(EmbeddingProviderError):
    """Raised when a gateway response violates the embedding contract."""


def _request_texts(texts: Sequence[str]) -> list[str]:
    if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
        raise ValueError("texts must be a sequence of non-blank strings")
    normalized: list[str] = []
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("texts must contain only non-blank strings")
        normalized.append(text)
    if not normalized:
        raise ValueError("texts must not be empty")
    return normalized


def _usage_value(usage: object, name: str) -> int:
    if usage is None:
        return 0
    if not isinstance(usage, Mapping):
        raise EmbeddingResponseError("embedding response usage must be an object")
    value = usage.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EmbeddingResponseError(
            f"embedding response {name} must be a non-negative integer"
        )
    return value


def parse_embeddings_response(
    payload: object,
    *,
    expected_count: int,
    model_id: str,
    dimensions: int = EMBEDDING_DIMENSIONS,
) -> EmbeddingBatch:
    """Validate an OpenAI-compatible response and restore input ordering."""

    if dimensions != EMBEDDING_DIMENSIONS:
        raise EmbeddingResponseError(
            f"embedding dimensions must be {EMBEDDING_DIMENSIONS} for M1"
        )
    if not isinstance(payload, Mapping):
        raise EmbeddingResponseError("embedding response must be an object")
    response_model = payload.get("model")
    if not isinstance(response_model, str) or response_model.strip() != model_id:
        raise EmbeddingResponseError(
            "embedding response model does not match the requested model"
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise EmbeddingResponseError("embedding response data must be a list")
    if len(data) != expected_count:
        raise EmbeddingResponseError(
            "embedding response count does not match the request"
        )

    ordered: list[Vector | None] = [None] * expected_count
    for item in data:
        if not isinstance(item, Mapping):
            raise EmbeddingResponseError("embedding response items must be objects")
        index = item.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= expected_count
            or ordered[index] is not None
        ):
            raise EmbeddingResponseError(
                "embedding response indexes must be unique and contiguous"
            )
        embedding = item.get("embedding")
        try:
            batch = EmbeddingBatch(
                vectors=(embedding,),  # type: ignore[arg-type]
                model=model_id,
            )
        except ValueError as exc:
            raise EmbeddingResponseError(str(exc)) from exc
        ordered[index] = batch.vectors[0]

    if any(vector is None for vector in ordered):
        raise EmbeddingResponseError(
            "embedding response indexes must be unique and contiguous"
        )
    usage = payload.get("usage")
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    return EmbeddingBatch(
        vectors=tuple(vector for vector in ordered if vector is not None),
        model=model_id,
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
    )


class OpenAICompatibleEmbeddingProvider:
    """Synchronous OpenAI-compatible `/embeddings` adapter."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        dimensions: int = EMBEDDING_DIMENSIONS,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport cannot both be provided")
        ready_settings = KnowledgeAgentSettings(
            embedding_base_url=base_url,
            embedding_api_key=api_key,
            embedding_model=model_id,
            embedding_dimensions=dimensions,
        )
        if not ready_settings.embedding_base_url:
            raise ValueError("base_url is required")
        if not ready_settings.embedding_api_key:
            raise ValueError("api_key is required")

        self._endpoint = (
            f"{ready_settings.embedding_base_url.rstrip('/')}/embeddings"
        )
        self._api_key = ready_settings.embedding_api_key
        self._model_id = ready_settings.embedding_model
        self._dimensions = ready_settings.embedding_dimensions
        self._owns_client = client is None
        self._client = (
            httpx.Client(transport=transport, timeout=timeout_seconds)
            if client is None
            else client
        )

    @classmethod
    def from_settings(
        cls,
        settings: KnowledgeAgentSettings,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> OpenAICompatibleEmbeddingProvider:
        settings.require_ready()
        assert settings.embedding_base_url is not None
        assert settings.embedding_api_key is not None
        return cls(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model_id=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout_seconds=timeout_seconds,
            client=client,
            transport=transport,
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        input_texts = _request_texts(texts)
        try:
            response = self._client.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model_id,
                    "input": input_texts,
                    "encoding_format": "float",
                    "dimensions": self._dimensions,
                },
            )
        except httpx.RequestError as exc:
            raise EmbeddingProviderError(
                f"embedding request failed ({type(exc).__name__})"
            ) from None
        if not response.is_success:
            raise EmbeddingProviderError(
                f"embedding request failed with HTTP {response.status_code}"
            )
        try:
            payload: Any = response.json()
        except ValueError:
            raise EmbeddingResponseError(
                "embedding gateway returned invalid JSON"
            ) from None
        return parse_embeddings_response(
            payload,
            expected_count=len(input_texts),
            model_id=self._model_id,
            dimensions=self._dimensions,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatibleEmbeddingProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
