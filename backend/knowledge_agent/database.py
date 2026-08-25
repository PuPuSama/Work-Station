from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url


def create_knowledge_engine(
    database_url: str,
    *,
    pool_size: int = 20,
    max_overflow: int = 20,
    pool_timeout: int = 60,
    pool_recycle: int = 300,
) -> Engine:
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
        pool_size=max(5, int(pool_size)),
        max_overflow=max(0, int(max_overflow)),
        pool_timeout=max(5, int(pool_timeout)),
        pool_recycle=max(60, int(pool_recycle)),
    )
