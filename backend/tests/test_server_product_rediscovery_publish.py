from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import ActorIdentity  # noqa: E402
from services.server_product_rediscovery import (  # noqa: E402
    ServerProductRediscoveryHandler,
)
from services.server_private_document_ingestion import (  # noqa: E402
    _asset_uri_matches_scope,
    _matching_confirmed_product,
)
from services.server_request_security import knowledge_permission_for  # noqa: E402
from knowledge_agent.catalog import KnowledgeProduct  # noqa: E402
from knowledge_agent.embedding import EmbeddingProviderError  # noqa: E402
from knowledge_agent.web_ingestion import WebPageIngestionConflict  # noqa: E402
from knowledge_agent.publication import KnowledgePublicationError  # noqa: E402


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def review_snapshot(self, **kwargs) -> None:
        self.calls.append(("review", kwargs))

    def publish_source(self, **kwargs) -> None:
        self.calls.append(("publish", kwargs))

    def confirm_product(self, **kwargs) -> None:
        self.calls.append(("confirm", kwargs))


class RacingCommands(RecordingCommands):
    def review_snapshot(self, **kwargs) -> None:
        self.calls.append(("review", kwargs))
        raise KnowledgePublicationError(
            "only the pending snapshot can be reviewed"
        )


class FlakyPublishingCommands(RecordingCommands):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def publish_source(self, **kwargs) -> None:
        self.calls.append(("publish", kwargs))
        if self.failures:
            self.failures -= 1
            raise EmbeddingProviderError("embedding request failed (ReadTimeout)")


def page(source_id: str, snapshot_id: str, product_id: str | None = None):
    return SimpleNamespace(
        source=SimpleNamespace(
            source_id=source_id,
            source_kind="official_web",
            trust_tier="official",
        ),
        snapshot=SimpleNamespace(snapshot_id=snapshot_id),
        product=(
            None
            if product_id is None
            else SimpleNamespace(product_id=product_id)
        ),
    )


class ProductRediscoveryAutoPublishTests(unittest.TestCase):
    def test_publishes_each_page_and_confirms_only_products(self) -> None:
        commands = RecordingCommands()
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        actor = ActorIdentity(organization_id="org", user_id="user")

        handler._publish(
            actor=actor,
            project_id="project",
            result=SimpleNamespace(
                pages=(
                    page("category", "snapshot-category"),
                    page("product", "snapshot-product", "revo-hess"),
                ),
            ),
        )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "publish", "review", "publish", "confirm"],
        )
        first_review = commands.calls[0][1]
        self.assertEqual(first_review["decision"], "approve")
        self.assertEqual(first_review["reviewer_kind"], "automation")
        self.assertEqual(first_review["source_id"], "category")
        self.assertEqual(
            commands.calls[-1][1]["product_id"],
            "revo-hess",
        )

    def test_already_active_snapshot_turns_review_race_into_success(self) -> None:
        commands = RacingCommands()
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        handler._snapshot_is_active = lambda **_kwargs: True  # type: ignore[method-assign]

        handler._publish(
            actor=ActorIdentity(organization_id="org", user_id="user"),
            project_id="project",
            result=SimpleNamespace(
                pages=(page("product", "snapshot-product", "revo-hess"),),
            ),
        )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "confirm"],
        )

    def test_content_classified_b2b_product_without_table_is_confirmed(self) -> None:
        commands = RecordingCommands()
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        candidate = page("weak", "snapshot-weak", "weak-product")
        candidate.classification = SimpleNamespace(
            page_type="product_detail",
            canonical_url="https://example.com/generic-machine/",
            reasons=("the conservative B2B product-page detector found evidence",),
        )
        candidate.product.metadata = {"specification_tables": []}

        handler._publish(
            actor=ActorIdentity(organization_id="org", user_id="user"),
            project_id="project",
            result=SimpleNamespace(pages=(candidate,)),
        )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "publish", "confirm"],
        )

    def test_non_product_classification_is_never_confirmed(self) -> None:
        commands = RecordingCommands()
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        candidate = page("blog", "snapshot-blog", "related-product")
        candidate.classification = SimpleNamespace(
            page_type="official_blog",
            canonical_url="https://example.com/single-post/guide/",
            reasons=("editorial article markers are present",),
        )

        handler._publish(
            actor=ActorIdentity(organization_id="org", user_id="user"),
            project_id="project",
            result=SimpleNamespace(pages=(candidate,)),
        )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "publish"],
        )

    def test_generic_b2b_product_with_specification_table_is_confirmed(self) -> None:
        commands = RecordingCommands()
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        candidate = page("strong", "snapshot-strong", "strong-product")
        candidate.classification = SimpleNamespace(
            page_type="product_detail",
            canonical_url="https://example.com/98-2x34-lip-plate-insert/",
            reasons=("the conservative B2B product-page detector found evidence",),
        )
        candidate.product.metadata = {
            "specification_tables": [{"headers": ["Material"], "rows": [["S136"]]}]
        }

        handler._publish(
            actor=ActorIdentity(organization_id="org", user_id="user"),
            project_id="project",
            result=SimpleNamespace(pages=(candidate,)),
        )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "publish", "confirm"],
        )

    def test_generic_product_schema_without_b2b_detector_is_not_confirmed(self) -> None:
        commands = RecordingCommands()
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        candidate = page("schema", "snapshot-schema", "schema-product")
        candidate.classification = SimpleNamespace(
            page_type="product_detail",
            canonical_url="https://example.com/generic-machine/",
            reasons=("schema.org Product data is present",),
        )

        handler._publish(
            actor=ActorIdentity(organization_id="org", user_id="user"),
            project_id="project",
            result=SimpleNamespace(pages=(candidate,)),
        )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "publish"],
        )

    def test_category_and_fixed_page_paths_are_never_confirmed(self) -> None:
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=RecordingCommands(),  # type: ignore[arg-type]
        )
        for path in (
            "/product-category/roof-ladders/",
            "/home-2/",
            "/home-3/",
            "/live/",
            "/odm/",
            "/oem/",
            "/oem-odm/",
            "/privacy-policy/",
            "/thanks/",
            "/fr/oem-odm/",
            "/fr/politique-de-confidentialite/",
            "/de/datenschutzerklarung/",
        ):
            with self.subTest(path=path):
                candidate = page("false", "snapshot-false", "false-product")
                candidate.classification = SimpleNamespace(
                    page_type="product_detail",
                    canonical_url=f"https://example.com{path}",
                    reasons=(
                        "the conservative B2B product-page detector found evidence",
                    ),
                )
                self.assertFalse(handler._product_is_auto_confirmable(candidate))

    def test_transient_embedding_failure_retries_the_same_page_publication(self) -> None:
        commands = FlakyPublishingCommands(failures=1)
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        with mock.patch(
            "services.server_product_rediscovery.PUBLICATION_RETRY_DELAYS_SECONDS",
            (0.0, 0.0),
        ):
            handler._publish(
                actor=ActorIdentity(organization_id="org", user_id="user"),
                project_id="project",
                result=SimpleNamespace(pages=(page("source", "snapshot"),)),
            )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "publish", "publish"],
        )

    def test_exhausted_embedding_retries_reject_pending_page_for_rescan(self) -> None:
        commands = FlakyPublishingCommands(failures=3)
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        with mock.patch(
            "services.server_product_rediscovery.PUBLICATION_RETRY_DELAYS_SECONDS",
            (0.0, 0.0),
        ):
            with self.assertRaisesRegex(
                WebPageIngestionConflict,
                "embedding retries",
            ):
                handler._publish(
                    actor=ActorIdentity(organization_id="org", user_id="user"),
                    project_id="project",
                    result=SimpleNamespace(pages=(page("source", "snapshot"),)),
                )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "publish", "publish", "publish", "review"],
        )
        self.assertEqual(commands.calls[-1][1]["decision"], "reject")

    def test_localized_product_mirror_is_published_but_not_confirmed(self) -> None:
        commands = RecordingCommands()
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        candidate = page("localized", "snapshot-localized", "localized-product")
        candidate.classification = SimpleNamespace(
            page_type="product_detail",
            canonical_url="https://example.com/de/produkt/parkettboden-lzy11/",
            reasons=("WooCommerce single-product markup is present",),
        )
        candidate.product.metadata = {
            "specification_tables": [{"rows": [["Material", "Oak"]]}]
        }

        handler._publish(
            actor=ActorIdentity(organization_id="org", user_id="user"),
            project_id="project",
            result=SimpleNamespace(pages=(candidate,)),
        )

        self.assertEqual(
            [name for name, _kwargs in commands.calls],
            ["review", "publish"],
        )

    def test_review_race_still_fails_when_snapshot_is_not_active(self) -> None:
        commands = RacingCommands()
        handler = ServerProductRediscoveryHandler(
            object(),
            sync_factory=lambda *_args: object(),
            commands=commands,  # type: ignore[arg-type]
        )
        handler._snapshot_is_active = lambda **_kwargs: False  # type: ignore[method-assign]

        with self.assertRaisesRegex(
            KnowledgePublicationError,
            "only the pending snapshot",
        ):
            handler._publish(
                actor=ActorIdentity(organization_id="org", user_id="user"),
                project_id="project",
                result=SimpleNamespace(
                    pages=(page("source", "snapshot"),),
                ),
            )

    def test_private_document_organizer_requires_one_exact_product_name(self) -> None:
        revo = KnowledgeProduct(
            project_id="project",
            product_id="revo-hess",
            name="REVO HESS",
            status="confirmed",
        )
        other = KnowledgeProduct(
            project_id="project",
            product_id="revo-vm",
            name="REVO VM",
            status="confirmed",
        )

        matched = _matching_confirmed_product(
            (revo, other),
            display_name="REVO-HESS datasheet.pdf",
            chunks=(SimpleNamespace(text="Rated power 8000W"),),
        )
        ambiguous = _matching_confirmed_product(
            (revo, other),
            display_name="comparison.pdf",
            chunks=(SimpleNamespace(text="REVO HESS and REVO VM"),),
        )

        self.assertEqual(matched, revo)
        self.assertIsNone(ambiguous)

    def test_manual_scan_requires_publish_and_keeps_object_scope_check(self) -> None:
        self.assertEqual(
            knowledge_permission_for(
                "POST",
                "/api/knowledge/project/official-site/scan",
            ),
            "knowledge.publish",
        )
        self.assertTrue(
            _asset_uri_matches_scope(
                "s3://bucket/organizations/org/projects/project/blobs/aa/"
                + "a" * 64,
                bucket="bucket",
                organization_id="org",
                project_id="project",
                content_hash="a" * 64,
            )
        )


if __name__ == "__main__":
    unittest.main()
