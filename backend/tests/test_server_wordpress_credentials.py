from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.server_wordpress_credentials import (  # noqa: E402
    ServerWordPressCredentials,
    WORDPRESS_CREDENTIALS_KEY_ENV,
    WordPressCredentialsUnavailable,
    decrypt_wordpress_app_password,
    encrypt_wordpress_app_password,
)


class ServerWordPressCredentialsTests(unittest.TestCase):
    def test_application_password_round_trips_without_plaintext_ciphertext(self) -> None:
        old = os.environ.get(WORDPRESS_CREDENTIALS_KEY_ENV)
        try:
            os.environ[WORDPRESS_CREDENTIALS_KEY_ENV] = (
                "local-test-wordpress-credentials-key-with-sufficient-length"
            )
            password = "abcd efgh ijkl mnop"
            encrypted = encrypt_wordpress_app_password(password)
            self.assertNotEqual(encrypted, password)
            self.assertNotIn(password, encrypted)
            self.assertEqual(decrypt_wordpress_app_password(encrypted), password)
        finally:
            if old is None:
                os.environ.pop(WORDPRESS_CREDENTIALS_KEY_ENV, None)
            else:
                os.environ[WORDPRESS_CREDENTIALS_KEY_ENV] = old

    def test_missing_encryption_key_fails_closed(self) -> None:
        old = os.environ.pop(WORDPRESS_CREDENTIALS_KEY_ENV, None)
        try:
            with self.assertRaises(WordPressCredentialsUnavailable):
                encrypt_wordpress_app_password("password")
        finally:
            if old is not None:
                os.environ[WORDPRESS_CREDENTIALS_KEY_ENV] = old

    def test_oversized_application_password_has_a_safe_error(self) -> None:
        old = os.environ.get(WORDPRESS_CREDENTIALS_KEY_ENV)
        try:
            os.environ[WORDPRESS_CREDENTIALS_KEY_ENV] = (
                "local-test-wordpress-credentials-key-with-sufficient-length"
            )
            with self.assertRaisesRegex(ValueError, "不能超过 1024"):
                encrypt_wordpress_app_password("x" * 1025)
        finally:
            if old is None:
                os.environ.pop(WORDPRESS_CREDENTIALS_KEY_ENV, None)
            else:
                os.environ[WORDPRESS_CREDENTIALS_KEY_ENV] = old

    def test_credential_repr_does_not_include_password(self) -> None:
        credentials = ServerWordPressCredentials(
            project_id="example.com",
            username="editor",
            app_password="password-that-must-stay-private",
        )
        self.assertNotIn(credentials.app_password, repr(credentials))


if __name__ == "__main__":
    unittest.main()
