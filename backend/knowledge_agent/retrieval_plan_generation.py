from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from hashlib import sha256

from .contracts import EVIDENCE_SOURCE_KINDS, RetrievalPlan, RetrievalScope


_H2_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_H3_PATTERN = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_FAQ_MARKERS = ("faq", "frequently asked", "common questions", "questions")
_EVIDENCE_FILTERS = {"source_kinds": sorted(EVIDENCE_SOURCE_KINDS)}


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (slug or fallback)[:80]


def _queries(*values: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(value.strip() for value in values if value.strip())
    )[:4]


def _outline_sections(outline: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Parse H2/H3 structure without treating the outline as executable input."""

    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for raw_line in outline.splitlines():
        line = raw_line.strip()
        h2 = re.match(r"^##\s+(.+?)\s*$", line)
        if h2:
            current = []
            sections.append((h2.group(1).strip(), current))
            continue
        h3 = re.match(r"^###\s+(.+?)\s*$", line)
        if h3 and current is not None:
            current.append(h3.group(1).strip())
    return tuple((title, tuple(h3s)) for title, h3s in sections)


def _claim_type(h2_title: str, h3_title: str) -> str:
    text = f"{h2_title} {h3_title}".casefold()
    if any(
        marker in text
        for marker in (
            "spec",
            "dimension",
            "material",
            "size",
            "power",
            "capacity",
            "certif",
            "standard",
            "性能",
            "规格",
            "尺寸",
            "材质",
        )
    ):
        return "hard_fact"
    if any(
        marker in text
        for marker in ("choose", "select", "procure", "compare", "选型", "采购")
    ):
        return "selection_logic"
    if any(
        marker in text
        for marker in ("install", "application", "project", "case", "应用", "工程", "案例")
    ):
        return "application"
    return "reference"


def _requirement(
    *,
    requirement_id: str,
    h2_title: str,
    h3_title: str,
    topic: str,
    product_identity: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    required_product_ids = tuple(
        str(product.get("product_id") or "").strip()
        for product in product_identity
        if str(product.get("product_id") or "").strip()
        and str(product.get("name") or "").casefold()
        in f"{h2_title} {h3_title}".casefold()
    )
    claim_type = _claim_type(h2_title, h3_title)
    return {
        "requirement_id": requirement_id,
        "h2_title": h2_title,
        "h3_title": h3_title,
        "claim_type": claim_type,
        "query_variants": list(
            _queries(
                f"{topic} {h2_title} {h3_title}",
                f"{h2_title} {h3_title}",
                f"{topic} {h3_title}",
            )
        ),
        "required_product_ids": list(required_product_ids),
        "require_hard_fact": claim_type == "hard_fact",
        "minimum_support": 1,
    }


def generate_retrieval_plan(
    *,
    project_id: str,
    article_id: str,
    task_id: str,
    outline_version: int,
    outline: str,
    topic: str,
    products: Sequence[Mapping[str, object]] = (),
    article_brief: Mapping[str, object] | None = None,
) -> RetrievalPlan:
    """Convert one confirmed Task outline snapshot into immutable M3 scopes."""

    normalized_outline = outline.strip()
    if not normalized_outline:
        raise ValueError("confirmed outline is required")
    outline_hash = sha256(normalized_outline.encode("utf-8")).hexdigest()
    product_identity: list[dict[str, str]] = []
    for product in products:
        name = str(product.get("name") or "").strip()
        if not name:
            continue
        product_identity.append(
            {
                "product_id": str(product.get("product_id") or "").strip(),
                "name": name,
                "url": str(product.get("url") or "").strip(),
                "article_role": str(product.get("article_role") or "").strip(),
            }
        )
    brief_identity = {
        "brief_id": str((article_brief or {}).get("brief_id") or "").strip(),
        "input_hash": str((article_brief or {}).get("input_hash") or "").strip(),
        "knowledge_snapshot_fingerprint": str(
            (article_brief or {}).get("knowledge_snapshot_fingerprint") or ""
        ).strip(),
    }
    identity_payload = json.dumps(
        {
            "task_id": task_id,
            "outline_hash": outline_hash,
            "topic": topic.strip(),
            "products": product_identity,
            "article_brief": brief_identity,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_fingerprint = sha256(
        identity_payload.encode("utf-8")
    ).hexdigest()[:16]
    plan_id = (
        f"plan-{_slug(article_id, 'article')}-outline-v{outline_version}-"
        f"{content_fingerprint}"
    )
    scopes: list[RetrievalScope] = []

    sections = _outline_sections(normalized_outline)
    for title, h3_titles in sections:
        title = re.sub(r"\s+#+\s*$", "", title).strip()
        if not title:
            continue
        scope_type = (
            "faq"
            if any(marker in title.casefold() for marker in _FAQ_MARKERS)
            else "h2_section"
        )
        ordinal = len(scopes)
        key = _slug(title, f"section-{ordinal + 1}")
        requirements = tuple(
            _requirement(
                requirement_id=f"{key}-req-{index + 1:02d}",
                h2_title=title,
                h3_title=h3_title,
                topic=topic,
                product_identity=product_identity,
            )
            for index, h3_title in enumerate(h3_titles)
            if h3_title
        )
        requirement_queries = tuple(
            query
            for requirement in requirements
            for query in requirement["query_variants"]
            if isinstance(query, str)
        )
        require_hard_fact = any(
            bool(requirement["require_hard_fact"])
            for requirement in requirements
        )
        scopes.append(
            RetrievalScope(
                project_id=project_id,
                retrieval_plan_id=plan_id,
                scope_id=f"scope-{ordinal + 1:02d}-{key}",
                ordinal=ordinal,
                scope_type=scope_type,
                scope_key=key,
                title=title,
                query_variants=_queries(
                    f"{topic} {title}",
                    title,
                    *requirement_queries,
                ),
                filters=_EVIDENCE_FILTERS,
                minimum_hits=2,
                minimum_distinct_sources=1,
                require_hard_fact=require_hard_fact,
                metadata={
                    "generated_from": "confirmed_outline_h2",
                    "claim_requirements": list(requirements),
                    "h3_titles": list(h3_titles),
                },
            )
        )

    for product in product_identity:
        name = str(product.get("name") or "").strip()
        if not name:
            continue
        ordinal = len(scopes)
        key = _slug(name, f"product-{ordinal + 1}")
        url = str(product.get("url") or "").strip()
        filters: dict[str, object] = dict(_EVIDENCE_FILTERS)
        product_id = str(product.get("product_id") or "").strip()
        if product_id:
            filters["product_ids"] = [product_id]
        elif url:
            filters["canonical_urls"] = [url]
        scopes.append(
            RetrievalScope(
                project_id=project_id,
                retrieval_plan_id=plan_id,
                scope_id=f"scope-{ordinal + 1:02d}-product-{key}",
                ordinal=ordinal,
                scope_type="product_fact",
                scope_key=key,
                title=f"{name} product facts",
                query_variants=_queries(
                    f"{name} specifications material dimensions",
                    f"{topic} {name}",
                ),
                filters=filters,
                minimum_hits=2,
                minimum_distinct_sources=1,
                require_hard_fact=True,
                metadata={
                    "generated_from": "selected_product",
                    "product_id": product_id,
                    "article_role": str(product.get("article_role") or "").strip(),
                },
            )
        )

    if not scopes:
        scopes.append(
            RetrievalScope(
                project_id=project_id,
                retrieval_plan_id=plan_id,
                scope_id="scope-01-introduction",
                ordinal=0,
                scope_type="introduction",
                scope_key="introduction",
                title="Introduction",
                query_variants=_queries(topic, article_id),
                filters=_EVIDENCE_FILTERS,
                minimum_hits=2,
                minimum_distinct_sources=1,
                metadata={"generated_from": "outline_fallback"},
            )
        )

    return RetrievalPlan(
        project_id=project_id,
        retrieval_plan_id=plan_id,
        article_id=article_id,
        outline_version=outline_version,
        scopes=tuple(scopes),
        max_gap_fill_rounds=2,
        metadata={
            "task_id": task_id,
            "outline_hash": outline_hash,
            "content_fingerprint": content_fingerprint,
            "generated_from": "confirmed_task_outline",
            "article_brief": brief_identity,
            "product_coverage": [
                {
                    "product_id": product.get("product_id", ""),
                    "name": product.get("name", ""),
                    "mentioned_in_outline": str(product.get("name") or "").casefold()
                    in normalized_outline.casefold(),
                }
                for product in product_identity
            ],
            "claim_requirement_count": sum(
                len(scope.metadata.get("claim_requirements", ()))
                for scope in scopes
            ),
        },
    )
