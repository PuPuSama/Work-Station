from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib import parse


def normalized_customer(value: str) -> str:
    raw = str(value or "").strip()
    parsed = parse.urlparse(raw if "://" in raw else f"https://{raw.strip('/')}")
    host = (parsed.hostname or raw).casefold().strip(".")
    return host.removeprefix("www.")


def normalized_topic(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def article_source_key(
    customer: str,
    topic: str,
    topic_index: int | None = None,
) -> str:
    """Identify one source row independently of its former week folder.

    The row index is intentional: a workbook can contain the same topic more
    than once.  Combining it with the normalized topic preserves those rows
    while still matching the same row across weekly copies of a workbook.
    """

    row = "" if topic_index is None else str(int(topic_index))
    identity = (
        f"{normalized_customer(customer)}|{row}|{normalized_topic(topic)}"
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]
