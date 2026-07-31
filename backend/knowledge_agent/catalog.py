from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from .schema import (
    knowledge_product_asset_evidence,
    knowledge_product_source_evidence,
    knowledge_products,
)


Metadata = Mapping[str, object]
ProductStatus = Literal["inbox", "confirmed", "rejected", "stale"]
ProductSourceRelation = Literal[
    "primary_detail",
    "category_listing",
    "private_specification",
    "supporting_page",
]
ProductAssetRole = Literal["candidate", "primary", "gallery", "detail", "hero"]
PRODUCT_STATUSES = frozenset({"inbox", "confirmed", "rejected", "stale"})
PRODUCT_SOURCE_RELATIONS = frozenset(
    {
        "primary_detail",
        "category_listing",
        "private_specification",
        "supporting_page",
    }
)
PRODUCT_ASSET_ROLES = frozenset(
    {"candidate", "primary", "gallery", "detail", "hero"}
)


class ProductCatalogRepositoryError(RuntimeError):
    """Base error for project-scoped product catalog persistence."""


class ProductCatalogConflictError(ProductCatalogRepositoryError):
    """Raised when immutable product evidence is retried with different data."""


class ProductCatalogNotFound(ProductCatalogRepositoryError):
    """Raised when a product or evidence target does not exist."""


class ProductConfirmationError(ProductCatalogRepositoryError):
    """Raised when a product lacks primary detail evidence."""


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _metadata(value: Metadata, field_name: str) -> Metadata:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return MappingProxyType(dict(value))


def _http_url(value: str | None, field_name: str) -> str | None:
    normalized = _optional_text(value, field_name)
    if normalized is None:
        return None
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and not 0 < port < 65536
    ):
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    return normalized


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number between 0 and 1")
    normalized = float(value)
    if not 0 <= normalized <= 1:
        raise ValueError("confidence must be a number between 0 and 1")
    return normalized


@dataclass(frozen=True, slots=True)
class KnowledgeProduct:
    """Stable project-level product identity assembled from source evidence."""

    project_id: str
    product_id: str
    name: str
    status: ProductStatus = "inbox"
    canonical_url: str | None = None
    category_path: tuple[str, ...] = ()
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("project_id", "product_id", "name"):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if self.status not in PRODUCT_STATUSES:
            raise ValueError(
                "status must be one of: " + ", ".join(sorted(PRODUCT_STATUSES))
            )
        object.__setattr__(
            self, "canonical_url", _http_url(self.canonical_url, "canonical_url")
        )
        if isinstance(self.category_path, (str, bytes)):
            raise ValueError("category_path must be a sequence of category names")
        object.__setattr__(
            self,
            "category_path",
            tuple(
                _required_text(item, "category_path") for item in self.category_path
            ),
        )
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class ProductSourceEvidence:
    """Immutable evidence connecting a product identity to one source snapshot."""

    project_id: str
    product_id: str
    source_id: str
    snapshot_id: str
    relation: ProductSourceRelation
    confidence: float
    reason: str
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "project_id",
            "product_id",
            "source_id",
            "snapshot_id",
            "reason",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if self.relation not in PRODUCT_SOURCE_RELATIONS:
            raise ValueError(
                "relation must be one of: "
                + ", ".join(sorted(PRODUCT_SOURCE_RELATIONS))
            )
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class ProductAssetEvidence:
    """Immutable evidence connecting a product to an asset occurrence."""

    project_id: str
    product_id: str
    source_id: str
    snapshot_id: str
    asset_id: str
    role: ProductAssetRole
    confidence: float
    reason: str
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "project_id",
            "product_id",
            "source_id",
            "snapshot_id",
            "asset_id",
            "reason",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if self.role not in PRODUCT_ASSET_ROLES:
            raise ValueError(
                "role must be one of: " + ", ".join(sorted(PRODUCT_ASSET_ROLES))
            )
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "metadata", _metadata(self.metadata, "metadata"))


@runtime_checkable
class ProductCatalogRepository(Protocol):
    """Persistence boundary for stable products and immutable evidence."""

    def upsert_product(self, product: KnowledgeProduct) -> None: ...

    def store_source_evidence(self, evidence: ProductSourceEvidence) -> None: ...

    def store_asset_evidence(self, evidence: ProductAssetEvidence) -> None: ...

    def confirm_product(self, project_id: str, product_id: str) -> None: ...

    def get_product(
        self, project_id: str, product_id: str
    ) -> KnowledgeProduct | None: ...

    def list_products(
        self, project_id: str, *, status: ProductStatus | None = None
    ) -> tuple[KnowledgeProduct, ...]: ...


class PostgresProductCatalogRepository:
    """SQLAlchemy Core product catalog built on M1 source/snapshot evidence."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert_product(self, product: KnowledgeProduct) -> None:
        try:
            with self._engine.begin() as connection:
                self.upsert_product_in_transaction(connection, product)
        except IntegrityError as exc:
            raise ProductCatalogConflictError(
                "product conflicts with an existing project-scoped record"
            ) from exc

    def upsert_product_in_transaction(
        self,
        connection: Connection,
        product: KnowledgeProduct,
    ) -> bool:
        """Upsert an unconfirmed product and report a real row change."""

        if not connection.in_transaction():
            raise ValueError("product writes require a business transaction")
        if product.status == "confirmed":
            raise ValueError("confirm_product must be used to confirm a product")
        statement = insert(knowledge_products).values(
            project_id=product.project_id,
            product_id=product.product_id,
            name=product.name,
            status=product.status,
            canonical_url=product.canonical_url,
            category_path=list(product.category_path),
            metadata=dict(product.metadata),
        )
        keep_confirmed = sa.and_(
            knowledge_products.c.status == "confirmed",
            statement.excluded.status == "inbox",
        )
        effective_values = {
            "name": sa.case(
                (keep_confirmed, knowledge_products.c.name),
                else_=statement.excluded.name,
            ),
            "status": sa.case(
                (keep_confirmed, knowledge_products.c.status),
                else_=statement.excluded.status,
            ),
            "canonical_url": sa.case(
                (keep_confirmed, knowledge_products.c.canonical_url),
                else_=statement.excluded.canonical_url,
            ),
            "category_path": sa.case(
                (keep_confirmed, knowledge_products.c.category_path),
                else_=statement.excluded.category_path,
            ),
            "metadata": sa.case(
                (keep_confirmed, knowledge_products.c.metadata),
                else_=statement.excluded.metadata,
            ),
        }
        statement = statement.on_conflict_do_update(
            index_elements=[
                knowledge_products.c.project_id,
                knowledge_products.c.product_id,
            ],
            set_={
                # New unreviewed evidence must not mutate the aggregate facts
                # currently served for a confirmed product. Snapshot-scoped
                # evidence remains available for the next review.
                **effective_values,
                "updated_at": sa.func.now(),
            },
            where=sa.or_(
                *(
                    getattr(knowledge_products.c, field_name)
                    .is_distinct_from(value)
                    for field_name, value in effective_values.items()
                )
            ),
        ).returning(knowledge_products.c.product_id)
        return connection.execute(statement).scalar_one_or_none() is not None

    def store_source_evidence(self, evidence: ProductSourceEvidence) -> None:
        try:
            with self._engine.begin() as connection:
                self.store_source_evidence_in_transaction(
                    connection,
                    evidence,
                )
        except IntegrityError as exc:
            raise ProductCatalogNotFound(
                "product or source snapshot was not found in the requested project"
            ) from exc

    def store_source_evidence_in_transaction(
        self,
        connection: Connection,
        evidence: ProductSourceEvidence,
    ) -> bool:
        """Store immutable source evidence in a caller-owned transaction."""

        if not connection.in_transaction():
            raise ValueError(
                "product source evidence requires a business transaction"
            )
        statement = (
            insert(knowledge_product_source_evidence)
            .values(
                project_id=evidence.project_id,
                product_id=evidence.product_id,
                source_id=evidence.source_id,
                snapshot_id=evidence.snapshot_id,
                relation=evidence.relation,
                confidence=evidence.confidence,
                reason=evidence.reason,
                metadata=dict(evidence.metadata),
            )
            .on_conflict_do_nothing()
            .returning(knowledge_product_source_evidence.c.source_id)
        )
        inserted_source_id = connection.execute(
            statement
        ).scalar_one_or_none()
        if inserted_source_id is not None:
            return True
        row = connection.execute(
            sa.select(knowledge_product_source_evidence).where(
                knowledge_product_source_evidence.c.project_id
                == evidence.project_id,
                knowledge_product_source_evidence.c.product_id
                == evidence.product_id,
                knowledge_product_source_evidence.c.source_id
                == evidence.source_id,
                knowledge_product_source_evidence.c.snapshot_id
                == evidence.snapshot_id,
            )
        ).mappings().one_or_none()
        if row is None or _source_evidence_from_row(row) != evidence:
            raise ProductCatalogConflictError(
                "product source evidence conflicts with an immutable record"
            )
        return False

    def store_asset_evidence(self, evidence: ProductAssetEvidence) -> None:
        try:
            with self._engine.begin() as connection:
                self.store_asset_evidence_in_transaction(
                    connection,
                    evidence,
                )
        except IntegrityError as exc:
            raise ProductCatalogNotFound(
                "product or snapshot asset was not found in the requested project"
            ) from exc

    def store_asset_evidence_in_transaction(
        self,
        connection: Connection,
        evidence: ProductAssetEvidence,
    ) -> bool:
        """Store immutable asset evidence in a caller-owned transaction."""

        if not connection.in_transaction():
            raise ValueError(
                "product asset evidence requires a business transaction"
            )
        statement = (
            insert(knowledge_product_asset_evidence)
            .values(
                project_id=evidence.project_id,
                product_id=evidence.product_id,
                source_id=evidence.source_id,
                snapshot_id=evidence.snapshot_id,
                asset_id=evidence.asset_id,
                role=evidence.role,
                confidence=evidence.confidence,
                reason=evidence.reason,
                metadata=dict(evidence.metadata),
            )
            .on_conflict_do_nothing()
            .returning(knowledge_product_asset_evidence.c.asset_id)
        )
        inserted_asset_id = connection.execute(
            statement
        ).scalar_one_or_none()
        if inserted_asset_id is not None:
            return True
        row = connection.execute(
            sa.select(knowledge_product_asset_evidence).where(
                knowledge_product_asset_evidence.c.project_id
                == evidence.project_id,
                knowledge_product_asset_evidence.c.product_id
                == evidence.product_id,
                knowledge_product_asset_evidence.c.source_id
                == evidence.source_id,
                knowledge_product_asset_evidence.c.snapshot_id
                == evidence.snapshot_id,
                knowledge_product_asset_evidence.c.asset_id
                == evidence.asset_id,
            )
        ).mappings().one_or_none()
        if row is None or _asset_evidence_from_row(row) != evidence:
            raise ProductCatalogConflictError(
                "product asset evidence conflicts with an immutable record"
            )
        return False

    def confirm_product(self, project_id: str, product_id: str) -> None:
        with self._engine.begin() as connection:
            self.confirm_product_in_transaction(
                connection,
                project_id,
                product_id,
            )

    def confirm_product_in_transaction(
        self,
        connection: Connection,
        project_id: str,
        product_id: str,
    ) -> bool:
        """Confirm a product in the caller's transaction.

        Returns True only when the status changes, so a retried Server command
        does not append a second audit event for the same confirmed state.
        """

        if not connection.in_transaction():
            raise ValueError(
                "product confirmation requires a business transaction"
            )
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_product_id = _required_text(product_id, "product_id")
        product_status = connection.execute(
            sa.select(knowledge_products.c.status)
            .where(
                knowledge_products.c.project_id == normalized_project_id,
                knowledge_products.c.product_id == normalized_product_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if product_status is None:
            raise ProductCatalogNotFound(
                "product was not found in the requested project"
            )
        if product_status == "confirmed":
            return False
        primary_detail_exists = connection.execute(
            sa.select(knowledge_product_source_evidence.c.product_id)
            .where(
                knowledge_product_source_evidence.c.project_id
                == normalized_project_id,
                knowledge_product_source_evidence.c.product_id
                == normalized_product_id,
                knowledge_product_source_evidence.c.relation
                == "primary_detail",
            )
            .limit(1)
        ).scalar_one_or_none()
        if primary_detail_exists is None:
            raise ProductConfirmationError(
                "product requires primary detail evidence before confirmation"
            )
        connection.execute(
            knowledge_products.update()
            .where(
                knowledge_products.c.project_id == normalized_project_id,
                knowledge_products.c.product_id == normalized_product_id,
            )
            .values(status="confirmed", updated_at=sa.func.now())
        )
        return True

    def get_product(
        self, project_id: str, product_id: str
    ) -> KnowledgeProduct | None:
        normalized_project_id = _required_text(project_id, "project_id")
        normalized_product_id = _required_text(product_id, "product_id")
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(knowledge_products).where(
                    knowledge_products.c.project_id == normalized_project_id,
                    knowledge_products.c.product_id == normalized_product_id,
                )
            ).mappings().one_or_none()
        return None if row is None else _product_from_row(row)

    def get_product_in_transaction(
        self,
        connection: Connection,
        project_id: str,
        product_id: str,
    ) -> KnowledgeProduct | None:
        """Read the stored aggregate product inside a caller transaction."""

        if not connection.in_transaction():
            raise ValueError(
                "product reads require a business transaction"
            )
        row = connection.execute(
            sa.select(knowledge_products).where(
                knowledge_products.c.project_id
                == _required_text(project_id, "project_id"),
                knowledge_products.c.product_id
                == _required_text(product_id, "product_id"),
            )
        ).mappings().one_or_none()
        return None if row is None else _product_from_row(row)

    def list_products(
        self, project_id: str, *, status: ProductStatus | None = None
    ) -> tuple[KnowledgeProduct, ...]:
        normalized_project_id = _required_text(project_id, "project_id")
        if status is not None and status not in PRODUCT_STATUSES:
            raise ValueError(
                "status must be one of: " + ", ".join(sorted(PRODUCT_STATUSES))
            )
        statement = sa.select(knowledge_products).where(
            knowledge_products.c.project_id == normalized_project_id
        )
        if status is not None:
            statement = statement.where(knowledge_products.c.status == status)
        statement = statement.order_by(
            knowledge_products.c.name.asc(),
            knowledge_products.c.product_id.asc(),
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_product_from_row(row) for row in rows)


def _product_from_row(row: Mapping[str, object]) -> KnowledgeProduct:
    return KnowledgeProduct(
        project_id=str(row["project_id"]),
        product_id=str(row["product_id"]),
        name=str(row["name"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        canonical_url=(
            None if row["canonical_url"] is None else str(row["canonical_url"])
        ),
        category_path=tuple(row["category_path"]),  # type: ignore[arg-type]
        metadata=dict(row["metadata"]),  # type: ignore[arg-type]
    )


def _source_evidence_from_row(row: Mapping[str, object]) -> ProductSourceEvidence:
    return ProductSourceEvidence(
        project_id=str(row["project_id"]),
        product_id=str(row["product_id"]),
        source_id=str(row["source_id"]),
        snapshot_id=str(row["snapshot_id"]),
        relation=str(row["relation"]),  # type: ignore[arg-type]
        confidence=float(row["confidence"]),
        reason=str(row["reason"]),
        metadata=dict(row["metadata"]),  # type: ignore[arg-type]
    )

def _asset_evidence_from_row(row: Mapping[str, object]) -> ProductAssetEvidence:
    return ProductAssetEvidence(
        project_id=str(row["project_id"]),
        product_id=str(row["product_id"]),
        source_id=str(row["source_id"]),
        snapshot_id=str(row["snapshot_id"]),
        asset_id=str(row["asset_id"]),
        role=str(row["role"]),  # type: ignore[arg-type]
        confidence=float(row["confidence"]),
        reason=str(row["reason"]),
        metadata=dict(row["metadata"]),  # type: ignore[arg-type]
    )
