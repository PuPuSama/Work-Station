from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from hashlib import sha256

from .contracts import EVIDENCE_SOURCE_KINDS, RetrievalPlan, RetrievalScope


_H2_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_FAQ_MARKERS = ("faq", "frequently asked", "common questions", "questions")
_EVIDENCE_FILTERS = {"source_kinds": sorted(EVIDENCE_SOURCE_KINDS)}


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (slug or fallback)[:80]


def _queries(*values: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(value.strip() for value in values if value.strip())
    )[:4]


def generate_retrieval_plan(
    *,
    project_id: str,
    article_id: str,
    task_id: str,
    outline_version: int,
    outline: str,
    topic: str,
    products: Sequence[Mapping[str, object]] = (),
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
                "name": name,
                "url": str(product.get("url") or "").strip(),
            }
        )
    identity_payload = json.dumps(
        {
            "task_id": task_id,
            "outline_hash": outline_hash,
            "topic": topic.strip(),
            "products": product_identity,
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

    for heading in _H2_PATTERN.findall(normalized_outline):
        title = re.sub(r"\s+#+\s*$", "", heading).strip()
        if not title:
            continue
        scope_type = (
            "faq"
            if any(marker in title.casefold() for marker in _FAQ_MARKERS)
            else "h2_section"
        )
        ordinal = len(scopes)
        key = _slug(title, f"section-{ordinal + 1}")
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
                ),
                filters=_EVIDENCE_FILTERS,
                minimum_hits=2,
                minimum_distinct_sources=1,
                require_hard_fact=False,
                metadata={"generated_from": "confirmed_outline_h2"},
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
        if url:
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
                metadata={"generated_from": "selected_product"},
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
        },
    )
