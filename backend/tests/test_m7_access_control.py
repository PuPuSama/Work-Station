from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.access_control import (  # noqa: E402
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessFacts,
    ProjectAccessService,
)
from services.audit_log import AuditEvent, PostgresAuditEventWriter  # noqa: E402


class FakeAccessRepository:
    def __init__(self, facts: ProjectAccessFacts | None) -> None:
        self.facts = facts
        self.calls: list[tuple[ActorIdentity, str]] = []

    def resolve_project_access(
        self,
        actor: ActorIdentity,
        project_id: str,
    ) -> ProjectAccessFacts | None:
        self.calls.append((actor, project_id))
        return self.facts


class ProjectAccessPolicyTests(unittest.TestCase):
    def test_org_admin_can_manage_and_delete(self) -> None:
        repository = FakeAccessRepository(
            ProjectAccessFacts(organization_role="org_admin")
        )
        service = ProjectAccessService(repository)
        actor = ActorIdentity("org-a", "admin")

        for permission in (
            "project.view",
            "article.edit",
            "article.deliver",
            "knowledge.publish",
            "project.members.manage",
            "knowledge.delete",
            "project.delete",
        ):
            with self.subTest(permission=permission):
                decision = service.require(actor, "project-a", permission)
                self.assertEqual(decision.effective_role, "org_admin")

    def test_team_lead_can_operate_team_project_but_not_delete(self) -> None:
        service = ProjectAccessService(
            FakeAccessRepository(
                ProjectAccessFacts(
                    organization_role="member",
                    team_role="team_lead",
                )
            )
        )
        actor = ActorIdentity("org-a", "lead")

        self.assertTrue(
            service.decide(actor, "project-a", "project.members.manage").allowed
        )
        self.assertTrue(
            service.decide(actor, "project-a", "knowledge.publish").allowed
        )
        self.assertFalse(
            service.decide(actor, "project-a", "knowledge.delete").allowed
        )
        self.assertFalse(
            service.decide(actor, "project-a", "project.delete").allowed
        )

    def test_project_roles_follow_self_delivery_matrix(self) -> None:
        actor = ActorIdentity("org-a", "member")
        cases = {
            "editor": {
                "allowed": (
                    "project.view",
                    "article.edit",
                    "article.review",
                    "article.deliver",
                    "knowledge.edit",
                    "knowledge.publish",
                ),
                "denied": (
                    "project.members.manage",
                    "knowledge.delete",
                    "project.delete",
                ),
            },
            "reviewer": {
                "allowed": ("project.view", "article.review"),
                "denied": (
                    "article.edit",
                    "article.deliver",
                    "knowledge.edit",
                ),
            },
            "viewer": {
                "allowed": ("project.view",),
                "denied": (
                    "article.edit",
                    "article.review",
                    "article.deliver",
                ),
            },
        }

        for role, expectations in cases.items():
            service = ProjectAccessService(
                FakeAccessRepository(
                    ProjectAccessFacts(
                        organization_role="member",
                        project_role=role,  # type: ignore[arg-type]
                    )
                )
            )
            for permission in expectations["allowed"]:
                with self.subTest(role=role, permission=permission):
                    self.assertTrue(
                        service.decide(
                            actor,
                            "project-a",
                            permission,  # type: ignore[arg-type]
                        ).allowed
                    )
            for permission in expectations["denied"]:
                with self.subTest(role=role, permission=permission):
                    self.assertFalse(
                        service.decide(
                            actor,
                            "project-a",
                            permission,  # type: ignore[arg-type]
                        ).allowed
                    )

    def test_plain_team_member_and_unbound_project_are_denied(self) -> None:
        actor = ActorIdentity("org-a", "member")
        team_member_service = ProjectAccessService(
            FakeAccessRepository(
                ProjectAccessFacts(
                    organization_role="member",
                    team_role="member",
                )
            )
        )
        unbound_service = ProjectAccessService(FakeAccessRepository(None))

        self.assertFalse(
            team_member_service.decide(
                actor,
                "project-a",
                "project.view",
            ).allowed
        )
        with self.assertRaisesRegex(
            ProjectAccessDenied,
            "^project access denied$",
        ):
            unbound_service.require(actor, "unknown-project", "project.view")

    def test_identity_and_project_scope_are_normalized(self) -> None:
        repository = FakeAccessRepository(
            ProjectAccessFacts(
                organization_role="member",
                project_role="viewer",
            )
        )
        service = ProjectAccessService(repository)
        actor = ActorIdentity(" org-a ", " user-a ")

        service.require(actor, " project-a ", "project.view")

        self.assertEqual(actor, ActorIdentity("org-a", "user-a"))
        self.assertEqual(repository.calls, [(actor, "project-a")])
        with self.assertRaisesRegex(ValueError, "organization_id is required"):
            ActorIdentity(" ", "user-a")
        with self.assertRaisesRegex(ValueError, "project_id is required"):
            service.decide(actor, " ", "project.view")

    def test_runtime_repository_type_is_importable(self) -> None:
        self.assertTrue(callable(PostgresProjectAccessRepository))

    def test_audit_event_normalizes_identity_and_copies_details(self) -> None:
        details = {"role": "editor"}
        event = AuditEvent(
            organization_id=" org-a ",
            event_id=" event-a ",
            actor_user_id=" admin ",
            project_id=" project-a ",
            action=" project.membership.granted ",
            target_type=" project_membership ",
            target_id=" editor ",
            details=details,
        )
        details["role"] = "tampered"

        self.assertEqual(event.organization_id, "org-a")
        self.assertEqual(event.event_id, "event-a")
        self.assertEqual(event.actor_user_id, "admin")
        self.assertEqual(event.project_id, "project-a")
        self.assertEqual(event.details, {"role": "editor"})
        with self.assertRaisesRegex(ValueError, "action is required"):
            AuditEvent(
                organization_id="org-a",
                event_id="event-a",
                action=" ",
                target_type="project",
                target_id="project-a",
            )
        self.assertTrue(callable(PostgresAuditEventWriter))


if __name__ == "__main__":
    unittest.main()
