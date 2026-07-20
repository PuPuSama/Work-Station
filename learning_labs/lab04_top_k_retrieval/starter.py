"""Starter code for Lab 04. It depends on the completed Lab 03."""

from dataclasses import dataclass
from typing import Optional

from learning_labs.lab03_vector_similarity.starter import cosine_similarity


@dataclass(frozen=True)
class SearchDocument:
    document_id: str
    text: str
    vector: tuple[float, ...]
    page_type: str
    category_url: Optional[str]


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    score: float


def top_k_search(
    query_vector: tuple[float, ...],
    documents: list[SearchDocument],
    k: int,
    allowed_page_types: Optional[set[str]] = None,
    required_category_url: Optional[str] = None,
) -> list[SearchHit]:
    """Filter, rank, and return the top K toy-vector documents."""

    # TODO: implement only after Lab 03 passes review.
    raise NotImplementedError("TODO: implement top_k_search")

