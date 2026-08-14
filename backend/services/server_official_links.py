from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from knowledge_agent.schema import knowledge_sources
from models import OfficialLink
from services.job_queue import JobConflict


MAX_OFFICIAL_ARTICLE_LINKS = 8

_CONTACT_TERMS = (
    "contact us",
    "contact-us",
    "contact_us",
    "get in touch",
    "get-in-touch",
    "inquiry",
    "enquiry",
    "联系我们",
    "联系",
    "询价",
)
_COMPANY_TERMS = (
    "about us",
    "about-us",
    "our company",
    "company profile",
    "company-profile",
    "关于我们",
    "公司介绍",
    "公司简介",
)
_SERVICE_TERMS = (
    "services",
    "service",
    "capabilities",
    "solutions",
    "customization",
    "服务",
    "能力",
    "解决方案",
)
_CERTIFICATE_TERMS = (
    "certificate",
    "certification",
    "certifications",
    "quality assurance",
    "资质",
    "认证",
)
_SUPPORT_TERMS = (
    "support",
    "request a quote",
    "request-a-quote",
    "get a quote",
    "get-a-quote",
    "报价",
    "支持",
)
_EXCLUDED_TERMS = (
    "privacy",
    "cookie",
    "terms-of-use",
    "terms of use",
    "login",
    "sign-in",
    "account",
    "cart",
    "checkout",
    "隐私",
    "登录",
    "购物车",
)


def _host(value: str) -> str:
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _same_official_site(customer: str, url: str) -> bool:
    customer_host = _host(customer)
    link_host = _host(url)
    return bool(
        customer_host
        and link_host
        and (
            link_host == customer_host
            or link_host.endswith(f".{customer_host}")
        )
    )


def _candidate_text(
    display_name: str,
    canonical_url: str,
    metadata: Mapping[str, object] | None,
) -> str:
    metadata_text = " ".join(
        str(value)
        for key, value in dict(metadata or {}).items()
        if key in {"page_type", "title", "heading", "description"}
    )
    return re.sub(
        r"[\s_/]+",
        " ",
        f"{display_name} {canonical_url} {metadata_text}".casefold(),
    )


def classify_official_link(
    *,
    display_name: str,
    canonical_url: str,
    source_kind: str,
    metadata: Mapping[str, object] | None = None,
) -> tuple[str, int] | None:
    """Classify useful company navigation without trusting URL shape alone."""

    if source_kind not in {"knowledge_page", "product_category"}:
        return None
    text = _candidate_text(display_name, canonical_url, metadata)
    if any(term in text for term in _EXCLUDED_TERMS):
        return None
    groups = (
        ("contact", 100, _CONTACT_TERMS),
        ("company", 80, _COMPANY_TERMS),
        ("support", 75, _SUPPORT_TERMS),
        ("service", 70, _SERVICE_TERMS),
        ("certificate", 65, _CERTIFICATE_TERMS),
    )
    for role, score, terms in groups:
        if any(term in text for term in terms):
            return role, score
    # Keep a small multilingual fallback pool of general published pages. The
    # high-confidence roles above always sort first.
    return "other", 10 if source_kind == "knowledge_page" else 0


def _fallback_label(role: str) -> str:
    return {
        "contact": "Contact Us",
        "company": "About the Company",
        "support": "Customer Support",
        "service": "Services",
        "certificate": "Certifications",
    }.get(role, "Official Information")


def _official_link_from_row(
    row: sa.RowMapping,
    *,
    customer: str,
) -> tuple[OfficialLink, int] | None:
    url = str(row["canonical_url"] or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not _same_official_site(customer, url)
        or (parsed.path.rstrip("/") == "" and not parsed.query)
    ):
        return None
    classified = classify_official_link(
        display_name=str(row["display_name"] or ""),
        canonical_url=url,
        source_kind=str(row["source_kind"] or ""),
        metadata=dict(row["metadata"] or {}),
    )
    if classified is None:
        return None
    role, score = classified
    label = " ".join(str(row["display_name"] or "").split())
    if not label or label.casefold() == url.casefold():
        label = _fallback_label(role)
    return (
        OfficialLink(
            source_id=str(row["source_id"]),
            snapshot_id=str(row["current_snapshot_id"]),
            label=label[:240],
            url=url,
            role=role,
        ),
        score,
    )


class PostgresPublishedOfficialLinks:
    """Select and revalidate bounded current official company links."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _statement(project_id: str) -> sa.Select:
        return sa.select(
            knowledge_sources.c.source_id,
            knowledge_sources.c.display_name,
            knowledge_sources.c.source_kind,
            knowledge_sources.c.canonical_url,
            knowledge_sources.c.current_snapshot_id,
            knowledge_sources.c.metadata,
        ).where(
            knowledge_sources.c.project_id == project_id,
            knowledge_sources.c.status == "published",
            knowledge_sources.c.public_source.is_(True),
            knowledge_sources.c.current_snapshot_id.is_not(None),
            knowledge_sources.c.canonical_url.is_not(None),
            knowledge_sources.c.source_kind.in_(
                ("knowledge_page", "product_category")
            ),
        )

    def select(
        self,
        *,
        project_id: str,
        customer: str,
        limit: int = MAX_OFFICIAL_ARTICLE_LINKS,
    ) -> tuple[OfficialLink, ...]:
        normalized_project = project_id.strip()
        if not normalized_project:
            raise ValueError("project_id is required")
        bounded_limit = max(1, min(int(limit), MAX_OFFICIAL_ARTICLE_LINKS))
        with self._engine.connect() as connection:
            rows = connection.execute(
                self._statement(normalized_project)
            ).mappings().all()
        candidates = [
            item
            for row in rows
            if (item := _official_link_from_row(row, customer=customer))
            is not None
        ]
        candidates.sort(
            key=lambda item: (
                -item[1],
                item[0].label.casefold(),
                item[0].source_id,
            )
        )
        return tuple(link for link, _score in candidates[:bounded_limit])

    def load_current(
        self,
        *,
        project_id: str,
        customer: str,
        references: Sequence[OfficialLink],
    ) -> tuple[OfficialLink, ...]:
        if len(references) > MAX_OFFICIAL_ARTICLE_LINKS:
            raise JobConflict("official link identity is invalid")
        source_ids = tuple(reference.source_id for reference in references)
        if len(source_ids) != len(set(source_ids)) or any(
            not value.strip() for value in source_ids
        ):
            raise JobConflict("official link identity is invalid")
        if not references:
            return ()
        statement = self._statement(project_id).where(
            knowledge_sources.c.source_id.in_(source_ids)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        current_by_id: dict[str, OfficialLink] = {}
        for row in rows:
            item = _official_link_from_row(row, customer=customer)
            if item is not None:
                current_by_id[item[0].source_id] = item[0]
        if set(current_by_id) != set(source_ids):
            raise JobConflict("published official links changed")
        current = tuple(current_by_id[source_id] for source_id in source_ids)
        if any(
            actual.model_dump() != expected.model_dump()
            for actual, expected in zip(current, references, strict=True)
        ):
            raise JobConflict("published official links changed")
        return current


__all__ = [
    "MAX_OFFICIAL_ARTICLE_LINKS",
    "PostgresPublishedOfficialLinks",
    "classify_official_link",
]
