from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from knowledge_agent.schema import (
    knowledge_product_asset_evidence,
    knowledge_product_source_evidence,
    knowledge_products,
    knowledge_sources,
)
from knowledge_agent.catalog import MANUAL_SPECIFICATION_TABLES_KEY
from models import Product
from services.task_identity import normalized_customer


class ConfirmedProductSelectionError(ValueError):
    """A requested product cannot be projected into a server Task."""


def _required_ids(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(str(value).strip() for value in values)
    if not normalized or any(not value for value in normalized):
        raise ConfirmedProductSelectionError(
            "between one and three product ids are required"
        )
    if len(normalized) > 3:
        raise ConfirmedProductSelectionError(
            "between one and three product ids are required"
        )
    if len(set(normalized)) != len(normalized):
        raise ConfirmedProductSelectionError(
            "product ids must be unique"
        )
    return normalized


def _string(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]


def _facts(metadata: Mapping[str, object]) -> list[str]:
    values = metadata.get("reference_facts")
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        text = _string(value, maximum=500)
        if text and text not in result:
            result.append(text)
        if len(result) == 8:
            break
    return result


def _specifications(metadata: Mapping[str, object]) -> dict[str, str]:
    """Flatten table cells while preserving model-column identity."""

    tables = metadata.get("specification_tables")
    if not isinstance(tables, list):
        return {}
    result: dict[str, str] = {}
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        headers_value = table.get("headers")
        headers = (
            [
                _string(value, maximum=160)
                for value in headers_value
            ]
            if isinstance(headers_value, list)
            else []
        )
        rows = table.get("rows")
        if not isinstance(rows, list):
            continue
        generic_column_labels = {"", "value", "值", "数值"}
        header_model_labels = headers[1:]
        model_labels = (
            header_model_labels
            if header_model_labels
            and any(
                label.casefold() not in generic_column_labels
                for label in header_model_labels
            )
            else []
        )
        for row in rows if not model_labels else []:
            if not isinstance(row, list) or len(row) < 3:
                continue
            label = _string(row[0], maximum=160).casefold()
            if label in {
                "model",
                "models",
                "model no.",
                "model number",
                "rated power",
                "型号",
                "产品型号",
                "额定功率",
            }:
                candidates = [
                    _string(value, maximum=160) for value in row[1:]
                ]
                if any(
                    label.casefold() not in generic_column_labels
                    for label in candidates
                    if label
                ):
                    model_labels = candidates
                break
        for row in rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            parameter = _string(row[0], maximum=160)
            if not parameter:
                continue
            if len(row) == 2:
                value = _string(row[1], maximum=500)
                if value and parameter not in result:
                    result[parameter] = value
            else:
                for column_index, raw_value in enumerate(row[1:], start=1):
                    value = _string(raw_value, maximum=500)
                    if not value:
                        continue
                    column_label = (
                        model_labels[column_index - 1]
                        if column_index - 1 < len(model_labels)
                        else ""
                    )
                    if not column_label and column_index < len(headers):
                        column_label = headers[column_index]
                    if not column_label:
                        column_label = f"column {column_index + 1}"
                    key = f"{parameter} [{column_label}]"
                    if key not in result:
                        result[key] = value
                    if len(result) == 36:
                        return result
            if len(result) == 36:
                return result
    return result


def _selection_projection(
    metadata: object,
) -> Mapping[str, object] | None:
    if not isinstance(metadata, Mapping):
        return None
    projection = metadata.get("selection_projection")
    if not isinstance(projection, Mapping):
        return None
    if projection.get("schema_version") != 1:
        return None
    name = _string(projection.get("name"), maximum=240)
    canonical_url = _string(
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


class PostgresConfirmedProductSelection:
    """Read-only projection from published catalog evidence to Task products.

    The query deliberately returns only confirmed products whose primary
    detail evidence belongs to the current snapshot of a published source.
    Image bytes and object-store URIs never enter the Task payload; only the
    stable ``asset_id`` is copied so later reads still pass through the
    authorized asset-download boundary.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def select(
        self,
        project_id: str,
        product_ids: Sequence[str],
    ) -> tuple[Product, ...]:
        normalized_project_id = normalized_customer(project_id)
        requested = _required_ids(product_ids)
        product_statement = sa.select(
            knowledge_products.c.product_id,
            knowledge_products.c.metadata,
        ).where(
            knowledge_products.c.project_id == normalized_project_id,
            knowledge_products.c.product_id.in_(requested),
            knowledge_products.c.status == "confirmed",
        )

        source_evidence = knowledge_product_source_evidence.alias(
            "selection_source_evidence"
        )
        current_source = knowledge_sources.alias(
            "selection_current_source"
        )
        evidence_statement = (
            sa.select(
                source_evidence.c.product_id,
                source_evidence.c.source_id,
                source_evidence.c.snapshot_id,
                source_evidence.c.metadata,
            )
            .select_from(
                source_evidence.join(
                    current_source,
                    sa.and_(
                        current_source.c.project_id
                        == source_evidence.c.project_id,
                        current_source.c.source_id
                        == source_evidence.c.source_id,
                    ),
                )
            )
            .where(
                source_evidence.c.project_id
                == normalized_project_id,
                source_evidence.c.product_id.in_(requested),
                source_evidence.c.relation == "primary_detail",
                current_source.c.status == "published",
                current_source.c.current_snapshot_id
                == source_evidence.c.snapshot_id,
            )
            .order_by(
                source_evidence.c.product_id,
                source_evidence.c.confidence.desc(),
                source_evidence.c.source_id,
                source_evidence.c.snapshot_id,
            )
        )

        asset_evidence = knowledge_product_asset_evidence.alias(
            "selection_asset_evidence"
        )
        asset_source = knowledge_sources.alias("selection_asset_source")
        role_rank = sa.case(
            (asset_evidence.c.role == "primary", 0),
            (asset_evidence.c.role == "hero", 1),
            (asset_evidence.c.role == "gallery", 2),
            (asset_evidence.c.role == "detail", 3),
            else_=4,
        )
        asset_statement = (
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
                asset_evidence.c.project_id == normalized_project_id,
                asset_evidence.c.product_id.in_(requested),
                asset_source.c.status == "published",
                asset_source.c.current_snapshot_id
                == asset_evidence.c.snapshot_id,
            )
            .order_by(
                asset_evidence.c.product_id,
                role_rank,
                asset_evidence.c.confidence.desc(),
                asset_evidence.c.asset_id,
            )
        )
        with self._engine.connect() as connection:
            product_rows = connection.execute(
                product_statement
            ).mappings().all()
            evidence_rows = connection.execute(
                evidence_statement
            ).mappings().all()
            asset_rows = connection.execute(
                asset_statement
            ).mappings().all()

        confirmed_product_ids = {
            str(row["product_id"]) for row in product_rows
        }
        product_metadata = {
            str(row["product_id"]): (
                row["metadata"] if isinstance(row["metadata"], Mapping) else {}
            )
            for row in product_rows
        }
        evidence_by_product: dict[
            str,
            tuple[str, str, Mapping[str, object]],
        ] = {}
        for row in evidence_rows:
            product_id = str(row["product_id"])
            if product_id in evidence_by_product:
                continue
            projection = _selection_projection(row["metadata"])
            if projection is not None:
                evidence_by_product[product_id] = (
                    str(row["source_id"]),
                    str(row["snapshot_id"]),
                    projection,
                )
        if (
            confirmed_product_ids != set(requested)
            or set(evidence_by_product) != set(requested)
        ):
            raise ConfirmedProductSelectionError(
                "one or more products are not selectable in the requested project"
            )

        asset_ids: dict[str, list[str]] = {}
        for row in asset_rows:
            product_id = str(row["product_id"])
            selected_source_id, selected_snapshot_id, _ = (
                evidence_by_product[product_id]
            )
            if (
                str(row["source_id"]) != selected_source_id
                or str(row["snapshot_id"]) != selected_snapshot_id
            ):
                continue
            asset_id = str(row["asset_id"])
            values = asset_ids.setdefault(product_id, [])
            if asset_id not in values:
                values.append(asset_id)

        result: list[Product] = []
        for product_id in requested:
            _, _, projection = evidence_by_product[product_id]
            has_manual_specifications = (
                MANUAL_SPECIFICATION_TABLES_KEY
                in product_metadata[product_id]
            )
            manual_tables = product_metadata[product_id].get(
                MANUAL_SPECIFICATION_TABLES_KEY
            )
            if isinstance(manual_tables, list):
                projection = {
                    **projection,
                    "specification_tables": manual_tables,
                }
            current_asset_ids = asset_ids.get(product_id, [])
            canonical_url = _string(
                projection.get("canonical_url"),
                maximum=4096,
            )
            description = _string(
                projection.get("description"),
                maximum=2000,
            )
            product_values: dict[str, object] = {
                "product_id": product_id,
                "name": _string(
                    projection.get("name"),
                    maximum=240,
                ),
                "url": canonical_url,
                "canonical_url": canonical_url,
                "image_path": "",
                "description": description,
                "reference_summary": description,
                "reference_facts": _facts(projection),
                "specifications": _specifications(projection),
                "reference_path": "",
                "asset_manifest_path": "",
                "asset_count": len(current_asset_ids),
                "selected_asset_id": (
                    current_asset_ids[0] if current_asset_ids else ""
                ),
                "selection_reason": (
                    "Operator selected a confirmed product from the "
                    "published project catalog."
                ),
                "discovery_source": "knowledge_catalog",
                "detail_page_verified": True,
                "asset_status": (
                    "ready" if current_asset_ids else "missing"
                ),
                "asset_error": "",
            }
            if has_manual_specifications:
                product_values["specifications_overridden"] = True
            result.append(Product.model_validate(product_values))
        return tuple(result)


__all__ = [
    "ConfirmedProductSelectionError",
    "PostgresConfirmedProductSelection",
]
