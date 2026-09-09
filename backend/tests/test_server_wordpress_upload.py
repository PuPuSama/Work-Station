from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_project_http as routes
from services.access_control import ActorIdentity
from storage import content_hash
from test_m7_server_delivery_package import prepared


class ServerWordPressUploadTests(unittest.TestCase):
    def run_upload(self, task, objects, publisher, target):
        actor = ActorIdentity('org-a', 'editor-a')
        authorized = SimpleNamespace(actor=actor, project_id='www.example.com')
        snapshots = []

        def save(_request, _authorized, current, *, expected_revision, **kwargs):
            self.assertEqual(current.revision, expected_revision)
            current.revision += 1
            snapshots.append(current.model_dump(mode='json'))
            return current

        with ExitStack() as stack:
            for name, value in {
                '_require_project_permission': authorized,
                '_task_store': SimpleNamespace(get=lambda _id: task),
                '_project_metadata_service': SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(wordpress_url=target)),
                '_wordpress_credentials_service': SimpleNamespace(get=lambda **_kwargs: None),
                '_knowledge_object_service': objects,
                'WordPressPublisher': publisher,
            }.items():
                stack.enter_context(patch.object(routes, name, return_value=value))
            stack.enter_context(patch.object(routes, '_save_audited_task', side_effect=save))
            stack.enter_context(patch.object(routes.WordPressSettings, 'from_environment_for_url', return_value=Mock()))
            result = routes.upload_project_task_wordpress(
                'www.example.com', task.id,
                routes.ProjectRevisionRequest(revision=task.revision), Mock(), authorized)
        return result, snapshots

    def test_changing_site_uploads_fresh_media_and_persists_draft(self):
        task, objects = prepared()
        task.final_article = '# Example\n\nA useful introduction.\n\n## Selection\n\nA practical answer.'
        task.wordpress_upload.status = 'draft_created'
        task.wordpress_upload.source_article_hash = content_hash(task.final_article)
        task.wordpress_upload.wordpress_url = 'https://old.example'
        task.wordpress_upload.post_id = 80
        task.wordpress_upload.media_ids = {task.images[0].prepared_content_hash: 99}
        publisher = Mock()
        publisher.upload_media.return_value = (5, 'https://new.example/hero.webp')
        publisher.create_or_get_draft.return_value = ('draft_created', 6, 'https://new.example/?p=6')
        result, snapshots = self.run_upload(task, objects, publisher, 'https://new.example')
        publisher.media_source.assert_not_called()
        publisher.upload_media.assert_called_once()
        self.assertEqual(publisher.create_or_get_draft.call_args.kwargs['featured_media'], 5)
        self.assertIn('https://new.example/hero.webp', publisher.create_or_get_draft.call_args.kwargs['content'])
        self.assertEqual(publisher.create_or_get_draft.call_args.kwargs['content'].count('<img '), 1)
        self.assertEqual(snapshots[0]['wordpress_upload']['post_id'], None)
        self.assertEqual(snapshots[-1]['wordpress_upload']['status'], 'draft_created')
        self.assertEqual(snapshots[-1]['wordpress_upload']['post_id'], 6)
        self.assertEqual(result.post_id, 6)
        publisher.close.assert_called_once()

    def test_retry_reuses_confirmed_media_on_same_site(self):
        task, objects = prepared()
        task.final_article = '# Example\n\nA useful introduction.\n\n## Selection\n\nA practical answer.'
        task.wordpress_upload.status = 'failed'
        task.wordpress_upload.source_article_hash = content_hash(task.final_article)
        task.wordpress_upload.wordpress_url = 'https://same.example'
        task.wordpress_upload.media_ids = {task.images[0].prepared_content_hash: 5}
        publisher = Mock()
        publisher.media_source.return_value = 'https://same.example/hero.webp'
        publisher.create_or_get_draft.return_value = ('draft_created', 6, 'https://same.example/?p=6')
        result, snapshots = self.run_upload(task, objects, publisher, 'https://same.example')
        publisher.upload_media.assert_not_called()
        publisher.media_source.assert_called_once_with(5)
        self.assertEqual(result.media_count, 1)
        self.assertEqual(snapshots[-1]['wordpress_upload']['status'], 'draft_created')

    def test_invalid_image_placement_fails_before_upload_or_state_change(self):
        task, objects = prepared()
        task.final_article = '# Example\n\nNo image position has been confirmed.'
        publisher = Mock()
        with self.assertRaises(routes.HTTPException) as raised:
            self.run_upload(task, objects, publisher, 'https://same.example')
        self.assertEqual(raised.exception.status_code, 409)
        publisher.upload_media.assert_not_called()
        publisher.create_or_get_draft.assert_not_called()
        self.assertNotEqual(task.wordpress_upload.status, 'uploading')


if __name__ == '__main__':
    unittest.main()
