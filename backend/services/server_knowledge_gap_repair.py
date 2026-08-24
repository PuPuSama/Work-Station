from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from knowledge_agent.contracts import RetrievalPlan, RetrievalScope
from knowledge_agent.evidence_repository import (
    EvidenceConflictError,
    PostgresRetrievalPlanRepository,
)
from knowledge_agent.schema import (
    evidence_pack_hits,
    evidence_packs,
    evidence_links,
    knowledge_chunks,
    knowledge_sources,
    research_graph_runs,
)
from models import TaskRecord
from services.job_queue import JobConflict
from services.server_knowledge_coverage import (
    HARD_FACT_PATTERN,
    current_article_for_coverage,
    extract_article_sentence_stream,
    sentence_content_hash,
)


MAX_TARGETED_GAPS = 12
MAX_TARGETED_QUERY_LENGTH = 500
MAX_TARGETED_QUERIES_PER_SCOPE = 20
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{3,}")
SELECTION_MARKERS = frozenset(
    {
        "choose",
        "select",
        "selection",
        "compare",
        "comparison",
        "suitable",
        "suitability",
        "procurement",
        "buying",
        "purchase",
    }
)
APPLICATION_MARKERS = frozenset(
    {
        "install",
        "installation",
        "application",
        "project",
        "worksite",
        "environment",
        "maintenance",
        "operate",
        "operation",
    }
)


@dataclass(frozen=True, slots=True)
class KnowledgeGap:
    gap_id: str
    sentence_id: str
    sentence_hash: str
    text: str
    claim_type: str
    hard_fact: bool
    scope_id: str
    scope_title: str
    product_id: str = ""
    reason: str = ""
    requirement_ids: tuple[str, ...] = ()
    h3_titles: tuple[str, ...] = ()
    query_variants: tuple[str, ...] = ()
    article_brief_id: str = ""
    knowledge_snapshot_fingerprint: str = ""

    @property
    def query(self) -> str:
        return (
            self.query_variants[0]
            if self.query_variants
            else self.text[:MAX_TARGETED_QUERY_LENGTH]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "gap_id": self.gap_id,
            "sentence_id": self.sentence_id,
            "sentence_hash": self.sentence_hash,
            "text": self.text,
            "claim_type": self.claim_type,
            "hard_fact": self.hard_fact,
            "scope_id": self.scope_id,
            "scope_title": self.scope_title,
            "product_id": self.product_id,
            "reason": self.reason,
            "query": self.query,
            "requirement_ids": list(self.requirement_ids),
            "h3_titles": list(self.h3_titles),
            "query_variants": list(self.query_variants),
            "article_brief_id": self.article_brief_id,
            "knowledge_snapshot_fingerprint": self.knowledge_snapshot_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class TargetedGapRepairResult:
    plan: RetrievalPlan
    gaps: tuple[KnowledgeGap, ...]
    targeted_scope_ids: tuple[str, ...]
    carried_evidence_pack_ids: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "retrieval_plan_id": self.plan.retrieval_plan_id,
            "gaps": [gap.to_mapping() for gap in self.gaps],
            "targeted_scope_ids": list(self.targeted_scope_ids),
            "carried_evidence_pack_ids": list(
                self.carried_evidence_pack_ids
            ),
        }


def _tokens(value: str) -> set[str]:
    return set(TOKEN_PATTERN.findall(value.casefold()))


def _claim_type(text: str, *, hard_fact: bool) -> str:
    if hard_fact:
        return "hard_fact"
    words = _tokens(text)
    if words & SELECTION_MARKERS:
        return "selection_logic"
    if words & APPLICATION_MARKERS:
        return "application"
    return "reference"


def _scope_metadata(scope: RetrievalScope) -> dict[str, object]:
    return dict(scope.metadata) if isinstance(scope.metadata, Mapping) else {}


def _scope_h3_text(scope: RetrievalScope) -> str:
    metadata = _scope_metadata(scope)
    values: list[str] = []
    raw_h3 = metadata.get("h3_titles") or []
    if isinstance(raw_h3, Sequence) and not isinstance(raw_h3, (str, bytes)):
        values.extend(str(value) for value in raw_h3)
    raw_requirements = metadata.get("claim_requirements") or []
    if isinstance(raw_requirements, Sequence) and not isinstance(
        raw_requirements,
        (str, bytes),
    ):
        for item in raw_requirements:
            if isinstance(item, Mapping):
                values.append(str(item.get("h3_title") or ""))
    return " ".join(value.strip() for value in values if value.strip())


def _scope_requirements(scope: RetrievalScope) -> tuple[Mapping[str, object], ...]:
    raw_requirements = _scope_metadata(scope).get("claim_requirements") or []
    if not isinstance(raw_requirements, Sequence) or isinstance(
        raw_requirements,
        (str, bytes),
    ):
        return ()
    return tuple(
        dict(item)
        for item in raw_requirements
        if isinstance(item, Mapping)
    )


def _requirement_context(
    sentence_text: str,
    scope: RetrievalScope,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Bind a coverage gap to the closest immutable H3 requirement."""

    requirements = _scope_requirements(scope)
    if not requirements:
        return (), (), ()
    sentence_tokens = _tokens(sentence_text)
    ranked: list[tuple[float, int, Mapping[str, object]]] = []
    for index, requirement in enumerate(requirements):
        h3_title = str(requirement.get("h3_title") or "").strip()
        raw_queries = requirement.get("query_variants") or []
        query_values = (
            tuple(
                str(value).strip()
                for value in raw_queries
                if str(value).strip()
            )
            if isinstance(raw_queries, Sequence)
            and not isinstance(raw_queries, (str, bytes))
            else ()
        )
        context_tokens = _tokens(" ".join((h3_title, *query_values)))
        ranked.append(
            (
                float(len(sentence_tokens & context_tokens)),
                index,
                requirement,
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    best_score = ranked[0][0]
    selected = [
        item[2]
        for item in ranked
        if item[0] == best_score and (best_score > 0 or item[1] == 0)
    ]
    requirement_ids = tuple(
        str(item.get("requirement_id") or "").strip()
        for item in selected
        if str(item.get("requirement_id") or "").strip()
    )
    h3_titles = tuple(
        str(item.get("h3_title") or "").strip()
        for item in selected
        if str(item.get("h3_title") or "").strip()
    )
    query_variants = tuple(
        dict.fromkeys(
            str(value).strip()
            for item in selected
            for value in (item.get("query_variants") or [])
            if str(value).strip()
        )
    )[:6]
    return requirement_ids, h3_titles, query_variants


def _targeted_query_variants(
    *,
    sentence_text: str,
    scope: RetrievalScope,
    requirement_queries: Sequence[str],
    task: TaskRecord,
) -> tuple[str, ...]:
    brief = task.article_brief
    brief_context = (
        *(brief.required_capabilities[:2] if brief is not None else ()),
        *(brief.selection_dimensions[:2] if brief is not None else ()),
    )
    values = (
        sentence_text,
        f"{scope.title} {sentence_text}",
        *requirement_queries,
        *brief_context,
    )
    return tuple(
        dict.fromkeys(
            value.strip()[:MAX_TARGETED_QUERY_LENGTH]
            for value in values
            if value and value.strip()
        )
    )[:8]


def _product_id_for_scope(scope: RetrievalScope) -> str:
    return str(_scope_metadata(scope).get("product_id") or "").strip()


def _choose_scope(
    sentence_text: str,
    *,
    task: TaskRecord,
    scopes: Sequence[RetrievalScope],
) -> RetrievalScope:
    normalized_sentence = sentence_text.casefold()
    product_scopes = [
        scope for scope in scopes if scope.scope_type == "product_fact"
    ]
    for product, scope in zip(task.products, product_scopes, strict=False):
        name = product.name.strip()
        if name and name.casefold() in normalized_sentence:
            return scope

    sentence_tokens = _tokens(sentence_text)
    candidates = [
        scope for scope in scopes if scope.scope_type != "product_fact"
    ] or list(scopes)
    ranked: list[tuple[float, str, RetrievalScope]] = []
    for scope in candidates:
        scope_text = " ".join(
            (scope.title, _scope_h3_text(scope), *scope.query_variants[:4])
        )
        overlap = len(sentence_tokens & _tokens(scope_text))
        title_overlap = len(sentence_tokens & _tokens(scope.title))
        ranked.append(
            (
                float(overlap * 1.5 + title_overlap * 2),
                scope.scope_id,
                scope,
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][2]


def _gap_reason(*, hard_fact: bool, claim_type: str) -> str:
    if hard_fact:
        return "hard-fact sentence has no current hard-fact evidence"
    if claim_type == "selection_logic":
        return "selection judgment has no current requirement-level support"
    if claim_type == "application":
        return "application statement has no current project support"
    return "sentence has no current project evidence link"


class ServerKnowledgeGapRepairService:
    """Create a narrow immutable repair Plan from sentence coverage gaps."""

    def __init__(
        self,
        engine: Engine,
        *,
        plans: PostgresRetrievalPlanRepository | None = None,
    ) -> None:
        self._engine = engine
        self._plans = plans or PostgresRetrievalPlanRepository(engine)

    def _coverage_sentence_ids(
        self,
        *,
        project_id: str,
        task: TaskRecord,
    ) -> tuple[set[str], set[str]]:
        article = current_article_for_coverage(task)
        sentences = extract_article_sentence_stream(article)
        current_hash = sentence_content_hash(
            tuple(sentence for sentence in sentences if sentence.eligible)
        )
        coverage = task.knowledge_coverage
        if (
            coverage.status != "available"
            or coverage.content_hash != current_hash
        ):
            raise JobConflict(
                "recheck knowledge coverage before targeted repair"
            )
        article_id = f"topic_{task.topic_index:03d}"
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    evidence_links.c.sentence_id,
                    evidence_links.c.claim_type,
                ).where(
                    evidence_links.c.project_id == project_id,
                    evidence_links.c.article_id == article_id,
                    evidence_links.c.support_scope == "sentence",
                    evidence_links.c.validation_status == "valid",
                )
            ).mappings().all()
        supported = {
            str(row["sentence_id"])
            for row in rows
            if row["sentence_id"] is not None
        }
        hard_fact_supported = {
            str(row["sentence_id"])
            for row in rows
            if row["sentence_id"] is not None
            and str(row["claim_type"] or "") == "hard_fact"
        }
        return supported, hard_fact_supported

    def list_gaps(
        self,
        *,
        project_id: str,
        task: TaskRecord,
        plan: RetrievalPlan,
        sentence_ids: Sequence[str] = (),
    ) -> tuple[KnowledgeGap, ...]:
        article = current_article_for_coverage(task)
        sentences = tuple(
            sentence for sentence in extract_article_sentence_stream(article)
            if sentence.eligible
        )
        supported, hard_fact_supported = self._coverage_sentence_ids(
            project_id=project_id,
            task=task,
        )
        requested = tuple(dict.fromkeys(str(value).strip() for value in sentence_ids))
        sentence_by_id = {sentence.sentence_id: sentence for sentence in sentences}
        if any(value not in sentence_by_id for value in requested):
            raise JobConflict("targeted repair sentence identity is invalid")
        selected = (
            tuple(sentence for sentence in sentences if sentence.sentence_id in requested)
            if requested
            else sentences
        )
        gaps: list[KnowledgeGap] = []
        for sentence in selected:
            hard_fact = bool(HARD_FACT_PATTERN.search(sentence.text))
            is_supported = sentence.sentence_id in supported
            if hard_fact:
                is_supported = sentence.sentence_id in hard_fact_supported
            if is_supported:
                continue
            scope = _choose_scope(
                sentence.text,
                task=task,
                scopes=plan.scopes,
            )
            claim_type = _claim_type(sentence.text, hard_fact=hard_fact)
            requirement_ids, h3_titles, requirement_queries = (
                _requirement_context(sentence.text, scope)
            )
            query_variants = _targeted_query_variants(
                sentence_text=sentence.text,
                scope=scope,
                requirement_queries=requirement_queries,
                task=task,
            )
            digest = hashlib.sha256(
                "\n".join(
                    (
                        plan.retrieval_plan_id,
                        task.knowledge_coverage.content_hash,
                        sentence.sentence_id,
                    )
                ).encode("utf-8")
            ).hexdigest()[:24]
            gaps.append(
                KnowledgeGap(
                    gap_id=f"gap_{digest}",
                    sentence_id=sentence.sentence_id,
                    sentence_hash=sentence.sentence_hash,
                    text=sentence.text,
                    claim_type=claim_type,
                    hard_fact=hard_fact,
                    scope_id=scope.scope_id,
                    scope_title=scope.title,
                    product_id=_product_id_for_scope(scope),
                    reason=_gap_reason(
                        hard_fact=hard_fact,
                        claim_type=claim_type,
                    ),
                    requirement_ids=requirement_ids,
                    h3_titles=h3_titles,
                    query_variants=query_variants,
                    article_brief_id=(
                        task.article_brief.brief_id
                        if task.article_brief is not None
                        else ""
                    ),
                    knowledge_snapshot_fingerprint=(
                        task.article_brief.knowledge_snapshot_fingerprint
                        if task.article_brief is not None
                        else ""
                    ),
                )
            )
        if not gaps:
            raise JobConflict("no unsupported sentences require targeted repair")
        if len(gaps) > MAX_TARGETED_GAPS:
            raise JobConflict(
                "targeted repair is limited to 12 sentences; select a smaller set"
            )
        return tuple(gaps)

    @staticmethod
    def _latest_pack_ids(
        connection: Connection,
        *,
        project_id: str,
        task: TaskRecord,
        plan: RetrievalPlan,
    ) -> tuple[str, ...]:
        outline_hash = hashlib.sha256(
            task.outline.strip().encode("utf-8")
        ).hexdigest()
        rows = connection.execute(
            sa.select(
                research_graph_runs.c.evidence_pack_ids,
                research_graph_runs.c.metadata,
            )
            .where(
                research_graph_runs.c.project_id == project_id,
                research_graph_runs.c.retrieval_plan_id
                == plan.retrieval_plan_id,
                research_graph_runs.c.article_id == plan.article_id,
                research_graph_runs.c.outline_version == plan.outline_version,
                research_graph_runs.c.status.in_(
                    ("completed", "completed_with_warnings")
                ),
            )
            .order_by(
                research_graph_runs.c.finished_at.desc(),
                research_graph_runs.c.created_at.desc(),
                research_graph_runs.c.thread_id.desc(),
            )
        ).mappings()
        for row in rows:
            metadata = dict(row.get("metadata") or {})
            if str(metadata.get("task_id") or "") != task.id:
                continue
            stored_hash = str(metadata.get("outline_hash") or "").strip()
            if stored_hash and stored_hash != outline_hash:
                continue
            return tuple(str(value) for value in row["evidence_pack_ids"])
        raise JobConflict(
            "complete research evidence is required before targeted repair"
        )

    @staticmethod
    def _copyable_packs(
        connection: Connection,
        *,
        project_id: str,
        plan: RetrievalPlan,
        pack_ids: Sequence[str],
        targeted_scope_ids: set[str],
    ) -> dict[str, str]:
        if not pack_ids:
            return {}
        pack_rows = connection.execute(
            sa.select(
                evidence_packs.c.evidence_pack_id,
                evidence_packs.c.scope_id,
                evidence_packs.c.sufficiency,
            ).where(
                evidence_packs.c.project_id == project_id,
                evidence_packs.c.retrieval_plan_id == plan.retrieval_plan_id,
                evidence_packs.c.evidence_pack_id.in_(tuple(pack_ids)),
            )
        ).mappings().all()
        by_pack = {str(row["evidence_pack_id"]): row for row in pack_rows}
        hit_rows = connection.execute(
            sa.select(
                evidence_pack_hits.c.evidence_pack_id,
                evidence_pack_hits.c.chunk_id,
            )
            .where(
                evidence_pack_hits.c.project_id == project_id,
                evidence_pack_hits.c.evidence_pack_id.in_(tuple(pack_ids)),
            )
        ).mappings().all()
        hit_ids: dict[str, set[str]] = {pack_id: set() for pack_id in pack_ids}
        for row in hit_rows:
            hit_ids[str(row["evidence_pack_id"])].add(str(row["chunk_id"]))
        valid_rows = connection.execute(
            sa.select(
                evidence_pack_hits.c.evidence_pack_id,
                evidence_pack_hits.c.chunk_id,
            )
            .select_from(
                evidence_pack_hits.join(
                    knowledge_chunks,
                    sa.and_(
                        knowledge_chunks.c.project_id
                        == evidence_pack_hits.c.project_id,
                        knowledge_chunks.c.chunk_id == evidence_pack_hits.c.chunk_id,
                    ),
                ).join(
                    knowledge_sources,
                    sa.and_(
                        knowledge_sources.c.project_id
                        == knowledge_chunks.c.project_id,
                        knowledge_sources.c.source_id == knowledge_chunks.c.source_id,
                        knowledge_sources.c.current_snapshot_id
                        == knowledge_chunks.c.snapshot_id,
                    ),
                )
            )
            .where(
                evidence_pack_hits.c.project_id == project_id,
                evidence_pack_hits.c.evidence_pack_id.in_(tuple(pack_ids)),
                knowledge_sources.c.status == "published",
                knowledge_sources.c.source_kind != "official_blog",
            )
        ).mappings().all()
        valid_ids: dict[str, set[str]] = {pack_id: set() for pack_id in pack_ids}
        for row in valid_rows:
            valid_ids[str(row["evidence_pack_id"])].add(str(row["chunk_id"]))
        result: dict[str, str] = {}
        for old_id, row in by_pack.items():
            scope_id = str(row["scope_id"])
            if scope_id in targeted_scope_ids:
                continue
            if str(row["sufficiency"]) != "sufficient":
                continue
            if hit_ids.get(old_id, set()) != valid_ids.get(old_id, set()):
                continue
            result[old_id] = old_id
        return result

    @staticmethod
    def _copy_packs_in_transaction(
        connection: Connection,
        *,
        project_id: str,
        old_to_new: Mapping[str, str],
        new_plan: RetrievalPlan,
    ) -> None:
        if not old_to_new:
            return
        old_ids = tuple(old_to_new)
        rows = connection.execute(
            sa.select(evidence_packs).where(
                evidence_packs.c.project_id == project_id,
                evidence_packs.c.evidence_pack_id.in_(old_ids),
            )
        ).mappings().all()
        for row in rows:
            old_id = str(row["evidence_pack_id"])
            new_id = old_to_new[old_id]
            connection.execute(
                evidence_packs.insert().values(
                    project_id=project_id,
                    evidence_pack_id=new_id,
                    retrieval_plan_id=new_plan.retrieval_plan_id,
                    scope_id=str(row["scope_id"]),
                    article_id=new_plan.article_id,
                    outline_version=new_plan.outline_version,
                    sufficiency=str(row["sufficiency"]),
                    gap_reasons=list(row["gap_reasons"] or []),
                    hard_fact_chunk_ids=list(row["hard_fact_chunk_ids"] or []),
                    public_citation_urls=list(
                        row["public_citation_urls"] or []
                    ),
                    created_at=datetime.now(timezone.utc),
                )
            )
            hit_rows = connection.execute(
                sa.select(evidence_pack_hits).where(
                    evidence_pack_hits.c.project_id == project_id,
                    evidence_pack_hits.c.evidence_pack_id == old_id,
                )
            ).mappings().all()
            if hit_rows:
                connection.execute(
                    evidence_pack_hits.insert(),
                    [
                        {
                            "project_id": project_id,
                            "evidence_pack_id": new_id,
                            "chunk_id": str(hit["chunk_id"]),
                            "rank": int(hit["rank"]),
                            "score": float(hit["score"]),
                            "provenance": dict(hit["provenance"] or {}),
                            "explanation": dict(hit["explanation"] or {}),
                        }
                        for hit in hit_rows
                    ],
                )

    def create_in_transaction(
        self,
        connection: Connection,
        *,
        project_id: str,
        task: TaskRecord,
        base_plan: RetrievalPlan,
        sentence_ids: Sequence[str] = (),
    ) -> TargetedGapRepairResult:
        gaps = self.list_gaps(
            project_id=project_id,
            task=task,
            plan=base_plan,
            sentence_ids=sentence_ids,
        )
        initial_targeted_scope_ids = {gap.scope_id for gap in gaps}
        old_pack_ids = self._latest_pack_ids(
            connection,
            project_id=project_id,
            task=task,
            plan=base_plan,
        )
        copyable = self._copyable_packs(
            connection,
            project_id=project_id,
            plan=base_plan,
            pack_ids=old_pack_ids,
            targeted_scope_ids=initial_targeted_scope_ids,
        )
        copied_pack_rows = connection.execute(
            sa.select(
                evidence_packs.c.evidence_pack_id,
                evidence_packs.c.scope_id,
            ).where(
                evidence_packs.c.project_id == project_id,
                evidence_packs.c.evidence_pack_id.in_(tuple(copyable)),
            )
        ).mappings().all()
        scope_to_pack = {
            str(row["scope_id"]): str(row["evidence_pack_id"])
            for row in copied_pack_rows
        }
        targeted_scope_ids = set(initial_targeted_scope_ids)
        targeted_scope_ids.update(
            scope.scope_id
            for scope in base_plan.scopes
            if scope.scope_id not in scope_to_pack
        )
        digest_payload = {
            "base_plan": base_plan.retrieval_plan_id,
            "coverage": task.knowledge_coverage.content_hash,
            "gaps": [gap.gap_id for gap in gaps],
            "targeted_scopes": sorted(targeted_scope_ids),
            "repair_schema_version": 2,
        }
        digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:20]
        new_plan_id = f"plan-{base_plan.article_id}-repair-{digest}"
        old_to_new = {
            old_id: "ep_repair_"
            + hashlib.sha256(
                f"{new_plan_id}:{old_id}".encode("utf-8")
            ).hexdigest()[:32]
            for old_id in copyable.values()
        }
        carried_ids = tuple(old_to_new.values())
        gaps_by_scope: dict[str, list[KnowledgeGap]] = {}
        for gap in gaps:
            gaps_by_scope.setdefault(gap.scope_id, []).append(gap)
        new_scopes: list[RetrievalScope] = []
        for scope in base_plan.scopes:
            metadata = _scope_metadata(scope)
            scope_gaps = gaps_by_scope.get(scope.scope_id, [])
            if scope.scope_id in targeted_scope_ids:
                targeted_queries = tuple(
                    query
                    for gap in scope_gaps
                    for query in gap.query_variants
                )
                if not targeted_queries:
                    targeted_queries = tuple(gap.query for gap in scope_gaps)
                metadata.update(
                    {
                        "targeted_repair": True,
                        "targeted_gap_ids": [gap.gap_id for gap in scope_gaps],
                        "targeted_requirement_ids": [
                            requirement_id
                            for gap in scope_gaps
                            for requirement_id in gap.requirement_ids
                        ],
                    }
                )
                query_variants = tuple(
                    dict.fromkeys(
                        (*targeted_queries, *scope.query_variants)
                    )
                )[:MAX_TARGETED_QUERIES_PER_SCOPE]
            else:
                query_variants = scope.query_variants
            new_scopes.append(
                replace(
                    scope,
                    retrieval_plan_id=new_plan_id,
                    query_variants=query_variants,
                    metadata=metadata,
                )
            )
        metadata = dict(base_plan.metadata)
        metadata.update(
            {
                "generated_from": "confirmed_task_outline",
                "repair_type": "targeted_knowledge_gap",
                "repair_of_plan_id": base_plan.retrieval_plan_id,
                "targeted_scope_ids": sorted(targeted_scope_ids),
                "targeted_gap_ids": [gap.gap_id for gap in gaps],
                "coverage_content_hash": task.knowledge_coverage.content_hash,
                "carried_evidence_pack_ids": list(carried_ids),
                "repair_digest": digest,
                "repair_schema_version": 2,
                "article_brief_id": (
                    task.article_brief.brief_id
                    if task.article_brief is not None
                    else ""
                ),
                "knowledge_snapshot_fingerprint": (
                    task.article_brief.knowledge_snapshot_fingerprint
                    if task.article_brief is not None
                    else ""
                ),
                "targeted_gaps": [gap.to_mapping() for gap in gaps],
            }
        )
        plan = RetrievalPlan(
            project_id=base_plan.project_id,
            retrieval_plan_id=new_plan_id,
            article_id=base_plan.article_id,
            outline_version=base_plan.outline_version,
            scopes=tuple(new_scopes),
            max_gap_fill_rounds=base_plan.max_gap_fill_rounds,
            metadata=metadata,
            created_at=datetime.now(timezone.utc),
        )
        existing = self._plans.get_retrieval_plan_in_transaction(
            connection,
            project_id,
            new_plan_id,
        )
        if existing is not None:
            return TargetedGapRepairResult(
                plan=existing,
                gaps=gaps,
                targeted_scope_ids=tuple(
                    str(value)
                    for value in existing.metadata.get("targeted_scope_ids", ())
                ),
                carried_evidence_pack_ids=tuple(
                    str(value)
                    for value in existing.metadata.get(
                        "carried_evidence_pack_ids",
                        (),
                    )
                ),
            )
        try:
            persisted = self._plans.save_retrieval_plan_in_transaction(
                connection,
                plan,
            )
        except EvidenceConflictError as exc:
            raise JobConflict("targeted repair Plan identity changed") from exc
        self._copy_packs_in_transaction(
            connection,
            project_id=project_id,
            old_to_new=old_to_new,
            new_plan=persisted,
        )
        return TargetedGapRepairResult(
            plan=persisted,
            gaps=gaps,
            targeted_scope_ids=tuple(sorted(targeted_scope_ids)),
            carried_evidence_pack_ids=carried_ids,
        )


__all__ = [
    "KnowledgeGap",
    "MAX_TARGETED_GAPS",
    "ServerKnowledgeGapRepairService",
    "TargetedGapRepairResult",
]
