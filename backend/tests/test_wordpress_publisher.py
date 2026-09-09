from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.wordpress_publisher import (  # noqa: E402
    WordPressPublisher,
    WordPressPublisherError,
    WordPressSettings,
    configured_wordpress_base_url,
    markdown_to_wordpress_html,
)


class WordPressPublisherTests(unittest.TestCase):
    def test_project_url_overrides_deployment_fallback(self) -> None:
        old = {key: os.environ.get(key) for key in (
            "ARTICLE_AGENT_WORDPRESS_URL",
            "ARTICLE_AGENT_WORDPRESS_USERNAME",
            "ARTICLE_AGENT_WORDPRESS_APP_PASSWORD",
            "ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS",
            "ARTICLE_AGENT_LOCAL_DEBUG",
        )}
        try:
            os.environ.update({
                "ARTICLE_AGENT_WORDPRESS_URL": "http://localhost:8088",
                "ARTICLE_AGENT_WORDPRESS_USERNAME": "editor",
                "ARTICLE_AGENT_WORDPRESS_APP_PASSWORD": "secret",
                "ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS": "",
                "ARTICLE_AGENT_LOCAL_DEBUG": "1",
            })
            self.assertEqual(
                configured_wordpress_base_url("http://127.0.0.1:8088/"),
                "http://127.0.0.1:8088",
            )
            self.assertEqual(
                WordPressSettings.from_environment_for_url(
                    "http://127.0.0.1:8088/"
                ).base_url,
                "http://127.0.0.1:8088",
            )
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_project_credentials_use_environment_references(self) -> None:
        keys = (
            "ARTICLE_AGENT_WORDPRESS_URL",
            "ARTICLE_AGENT_WORDPRESS_USERNAME",
            "ARTICLE_AGENT_WORDPRESS_APP_PASSWORD",
            "ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS",
            "ARTICLE_AGENT_LOCAL_DEBUG",
            "WP_SITE_A_USERNAME",
            "WP_SITE_A_APP_PASSWORD",
        )
        old = {key: os.environ.get(key) for key in keys}
        try:
            os.environ.update({
                "ARTICLE_AGENT_WORDPRESS_URL": "http://localhost:8088",
                "ARTICLE_AGENT_WORDPRESS_USERNAME": "global-user",
                "ARTICLE_AGENT_WORDPRESS_APP_PASSWORD": "global-password",
                "ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS": json.dumps({
                    "site-a.example": {
                        "username_env": "WP_SITE_A_USERNAME",
                        "app_password_env": "WP_SITE_A_APP_PASSWORD",
                    }
                }),
                "ARTICLE_AGENT_LOCAL_DEBUG": "1",
                "WP_SITE_A_USERNAME": "site-a-user",
                "WP_SITE_A_APP_PASSWORD": "site-a-password",
            })
            settings = WordPressSettings.from_environment_for_url(
                "http://localhost:8088",
                project_id="site-a.example",
            )
            self.assertEqual(settings.username, "site-a-user")
            self.assertEqual(settings.app_password, "site-a-password")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_project_credentials_fail_closed_when_project_is_unmapped(self) -> None:
        keys = (
            "ARTICLE_AGENT_WORDPRESS_URL",
            "ARTICLE_AGENT_WORDPRESS_USERNAME",
            "ARTICLE_AGENT_WORDPRESS_APP_PASSWORD",
            "ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS",
            "ARTICLE_AGENT_LOCAL_DEBUG",
        )
        old = {key: os.environ.get(key) for key in keys}
        try:
            os.environ.update({
                "ARTICLE_AGENT_WORDPRESS_URL": "http://localhost:8088",
                "ARTICLE_AGENT_WORDPRESS_USERNAME": "global-user",
                "ARTICLE_AGENT_WORDPRESS_APP_PASSWORD": "global-password",
                "ARTICLE_AGENT_WORDPRESS_PROJECT_CREDENTIALS": json.dumps({
                    "site-a.example": {
                        "username_env": "WP_SITE_A_USERNAME",
                        "app_password_env": "WP_SITE_A_APP_PASSWORD",
                    }
                }),
                "ARTICLE_AGENT_LOCAL_DEBUG": "1",
            })
            with self.assertRaisesRegex(WordPressPublisherError, "当前项目"):
                WordPressSettings.from_environment_for_url(
                    "http://localhost:8088",
                    project_id="site-b.example",
                )
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_settings_reject_insecure_non_loopback_url(self) -> None:
        old = {key: os.environ.get(key) for key in (
            "ARTICLE_AGENT_WORDPRESS_URL",
            "ARTICLE_AGENT_WORDPRESS_USERNAME",
            "ARTICLE_AGENT_WORDPRESS_APP_PASSWORD",
            "ARTICLE_AGENT_LOCAL_DEBUG",
        )}
        try:
            os.environ.update({
                "ARTICLE_AGENT_WORDPRESS_URL": "http://wordpress.internal",
                "ARTICLE_AGENT_WORDPRESS_USERNAME": "editor",
                "ARTICLE_AGENT_WORDPRESS_APP_PASSWORD": "secret",
                "ARTICLE_AGENT_LOCAL_DEBUG": "1",
            })
            with self.assertRaises(WordPressPublisherError):
                WordPressSettings.from_environment()
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_markdown_conversion_escapes_html_and_renders_article_blocks(self) -> None:
        rendered = markdown_to_wordpress_html(
            """# A title

## Checks

Use **bold** and [the docs](https://example.test/docs).

| Check | Result |
| --- | --- |
| Safety | Pass |

- One
- Two

img.hero.webp

<script>alert(1)</script>""",
            {"img.hero.webp": "https://example.test/image.webp"},
        )
        self.assertIn("<h1>A title</h1>", rendered)
        self.assertIn("<table>", rendered)
        self.assertIn('<img src="https://example.test/image.webp"', rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)

    def test_create_draft_and_reuse_same_source(self) -> None:
        calls: list[httpx.Request] = []
        existing = {"id": 42, "link": "http://wp.test/?p=42", "meta": {
            "article_agent_source_hash": "hash-1",
            "article_agent_task_id": "task-1",
        }}

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=[] if len(calls) == 1 else [existing])
            return httpx.Response(201, json={"id": 42, "link": "http://wp.test/?p=42"})

        settings = WordPressSettings("https://wp.test", "editor", "app-password")
        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            auth=httpx.BasicAuth(settings.username, settings.app_password),
        )
        publisher = WordPressPublisher(settings, client)
        status, post_id, _ = publisher.create_or_get_draft(
            title="A title",
            content="<p>Body</p>",
            slug="article-agent-task-1",
            source_hash="hash-1",
            task_id="task-1",
        )
        self.assertEqual((status, post_id), ("draft_created", 42))
        status, post_id, _ = publisher.create_or_get_draft(
            title="A title",
            content="<p>Body</p>",
            slug="article-agent-task-1",
            source_hash="hash-1",
            task_id="task-1",
        )
        self.assertEqual((status, post_id), ("already_exists", 42))
        self.assertEqual(calls[1].method, "POST")
        self.assertIn("Basic", calls[1].headers.get("authorization", ""))
        self.assertEqual(json.loads(calls[1].content)["status"], "draft")

    def test_retryable_response_does_not_expose_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="password=secret-value")

        settings = WordPressSettings("https://wp.test", "editor", "app-password")
        publisher = WordPressPublisher(
            settings,
            httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with self.assertRaises(WordPressPublisherError) as raised:
            publisher.media_source(1)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("secret-value", str(raised.exception))

    def test_settings_repr_does_not_include_application_password(self) -> None:
        settings = WordPressSettings("https://wp.test", "editor", "private-app-password")
        self.assertNotIn(settings.app_password, repr(settings))

    def test_existing_different_source_is_a_conflict(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{
                "id": 9,
                "link": "https://wp.test/?p=9",
                "meta": {
                    "article_agent_source_hash": "another-hash",
                    "article_agent_task_id": "task-1",
                },
            }])

        settings = WordPressSettings("https://wp.test", "editor", "app-password")
        publisher = WordPressPublisher(
            settings,
            httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with self.assertRaises(WordPressPublisherError) as raised:
            publisher.create_or_get_draft(
                title="A title",
                content="<p>Body</p>",
                slug="article-agent-task-1",
                source_hash="hash-1",
                task_id="task-1",
            )
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
