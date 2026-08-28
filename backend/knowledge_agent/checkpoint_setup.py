from __future__ import annotations

import sys

from langgraph.checkpoint.postgres import PostgresSaver

from config import initialize_environment
from .settings import KnowledgeAgentSettings


def psycopg_connection_url(database_url: str) -> str:
    """Convert the SQLAlchemy psycopg URL into the driver's native URL."""

    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def main() -> None:
    initialize_environment()
    settings = KnowledgeAgentSettings.from_env(enabled=True)
    if settings.database_url is None:
        raise ValueError("ARTICLE_AGENT_DATABASE_URL is required")
    with PostgresSaver.from_conn_string(
        psycopg_connection_url(settings.database_url)
    ) as checkpointer:
        checkpointer.setup()
    print("LangGraph PostgreSQL checkpoint schema is ready.")


def cli() -> None:
    try:
        main()
    except Exception:
        print("LANGGRAPH_CHECKPOINT_SETUP_FAILED", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
