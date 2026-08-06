from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import load_config  # noqa: E402
from models import TaskRecord  # noqa: E402
from services.access_control import (  # noqa: E402
    ActorIdentity,
    ProjectAccessDenied,
)
from services.job_queue import JobCancelled, JobConflict  # noqa: E402
from services.server_product_generation import (  # noqa: E402
    LlmServerProductProvider,
    ProductEvidenceBinding,
    ProductGenerationProduct,
    ProductGenerationUnavailable,
    ProductProviderReference,
    ProductTemplateReference,
    ServerProductGenerationHandler,
    ServerProductGenerationRegistry,
    apply_generated_product_candidates,
    build_server_product_prompt,
)
from server_project_http import (  # noqa: E402
    require_server_project_access,
    router as server_project_router,
)
from services.server_job_control import (  # noqa: E402
    SERVER_JOB_CONTROL_OPERATIONS,
)
from services.server_request_security import (  # noqa: E402
    AuthorizedProjectRequest,
)
from storage import RevisionConflictError  # noqa: E402


def make_task(*, revision: int = 7) -> TaskRecord:
    """Build a post-title Task whose downstream state detects side effects."""

    return TaskRecord(
        id="task-a",
        week_folder="server",
        customer="project-a.example",
        topic_index=6,
        topic="How buyers choose corrosion-resistant fasteners",
        competitor_keyword="industrial fastener guide",
        status="title_selected",
        task_dir="/server/task-a",
        selected_title="A Buyer Guide to Corrosion-Resistant Fasteners",
        outline="existing outline must remain",
        article="existing article must remain",
        revision=revision,
        created_at="2026-08-06T00:00:00+00:00",
        updated_at="2026-08-06T00:00:00+00:00",
    )


def make_product(
    product_id: str,
    *,
    source_id: str | None = None,
    snapshot_id: str | None = None,
    projection_hash: str | None = None,
) -> ProductGenerationProduct:
    """Create one already-published context row; no URLs reach the provider."""

    return ProductGenerationProduct(
        binding=ProductEvidenceBinding(
            product_id=product_id,
            source_id=source_id or f"source-{product_id}",
            snapshot_id=snapshot_id or f"snapshot-{product_id}",
            projection_hash=projection_hash or ("a" * 64),
        ),
        name=f"Product {product_id}",
        description="Verified corrosion-resistant industrial fastener.",
        category_path=("Fasteners", "Industrial"),
        reference_facts=("Published primary-detail fact",),
    )


class StubProductLlm:
    ready = True

    def __init__(
        self,
        response: str = '{"product_ids":["product-a"]}',
        *,
        model: str = "model-a",
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.model = model
        self.error = error
        self.calls: list[list[dict[str, object]]] = []

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del temperature, max_tokens
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return self.response


class RecordingProductProvider:
    ready = True
    model_identity = "model-a"

    def __init__(self, result=("product-a",)) -> None:
        self.result = tuple(result)
        self.calls: list[dict[str, object]] = []

    def generate(self, task, *, products):
        self.calls.append(
            {
                "task_id": task.id,
                "product_ids": [
                    product.binding.product_id for product in products
                ],
            }
        )
        return self.result


class RecordingContext:
    """Handler test double that exposes exact project/binding reloads."""

    def __init__(self, products, *, error: Exception | None = None) -> None:
        self.products = tuple(products)
        self.error = error
        self.calls: list[dict[str, object]] = []

    def load_current(self, *, project_id, bindings):
        self.calls.append(
            {
                "project_id": project_id,
                "bindings": tuple(bindings),
            }
        )
        if self.error is not None:
            raise self.error
        return self.products


class FakeTaskRepository:
    payload: dict[str, object] | None = None

    def __init__(self, engine, *, organization_id, project_id) -> None:
        del engine
        self.organization_id = organization_id
        self.project_id = project_id

    def get(self, task_id):
        if self.payload is None or task_id != self.payload.get("id"):
            return None
        return dict(self.payload)


class RecordingTaskWriter:
    calls: list[dict[str, object]] = []
    error: Exception | None = None

    def __init__(self, engine, **kwargs) -> None:
        del engine, kwargs

    def put(
        self,
        task,
        *,
        expected_revision,
        actor,
        action,
        details,
    ):
        self.calls.append(
            {
                "task": task.model_copy(deep=True),
                "expected_revision": expected_revision,
                "actor": actor,
                "action": action,
                "details": dict(details),
            }
        )
        if self.error is not None:
            raise self.error
        return task.model_copy(update={"revision": expected_revision + 1})


def make_job(task: TaskRecord, products) -> dict[str, object]:
    """Build the private Job request exactly as Registry enqueue does."""

    template = ProductTemplateReference.current()
    provider = ProductProviderReference(model_identity="model-a")
    return {
        "operation": "products",
        "organization_id": "organization-a",
        "project_id": "project-a.example",
        "task_id": task.id,
        "requested_by_user_id": "editor-a",
        "source_revision": task.revision,
        "request": {
            **template.private_values(),
            **provider.private_values(),
            "product_bindings": [
                product.binding.private_values() for product in products
            ],
        },
    }


class ServerProductGenerationRegistryTests(unittest.TestCase):
    def test_stop_reports_bounded_drain_and_is_idempotent(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.timeouts: list[float] = []

            def stop(self, *, timeout_seconds: float):
                self.timeouts.append(timeout_seconds)
                return SimpleNamespace(
                    dispatcher_stopped=True,
                    remaining_jobs=0,
                )

        runner = FakeRunner()
        registry = ServerProductGenerationRegistry(
            object(),
            access=object(),
            provider=SimpleNamespace(ready=True),
            handler=None,
            context=object(),
            access_repository=object(),
            audit=object(),
        )
        registry._projects[("organization-a", "project-a")] = SimpleNamespace(
            runner=runner,
        )

        report = registry.stop(timeout_seconds=0.25)

        self.assertTrue(report.drained)
        self.assertEqual(report.project_runner_count, 1)
        self.assertEqual(report.remaining_jobs, 0)
        self.assertEqual(len(runner.timeouts), 1)
        self.assertGreaterEqual(runner.timeouts[0], 0.0)
        self.assertLessEqual(runner.timeouts[0], 0.25)
        self.assertEqual(registry.stop(timeout_seconds=0.25), report)
        self.assertEqual(len(runner.timeouts), 1)


class ServerProductProviderTests(unittest.TestCase):
    def test_candidate_ids_are_explicit_in_task_json(self) -> None:
        task = make_task()
        task.product_candidate_ids = ["product-a", "product-b"]
        restored = TaskRecord.model_validate_json(task.model_dump_json())
        self.assertEqual(
            restored.product_candidate_ids, ["product-a", "product-b"]
        )

    def test_prompt_contains_only_bounded_catalog_projection(self) -> None:
        prompt = build_server_product_prompt(
            make_task(),
            products=(make_product("product-a"),),
        )
        self.assertIn('"product_id":"product-a"', prompt)
        self.assertIn("Published primary-detail fact", prompt)
        self.assertNotIn("snapshot-product-a", prompt)
        self.assertNotIn("source-product-a", prompt)

    def test_provider_requires_exact_json_and_rejects_duplicate_keys(
        self,
    ) -> None:
        invalid_responses = (
            '```json\n{"product_ids":["product-a"]}\n```',
            '{"product_ids":["product-a"],"reason":"private"}',
            '{"product_ids":["product-a"],'
            '"product_ids":["product-b"]}',
            '{"product_ids":[]}',
            '{"product_ids":["product-a","product-a"]}',
            '{"product_ids":[1]}',
            "x" * 8_001,
        )
        for response in invalid_responses:
            with self.subTest(response=response[:60]):
                provider = LlmServerProductProvider(
                    load_config(),
                    llm=StubProductLlm(response),
                )
                with self.assertRaisesRegex(
                    ProductGenerationUnavailable,
                    "^product provider returned an invalid result$",
                ):
                    provider.generate(
                        make_task(),
                        products=(make_product("product-a"),),
                    )

    def test_provider_and_prompt_failures_hide_private_details(self) -> None:
        provider = LlmServerProductProvider(
            load_config(),
            llm=StubProductLlm(
                error=RuntimeError(
                    "private-provider-detail sk-secret-value"
                )
            ),
        )
        with self.assertRaisesRegex(
            ProductGenerationUnavailable,
            "^product provider is temporarily unavailable$",
        ) as caught:
            provider.generate(
                make_task(),
                products=(make_product("product-a"),),
            )
        self.assertNotIn("private-provider-detail", str(caught.exception))
        self.assertNotIn("sk-secret-value", str(caught.exception))

        with patch(
            "services.server_product_generation.render_prompt",
            side_effect=RuntimeError("private-template-path"),
        ):
            with self.assertRaisesRegex(
                ProductGenerationUnavailable,
                "^product provider is temporarily unavailable$",
            ) as render_caught:
                provider.generate(
                    make_task(),
                    products=(make_product("product-a"),),
                )
        self.assertNotIn("private-template-path", str(render_caught.exception))

    def test_unknown_and_duplicate_provider_ids_are_rejected(self) -> None:
        task = make_task()
        for values in (("unknown",), ("product-a", "product-a")):
            with self.subTest(values=values):
                with self.assertRaisesRegex(
                    ProductGenerationUnavailable,
                    "^product provider returned an invalid result$",
                ):
                    apply_generated_product_candidates(
                        task,
                        product_ids=values,
                        allowed_product_ids=("product-a", "product-b"),
                    )

    def test_template_and_model_drift_are_conflicts_without_details(
        self,
    ) -> None:
        template = ProductTemplateReference.current()
        with patch(
            "services.server_product_generation.load_prompt_template",
            side_effect=OSError("D:/private/products.txt"),
        ):
            with self.assertRaisesRegex(
                JobConflict,
                "^pinned product template changed$",
            ) as caught:
                template.verify_current()
        self.assertNotIn("D:/private", str(caught.exception))

        reference = ProductProviderReference(model_identity="model-a")
        changed = RecordingProductProvider()
        changed.model_identity = "model-b"
        with self.assertRaisesRegex(
            JobConflict,
            "^pinned product provider changed$",
        ):
            reference.verify_current(changed)


class ServerProductGenerationHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        RecordingTaskWriter.calls = []
        RecordingTaskWriter.error = None

    def _run(
        self,
        *,
        provider=None,
        context=None,
        task=None,
        cancelled=lambda: False,
    ) -> tuple[int, RecordingContext, RecordingProductProvider]:
        current_task = task or make_task()
        products = (make_product("product-a"), make_product("product-b"))
        selected_context = context or RecordingContext(products)
        selected_provider = provider or RecordingProductProvider()
        FakeTaskRepository.payload = current_task.model_dump(mode="json")
        handler = ServerProductGenerationHandler(
            object(),
            provider=selected_provider,
            context=selected_context,
        )
        with (
            patch(
                "services.server_product_generation.PostgresTaskRepository",
                FakeTaskRepository,
            ),
            patch(
                "services.server_product_generation.PostgresAuditedTaskWriter",
                RecordingTaskWriter,
            ),
        ):
            revision = handler(
                make_job(current_task, products),
                cancelled,
            )
        return revision, selected_context, selected_provider

    def test_success_only_writes_advisory_ids_and_safe_audit_counts(self) -> None:
        task = make_task()
        before = task.model_dump(mode="json")
        revision, context, provider = self._run(task=task)
        self.assertEqual(revision, task.revision + 1)
        self.assertEqual(context.calls[0]["project_id"], "project-a.example")
        self.assertEqual(
            provider.calls[0]["product_ids"],
            ["product-a", "product-b"],
        )
        call = RecordingTaskWriter.calls[0]
        saved = call["task"]
        self.assertEqual(saved.product_candidate_ids, ["product-a"])
        self.assertEqual(saved.products, task.products)
        self.assertEqual(saved.status, before["status"])
        self.assertEqual(saved.outline, before["outline"])
        self.assertEqual(saved.article, before["article"])
        self.assertEqual(call["action"], "article.products.generated")
        self.assertEqual(
            call["details"],
            {"candidate_count": 1, "candidate_pool_count": 2},
        )

    def test_pinned_evidence_drift_blocks_provider_and_commit(self) -> None:
        context = RecordingContext(
            (),
            error=JobConflict("pinned product evidence changed"),
        )
        provider = RecordingProductProvider()
        with self.assertRaisesRegex(
            JobConflict,
            "^pinned product evidence changed$",
        ):
            self._run(provider=provider, context=context)
        self.assertEqual(provider.calls, [])
        self.assertEqual(RecordingTaskWriter.calls, [])

    def test_cancel_is_checked_before_load_provider_and_commit(self) -> None:
        for stop_at, expected_provider_calls in ((1, 0), (2, 0), (3, 1)):
            with self.subTest(stop_at=stop_at):
                calls = 0

                def cancelled() -> bool:
                    nonlocal calls
                    calls += 1
                    return calls == stop_at

                RecordingTaskWriter.calls = []
                provider = RecordingProductProvider()
                with self.assertRaises(JobCancelled):
                    self._run(
                        provider=provider,
                        cancelled=cancelled,
                    )
                self.assertEqual(len(provider.calls), expected_provider_calls)
                self.assertEqual(RecordingTaskWriter.calls, [])

    def test_cas_and_reauthorization_failures_become_generic_conflicts(
        self,
    ) -> None:
        failures = (
            RevisionConflictError("task-a", 7, 8),
            ProjectAccessDenied("private role detail"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                RecordingTaskWriter.calls = []
                RecordingTaskWriter.error = failure
                with self.assertRaisesRegex(
                    JobConflict,
                    "^(source task revision changed|job actor is not authorized)$",
                ) as caught:
                    self._run()
                self.assertNotIn("private role detail", str(caught.exception))
        RecordingTaskWriter.error = None

    def test_wrong_project_or_stale_source_cannot_reuse_job(self) -> None:
        task = make_task()
        products = (make_product("product-a"), make_product("product-b"))
        job = make_job(task, products)
        job["project_id"] = "project-b.example"
        FakeTaskRepository.payload = None
        handler = ServerProductGenerationHandler(
            object(),
            provider=RecordingProductProvider(),
            context=RecordingContext(products),
        )
        with patch(
            "services.server_product_generation.PostgresTaskRepository",
            FakeTaskRepository,
        ):
            with self.assertRaisesRegex(
                JobConflict,
                "^source task is unavailable$",
            ):
                handler(job, lambda: False)

        stale = make_task(revision=8)
        FakeTaskRepository.payload = stale.model_dump(mode="json")
        with patch(
            "services.server_product_generation.PostgresTaskRepository",
            FakeTaskRepository,
        ):
            with self.assertRaisesRegex(
                JobConflict,
                "^source task revision changed$",
            ):
                handler(make_job(task, products), lambda: False)

    def test_public_job_projection_hides_request_error_and_model(self) -> None:
        public = ServerProductGenerationRegistry._public_job(
            {
                "id": "job-a",
                "batch_id": "batch-a",
                "task_id": "task-a",
                "operation": "products",
                "status": "failed",
                "source_revision": 7,
                "result_revision": None,
                "attempts": 1,
                "created_at": "2026-08-06T00:00:00+00:00",
                "started_at": None,
                "finished_at": "2026-08-06T00:01:00+00:00",
                "updated_at": "2026-08-06T00:01:00+00:00",
                "error": "private-provider-detail sk-secret-value",
                "request": {
                    "provider_model": "private-model",
                    "product_bindings": ["private-binding"],
                },
            }
        )
        self.assertTrue(public["has_error"])
        self.assertNotIn("error", public)
        self.assertNotIn("request", public)
        self.assertNotIn("private", str(public))


class RecordingProductHttpRegistry:
    def __init__(self) -> None:
        self.enqueue_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    @staticmethod
    def public_job(*, status: str = "queued") -> dict[str, object]:
        return {
            "job_id": "job-products-a",
            "batch_id": "batch-products-a",
            "task_id": "task-a",
            "operation": "products",
            "status": status,
            "source_revision": 7,
            "result_revision": 8 if status == "succeeded" else None,
            "attempts": 1,
            "created_at": "2026-08-06T00:00:00+00:00",
            "started_at": None,
            "finished_at": (
                "2026-08-06T00:01:00+00:00"
                if status == "succeeded"
                else None
            ),
            "updated_at": "2026-08-06T00:01:00+00:00",
            "has_error": False,
        }

    def enqueue(self, **kwargs):
        self.enqueue_calls.append(dict(kwargs))
        return self.public_job()

    def get_job(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        return self.public_job(status="succeeded")


class ServerProductGenerationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = RecordingProductHttpRegistry()
        self.authorized = AuthorizedProjectRequest(
            actor=ActorIdentity("organization-a", "editor-a"),
            project_id="project-a.example",
            permission="article.edit",
        )
        self.application = FastAPI()
        self.application.include_router(server_project_router)
        self.application.dependency_overrides[
            require_server_project_access
        ] = lambda: self.authorized
        self.client = TestClient(self.application)

    def tearDown(self) -> None:
        self.client.close()

    def test_strict_product_routes_return_only_safe_job_projection(
        self,
    ) -> None:
        def preserve_authorized(_request, authorized, _permission):
            return authorized

        with (
            patch(
                "server_project_http._require_project_permission",
                side_effect=preserve_authorized,
            ),
            patch(
                "server_project_http._product_generation",
                return_value=self.registry,
            ),
        ):
            rejected = self.client.post(
                "/api/projects/project-a.example/tasks/task-a/products",
                json={
                    "revision": 7,
                    "provider_model": "client-override",
                },
            )
            self.assertEqual(rejected.status_code, 422, rejected.text)

            queued = self.client.post(
                "/api/projects/project-a.example/tasks/task-a/products",
                json={"revision": 7},
            )
            self.assertEqual(queued.status_code, 200, queued.text)
            self.assertEqual(queued.json()["operation"], "products")
            self.assertNotIn("request", queued.json())
            self.assertNotIn("provider_model", queued.text)
            self.assertEqual(
                self.registry.enqueue_calls[0]["source_revision"],
                7,
            )
            self.assertEqual(
                self.registry.enqueue_calls[0]["actor"],
                self.authorized.actor,
            )

            status = self.client.get(
                "/api/projects/project-a.example/tasks/task-a/"
                "products/jobs/job-products-a"
            )
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["status"], "succeeded")
            self.assertNotIn("request", status.json())
            self.assertEqual(
                self.registry.get_calls[0]["job_id"],
                "job-products-a",
            )
            self.assertIn("products", SERVER_JOB_CONTROL_OPERATIONS)


if __name__ == "__main__":
    unittest.main()
