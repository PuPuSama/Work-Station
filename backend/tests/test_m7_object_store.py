from __future__ import annotations

import hashlib
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.object_store import (  # noqa: E402
    ObjectStat,
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
        self.head_calls: list[dict[str, str]] = []
        self.head_response: dict[str, object] = {
            "ContentLength": 4,
            "ContentType": "application/json",
            "Metadata": {"sha256": "a" * 64},
            "ETag": '"etag-head"',
        }
        self.presign_calls: list[dict[str, object]] = []
        self.deleted: list[dict[str, str]] = []
        self.list_responses: list[dict[str, object]] = []
        self.fail_put = False
        self.fail_ready = False
        self.fail_head = False
        self.fail_presign = False

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

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs)
        if self.fail_head:
            raise ClientError(
                {"Error": {"Code": "500", "Message": "secret head body"}},
                "HeadObject",
            )
        return self.head_response

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.presign_calls.append(
            {
                "operation": operation,
                "Params": Params,
                "ExpiresIn": ExpiresIn,
            }
        )
        if self.fail_presign:
            raise ClientError(
                {"Error": {"Code": "500", "Message": "secret sign body"}},
                "GetObject",
            )
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

    def test_head_returns_provider_neutral_verified_metadata(self) -> None:
        digest = hashlib.sha256(b"data").hexdigest()
        self.client.head_response = {
            "ContentLength": 4,
            "ContentType": "application/json",
            "Metadata": {"sha256": digest, "private-provider-field": "ignored"},
            "ETag": '"etag-head"',
        }

        metadata = self.store.head("scope/private-object")

        self.assertIsInstance(metadata, ObjectStat)
        self.assertEqual(metadata.key, "scope/private-object")
        self.assertEqual(metadata.byte_size, 4)
        self.assertEqual(metadata.content_type, "application/json")
        self.assertEqual(metadata.sha256, digest)
        self.assertEqual(metadata.etag, "etag-head")
        self.assertEqual(
            self.client.head_calls,
            [
                {
                    "Bucket": self.settings.bucket,
                    "Key": "scope/private-object",
                }
            ],
        )

    def test_head_rejects_invalid_provider_metadata_and_hides_errors(self) -> None:
        invalid_responses = (
            {
                "ContentLength": 4,
                "ContentType": "application/json",
                "Metadata": {},
            },
            {
                "ContentLength": 4,
                "ContentType": "application/json\r\nX-Unsafe: yes",
                "Metadata": {"sha256": "a" * 64},
            },
            {
                "ContentLength": -1,
                "ContentType": "application/json",
                "Metadata": {"sha256": "a" * 64},
            },
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                self.client.head_response = response
                with self.assertRaisesRegex(
                    ObjectStoreError,
                    "^object store head failed$",
                ):
                    self.store.head("scope/private-object")

        self.client.fail_head = True
        with self.assertRaisesRegex(
            ObjectStoreError,
            "^object store head failed$",
        ) as caught:
            self.store.head("scope/private-object")
        self.assertNotIn("secret head body", str(caught.exception))

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

    def test_internal_endpoint_separates_data_and_download_clients(self) -> None:
        data_client = FakeS3Client()
        download_client = FakeS3Client()
        settings = S3ObjectStoreSettings(
            bucket="article-agent-private",
            endpoint_url="https://objects.example.test",
            internal_endpoint_url="http://object-store:9000",
            access_key_id="test-access",
            secret_access_key="test-secret",
        )

        with patch(
            "services.object_store.boto3.client",
            side_effect=(data_client, download_client),
        ) as client_factory:
            store = S3ObjectStore(settings)

        self.assertEqual(
            [call.kwargs["endpoint_url"] for call in client_factory.call_args_list],
            ["http://object-store:9000", "https://objects.example.test"],
        )
        store.head("scope/private-object")
        signed = store.create_download_url(
            "scope/private-object",
            expires_seconds=300,
        )

        self.assertEqual(len(data_client.head_calls), 1)
        self.assertEqual(data_client.presign_calls, [])
        self.assertEqual(len(download_client.presign_calls), 1)
        self.assertTrue(signed.startswith("https://signed.example.test/"))

    def test_download_url_supports_safe_response_header_overrides(self) -> None:
        url = self.store.create_download_url(
            "private-key",
            expires_seconds=60,
            response_content_type="text/plain; charset=utf-8",
            response_content_disposition='attachment; filename="evidence.txt"',
        )

        self.assertEqual(
            url,
            "https://signed.example.test/private-key?ttl=60",
        )
        self.assertEqual(
            self.client.presign_calls[-1],
            {
                "operation": "get_object",
                "Params": {
                    "Bucket": self.settings.bucket,
                    "Key": "private-key",
                    "ResponseContentType": "text/plain; charset=utf-8",
                    "ResponseContentDisposition": (
                        'attachment; filename="evidence.txt"'
                    ),
                },
                "ExpiresIn": 60,
            },
        )

        unsafe_values = (
            {"response_content_type": "text/plain\r\nX-Unsafe: yes"},
            {"response_content_disposition": "attachment\x00unsafe"},
            {"response_content_type": "text/plain;" + "x" * 255},
            {"response_content_disposition": "attachment;" + "x" * 512},
            {"response_content_type": "text/浜у搧"},
        )
        for kwargs in unsafe_values:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.store.create_download_url(
                        "private-key",
                        expires_seconds=60,
                        **kwargs,
                    )

        self.client.fail_presign = True
        with self.assertRaisesRegex(
            ObjectStoreError,
            "^object store download signing failed$",
        ) as caught:
            self.store.create_download_url(
                "private-key",
                expires_seconds=60,
                response_content_type="text/plain",
            )
        self.assertNotIn("secret sign body", str(caught.exception))

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
            "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT": "https://objects.example.test",
            "ARTICLE_AGENT_OBJECT_STORE_INTERNAL_ENDPOINT": (
                "http://object-store:9000"
            ),
            "ARTICLE_AGENT_OBJECT_STORE_ACCESS_KEY": "object-access",
            "ARTICLE_AGENT_OBJECT_STORE_SECRET_KEY": "unique-object-secret",
            "LLM_API_KEY": "llm-secret",
            "EMBEDDING_API_KEY": "embedding-secret",
        }
        settings = S3ObjectStoreSettings.from_environment(environment)

        self.assertEqual(settings.access_key_id, "object-access")
        self.assertEqual(settings.secret_access_key, "unique-object-secret")
        self.assertEqual(
            settings.internal_endpoint_url,
            "http://object-store:9000",
        )
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
        with self.assertRaisesRegex(ValueError, "endpoint_url is required"):
            S3ObjectStoreSettings(
                bucket="private-bucket",
                internal_endpoint_url="http://object-store:9000",
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
