from __future__ import annotations

import hashlib
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.object_store import (  # noqa: E402
    ObjectStoreError,
    ObjectTooLarge,
    ProjectObjectUploader,
    S3ObjectStore,
    S3ObjectStoreSettings,
    build_project_object_key,
)


class FakeS3Client:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []
        self.get_response: dict[str, object] = {
            "ContentLength": 4,
            "Body": io.BytesIO(b"data"),
        }
        self.deleted: list[dict[str, str]] = []
        self.list_responses: list[dict[str, object]] = []
        self.fail_put = False
        self.fail_ready = False

    def head_bucket(self, **kwargs):
        if self.fail_ready:
            raise ClientError(
                {"Error": {"Code": "403", "Message": "secret provider body"}},
                "HeadBucket",
            )
        return {}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        if self.fail_put:
            raise ClientError(
                {"Error": {"Code": "500", "Message": "secret provider body"}},
                "PutObject",
            )
        return {"ETag": '"etag-1"'}

    def get_object(self, **kwargs):
        return self.get_response

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        return f"https://signed.example.test/{Params['Key']}?ttl={ExpiresIn}"

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)

    def list_objects_v2(self, **kwargs):
        if self.list_responses:
            return self.list_responses.pop(0)
        return {"Contents": [], "IsTruncated": False}


class ObjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = S3ObjectStoreSettings(
            bucket="article-agent-private",
            region="us-east-1",
            endpoint_url="http://object-store.test",
            access_key_id="test-access",
            secret_access_key="test-secret",
            force_path_style=True,
            server_side_encryption="AES256",
        )
        self.client = FakeS3Client()
        self.store = S3ObjectStore(
            self.settings,
            client=self.client,  # type: ignore[arg-type]
        )

    def test_key_is_deterministic_and_rejects_scope_traversal(self) -> None:
        digest = hashlib.sha256(b"asset").hexdigest()
        key = build_project_object_key("org-a", "project-a", digest)

        self.assertEqual(
            key,
            f"organizations/org-a/projects/project-a/blobs/{digest[:2]}/{digest}",
        )
        for value in ("../org", "org/a", r"org\a", ".", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    build_project_object_key(value, "project-a", digest)

    def test_put_is_private_encrypted_and_hash_bound(self) -> None:
        uploaded = ProjectObjectUploader(
            self.store,
            bucket=self.settings.bucket,
        ).upload(
            organization_id="org-a",
            project_id="project-a",
            data=b"private asset",
            content_type="image/webp",
            metadata={"Source Kind": "product_image"},
        )
        call = self.client.put_calls[0]

        self.assertEqual(call["Bucket"], self.settings.bucket)
        self.assertEqual(call["Key"], uploaded.stored.key)
        self.assertEqual(call["ServerSideEncryption"], "AES256")
        self.assertNotIn("ACL", call)
        self.assertEqual(
            call["Metadata"],
            {
                "sha256": uploaded.stored.content_hash,
                "source-kind": "product_image",
            },
        )
        self.assertEqual(uploaded.stored.byte_size, len(b"private asset"))
        self.assertEqual(uploaded.stored.etag, "etag-1")
        self.assertEqual(
            uploaded.object_uri,
            f"s3://{self.settings.bucket}/{uploaded.stored.key}",
        )

    def test_get_enforces_size_limit_and_closes_stream(self) -> None:
        body = io.BytesIO(b"data")
        self.client.get_response = {"ContentLength": 4, "Body": body}
        self.assertEqual(self.store.get("object-key", max_bytes=4), b"data")
        self.assertTrue(body.closed)

        oversized = io.BytesIO(b"oversized")
        self.client.get_response = {
            "ContentLength": 9,
            "Body": oversized,
        }
        with self.assertRaises(ObjectTooLarge):
            self.store.get("object-key", max_bytes=4)

    def test_download_expiry_delete_and_configuration_gate(self) -> None:
        url = self.store.create_download_url(
            "private-key",
            expires_seconds=300,
        )
        self.assertEqual(
            url,
            "https://signed.example.test/private-key?ttl=300",
        )
        with self.assertRaises(ValueError):
            self.store.create_download_url(
                "private-key",
                expires_seconds=3601,
            )
        self.store.delete("private-key")
        self.assertEqual(
            self.client.deleted,
            [{"Bucket": self.settings.bucket, "Key": "private-key"}],
        )
        with self.assertRaisesRegex(ValueError, "configured together"):
            S3ObjectStoreSettings(
                bucket="bucket",
                access_key_id="only-access-key",
            )

    def test_list_is_paginated_sorted_and_provider_errors_are_stable(self) -> None:
        modified = datetime(2026, 7, 31, tzinfo=timezone.utc)
        self.client.list_responses = [
            {
                "Contents": [
                    {
                        "Key": "scope/z",
                        "Size": 2,
                        "LastModified": modified,
                        "ETag": '"z"',
                    }
                ],
                "IsTruncated": True,
                "NextContinuationToken": "page-2",
            },
            {
                "Contents": [
                    {
                        "Key": "scope/a",
                        "Size": 1,
                        "LastModified": modified,
                        "ETag": '"a"',
                    }
                ],
                "IsTruncated": False,
            },
        ]

        listed = self.store.list(prefix="scope")

        self.assertEqual([item.key for item in listed], ["scope/a", "scope/z"])
        self.assertEqual(listed[0].etag, "a")
        self.client.list_responses = [
            {"IsTruncated": True, "NextContinuationToken": ""}
        ]
        with self.assertRaisesRegex(ObjectStoreError, "^object store list failed$"):
            self.store.list(prefix="scope")

    def test_settings_hide_secrets_and_do_not_reuse_other_keys(self) -> None:
        environment = {
            "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "private-bucket",
            "ARTICLE_AGENT_OBJECT_STORE_ACCESS_KEY": "object-access",
            "ARTICLE_AGENT_OBJECT_STORE_SECRET_KEY": "unique-object-secret",
            "LLM_API_KEY": "llm-secret",
            "EMBEDDING_API_KEY": "embedding-secret",
        }
        settings = S3ObjectStoreSettings.from_environment(environment)

        self.assertEqual(settings.access_key_id, "object-access")
        self.assertEqual(settings.secret_access_key, "unique-object-secret")
        self.assertNotIn("unique-object-secret", repr(settings))
        self.assertNotIn("object-access", repr(settings))
        self.assertEqual(
            S3ObjectStoreSettings.from_environment(
                {
                    "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "private-bucket",
                    "ARTICLE_AGENT_OBJECT_STORE_SSE": "none",
                }
            ).server_side_encryption,
            "",
        )
        with self.assertRaises(ValueError):
            S3ObjectStoreSettings.from_environment(
                {
                    "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "",
                    "LLM_API_KEY": "must-not-fallback",
                }
            )
        for endpoint in (
            "ftp://object-store.test",
            "http://user:secret@object-store.test",
            "http://object-store.test/path",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, "absolute HTTP"):
                    S3ObjectStoreSettings(
                        bucket="private-bucket",
                        endpoint_url=endpoint,
                    )

    def test_metadata_rejects_non_ascii_provider_headers(self) -> None:
        with self.assertRaisesRegex(ValueError, "ASCII"):
            self.store.put(
                key="private-key",
                data=b"data",
                content_type="application/octet-stream",
                metadata={"caption": "产品主图"},
            )

    def test_provider_errors_do_not_expose_secret_or_body(self) -> None:
        self.client.fail_put = True
        with self.assertRaisesRegex(
            ObjectStoreError,
            "^object store put failed$",
        ) as caught:
            self.store.put(
                key="private-key",
                data=b"data",
                content_type="application/octet-stream",
            )
        message = str(caught.exception)
        self.assertNotIn("test-secret", message)
        self.assertNotIn("secret provider body", message)

        self.client.fail_ready = True
        with self.assertRaisesRegex(
            ObjectStoreError,
            "^object store readiness check failed$",
        ) as readiness:
            self.store.check_ready()
        self.assertNotIn("secret provider body", str(readiness.exception))


if __name__ == "__main__":
    unittest.main()
