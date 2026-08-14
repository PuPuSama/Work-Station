from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from knowledge_agent.database import create_knowledge_engine  # noqa: E402
from knowledge_agent.schema import projects  # noqa: E402
from config import load_config  # noqa: E402
from models import OfficialLink, PromptSnapshot, TaskRecord  # noqa: E402
from server_schema import (  # noqa: E402
    organizations,
    project_memberships,
    project_ownership,
    project_prompt_defaults,
    project_prompt_heads,
    project_prompt_versions,
    workspace_users,
)
from services.access_control import (  # noqa: E402
    ActorIdentity,
    ProjectAccessDenied,
)
from services.job_queue import JobConflict  # noqa: E402
from services.server_project_prompts import (  # noqa: E402
    PostgresProjectPromptService,
    ServerProjectPromptConflict,
    ServerProjectPromptError,
    ServerProjectPromptUnavailable,
    ServerProjectPromptServiceFactory,
    _prompt_scope_lock_identity,
)
from services.postgres_task_repository import (  # noqa: E402
    PostgresTaskRepository,
)
from services.server_outline_generation import (  # noqa: E402
    LlmServerOutlineProvider,
    OutlineGenerationUnavailable,
    OutlinePromptReference,
    ServerOutlineGenerationHandler,
)
from services.server_title_generation import (  # noqa: E402
    LlmServerTitleProvider,
    ServerTitleGenerationHandler,
    TitleGenerationUnavailable,
    TitleTemplateReference,
    build_server_title_prompt,
)
from services.server_article_generation import (  # noqa: E402
    ArticleGenerationUnavailable,
    LlmServerArticleProvider,
    ServerArticleGenerationHandler,
    apply_generated_article_draft,
    build_server_article_prompt,
)
from services.server_task_commands import (  # noqa: E402
    ServerTaskCommandUnavailable,
)
from services.server_auth import (  # noqa: E402
    SERVER_AUTH_COOKIE_NAME,
    ServerActorSessionCodec,
)


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[object] = []

    def append(self, connection, event) -> None:
        if not connection.in_transaction():
            raise AssertionError("audit must share the prompt transaction")
        self.events.append(event)


class FailingAuditWriter:
    def append(self, connection, event) -> None:
        del connection, event
        raise RuntimeError("private injected audit failure")


class RecordingOutlineProvider:
    def __init__(self) -> None:
        self.versions: list[int] = []

    def generate(
        self,
        task,
        *,
        prompt_snapshot,
        context_chunks,
    ):
        del task, context_chunks
        self.versions.append(prompt_snapshot.version)
        return "## Pinned prompt outline"


class LeakingOutlineLlm:
    ready = True

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del messages, temperature, max_tokens
        raise RuntimeError("provider leaked private-provider-detail")


class RecordingTitleProvider:
    def generate(
        self,
        task,
        *,
        title_count,
        context_chunks,
    ):
        del task, context_chunks
        return tuple(
            f"Rollback candidate {index}"
            for index in range(1, title_count + 1)
        )


class LeakingTitleLlm:
    ready = True

    def __init__(self, response: str | None = None) -> None:
        self.response = response

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del messages, temperature, max_tokens
        if self.response is None:
            raise RuntimeError(
                "provider leaked private-title-provider-detail"
            )
        return self.response


VALID_SERVER_ARTICLE = """# Pinned Article

This introduction provides a transition before the detailed sections.

## Buyer Checks

### Confirm requirements

Confirm the application requirements before selection.

### Compare evidence

Compare published evidence before approval.

## FAQ

### What should buyers confirm?

Buyers should confirm application requirements.

### Why compare evidence?

Evidence supports a reliable decision.

### When should approval happen?

Approval should follow the documented checks.
"""


class RecordingArticleProvider:
    def __init__(self) -> None:
        self.versions: list[int] = []

    def generate(
        self,
        task,
        *,
        target_words,
        prompt_snapshot,
        context_chunks,
    ):
        del task, target_words, context_chunks
        self.versions.append(prompt_snapshot.version)
        return VALID_SERVER_ARTICLE


class LeakingArticleLlm:
    ready = True

    def __init__(self, response: str | None = None) -> None:
        self.response = response

    def chat(self, messages, temperature=0.7, max_tokens=1800):
        del messages, temperature, max_tokens
        if self.response is None:
            raise RuntimeError(
                "provider leaked private-article-provider-detail"
            )
        return self.response


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_DATABASE_URL"),
    "ARTICLE_AGENT_DATABASE_URL is required for PostgreSQL integration tests",
)
class ServerProjectPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_knowledge_engine(
            os.environ["ARTICLE_AGENT_DATABASE_URL"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def setUp(self) -> None:
        prefix = f"m7-prompt-{uuid.uuid4().hex}"
        self.organization_id = f"{prefix}-org"
        self.project_id = f"{prefix}.example.test"
        self.other_project_id = f"other-{prefix}.example.test"
        self.editor_id = f"{prefix}-editor"
        self.viewer_id = f"{prefix}-viewer"
        with self.engine.begin() as connection:
            connection.execute(
                organizations.insert().values(
                    organization_id=self.organization_id,
                    name="Prompt Test Organization",
                )
            )
            connection.execute(
                workspace_users.insert(),
                [
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.editor_id,
                        "display_name": "Prompt Editor",
                    },
                    {
                        "organization_id": self.organization_id,
                        "user_id": self.viewer_id,
                        "display_name": "Prompt Viewer",
                    },
                ],
            )
            connection.execute(
                projects.insert(),
                [
                    {
                        "project_id": self.project_id,
                        "customer_name": "Prompt Project",
                        "official_domain": self.project_id,
                    },
                    {
                        "project_id": self.other_project_id,
                        "customer_name": "Other Prompt Project",
                        "official_domain": self.other_project_id,
                    },
                ],
            )
            connection.execute(
                project_ownership.insert(),
                [
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.other_project_id,
                    },
                ],
            )
            connection.execute(
                project_memberships.insert(),
                [
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                        "user_id": self.editor_id,
                        "role": "editor",
                        "granted_by_user_id": self.editor_id,
                    },
                    {
                        "organization_id": self.organization_id,
                        "project_id": self.project_id,
                        "user_id": self.viewer_id,
                        "role": "viewer",
                        "granted_by_user_id": self.editor_id,
                    },
                ],
            )
        self.editor = ActorIdentity(
            self.organization_id,
            self.editor_id,
        )
        self.viewer = ActorIdentity(
            self.organization_id,
            self.viewer_id,
        )
        self.audit = RecordingAuditWriter()
        self.service = PostgresProjectPromptService(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            audit=self.audit,
        )


    def test_edit_updates_current_prompt_version_and_default(
        self,
    ) -> None:
        created = self.service.create(
            self.editor,
            name="  Buyer outline  ",
            kind="outline",
            content="First\r\nprompt",
        )
        self.assertEqual(created.version, 1)
        self.assertEqual(created.name, "Buyer outline")
        self.assertEqual(created.content, "First\nprompt")

        default_v1 = self.service.set_default(
            self.editor,
            kind="outline",
            prompt_id=created.prompt_id,
        )
        self.assertEqual(default_v1.version, 1)
        updated = self.service.update(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            name="Buyer outline v2",
            content="Second prompt",
        )
        self.assertEqual(updated.version, 1)
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="outline",
                selection="project_default",
            ).version,
            1,
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="outline",
                selection="project_default",
            ).content,
            "Second prompt",
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="outline",
                selection=created.prompt_id,
            ).version,
            1,
        )
        default_v2 = self.service.set_default(
            self.editor,
            kind="outline",
            prompt_id=created.prompt_id,
        )
        self.assertEqual(default_v2.version, 1)

        with self.engine.connect() as connection:
            versions = connection.execute(
                sa.select(
                    project_prompt_versions.c.version,
                    project_prompt_versions.c.content,
                    project_prompt_versions.c.content_hash,
                )
                .where(
                    project_prompt_versions.c.organization_id
                    == self.organization_id,
                    project_prompt_versions.c.project_id
                    == self.project_id,
                    project_prompt_versions.c.prompt_id
                    == created.prompt_id,
                )
                .order_by(project_prompt_versions.c.version)
            ).all()
        self.assertEqual(
            [(row.version, row.content) for row in versions],
            [(1, "Second prompt")],
        )
        self.assertTrue(
            all(len(row.content_hash) == 64 for row in versions)
        )
        self.assertEqual(
            [event.action for event in self.audit.events],
            [
                "project_prompt.created",
                "project_prompt.default.updated",
                "project_prompt.updated",
            ],
        )
        self.assertNotIn("First", str(self.audit.events))
        self.assertNotIn("Second", str(self.audit.events))

    def test_humanize_prompt_requires_one_article_placeholder(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ServerProjectPromptError,
            "exactly one",
        ):
            self.service.create(
                self.editor,
                name="Invalid humanizer",
                kind="humanize",
                content="Rewrite without an article placeholder.",
            )
        created = self.service.create(
            self.editor,
            name="Project humanizer",
            kind="humanize",
            content="Rewrite safely.\n\n{{ARTICLE}}",
        )
        self.service.set_default(
            self.editor,
            kind="humanize",
            prompt_id=created.prompt_id,
        )

        resolved = self.service.resolve(
            self.viewer,
            kind="humanize",
            selection="project_default",
        )

        self.assertEqual(resolved.prompt_id, created.prompt_id)
        self.assertEqual(resolved.kind, "humanize")
        self.assertEqual(resolved.content.count("{{ARTICLE}}"), 1)

    def test_update_can_change_kind_without_mutating_old_history(self) -> None:
        created = self.service.create(
            self.editor,
            name="Misclassified review prompt",
            kind="outline",
            content="Check factual and SEO requirements.",
        )
        self.service.set_default(
            self.editor,
            kind="outline",
            prompt_id=created.prompt_id,
        )

        replacement = self.service.update(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            name="Review prompt",
            kind="review",
            content="Check factual and SEO requirements.",
        )

        self.assertNotEqual(replacement.prompt_id, created.prompt_id)
        self.assertEqual(replacement.kind, "review")
        self.assertEqual(replacement.version, 1)
        directory = self.service.list(self.viewer)
        items = {item.snapshot.prompt_id: item for item in directory.prompts}
        self.assertEqual(items[created.prompt_id].status, "archived")
        self.assertEqual(items[replacement.prompt_id].status, "active")
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="outline",
                selection="project_default",
            ).source,
            "system",
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="review",
                selection=replacement.prompt_id,
            ).prompt_id,
            replacement.prompt_id,
        )
        with self.engine.connect() as connection:
            versions = connection.execute(
                sa.select(
                    project_prompt_versions.c.prompt_id,
                    project_prompt_versions.c.kind,
                ).where(
                    project_prompt_versions.c.organization_id
                    == self.organization_id,
                    project_prompt_versions.c.project_id == self.project_id,
                    project_prompt_versions.c.prompt_id.in_(
                        (created.prompt_id, replacement.prompt_id)
                    ),
                )
            ).all()
        self.assertEqual(
            set(versions),
            {
                (created.prompt_id, "outline"),
                (replacement.prompt_id, "review"),
            },
        )

    def test_writing_settings_resolution_holds_stable_prompt_scope_lock(
        self,
    ) -> None:
        lock_identity = _prompt_scope_lock_identity(
            self.organization_id,
            self.project_id,
        )
        with self.engine.begin() as connection:
            resolved = self.service.resolve_for_update_in_transaction(
                connection,
                self.editor,
                kind="outline",
                selection="project_default",
            )
            self.assertEqual(resolved.source, "system")
            with self.engine.begin() as competing_connection:
                competing_lock = competing_connection.execute(
                    sa.select(
                        sa.func.pg_try_advisory_xact_lock(
                            sa.func.hashtextextended(lock_identity, 0)
                        )
                    )
                ).scalar_one()
                self.assertFalse(competing_lock)

        with self.engine.begin() as connection:
            released_lock = connection.execute(
                sa.select(
                    sa.func.pg_try_advisory_xact_lock(
                        sa.func.hashtextextended(lock_identity, 0)
                    )
                )
            ).scalar_one()
            self.assertTrue(released_lock)

    def test_archive_clears_default_without_deleting_versions(self) -> None:
        created = self.service.create(
            self.editor,
            name="Article prompt",
            kind="article",
            content="Write a useful article.",
        )
        self.service.set_default(
            self.editor,
            kind="article",
            prompt_id=created.prompt_id,
        )
        archived = self.service.set_active(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            active=False,
        )
        self.assertEqual(archived.status, "archived")
        resolved = self.service.resolve(
            self.viewer,
            kind="article",
            selection="project_default",
        )
        self.assertEqual(resolved.source, "system")
        with self.assertRaises(ServerProjectPromptError):
            self.service.resolve(
                self.viewer,
                kind="article",
                selection=created.prompt_id,
            )
        self.service.set_active(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            active=True,
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="article",
                selection=created.prompt_id,
            ).version,
            1,
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(project_prompt_versions)
                    .where(
                        project_prompt_versions.c.organization_id
                        == self.organization_id,
                        project_prompt_versions.c.project_id
                        == self.project_id,
                        project_prompt_versions.c.prompt_id
                        == created.prompt_id,
                    )
            ).scalar_one(),
            1,
        )

    def test_delete_removes_prompt_and_default(self) -> None:
        created = self.service.create(
            self.editor,
            name="Temporary prompt",
            kind="outline",
            content="Delete this prompt.",
        )
        self.service.set_default(
            self.editor,
            kind="outline",
            prompt_id=created.prompt_id,
        )

        self.service.delete(
            self.editor,
            prompt_id=created.prompt_id,
        )

        self.assertNotIn(
            created.prompt_id,
            {item.snapshot.prompt_id for item in self.service.list(self.viewer).prompts},
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="outline",
                selection="project_default",
            ).source,
            "system",
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(project_prompt_heads)
                    .where(
                        project_prompt_heads.c.organization_id
                        == self.organization_id,
                        project_prompt_heads.c.project_id == self.project_id,
                        project_prompt_heads.c.prompt_id == created.prompt_id,
                    )
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(project_prompt_versions)
                    .where(
                        project_prompt_versions.c.organization_id
                        == self.organization_id,
                        project_prompt_versions.c.project_id == self.project_id,
                        project_prompt_versions.c.prompt_id == created.prompt_id,
                    )
                ).scalar_one(),
                0,
            )

    def test_viewer_can_resolve_but_cannot_write_or_cross_project(
        self,
    ) -> None:
        created = self.service.create(
            self.editor,
            name="Review prompt",
            kind="review",
            content="Review evidence.",
        )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="review",
                selection=created.prompt_id,
            ).prompt_id,
            created.prompt_id,
        )
        with self.assertRaises(ProjectAccessDenied):
            self.service.create(
                self.viewer,
                name="Denied",
                kind="review",
                content="Must not persist.",
            )
        other = PostgresProjectPromptService(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.other_project_id,
        )
        with self.assertRaises(ProjectAccessDenied):
            other.resolve(
                self.editor,
                kind="review",
                selection=created.prompt_id,
            )
        with self.assertRaises(ProjectAccessDenied):
            self.service.resolve(
                ActorIdentity("another-organization", self.editor_id),
                kind="review",
                selection=created.prompt_id,
            )

    def test_update_keeps_version_and_rejects_wrong_current_version(self) -> None:
        created = self.service.create(
            self.editor,
            name="Outline prompt",
            kind="outline",
            content="Outline v1.",
        )
        self.service.update(
            self.editor,
            prompt_id=created.prompt_id,
            expected_version=1,
            name="Outline prompt",
            content="Outline v2.",
        )
        event_count = len(self.audit.events)
        with self.assertRaises(ServerProjectPromptConflict):
            self.service.update(
                self.editor,
                prompt_id=created.prompt_id,
                expected_version=2,
                name="Stale",
                content="Must not persist.",
            )
        with self.assertRaises(ServerProjectPromptError):
            self.service.set_default(
                self.editor,
                kind="article",
                prompt_id=created.prompt_id,
            )
        self.assertEqual(len(self.audit.events), event_count)
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(project_prompt_versions)
                    .where(
                        project_prompt_versions.c.organization_id
                        == self.organization_id,
                        project_prompt_versions.c.project_id
                        == self.project_id,
                        project_prompt_versions.c.prompt_id
                        == created.prompt_id,
                    )
                ).scalar_one(),
                1,
            )

    def test_audit_failure_rolls_back_prompt_creation(self) -> None:
        service = PostgresProjectPromptService(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            audit=FailingAuditWriter(),
        )
        with self.assertRaisesRegex(
            ServerProjectPromptUnavailable,
            "temporarily unavailable",
        ) as captured:
            service.create(
                self.editor,
                name="Rollback prompt",
                kind="outline",
                content="Private prompt body.",
            )
        self.assertNotIn(
            "private injected audit failure",
            str(captured.exception),
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(project_prompt_heads)
                    .where(
                        project_prompt_heads.c.organization_id
                        == self.organization_id,
                        project_prompt_heads.c.project_id
                        == self.project_id,
                    )
                ).scalar_one(),
                0,
            )

    def test_audit_failure_rolls_back_prompt_edit_and_default(self) -> None:
        created = self.service.create(
            self.editor,
            name="Stable prompt",
            kind="outline",
            content="Stable version one.",
        )
        service = PostgresProjectPromptService(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
            audit=FailingAuditWriter(),
        )
        with self.assertRaises(ServerProjectPromptUnavailable):
            service.update(
                self.editor,
                prompt_id=created.prompt_id,
                expected_version=1,
                name="Must roll back",
                content="Private version two.",
            )
        with self.assertRaises(ServerProjectPromptUnavailable):
            service.set_default(
                self.editor,
                kind="outline",
                prompt_id=created.prompt_id,
            )
        with self.engine.connect() as connection:
            head = connection.execute(
                sa.select(project_prompt_heads.c.current_version).where(
                    project_prompt_heads.c.organization_id
                    == self.organization_id,
                    project_prompt_heads.c.project_id
                    == self.project_id,
                    project_prompt_heads.c.prompt_id
                    == created.prompt_id,
                )
            ).scalar_one()
            version_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(project_prompt_versions)
                .where(
                    project_prompt_versions.c.organization_id
                    == self.organization_id,
                    project_prompt_versions.c.project_id
                    == self.project_id,
                    project_prompt_versions.c.prompt_id
                    == created.prompt_id,
                )
            ).scalar_one()
            default_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(project_prompt_defaults)
                .where(
                    project_prompt_defaults.c.organization_id
                    == self.organization_id,
                    project_prompt_defaults.c.project_id
                    == self.project_id,
                )
            ).scalar_one()
        self.assertEqual(head, 1)
        self.assertEqual(version_count, 1)
        self.assertEqual(default_count, 0)

    def test_database_allows_version_edit_and_rejects_cross_project_pointer(
        self,
    ) -> None:
        created = self.service.create(
            self.editor,
            name="Immutable prompt",
            kind="outline",
            content="Immutable body.",
        )
        with self.engine.begin() as connection:
            connection.execute(
                project_prompt_versions.update()
                .where(
                    project_prompt_versions.c.organization_id
                    == self.organization_id,
                    project_prompt_versions.c.project_id
                    == self.project_id,
                    project_prompt_versions.c.prompt_id
                    == created.prompt_id,
                )
                .values(content="mutated")
            )
        self.assertEqual(
            self.service.resolve(
                self.viewer,
                kind="outline",
                selection=created.prompt_id,
            ).content,
            "mutated",
        )
        with self.assertRaises(sa.exc.IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    project_prompt_defaults.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.other_project_id,
                        kind="outline",
                        prompt_id=created.prompt_id,
                        version=1,
                    )
                )
        with self.assertRaises(sa.exc.IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    project_prompt_defaults.insert().values(
                        organization_id=self.organization_id,
                        project_id=self.project_id,
                        kind="article",
                        prompt_id=created.prompt_id,
                        version=1,
                    )
                )

    def test_schema_exposes_prompt_constraints_and_indexes(self) -> None:
        inspector = sa.inspect(self.engine)
        self.assertTrue(
            {
                "project_prompt_heads",
                "project_prompt_versions",
                "project_prompt_defaults",
            }.issubset(inspector.get_table_names())
        )
        head_check_rows = {
            item["name"]: str(item.get("sqltext") or "")
            for item in inspector.get_check_constraints(
                "project_prompt_heads"
            )
        }
        version_check_rows = {
            item["name"]: str(item.get("sqltext") or "")
            for item in inspector.get_check_constraints(
                "project_prompt_versions"
            )
        }
        default_check_rows = {
            item["name"]: str(item.get("sqltext") or "")
            for item in inspector.get_check_constraints(
                "project_prompt_defaults"
            )
        }
        head_checks = set(head_check_rows)
        version_checks = set(version_check_rows)
        head_foreign_keys = {
            item["name"]
            for item in inspector.get_foreign_keys(
                "project_prompt_heads"
            )
        }
        default_foreign_keys = {
            item["name"]
            for item in inspector.get_foreign_keys(
                "project_prompt_defaults"
            )
        }
        indexes = {
            item["name"]
            for item in inspector.get_indexes(
                "project_prompt_heads"
            )
        }
        self.assertIn("ck_project_prompt_heads_kind", head_checks)
        self.assertIn(
            "ck_project_prompt_heads_current_version",
            head_checks,
        )
        self.assertIn(
            "ck_project_prompt_versions_hash",
            version_checks,
        )
        self.assertIn(
            "ck_project_prompt_versions_kind",
            version_checks,
        )
        self.assertIn(
            "ck_project_prompt_defaults_kind",
            default_check_rows,
        )
        for expression in (
            head_check_rows["ck_project_prompt_heads_kind"],
            version_check_rows["ck_project_prompt_versions_kind"],
            default_check_rows["ck_project_prompt_defaults_kind"],
        ):
            self.assertIn("humanize", expression.lower())
        self.assertIn(
            "fk_project_prompt_heads_current_version",
            head_foreign_keys,
        )
        self.assertIn(
            "fk_project_prompt_defaults_version",
            default_foreign_keys,
        )
        self.assertIn(
            "ix_project_prompt_heads_directory",
            indexes,
        )

    def test_project_scoped_http_uses_postgres_and_exact_bodies(
        self,
    ) -> None:
        import app as app_module

        codec = ServerActorSessionCodec(b"p" * 32)
        base_config = app_module.config()
        with tempfile.TemporaryDirectory() as directory:
            local_state = Path(directory) / "must-not-exist"
            isolated = replace(
                base_config,
                knowledge_agent_enabled=False,
            )
            with (
                patch.object(app_module, "config", return_value=isolated),
                patch.dict(
                    os.environ,
                    {
                        "ARTICLE_AGENT_SERVER_MODE": "true",
                        "ARTICLE_AGENT_SERVER_SESSION_SECRET": "p" * 32,
                        "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "",
                    },
                    clear=False,
                ),
                TestClient(app_module.app) as client,
            ):
                audit = RecordingAuditWriter()
                client.app.state.server_project_prompt_service_factory = (
                    ServerProjectPromptServiceFactory(
                        self.engine,
                        audit=audit,
                    )
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(self.viewer),
                )
                base_path = (
                    f"/api/projects/{self.project_id}/prompt-snapshots"
                )
                self.assertEqual(
                    client.get(base_path).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(
                        base_path,
                        json={
                            "name": "Denied",
                            "kind": "outline",
                            "content": "Must not persist.",
                        },
                    ).status_code,
                    403,
                )
                client.cookies.set(
                    SERVER_AUTH_COOKIE_NAME,
                    codec.create(self.editor),
                )
                self.assertEqual(
                    client.post(
                        base_path,
                        json={
                            "name": "Unsafe",
                            "kind": "outline",
                            "content": "Prompt.",
                            "role": "admin",
                        },
                    ).status_code,
                    422,
                )
                created = client.post(
                    base_path,
                    json={
                        "name": "HTTP outline",
                        "kind": "outline",
                        "content": "Version one.",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                prompt_id = created.json()["prompt_id"]
                updated = client.put(
                    f"{base_path}/{prompt_id}",
                    json={
                        "expected_version": 1,
                        "name": "HTTP outline v2",
                        "content": "Version two.",
                    },
                )
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertEqual(updated.json()["version"], 1)
                self.assertEqual(
                    client.put(
                        f"{base_path}/{prompt_id}",
                        json={
                            "expected_version": 2,
                            "name": "Stale",
                            "content": "Must not persist.",
                        },
                    ).status_code,
                    409,
                )
                default = client.put(
                    f"/api/projects/{self.project_id}/"
                    "prompt-defaults/outline",
                    json={"prompt_id": prompt_id},
                )
                self.assertEqual(default.status_code, 200, default.text)
                self.assertEqual(default.json()["version"], 1)
                humanizer = client.post(
                    base_path,
                    json={
                        "name": "HTTP humanizer",
                        "kind": "humanize",
                        "content": "Rewrite {{ARTICLE}} naturally.",
                    },
                )
                self.assertEqual(
                    humanizer.status_code,
                    201,
                    humanizer.text,
                )
                humanizer_id = humanizer.json()["prompt_id"]
                humanize_default = client.put(
                    f"/api/projects/{self.project_id}/"
                    "prompt-defaults/humanize",
                    json={"prompt_id": humanizer_id},
                )
                self.assertEqual(
                    humanize_default.status_code,
                    200,
                    humanize_default.text,
                )
                self.assertEqual(
                    humanize_default.json()["kind"],
                    "humanize",
                )
                self.assertEqual(
                    client.put(
                        f"{base_path}/{prompt_id}/active",
                        json={
                            "expected_version": 2,
                            "active": False,
                        },
                    ).status_code,
                    409,
                )
                archived = client.put(
                    f"{base_path}/{prompt_id}/active",
                    json={
                        "expected_version": 1,
                        "active": False,
                    },
                )
                self.assertEqual(
                    archived.status_code,
                    200,
                    archived.text,
                )
                self.assertEqual(
                    archived.json()["status"],
                    "archived",
                )
                listing = client.get(base_path)
                self.assertEqual(listing.status_code, 200)
                listed_prompts = {
                    item["prompt_id"]: item
                    for item in listing.json()["prompts"]
                }
                self.assertEqual(
                    listed_prompts[prompt_id]["status"],
                    "archived",
                )
                self.assertEqual(
                    listed_prompts[humanizer_id]["kind"],
                    "humanize",
                )
                self.assertNotIn(
                    "outline",
                    listing.json()["defaults"],
                )
                self.assertEqual(
                    listing.json()["defaults"]["humanize"]["prompt_id"],
                    humanizer_id,
                )
                deleted = client.delete(f"{base_path}/{prompt_id}")
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertNotIn(
                    prompt_id,
                    {
                        item["prompt_id"]
                        for item in client.get(base_path).json()["prompts"]
                    },
                )
                self.assertEqual(
                    client.post(
                        f"/api/projects/{self.other_project_id}/"
                        "prompt-snapshots",
                        json={
                            "name": "Cross project",
                            "kind": "outline",
                            "content": "Must not persist.",
                        },
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    [event.action for event in audit.events],
                    [
                        "project_prompt.created",
                        "project_prompt.updated",
                        "project_prompt.default.updated",
                        "project_prompt.created",
                        "project_prompt.default.updated",
                        "project_prompt.status.updated",
                        "project_prompt.deleted",
                    ],
                )
                self.assertNotIn("Version one", str(audit.events))
                self.assertNotIn("Version two", str(audit.events))
                self.assertFalse(local_state.exists())

    def test_project_prompt_http_requires_server_runtime(self) -> None:
        import app as app_module

        previous_mode = getattr(
            app_module.app.state,
            "server_mode_enabled",
            None,
        )
        app_module.app.state.server_mode_enabled = False
        try:
            self.assertEqual(
                TestClient(app_module.app).get(
                    f"/api/projects/{self.project_id}/prompt-snapshots"
                ).status_code,
                503,
            )
        finally:
            app_module.app.state.server_mode_enabled = previous_mode


    def test_outline_worker_keeps_enqueued_prompt_version_after_default_moves(
        self,
    ) -> None:
        prompt_v1 = self.service.create(
            self.editor,
            name="Outline v1",
            kind="outline",
            content="Pinned outline instructions v1.",
        )
        self.service.set_default(
            self.editor,
            kind="outline",
            prompt_id=prompt_v1.prompt_id,
        )
        pinned = self.service.resolve(
            self.editor,
            kind="outline",
            selection="project_default",
        )
        prompt_v2 = self.service.create(
            self.editor,
            name="Outline v2",
            kind="outline",
            content="New default instructions v2.",
        )
        self.service.set_default(
            self.editor,
            kind="outline",
            prompt_id=prompt_v2.prompt_id,
        )
        task_id = f"{self.project_id}-outline-task"
        repository = PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        repository.upsert(
            TaskRecord(
                id=task_id,
                week_folder="server",
                customer=self.project_id,
                topic_index=1,
                topic="Pinned prompt topic",
                status="title_selected",
                selected_title="Pinned prompt title",
                task_dir=f"/server/{task_id}",
                created_at="2026-07-31T00:00:00+00:00",
                updated_at="2026-07-31T00:00:00+00:00",
            ).model_dump(mode="json")
        )
        provider = RecordingOutlineProvider()
        reference = OutlinePromptReference.from_snapshot(pinned)
        result_revision = ServerOutlineGenerationHandler(
            self.engine,
            provider=provider,
            audit=self.audit,
        )(
            {
                "organization_id": self.organization_id,
                "project_id": self.project_id,
                "task_id": task_id,
                "requested_by_user_id": self.editor_id,
                "operation": "outline",
                "source_revision": 0,
                "request": {
                    **reference.private_values(),
                    "context_chunk_ids": [],
                },
            },
            lambda: False,
        )
        self.assertEqual(result_revision, 1)
        self.assertEqual(provider.versions, [1])
        stored_payload = repository.get(task_id)
        assert stored_payload is not None
        stored = TaskRecord.model_validate(stored_payload)
        assert stored.last_outline_prompt_snapshot is not None
        self.assertEqual(
            stored.last_outline_prompt_snapshot.version,
            1,
        )
        self.assertEqual(
            stored.last_outline_prompt_snapshot.content,
            "Pinned outline instructions v1.",
        )
        current_default = self.service.resolve(
            self.editor,
            kind="outline",
            selection="project_default",
        )
        self.assertEqual(current_default.version, 1)
        self.assertEqual(current_default.prompt_id, prompt_v2.prompt_id)

    def test_outline_worker_rolls_back_generated_draft_when_audit_fails(
        self,
    ) -> None:
        task_id = f"{self.project_id}-outline-audit-task"
        repository = PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        repository.upsert(
            TaskRecord(
                id=task_id,
                week_folder="server",
                customer=self.project_id,
                topic_index=2,
                topic="Audit rollback topic",
                status="title_selected",
                selected_title="Audit rollback title",
                task_dir=f"/server/{task_id}",
                created_at="2026-07-31T00:00:00+00:00",
                updated_at="2026-07-31T00:00:00+00:00",
            ).model_dump(mode="json")
        )
        system = self.service.resolve(
            self.editor,
            kind="outline",
            selection="system",
        )
        reference = OutlinePromptReference.from_snapshot(system)
        with self.assertRaises(ServerTaskCommandUnavailable):
            ServerOutlineGenerationHandler(
                self.engine,
                provider=RecordingOutlineProvider(),
                audit=FailingAuditWriter(),
            )(
                {
                    "organization_id": self.organization_id,
                    "project_id": self.project_id,
                    "task_id": task_id,
                    "requested_by_user_id": self.editor_id,
                    "operation": "outline",
                    "source_revision": 0,
                    "request": {
                        **reference.private_values(),
                        "context_chunk_ids": [],
                    },
                },
                lambda: False,
            )
        stored_payload = repository.get(task_id)
        assert stored_payload is not None
        stored = TaskRecord.model_validate(stored_payload)
        self.assertEqual(stored.revision, 0)
        self.assertEqual(stored.outline_draft, "")
        self.assertEqual(stored.article_versions, [])
        self.assertIsNone(stored.last_outline_prompt_snapshot)

    def test_title_worker_rolls_back_candidates_when_audit_fails(
        self,
    ) -> None:
        task_id = f"{self.project_id}-title-audit-task"
        repository = PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        repository.upsert(
            TaskRecord(
                id=task_id,
                week_folder="server",
                customer=self.project_id,
                topic_index=3,
                topic="Title audit rollback topic",
                status="new",
                task_dir=f"/server/{task_id}",
                created_at="2026-07-31T00:00:00+00:00",
                updated_at="2026-07-31T00:00:00+00:00",
            ).model_dump(mode="json")
        )
        template = TitleTemplateReference.current()
        with self.assertRaises(ServerTaskCommandUnavailable):
            ServerTitleGenerationHandler(
                self.engine,
                provider=RecordingTitleProvider(),
                audit=FailingAuditWriter(),
            )(
                {
                    "organization_id": self.organization_id,
                    "project_id": self.project_id,
                    "task_id": task_id,
                    "requested_by_user_id": self.editor_id,
                    "operation": "titles",
                    "source_revision": 0,
                    "request": {
                        **template.private_values(),
                        "context_chunk_ids": [],
                        "title_count": 3,
                    },
                },
                lambda: False,
            )
        stored_payload = repository.get(task_id)
        assert stored_payload is not None
        stored = TaskRecord.model_validate(stored_payload)
        self.assertEqual(stored.revision, 0)
        self.assertEqual(stored.status, "new")
        self.assertEqual(stored.title_candidates, [])

    def test_article_worker_keeps_pinned_prompt_after_default_moves(
        self,
    ) -> None:
        prompt_v1 = self.service.create(
            self.editor,
            name="Article v1",
            kind="article",
            content="Pinned article instructions v1.",
        )
        self.service.set_default(
            self.editor,
            kind="article",
            prompt_id=prompt_v1.prompt_id,
        )
        pinned = self.service.resolve(
            self.editor,
            kind="article",
            selection="project_default",
        )
        prompt_v2 = self.service.create(
            self.editor,
            name="Article v2",
            kind="article",
            content="New article instructions v2.",
        )
        self.service.set_default(
            self.editor,
            kind="article",
            prompt_id=prompt_v2.prompt_id,
        )
        task_id = f"{self.project_id}-article-task"
        repository = PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        repository.upsert(
            TaskRecord(
                id=task_id,
                week_folder="server",
                customer=self.project_id,
                topic_index=4,
                topic="Pinned article topic",
                status="outline_confirmed",
                selected_title="Pinned article title",
                outline="## Buyer Checks\n\n### Requirements\n\n"
                "### Evidence\n\n## FAQ",
                task_dir=f"/server/{task_id}",
                created_at="2026-07-31T00:00:00+00:00",
                updated_at="2026-07-31T00:00:00+00:00",
            ).model_dump(mode="json")
        )
        provider = RecordingArticleProvider()
        reference = OutlinePromptReference.from_snapshot(pinned)
        result_revision = ServerArticleGenerationHandler(
            self.engine,
            provider=provider,
            audit=self.audit,
        )(
            {
                "organization_id": self.organization_id,
                "project_id": self.project_id,
                "task_id": task_id,
                "requested_by_user_id": self.editor_id,
                "operation": "article",
                "source_revision": 0,
                "request": {
                    **reference.private_values(),
                    "context_chunk_ids": [],
                    "target_words": 1100,
                },
            },
            lambda: False,
        )
        self.assertEqual(result_revision, 1)
        self.assertEqual(provider.versions, [1])
        stored_payload = repository.get(task_id)
        assert stored_payload is not None
        stored = TaskRecord.model_validate(stored_payload)
        self.assertEqual(stored.status, "draft_ready")
        self.assertEqual(
            stored.initial_article,
            VALID_SERVER_ARTICLE.strip(),
        )
        assert stored.last_article_prompt_snapshot is not None
        self.assertEqual(stored.last_article_prompt_snapshot.version, 1)
        self.assertEqual(
            stored.last_article_prompt_snapshot.content,
            "Pinned article instructions v1.",
        )
        current_default = self.service.resolve(
            self.editor,
            kind="article",
            selection="project_default",
        )
        self.assertEqual(current_default.version, 1)
        self.assertEqual(current_default.prompt_id, prompt_v2.prompt_id)

    def test_article_worker_rolls_back_draft_when_audit_fails(
        self,
    ) -> None:
        task_id = f"{self.project_id}-article-audit-task"
        repository = PostgresTaskRepository(
            self.engine,
            organization_id=self.organization_id,
            project_id=self.project_id,
        )
        repository.upsert(
            TaskRecord(
                id=task_id,
                week_folder="server",
                customer=self.project_id,
                topic_index=5,
                topic="Article audit rollback topic",
                status="outline_confirmed",
                selected_title="Article audit rollback title",
                outline="## Buyer Checks\n\n### Requirements\n\n"
                "### Evidence\n\n## FAQ",
                task_dir=f"/server/{task_id}",
                created_at="2026-07-31T00:00:00+00:00",
                updated_at="2026-07-31T00:00:00+00:00",
            ).model_dump(mode="json")
        )
        system = self.service.resolve(
            self.editor,
            kind="article",
            selection="system",
        )
        reference = OutlinePromptReference.from_snapshot(system)
        with self.assertRaises(ServerTaskCommandUnavailable):
            ServerArticleGenerationHandler(
                self.engine,
                provider=RecordingArticleProvider(),
                audit=FailingAuditWriter(),
            )(
                {
                    "organization_id": self.organization_id,
                    "project_id": self.project_id,
                    "task_id": task_id,
                    "requested_by_user_id": self.editor_id,
                    "operation": "article",
                    "source_revision": 0,
                    "request": {
                        **reference.private_values(),
                        "context_chunk_ids": [],
                        "target_words": 1100,
                    },
                },
                lambda: False,
            )
        stored_payload = repository.get(task_id)
        assert stored_payload is not None
        stored = TaskRecord.model_validate(stored_payload)
        self.assertEqual(stored.revision, 0)
        self.assertEqual(stored.status, "outline_confirmed")
        self.assertEqual(stored.raw_draft_article, "")
        self.assertEqual(stored.initial_article, "")
        self.assertEqual(stored.article_versions, [])
        self.assertIsNone(stored.last_article_prompt_snapshot)




class ServerOutlineProviderTests(unittest.TestCase):
    def test_provider_error_does_not_expose_private_gateway_message(
        self,
    ) -> None:
        provider = LlmServerOutlineProvider(
            load_config(),
            llm=LeakingOutlineLlm(),
        )
        task = TaskRecord(
            id="outline-provider-task",
            week_folder="server",
            customer="example.test",
            topic_index=1,
            topic="Provider safety topic",
            status="title_selected",
            selected_title="Provider safety title",
            task_dir="/server/outline-provider-task",
            created_at="2026-07-31T00:00:00+00:00",
            updated_at="2026-07-31T00:00:00+00:00",
        )
        with self.assertRaisesRegex(
            OutlineGenerationUnavailable,
            "^outline provider is temporarily unavailable$",
        ) as caught:
            provider.generate(
                task,
                prompt_snapshot=PromptSnapshot(
                    kind="outline",
                    source="system",
                    captured_at="2026-07-31T00:00:00+00:00",
                ),
                context_chunks=(),
            )
        self.assertNotIn(
            "private-provider-detail",
            str(caught.exception),
        )


class ServerTitleProviderTests(unittest.TestCase):
    @staticmethod
    def _task() -> TaskRecord:
        return TaskRecord(
            id="title-provider-task",
            week_folder="server",
            customer="example.test",
            topic_index=1,
            topic="Title provider safety topic",
            status="new",
            task_dir="/server/title-provider-task",
            created_at="2026-07-31T00:00:00+00:00",
            updated_at="2026-07-31T00:00:00+00:00",
        )

    def test_provider_error_and_short_output_do_not_fall_back_to_mock(
        self,
    ) -> None:
        leaking = LlmServerTitleProvider(
            load_config(),
            llm=LeakingTitleLlm(),
        )
        with self.assertRaisesRegex(
            TitleGenerationUnavailable,
            "^title provider is temporarily unavailable$",
        ) as caught:
            leaking.generate(
                self._task(),
                title_count=3,
                context_chunks=(),
            )
        self.assertNotIn(
            "private-title-provider-detail",
            str(caught.exception),
        )
        short = LlmServerTitleProvider(
            load_config(),
            llm=LeakingTitleLlm("1. Only one candidate"),
        )
        with self.assertRaisesRegex(
            TitleGenerationUnavailable,
            "^title provider returned an invalid result$",
        ):
            short.generate(
                self._task(),
                title_count=3,
                context_chunks=(),
            )

    def test_title_prompt_includes_operator_project_rules(self) -> None:
        task = self._task()
        task.project_notes = (
            "Use engineered wood flooring; exclude laminate topics."
        )
        prompt = build_server_title_prompt(
            task,
            title_count=3,
            context_chunks=(),
        )
        self.assertIn("Use engineered wood flooring", prompt)

        task.include_project_notes = False
        excluded_prompt = build_server_title_prompt(
            task,
            title_count=3,
            context_chunks=(),
        )
        self.assertNotIn("Use engineered wood flooring", excluded_prompt)
        self.assertIn(
            "[Not included for this generation by the operator.]",
            excluded_prompt,
        )

    def test_template_reference_detects_checked_in_prompt_drift(
        self,
    ) -> None:
        reference = TitleTemplateReference.current()
        with patch(
            "services.server_title_generation.load_prompt_template",
            return_value="changed checked-in title template",
        ):
            with self.assertRaisesRegex(
                JobConflict,
                "^pinned title template changed$",
            ):
                reference.verify_current()


class ServerArticleProviderTests(unittest.TestCase):
    @staticmethod
    def _task() -> TaskRecord:
        return TaskRecord(
            id="article-provider-task",
            week_folder="server",
            customer="example.test",
            topic_index=1,
            topic="Article provider safety topic",
            status="outline_confirmed",
            selected_title="Article provider safety title",
            outline="## Buyer Checks\n\n### Requirements\n\n"
            "### Evidence\n\n## FAQ",
            task_dir="/server/article-provider-task",
            created_at="2026-07-31T00:00:00+00:00",
            updated_at="2026-07-31T00:00:00+00:00",
        )

    def test_provider_error_and_empty_output_do_not_fall_back_to_mock(
        self,
    ) -> None:
        snapshot = PromptSnapshot(
            kind="article",
            source="system",
            captured_at="2026-07-31T00:00:00+00:00",
        )
        leaking = LlmServerArticleProvider(
            load_config(),
            llm=LeakingArticleLlm(),
        )
        with self.assertRaisesRegex(
            ArticleGenerationUnavailable,
            "^article provider is temporarily unavailable$",
        ) as caught:
            leaking.generate(
                self._task(),
                target_words=1100,
                prompt_snapshot=snapshot,
                context_chunks=(),
            )
        self.assertNotIn(
            "private-article-provider-detail",
            str(caught.exception),
        )
        empty = LlmServerArticleProvider(
            load_config(),
            llm=LeakingArticleLlm(""),
        )
        with self.assertRaisesRegex(
            ArticleGenerationUnavailable,
            "^article provider returned an invalid result$",
        ):
            empty.generate(
                self._task(),
                target_words=1100,
                prompt_snapshot=snapshot,
                context_chunks=(),
            )

    def test_prompt_receives_only_pinned_official_contact_link(self) -> None:
        task = self._task()
        task.official_links = [
            OfficialLink(
                source_id="contact-source",
                snapshot_id="contact-v1",
                label="Contact Us",
                url="https://example.test/contact-us/",
                role="contact",
            )
        ]

        prompt = build_server_article_prompt(
            task,
            target_words=1100,
            prompt_snapshot=PromptSnapshot(
                kind="article",
                source="system",
                captured_at="2026-07-31T00:00:00+00:00",
            ),
            context_chunks=(),
        )

        self.assertIn("contact: Contact Us", prompt)
        self.assertIn("https://example.test/contact-us/", prompt)
        self.assertIn("never guess a contact path", prompt)

    def test_invalid_structure_is_rejected_before_task_mutation(
        self,
    ) -> None:
        task = self._task()
        snapshot = PromptSnapshot(
            kind="article",
            source="system",
            captured_at="2026-07-31T00:00:00+00:00",
        )
        with self.assertRaisesRegex(
            ArticleGenerationUnavailable,
            "^article provider returned an invalid result$",
        ):
            apply_generated_article_draft(
                task,
                raw_article="# Missing transition\n\n## FAQ",
                prompt_snapshot=snapshot,
            )
        self.assertEqual(task.raw_draft_article, "")
        self.assertEqual(task.initial_article, "")
        self.assertEqual(task.article_versions, [])


if __name__ == "__main__":
    unittest.main()
