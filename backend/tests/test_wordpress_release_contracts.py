from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from knowledge_agent.schema import wordpress_project_credentials
from services.access_control import ProjectAccessDenied
from services.deployment_readiness import EXPECTED_ALEMBIC_HEAD
from services.server_wordpress_credentials import (
    PostgresServerWordPressCredentials,
    WordPressCredentialsConflict,
)
import test_m7_server_project_metadata as fixtures


class WordPressMigrationTests(unittest.TestCase):
    def test_release_preflight_matches_single_migration_head(self):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / 'backend/alembic.ini'))
        config.set_main_option('script_location', str(root / 'backend/migrations'))
        scripts = ScriptDirectory.from_config(config)
        self.assertEqual(scripts.get_heads(), [EXPECTED_ALEMBIC_HEAD])


@unittest.skipUnless(os.getenv('ARTICLE_AGENT_DATABASE_URL'), 'isolated PostgreSQL is required')
class WordPressCredentialsDatabaseTests(unittest.TestCase):
    # Reuse the existing isolated organization/project fixture without
    # duplicating its metadata tests or touching production records.
    setUpClass = classmethod(fixtures.ServerProjectMetadataTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ServerProjectMetadataTests.tearDownClass.__func__)
    tearDown = fixtures.ServerProjectMetadataTests.tearDown

    def setUp(self):
        fixtures.ServerProjectMetadataTests.setUp(self)
        env = patch.dict(os.environ, {'ARTICLE_AGENT_WORDPRESS_CREDENTIALS_KEY': 'isolated-ci-wordpress-key-at-least-32-characters'})
        env.start()
        self.addCleanup(env.stop)
        self.credentials = PostgresServerWordPressCredentials(self.engine, audit=self.audit)

    def test_password_is_encrypted_and_stale_revision_cannot_overwrite(self):
        saved = self.credentials.save(actor=self.editor, project_id=self.project_id,
            username='test-editor', app_password='isolated-application-password', expected_revision=0)
        self.assertEqual(saved.revision, 1)
        with self.engine.connect() as connection:
            raw = connection.execute(sa.select(wordpress_project_credentials.c.app_password_ciphertext)
                .where(wordpress_project_credentials.c.project_id == self.project_id)).scalar_one()
        self.assertNotIn('isolated-application-password', raw)
        loaded = self.credentials.get(actor=self.editor, project_id=self.project_id)
        self.assertEqual(loaded.app_password, 'isolated-application-password')
        self.assertNotIn('isolated-application-password', repr(self.audit.events))
        with self.assertRaises(WordPressCredentialsConflict):
            self.credentials.save(actor=self.editor, project_id=self.project_id,
                username='test-editor', app_password='stale-password', expected_revision=0)
        self.assertEqual(self.credentials.get(actor=self.editor, project_id=self.project_id).revision, 1)

    def test_other_project_credentials_are_denied(self):
        self.credentials.save(actor=self.admin, project_id=self.other_project_id,
            username='another-editor', app_password='another-test-password', expected_revision=0)
        with self.assertRaises(ProjectAccessDenied):
            self.credentials.get(actor=self.editor, project_id=self.other_project_id)

    def test_audit_failure_rolls_back_credential_write(self):
        service = PostgresServerWordPressCredentials(self.engine, audit=fixtures.FailingAuditWriter())
        with self.assertRaises(RuntimeError):
            service.save(actor=self.editor, project_id=self.project_id,
                username='test-editor', app_password='isolated-password', expected_revision=0)
        self.assertIsNone(self.credentials.get(actor=self.editor, project_id=self.project_id))


if __name__ == '__main__':
    unittest.main()
