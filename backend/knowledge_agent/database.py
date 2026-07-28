from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url


def create_knowledge_engine(database_url: str) -> Engine:
    """Create the synchronous PostgreSQL engine used by formal knowledge storage."""

    normalized = database_url.strip()
    if not normalized:
        raise ValueError("ARTICLE_AGENT_DATABASE_URL is required")

    url = make_url(normalized)
    if url.drivername != "postgresql+psycopg":
        raise ValueError(
            "ARTICLE_AGENT_DATABASE_URL must use the postgresql+psycopg driver"
        )

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=300,
    )
