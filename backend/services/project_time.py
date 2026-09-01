"""Project-wide time conventions.

The production host is configured for China Standard Time, but containers and
PostgreSQL intentionally default to UTC.  Keep persistence and connection
timestamps explicit so a container restart cannot change the meaning of a
project timestamp.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


PROJECT_TIMEZONE_NAME = "Asia/Shanghai"
PROJECT_TIMEZONE = timezone(
    timedelta(hours=8),
    name=PROJECT_TIMEZONE_NAME,
)


def project_now() -> datetime:
    """Return the current project time as an aware UTC+8 datetime."""

    return datetime.now(PROJECT_TIMEZONE)


def project_now_iso() -> str:
    """Return the current project time in an unambiguous ISO-8601 form."""

    return project_now().replace(microsecond=0).isoformat()


def postgres_connect_args() -> dict[str, str]:
    """Return connection options for PostgreSQL's project display timezone."""

    return {"options": f"-c timezone={PROJECT_TIMEZONE_NAME}"}


__all__ = [
    "PROJECT_TIMEZONE",
    "PROJECT_TIMEZONE_NAME",
    "postgres_connect_args",
    "project_now",
    "project_now_iso",
]
