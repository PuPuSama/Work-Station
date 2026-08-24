from __future__ import annotations

from datetime import datetime
from typing import Mapping

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from .contracts import (
    EvidenceLink,
    EvidencePack,
    EvidencePackRequest,
    KnowledgeChunk,
    RetrievalHit,
    RetrievalPlan,
    RetrievalProvenance,
    RetrievalScope,
)
from .schema import (
    evidence_links,
    evidence_pack_hits,
    evidence_packs,
    knowledge_chunks,
    knowledge_sources,
    retrieval_plans,
    retrieval_scopes,
)


class EvidenceRepositoryError(RuntimeError):
    """Base error for formal M3 evidence persistence."""


class EvidenceConflictError(EvidenceRepositoryError):
    """Raised when an immutable evidence identity is retried with new content."""


class EvidenceTargetError(EvidenceRepositoryError):
    """Raised when evidence points outside the active published knowledge set."""


def _chunk_from_row(row: Mapping[str, object] | RowMapping) -> KnowledgeChunk:
    return KnowledgeChunk(
        project_id=str(row["project_id"]),
        chunk_id=str(row["chunk_id"]),
        source_id=str(row["source_id"]),
        snapshot_id=str(row["snapshot_id"]),
        text=str(row["text"]),
        ordinal=int(row["ordinal"]),
        heading_path=tuple(row["heading_path"] or ()),  # type: ignore[arg-type]
        locator=dict(row["locator"] or {}),  # type: ignore[arg-type]
        metadata=dict(row["chunk_metadata"] or {}),  # type: ignore[arg-type]
    )


def _provenance_json(provenance: RetrievalProvenance | None) -> dict[str, object]:
    if provenance is None:
        return {}
    return {
        "project_id": provenance.project_id,
        "source_id": provenance.source_id,
        "snapshot_id": provenance.snapshot_id,
        "display_name": provenance.display_name,
        "source_kind": provenance.source_kind,
        "trust_tier": provenance.trust_tier,
        "public_source": provenance.public_source,
        "canonical_url": provenance.canonical_url,
        "fetched_at": (
            provenance.fetched_at.isoformat()
            if provenance.fetched_at is not None
            else None
        ),
    }


def _provenance_from_json(value: object) -> RetrievalProvenance | None:
    if not value:
        return None
    payload = dict(value)  # type: ignore[arg-type]
    fetched_at = payload.get("fetched_at")
    return RetrievalProvenance(
        project_id=str(payload["project_id"]),
        source_id=str(payload["source_id"]),
        snapshot_id=str(payload["snapshot_id"]),
        display_name=str(payload["display_name"]),
        source_kind=str(payload["source_kind"]),  # type: ignore[arg-type]
        trust_tier=str(payload["trust_tier"]),  # type: ignore[arg-type]
        public_source=bool(payload["public_source"]),
        canonical_url=(
            str(payload["canonical_url"])
            if payload.get("canonical_url") is not None
            else None
        ),
        fetched_at=(
            datetime.fromisoformat(str(fetched_at)) if fetched_at is not None else None
        ),
    )


def _plan_signature(plan: RetrievalPlan) -> tuple[object, ...]:
    return (
        plan.project_id,
        plan.retrieval_plan_id,
        plan.article_id,
        plan.outline_version,
        plan.max_gap_fill_rounds,
        dict(plan.metadata),
        tuple(plan.scopes),
    )


def _pack_signature(pack: EvidencePack) -> tuple[object, ...]:
    # Scores and explanations are persisted retrieval diagnostics. Provider
    # vectors can vary slightly across an otherwise identical retry, so those
    # fields must not turn the stable ordered chunk identity into a conflict.
    # The remaining fields still reject a changed business evidence outcome.
    hit_identities = tuple(
        (
            hit.chunk.project_id,
            hit.chunk.chunk_id,
            hit.chunk.source_id,
            hit.chunk.snapshot_id,
        )
        for hit in pack.hits
    )
    return (
        pack.evidence_pack_id,
        pack.request,
        hit_identities,
        pack.sufficiency,
        pack.gap_reasons,
        pack.hard_fact_chunk_ids,
        pack.public_citation_urls,
    )


class PostgresRetrievalPlanRepository:
    """Store immutable outline-versioned plans and their ordered scopes."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save_retrieval_plan(self, plan: RetrievalPlan) -> None:
        try:
            with self._engine.begin() as connection:
                self.save_retrieval_plan_in_transaction(connection, plan)
        except IntegrityError as exc:
            raise EvidenceConflictError(
                "retrieval plan violates project or outline identity constraints"
            ) from exc

    def save_retrieval_plan_in_transaction(
        self,
        connection: Connection,
        plan: RetrievalPlan,
    ) -> RetrievalPlan:
        """Persist one immutable plan inside a caller-owned command transaction."""

        existing = self._get(
            connection,
            plan.project_id,
            plan.retrieval_plan_id,
        )
        if existing is not None:
            if _plan_signature(existing) != _plan_signature(plan):
                raise EvidenceConflictError(
                    "retrieval plan identity already has different content"
                )
            return existing
        connection.execute(
            retrieval_plans.insert().values(
                project_id=plan.project_id,
                retrieval_plan_id=plan.retrieval_plan_id,
                article_id=plan.article_id,
                outline_version=plan.outline_version,
                max_gap_fill_rounds=plan.max_gap_fill_rounds,
                metadata=dict(plan.metadata),
                created_at=plan.created_at,
            )
        )
        connection.execute(
            retrieval_scopes.insert(),
            [
                {
                    "project_id": scope.project_id,
                    "retrieval_plan_id": scope.retrieval_plan_id,
                    "scope_id": scope.scope_id,
                    "ordinal": scope.ordinal,
                    "scope_type": scope.scope_type,
                    "scope_key": scope.scope_key,
                    "title": scope.title,
                    "query_variants": list(scope.query_variants),
                    "filters": dict(scope.filters),
                    "minimum_hits": scope.minimum_hits,
                    "minimum_distinct_sources": scope.minimum_distinct_sources,
                    "require_hard_fact": scope.require_hard_fact,
                    "metadata": dict(scope.metadata),
                }
                for scope in plan.scopes
            ],
        )
        return plan

    def get_retrieval_plan(
        self,
        project_id: str,
        retrieval_plan_id: str,
    ) -> RetrievalPlan | None:
        with self._engine.connect() as connection:
            return self._get(connection, project_id, retrieval_plan_id)

    def get_retrieval_plan_in_transaction(
        self,
        connection: Connection,
        project_id: str,
        retrieval_plan_id: str,
    ) -> RetrievalPlan | None:
        """Read an immutable plan through the caller's transaction snapshot."""

        return self._get(connection, project_id, retrieval_plan_id)

    def list_retrieval_plans(
        self,
        project_id: str,
        *,
        article_id: str | None = None,
        limit: int = 100,
    ) -> tuple[RetrievalPlan, ...]:
        """List newest immutable plans without crossing the project boundary."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        statement = sa.select(
            retrieval_plans.c.retrieval_plan_id
        ).where(retrieval_plans.c.project_id == project_id)
        if article_id is not None:
            statement = statement.where(
                retrieval_plans.c.article_id == article_id
            )
        with self._engine.connect() as connection:
            plan_ids = connection.execute(
                statement.order_by(
                    retrieval_plans.c.created_at.desc(),
                    retrieval_plans.c.retrieval_plan_id,
                ).limit(limit)
            ).scalars()
            return tuple(
                plan
                for plan_id in plan_ids
                if (
                    plan := self._get(connection, project_id, str(plan_id))
                )
                is not None
            )

    @staticmethod
    def _get(
        connection: sa.Connection,
        project_id: str,
        retrieval_plan_id: str,
    ) -> RetrievalPlan | None:
        plan_row = connection.execute(
            sa.select(retrieval_plans).where(
                retrieval_plans.c.project_id == project_id,
                retrieval_plans.c.retrieval_plan_id == retrieval_plan_id,
            )
        ).mappings().one_or_none()
        if plan_row is None:
            return None
        scope_rows = connection.execute(
            sa.select(retrieval_scopes)
            .where(
                retrieval_scopes.c.project_id == project_id,
                retrieval_scopes.c.retrieval_plan_id == retrieval_plan_id,
            )
            .order_by(retrieval_scopes.c.ordinal)
        ).mappings()
        scopes = tuple(
            RetrievalScope(
                project_id=str(row["project_id"]),
                retrieval_plan_id=str(row["retrieval_plan_id"]),
                scope_id=str(row["scope_id"]),
                ordinal=int(row["ordinal"]),
                scope_type=str(row["scope_type"]),  # type: ignore[arg-type]
                scope_key=str(row["scope_key"]),
                title=str(row["title"]),
                query_variants=tuple(row["query_variants"]),  # type: ignore[arg-type]
                filters=dict(row["filters"] or {}),  # type: ignore[arg-type]
                minimum_hits=int(row["minimum_hits"]),
                minimum_distinct_sources=int(row["minimum_distinct_sources"]),
                require_hard_fact=bool(row["require_hard_fact"]),
                metadata=dict(row["metadata"] or {}),  # type: ignore[arg-type]
            )
            for row in scope_rows
        )
        return RetrievalPlan(
            project_id=str(plan_row["project_id"]),
            retrieval_plan_id=str(plan_row["retrieval_plan_id"]),
            article_id=str(plan_row["article_id"]),
            outline_version=int(plan_row["outline_version"]),
            scopes=scopes,
            max_gap_fill_rounds=int(plan_row["max_gap_fill_rounds"]),
            metadata=dict(plan_row["metadata"] or {}),  # type: ignore[arg-type]
            created_at=plan_row["created_at"],  # type: ignore[arg-type]
        )


class PostgresEvidencePackRepository:
    """Store immutable evidence packs and the exact ranked chunks they used."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save_evidence_pack(self, evidence_pack: EvidencePack) -> None:
        request = evidence_pack.request
        if any(
            hit.provenance is not None
            and hit.provenance.source_kind == "official_blog"
            for hit in evidence_pack.hits
        ):
            raise EvidenceTargetError(
                "official blog chunks are writing references, not evidence"
            )
        if request.retrieval_plan_id is None or request.scope_id is None:
            raise ValueError(
                "persisted evidence packs require retrieval_plan_id and scope_id"
            )
        try:
            with self._engine.begin() as connection:
                if evidence_pack.hits:
                    hit_chunk_ids = tuple(
                        hit.chunk.chunk_id for hit in evidence_pack.hits
                    )
                    source_kinds = tuple(
                        connection.execute(
                            sa.select(knowledge_sources.c.source_kind)
                            .select_from(
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
                            )
                            .where(
                                knowledge_chunks.c.project_id
                                == evidence_pack.project_id,
                                knowledge_chunks.c.chunk_id.in_(hit_chunk_ids),
                                knowledge_sources.c.status == "published",
                            )
                        ).scalars()
                    )
                    if "official_blog" in source_kinds:
                        raise EvidenceTargetError(
                            "official blog chunks are writing references, not evidence"
                        )
                existing = self._get(
                    connection,
                    evidence_pack.project_id,
                    evidence_pack.evidence_pack_id,
                )
                if existing is not None:
                    if _pack_signature(existing) != _pack_signature(evidence_pack):
                        raise EvidenceConflictError(
                            "evidence pack identity already has different content"
                        )
                    return
                connection.execute(
                    evidence_packs.insert().values(
                        project_id=evidence_pack.project_id,
                        evidence_pack_id=evidence_pack.evidence_pack_id,
                        retrieval_plan_id=request.retrieval_plan_id,
                        scope_id=request.scope_id,
                        article_id=request.article_id,
                        outline_version=request.outline_version,
                        sufficiency=evidence_pack.sufficiency,
                        gap_reasons=list(evidence_pack.gap_reasons),
                        hard_fact_chunk_ids=list(
                            evidence_pack.hard_fact_chunk_ids
                        ),
                        public_citation_urls=list(
                            evidence_pack.public_citation_urls
                        ),
                        created_at=evidence_pack.created_at,
                    )
                )
                if evidence_pack.hits:
                    connection.execute(
                        evidence_pack_hits.insert(),
                        [
                            {
                                "project_id": evidence_pack.project_id,
                                "evidence_pack_id": evidence_pack.evidence_pack_id,
                                "chunk_id": hit.chunk.chunk_id,
                                "rank": rank,
                                "score": hit.score,
                                "provenance": _provenance_json(hit.provenance),
                                "explanation": dict(hit.explanation),
                            }
                            for rank, hit in enumerate(evidence_pack.hits, start=1)
                        ],
                    )
        except IntegrityError as exc:
            raise EvidenceTargetError(
                "evidence pack must match its project, plan, scope, outline, and chunks"
            ) from exc

    def get_evidence_pack(
        self,
        project_id: str,
        evidence_pack_id: str,
    ) -> EvidencePack | None:
        with self._engine.connect() as connection:
            return self._get(connection, project_id, evidence_pack_id)

    @staticmethod
    def _get(
        connection: sa.Connection,
        project_id: str,
        evidence_pack_id: str,
    ) -> EvidencePack | None:
        pack_row = connection.execute(
            sa.select(
                evidence_packs,
                retrieval_scopes.c.scope_type,
                retrieval_scopes.c.scope_key,
                retrieval_scopes.c.query_variants,
            )
            .join(
                retrieval_scopes,
                sa.and_(
                    retrieval_scopes.c.project_id == evidence_packs.c.project_id,
                    retrieval_scopes.c.retrieval_plan_id
                    == evidence_packs.c.retrieval_plan_id,
                    retrieval_scopes.c.scope_id == evidence_packs.c.scope_id,
                ),
            )
            .where(
                evidence_packs.c.project_id == project_id,
                evidence_packs.c.evidence_pack_id == evidence_pack_id,
            )
        ).mappings().one_or_none()
        if pack_row is None:
            return None
        hit_rows = connection.execute(
            sa.select(
                evidence_pack_hits,
                knowledge_chunks.c.source_id,
                knowledge_chunks.c.snapshot_id,
                knowledge_chunks.c.ordinal,
                knowledge_chunks.c.heading_path,
                knowledge_chunks.c.text,
                knowledge_chunks.c.locator,
                knowledge_chunks.c.metadata.label("chunk_metadata"),
            )
            .join(
                knowledge_chunks,
                sa.and_(
                    knowledge_chunks.c.project_id
                    == evidence_pack_hits.c.project_id,
                    knowledge_chunks.c.chunk_id == evidence_pack_hits.c.chunk_id,
                ),
            )
            .where(
                evidence_pack_hits.c.project_id == project_id,
                evidence_pack_hits.c.evidence_pack_id == evidence_pack_id,
            )
            .order_by(evidence_pack_hits.c.rank)
        ).mappings()
        hits = tuple(
            RetrievalHit(
                chunk=_chunk_from_row(row),
                score=float(row["score"]),
                provenance=_provenance_from_json(row["provenance"]),
                explanation=dict(row["explanation"] or {}),  # type: ignore[arg-type]
            )
            for row in hit_rows
        )
        request = EvidencePackRequest(
            project_id=str(pack_row["project_id"]),
            article_id=str(pack_row["article_id"]),
            outline_version=int(pack_row["outline_version"]),
            scope_type=str(pack_row["scope_type"]),
            scope_key=str(pack_row["scope_key"]),
            query_variants=tuple(pack_row["query_variants"]),  # type: ignore[arg-type]
            retrieval_plan_id=str(pack_row["retrieval_plan_id"]),
            scope_id=str(pack_row["scope_id"]),
        )
        return EvidencePack(
            evidence_pack_id=str(pack_row["evidence_pack_id"]),
            request=request,
            hits=hits,
            sufficiency=str(pack_row["sufficiency"]),  # type: ignore[arg-type]
            gap_reasons=tuple(pack_row["gap_reasons"]),  # type: ignore[arg-type]
            hard_fact_chunk_ids=tuple(
                pack_row["hard_fact_chunk_ids"]  # type: ignore[arg-type]
            ),
            public_citation_urls=tuple(
                pack_row["public_citation_urls"]  # type: ignore[arg-type]
            ),
            created_at=pack_row["created_at"],  # type: ignore[arg-type]
        )


class PostgresEvidenceLinkRepository:
    """Persist claim links only when their chunks are currently publishable."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save_evidence_link(self, link: EvidenceLink) -> None:
        with self._engine.begin() as connection:
            source = connection.execute(
                sa.select(
                    knowledge_sources.c.source_kind,
                    knowledge_sources.c.trust_tier,
                    knowledge_sources.c.public_source,
                    knowledge_sources.c.canonical_url,
                )
                .select_from(
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
                )
                .where(
                    knowledge_chunks.c.project_id == link.project_id,
                    knowledge_chunks.c.chunk_id == link.chunk_id,
                    knowledge_sources.c.status == "published",
                )
            ).mappings().one_or_none()
            if source is None:
                raise EvidenceTargetError(
                    "evidence links require a current published chunk"
                )
            if source["source_kind"] == "official_blog":
                raise EvidenceTargetError(
                    "official blog chunks are writing references, not evidence"
                )
            if (
                link.claim_type == "hard_fact"
                and source["trust_tier"] != "hard_fact"
            ):
                raise EvidenceTargetError(
                    "hard-fact evidence requires a hard_fact source"
                )
            expected_citation = (
                str(source["canonical_url"])
                if source["public_source"] and source["canonical_url"] is not None
                else None
            )
            if link.public_citation_url != expected_citation:
                raise EvidenceTargetError(
                    "public_citation_url must match the active source visibility"
                )

            existing = connection.execute(
                sa.select(evidence_links).where(
                    evidence_links.c.project_id == link.project_id,
                    evidence_links.c.evidence_link_id == link.evidence_link_id,
                )
            ).mappings().one_or_none()
            values = {
                "project_id": link.project_id,
                "evidence_link_id": link.evidence_link_id,
                "article_id": link.article_id,
                "paragraph_id": link.paragraph_id,
                "sentence_id": link.sentence_id,
                "paragraph_hash": link.paragraph_hash,
                "chunk_id": link.chunk_id,
                "support_scope": link.support_scope,
                "claim_type": link.claim_type,
                "support_type": link.support_type,
                "visible_words": link.visible_words,
                "public_citation_url": link.public_citation_url,
                "validation_status": link.validation_status,
                "metadata": dict(link.metadata),
            }
            if existing is not None:
                comparable = {
                    key: existing[key]
                    for key in values
                }
                if comparable != values:
                    raise EvidenceConflictError(
                        "evidence link identity already has different content"
                    )
                return
            connection.execute(evidence_links.insert().values(**values))

    def list_evidence_links(
        self,
        project_id: str,
        article_id: str,
    ) -> tuple[EvidenceLink, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(evidence_links)
                .where(
                    evidence_links.c.project_id == project_id,
                    evidence_links.c.article_id == article_id,
                )
                .order_by(evidence_links.c.evidence_link_id)
            ).mappings()
            return tuple(self._link_from_row(row) for row in rows)

    def mark_paragraph_links_for_review(
        self,
        project_id: str,
        article_id: str,
        paragraph_id: str,
        current_paragraph_hash: str,
    ) -> int:
        if len(current_paragraph_hash) != 64:
            raise ValueError("current_paragraph_hash must be a SHA-256 hex digest")
        with self._engine.begin() as connection:
            result = connection.execute(
                evidence_links.update()
                .where(
                    evidence_links.c.project_id == project_id,
                    evidence_links.c.article_id == article_id,
                    evidence_links.c.paragraph_id == paragraph_id,
                    evidence_links.c.paragraph_hash != current_paragraph_hash,
                    evidence_links.c.validation_status == "valid",
                )
                .values(
                    validation_status="needs_review",
                    updated_at=sa.func.now(),
                )
            )
            return int(result.rowcount or 0)

    @staticmethod
    def _link_from_row(row: Mapping[str, object] | RowMapping) -> EvidenceLink:
        return EvidenceLink(
            project_id=str(row["project_id"]),
            evidence_link_id=str(row["evidence_link_id"]),
            article_id=str(row["article_id"]),
            paragraph_id=str(row["paragraph_id"]),
            sentence_id=(
                str(row["sentence_id"]) if row["sentence_id"] is not None else None
            ),
            paragraph_hash=str(row["paragraph_hash"]),
            chunk_id=str(row["chunk_id"]),
            support_scope=str(row["support_scope"]),  # type: ignore[arg-type]
            claim_type=str(row["claim_type"]),  # type: ignore[arg-type]
            support_type=str(row["support_type"]),  # type: ignore[arg-type]
            visible_words=int(row["visible_words"]),
            public_citation_url=(
                str(row["public_citation_url"])
                if row["public_citation_url"] is not None
                else None
            ),
            validation_status=str(row["validation_status"]),  # type: ignore[arg-type]
            metadata=dict(row["metadata"] or {}),  # type: ignore[arg-type]
        )
