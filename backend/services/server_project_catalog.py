from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from knowledge_agent.schema import (
    knowledge_assets,
    knowledge_product_asset_evidence,
    knowledge_product_source_evidence,
    knowledge_products,
    knowledge_sources,
    snapshot_assets,
)
from services.task_identity import normalized_customer


def _display_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]


def _selection_projection(metadata: object) -> Mapping[str, object] | None:
    if not isinstance(metadata, Mapping):
        return None
    projection = metadata.get("selection_projection")
    if not isinstance(projection, Mapping):
        return None
    if projection.get("schema_version") != 1:
        return None
    name = _display_text(projection.get("name"), maximum=240)
    canonical_url = _display_text(
        projection.get("canonical_url"),
        maximum=4096,
    )
    parsed = urlsplit(canonical_url)
    if (
        not name
        or parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
    ):
        return None
    return projection


@dataclass(frozen=True, slots=True)
class ServerCatalogProduct:
    """Minimal product identity exposed to the Server article workbench."""

    product_id: str
    name: str
    asset_count: int
    selected_asset_id: str


@dataclass(frozen=True, slots=True)
class ServerCatalogImageAsset:
    """Safe image summary; storage identities and source URLs stay private."""

    asset_id: str
    content_type: str
    byte_size: int
    width: int | None
    height: int | None
    label: str
    evidence_kind: str


@dataclass(frozen=True, slots=True)
class ServerProjectCatalog:
    products: tuple[ServerCatalogProduct, ...]
    image_assets: tuple[ServerCatalogImageAsset, ...]


class PostgresServerProjectCatalog:
    """Read the smallest catalog projection needed by the Server UI.

    Products must be confirmed and backed by a primary-detail projection from
    the current snapshot of a published source. Images must likewise belong to
    the current snapshot of a published source. The projection intentionally
    omits canonical/source URLs, object URIs, object keys, hashes, and metadata.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read(
        self,
        project_id: str,
        *,
        product_limit: int = 100,
        image_limit: int = 48,
    ) -> ServerProjectCatalog:
        normalized_project_id = normalized_customer(project_id)
        if not 1 <= product_limit <= 200:
            raise ValueError("product_limit must be between 1 and 200")
        if not 1 <= image_limit <= 100:
            raise ValueError("image_limit must be between 1 and 100")
        with self._engine.connect() as connection:
            products = self._products(
                connection,
                normalized_project_id,
                product_limit,
            )
            images = self._images(
                connection,
                normalized_project_id,
                image_limit,
            )
        return ServerProjectCatalog(products=products, image_assets=images)

    @staticmethod
    def _products(
        connection: sa.Connection,
        project_id: str,
        limit: int,
    ) -> tuple[ServerCatalogProduct, ...]:
        evidence = knowledge_product_source_evidence.alias(
            "server_catalog_product_evidence"
        )
        source = knowledge_sources.alias("server_catalog_product_source")
        rows = connection.execute(
            sa.select(
                knowledge_products.c.product_id,
                evidence.c.metadata,
                evidence.c.confidence,
                evidence.c.source_id,
                evidence.c.snapshot_id,
            )
            .select_from(
                knowledge_products.join(
                    evidence,
                    sa.and_(
                        evidence.c.project_id
                        == knowledge_products.c.project_id,
                        evidence.c.product_id
                        == knowledge_products.c.product_id,
                    ),
                ).join(
                    source,
                    sa.and_(
                        source.c.project_id == evidence.c.project_id,
                        source.c.source_id == evidence.c.source_id,
                    ),
                )
            )
            .where(
                knowledge_products.c.project_id == project_id,
                knowledge_products.c.status == "confirmed",
                evidence.c.relation == "primary_detail",
                source.c.status == "published",
                source.c.current_snapshot_id == evidence.c.snapshot_id,
            )
            .order_by(
                knowledge_products.c.product_id.asc(),
                evidence.c.confidence.desc(),
                evidence.c.source_id.asc(),
                evidence.c.snapshot_id.asc(),
            )
        ).mappings().all()

        selected: dict[str, tuple[str, str, str]] = {}
        for row in rows:
            product_id = str(row["product_id"])
            if product_id in selected:
                continue
            projection = _selection_projection(row["metadata"])
            if projection is None:
                continue
            selected[product_id] = (
                _display_text(projection.get("name"), maximum=240),
                str(row["source_id"]),
                str(row["snapshot_id"]),
            )
            if len(selected) == limit:
                break
        if not selected:
            return ()

        asset_evidence = knowledge_product_asset_evidence.alias(
            "server_catalog_asset_evidence"
        )
        asset_source = knowledge_sources.alias("server_catalog_asset_source")
        role_rank = sa.case(
            (asset_evidence.c.role == "primary", 0),
            (asset_evidence.c.role == "hero", 1),
            (asset_evidence.c.role == "gallery", 2),
            (asset_evidence.c.role == "detail", 3),
            else_=4,
        )
        asset_rows = connection.execute(
            sa.select(
                asset_evidence.c.product_id,
                asset_evidence.c.source_id,
                asset_evidence.c.snapshot_id,
                asset_evidence.c.asset_id,
            )
            .select_from(
                asset_evidence.join(
                    asset_source,
                    sa.and_(
                        asset_source.c.project_id
                        == asset_evidence.c.project_id,
                        asset_source.c.source_id
                        == asset_evidence.c.source_id,
                    ),
                )
            )
            .where(
                asset_evidence.c.project_id == project_id,
                asset_evidence.c.product_id.in_(tuple(selected)),
                asset_source.c.status == "published",
                asset_source.c.current_snapshot_id
                == asset_evidence.c.snapshot_id,
            )
            .order_by(
                asset_evidence.c.product_id.asc(),
                role_rank,
                asset_evidence.c.confidence.desc(),
                asset_evidence.c.asset_id.asc(),
            )
        ).mappings().all()
        asset_ids: dict[str, list[str]] = {}
        for row in asset_rows:
            product_id = str(row["product_id"])
            _, source_id, snapshot_id = selected[product_id]
            if (
                str(row["source_id"]) != source_id
                or str(row["snapshot_id"]) != snapshot_id
            ):
                continue
            asset_id = str(row["asset_id"])
            values = asset_ids.setdefault(product_id, [])
            if asset_id not in values:
                values.append(asset_id)

        return tuple(
            ServerCatalogProduct(
                product_id=product_id,
                name=name,
                asset_count=len(asset_ids.get(product_id, ())),
                selected_asset_id=(
                    asset_ids.get(product_id, [""])[0]
                    if asset_ids.get(product_id)
                    else ""
                ),
            )
            for product_id, (name, _, _) in selected.items()
        )

    @staticmethod
    def _images(
        connection: sa.Connection,
        project_id: str,
        limit: int,
    ) -> tuple[ServerCatalogImageAsset, ...]:
        source = knowledge_sources.alias("server_catalog_image_source")
        ranked = (
            sa.select(
                knowledge_assets.c.asset_id,
                knowledge_assets.c.content_type,
                knowledge_assets.c.byte_size,
                knowledge_assets.c.width,
                knowledge_assets.c.height,
                snapshot_assets.c.evidence_kind,
                snapshot_assets.c.alt_text,
                snapshot_assets.c.title,
                snapshot_assets.c.caption,
                source.c.display_name,
                sa.func.row_number()
                .over(
                    partition_by=knowledge_assets.c.asset_id,
                    order_by=(
                        source.c.display_name.asc(),
                        snapshot_assets.c.ordinal.asc(),
                        snapshot_assets.c.asset_id.asc(),
                    ),
                )
                .label("asset_rank"),
            )
            .select_from(
                knowledge_assets.join(
                    snapshot_assets,
                    sa.and_(
                        snapshot_assets.c.project_id
                        == knowledge_assets.c.project_id,
                        snapshot_assets.c.asset_id
                        == knowledge_assets.c.asset_id,
                    ),
                ).join(
                    source,
                    sa.and_(
                        source.c.project_id == snapshot_assets.c.project_id,
                        source.c.source_id == snapshot_assets.c.source_id,
                    ),
                )
            )
            .where(
                knowledge_assets.c.project_id == project_id,
                knowledge_assets.c.content_type.ilike("image/%"),
                source.c.status == "published",
                source.c.current_snapshot_id
                == snapshot_assets.c.snapshot_id,
            )
            .subquery()
        )
        rows = connection.execute(
            sa.select(ranked)
            .where(ranked.c.asset_rank == 1)
            .order_by(ranked.c.display_name.asc(), ranked.c.asset_id.asc())
            .limit(limit)
        ).mappings().all()
        result: list[ServerCatalogImageAsset] = []
        for row in rows:
            label = next(
                (
                    text
                    for text in (
                        _display_text(row["alt_text"], maximum=240),
                        _display_text(row["title"], maximum=240),
                        _display_text(row["caption"], maximum=240),
                        _display_text(row["display_name"], maximum=240),
                    )
                    if text
                ),
                str(row["asset_id"]),
            )
            result.append(
                ServerCatalogImageAsset(
                    asset_id=str(row["asset_id"]),
                    content_type=str(row["content_type"]),
                    byte_size=int(row["byte_size"]),
                    width=(
                        None if row["width"] is None else int(row["width"])
                    ),
                    height=(
                        None if row["height"] is None else int(row["height"])
                    ),
                    label=label,
                    evidence_kind=str(row["evidence_kind"]),
                )
            )
        return tuple(result)


__all__ = [
    "PostgresServerProjectCatalog",
    "ServerCatalogImageAsset",
    "ServerCatalogProduct",
    "ServerProjectCatalog",
]
