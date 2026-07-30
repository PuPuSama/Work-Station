from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .contracts import RetrievalHit, RetrievalProvenance, RetrievalQuery
from .repository import _chunk_from_row
from .schema import knowledge_chunks, knowledge_sources, source_snapshots


class LightRAGProviderError(RuntimeError):
    """Safe-to-log failure from the optional LightRAG Server."""


class LightRAGResponseError(LightRAGProviderError):
    """Raised when LightRAG returns an incompatible data response."""


@dataclass(frozen=True, slots=True)
class LightRAGCandidate:
    """Untrusted candidate identity returned by an experimental graph index."""

    chunk_id: str
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.chunk_id, str) or not self.chunk_id.strip():
            raise ValueError("chunk_id is required")
        object.__setattr__(self, "chunk_id", self.chunk_id.strip())
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not isfinite(float(self.score))
        ):
            raise ValueError("LightRAG candidate score must be finite")
        object.__setattr__(self, "score", float(self.score))


class LightRAGCandidateProvider(Protocol):
    """Experimental index boundary; candidates are re-authorized in PostgreSQL."""

    def search(
        self,
        *,
        project_id: str,
        text: str,
        limit: int,
    ) -> Sequence[LightRAGCandidate]: ...


def lightrag_document_path(project_id: str, chunk_id: str) -> str:
    """Encode authoritative identities into LightRAG's opaque file path field."""

    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id is required")
    if not isinstance(chunk_id, str) or not chunk_id.strip():
        raise ValueError("chunk_id is required")
    return (
        "knowledge-agent://"
        + quote(project_id.strip(), safe="")
        + "/"
        + quote(chunk_id.strip(), safe="")
    )


def _chunk_id_from_document_path(
    value: object,
    *,
    project_id: str,
) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "knowledge-agent":
        return None
    if unquote(parsed.netloc) != project_id:
        return None
    chunk_id = unquote(parsed.path.lstrip("/")).strip()
    return chunk_id or None


class LightRAGHttpCandidateProvider:
    """Synchronous `/query/data` client pinned to one LightRAG workspace/project.

    One authoritative KnowledgeChunk must be indexed as one LightRAG document
    using :func:`lightrag_document_path` as its ``file_path``. Returned text,
    entity IDs, and LightRAG chunk IDs are deliberately ignored.
    """

    def __init__(
        self,
        *,
        project_id: str,
        base_url: str,
        api_key: str | None = None,
        mode: str = "mix",
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport cannot both be provided")
        normalized_project = project_id.strip()
        if not normalized_project:
            raise ValueError("project_id is required")
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if mode not in {"local", "global", "hybrid", "naive", "mix"}:
            raise ValueError("mode is not supported by LightRAG")
        self._project_id = normalized_project
        self._endpoint = base_url.rstrip("/") + "/query/data"
        self._api_key = api_key.strip() if api_key else None
        self._mode = mode
        self._owns_client = client is None
        self._client = (
            httpx.Client(transport=transport, timeout=timeout_seconds)
            if client is None
            else client
        )

    def search(
        self,
        *,
        project_id: str,
        text: str,
        limit: int,
    ) -> tuple[LightRAGCandidate, ...]:
        if project_id != self._project_id:
            raise LightRAGProviderError(
                "LightRAG provider is pinned to a different project workspace"
            )
        if not isinstance(text, str) or len(text.strip()) < 3:
            raise ValueError("text must contain at least three characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["X-API-Key"] = self._api_key
        try:
            response = self._client.post(
                self._endpoint,
                headers=headers,
                json={
                    "query": text.strip(),
                    "mode": self._mode,
                    "chunk_top_k": limit,
                    "include_references": True,
                    "include_chunk_content": False,
                },
            )
        except httpx.RequestError as exc:
            raise LightRAGProviderError(
                f"LightRAG query failed ({type(exc).__name__})"
            ) from None
        if not response.is_success:
            raise LightRAGProviderError(
                f"LightRAG query failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise LightRAGResponseError(
                "LightRAG query returned invalid JSON"
            ) from None
        if not isinstance(payload, Mapping) or payload.get("status") != "success":
            raise LightRAGResponseError("LightRAG query did not succeed")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise LightRAGResponseError("LightRAG query data must be an object")
        chunks = data.get("chunks")
        if not isinstance(chunks, list):
            raise LightRAGResponseError("LightRAG query chunks must be a list")
        candidates: list[LightRAGCandidate] = []
        seen: set[str] = set()
        for rank, item in enumerate(chunks, start=1):
            if not isinstance(item, Mapping):
                continue
            chunk_id = _chunk_id_from_document_path(
                item.get("file_path"),
                project_id=self._project_id,
            )
            if chunk_id is None or chunk_id in seen:
                continue
            seen.add(chunk_id)
            candidates.append(
                LightRAGCandidate(
                    chunk_id=chunk_id,
                    score=1.0 / rank,
                )
            )
            if len(candidates) >= limit:
                break
        return tuple(candidates)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class LightRAGKnowledgeRetriever:
    """Resolve LightRAG candidates through the authoritative publication gate."""

    def __init__(
        self,
        engine: Engine,
        provider: LightRAGCandidateProvider,
        *,
        candidate_multiplier: int = 4,
    ) -> None:
        if (
            isinstance(candidate_multiplier, bool)
            or not isinstance(candidate_multiplier, int)
            or candidate_multiplier <= 0
        ):
            raise ValueError("candidate_multiplier must be positive")
        self._engine = engine
        self._provider = provider
        self._candidate_multiplier = candidate_multiplier

    def retrieve(self, query: RetrievalQuery) -> tuple[RetrievalHit, ...]:
        if query.filters:
            raise ValueError(
                "metadata filters are not supported by the experimental "
                "LightRAG retriever"
            )
        raw_candidates = tuple(
            self._provider.search(
                project_id=query.project_id,
                text=query.text,
                limit=query.limit * self._candidate_multiplier,
            )
        )
        candidates: list[LightRAGCandidate] = []
        seen: set[str] = set()
        for candidate in raw_candidates:
            if candidate.chunk_id in seen:
                continue
            seen.add(candidate.chunk_id)
            candidates.append(candidate)
        if not candidates:
            return ()
        current = (
            knowledge_chunks.join(
                knowledge_sources,
                sa.and_(
                    knowledge_sources.c.project_id
                    == knowledge_chunks.c.project_id,
                    knowledge_sources.c.source_id
                    == knowledge_chunks.c.source_id,
                    knowledge_sources.c.current_snapshot_id
                    == knowledge_chunks.c.snapshot_id,
                ),
            )
            .join(
                source_snapshots,
                sa.and_(
                    source_snapshots.c.project_id
                    == knowledge_chunks.c.project_id,
                    source_snapshots.c.source_id
                    == knowledge_chunks.c.source_id,
                    source_snapshots.c.snapshot_id
                    == knowledge_chunks.c.snapshot_id,
                ),
            )
        )
        statement = (
            sa.select(
                knowledge_chunks.c.project_id,
                knowledge_chunks.c.chunk_id,
                knowledge_chunks.c.source_id,
                knowledge_chunks.c.snapshot_id,
                knowledge_chunks.c.ordinal,
                knowledge_chunks.c.heading_path,
                knowledge_chunks.c.text,
                knowledge_chunks.c.locator,
                knowledge_chunks.c.metadata,
                knowledge_sources.c.display_name.label("source_display_name"),
                knowledge_sources.c.source_kind,
                knowledge_sources.c.trust_tier,
                knowledge_sources.c.public_source,
                knowledge_sources.c.canonical_url,
                source_snapshots.c.fetched_at,
            )
            .select_from(current)
            .where(
                knowledge_chunks.c.project_id == query.project_id,
                knowledge_sources.c.status == "published",
                knowledge_chunks.c.chunk_id.in_(
                    [candidate.chunk_id for candidate in candidates]
                ),
            )
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        rows_by_id = {str(row["chunk_id"]): row for row in rows}
        hits: list[RetrievalHit] = []
        for candidate_rank, candidate in enumerate(candidates, start=1):
            row = rows_by_id.get(candidate.chunk_id)
            if row is None:
                continue
            hits.append(
                RetrievalHit(
                    chunk=_chunk_from_row(row),
                    score=candidate.score,
                    provenance=RetrievalProvenance(
                        project_id=str(row["project_id"]),
                        source_id=str(row["source_id"]),
                        snapshot_id=str(row["snapshot_id"]),
                        display_name=str(row["source_display_name"]),
                        source_kind=str(row["source_kind"]),  # type: ignore[arg-type]
                        trust_tier=str(row["trust_tier"]),  # type: ignore[arg-type]
                        public_source=bool(row["public_source"]),
                        canonical_url=(
                            str(row["canonical_url"])
                            if row["canonical_url"] is not None
                            else None
                        ),
                        fetched_at=row["fetched_at"],  # type: ignore[arg-type]
                    ),
                    explanation={
                        "method": "lightrag_candidate_postgres_gate",
                        "candidate_rank": candidate_rank,
                        "candidate_score": candidate.score,
                    },
                )
            )
            if len(hits) >= query.limit:
                break
        return tuple(hits)


__all__ = [
    "LightRAGCandidate",
    "LightRAGCandidateProvider",
    "LightRAGHttpCandidateProvider",
    "LightRAGKnowledgeRetriever",
    "LightRAGProviderError",
    "LightRAGResponseError",
    "lightrag_document_path",
]
