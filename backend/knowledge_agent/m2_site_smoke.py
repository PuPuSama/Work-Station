from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import config as app_config
from config import initialize_environment
from .contracts import KnowledgeProject
from .runtime import create_knowledge_runtime
from .settings import KnowledgeAgentSettings
from .wordpress import normalize_site_url


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Persist one bounded official product-category slice into the M2 Inbox."
        )
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--category-url", required=True)
    parser.add_argument("--max-products", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    initialize_environment()
    settings = KnowledgeAgentSettings.from_env(enabled=True)
    if settings.database_url is None:
        raise ValueError("ARTICLE_AGENT_DATABASE_URL is required")
    project_id = args.project.strip().lower().removeprefix("www.").rstrip(".")
    site_url = normalize_site_url(args.site_url)
    artifact_root = Path(
        os.environ.get(
            "ARTICLE_AGENT_KNOWLEDGE_ROOT",
            str(app_config.application_root() / "data" / "knowledge-agent"),
        )
    )
    runtime = create_knowledge_runtime(
        database_url=settings.database_url,
        artifact_root=artifact_root,
    )
    try:
        runtime.repository.upsert_project(
            KnowledgeProject(
                project_id=project_id,
                customer_name=args.project,
                official_domain=project_id,
            )
        )
        result = runtime.wordpress_sync.sync_category(
            project_id=project_id,
            site_url=site_url,
            category_url=args.category_url,
            max_products=args.max_products,
        )
        print(
            json.dumps(
                {
                    "project_id": project_id,
                    "wordpress_detected": result.probe.detected,
                    "category_source_id": result.category.source.source_id,
                    "category_page_type": result.category.classification.page_type,
                    "category_chunk_count": len(result.category.chunks),
                    "product_count": len(result.products),
                    "product_ids": [
                        item.product.product_id
                        for item in result.products
                        if item.product is not None
                    ],
                    "product_source_ids": [
                        item.source.source_id for item in result.products
                    ],
                    "asset_count": sum(len(item.assets) for item in result.products),
                    "skipped_count": len(result.skipped_urls),
                    "warning_count": (
                        len(result.warnings)
                        + sum(len(item.warnings) for item in result.products)
                    ),
                },
                ensure_ascii=False,
            )
        )
    finally:
        runtime.close()


def cli() -> None:
    """Keep smoke output stable and prevent exception details from leaking secrets."""

    try:
        main()
    except Exception:
        print(
            json.dumps({"error_code": "M2_SITE_SMOKE_FAILED"}),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
