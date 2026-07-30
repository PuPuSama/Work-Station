from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import ClientError


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.object_store import (  # noqa: E402
    S3ObjectStore,
    S3ObjectStoreSettings,
    build_project_object_key,
    build_project_object_prefix,
)


@unittest.skipUnless(
    os.environ.get("ARTICLE_AGENT_OBJECT_STORE_INTEGRATION") == "1",
    "set ARTICLE_AGENT_OBJECT_STORE_INTEGRATION=1 for a real S3 test",
)
class S3ObjectStoreIntegrationTests(unittest.TestCase):
    """Opt-in contract test for a dedicated disposable S3-compatible bucket."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = S3ObjectStoreSettings.from_environment()
        if not cls.settings.bucket.startswith("article-agent-test-"):
            raise unittest.SkipTest(
                "integration bucket must start with article-agent-test-"
            )
        cls.store = S3ObjectStore(cls.settings)
        cls.client = cls.store._client
        try:
            cls.client.head_bucket(Bucket=cls.settings.bucket)
        except ClientError as exc:
            error_code = str(
                exc.response.get("Error", {}).get("Code", "")
            )
            if error_code not in {"404", "NoSuchBucket"}:
                raise
            arguments = {"Bucket": cls.settings.bucket}
            if (
                cls.settings.endpoint_url == ""
                and cls.settings.region != "us-east-1"
            ):
                arguments["CreateBucketConfiguration"] = {
                    "LocationConstraint": cls.settings.region
                }
            cls.client.create_bucket(**arguments)

    def test_put_get_sign_and_delete_round_trip(self) -> None:
        data = f"m7-object-store-{uuid4()}".encode()
        digest = hashlib.sha256(data).hexdigest()
        key = build_project_object_key(
            "integration-org",
            "integration-project",
            digest,
        )
        try:
            stored = self.store.put(
                key=key,
                data=data,
                content_type="application/octet-stream",
                metadata={"test-run": "m7"},
            )
            self.assertEqual(stored.content_hash, digest)
            self.assertEqual(self.store.get(key, max_bytes=4096), data)
            listed = self.store.list(
                prefix=build_project_object_prefix(
                    "integration-org",
                    "integration-project",
                )
            )
            self.assertIn(key, {item.key for item in listed})
            signed = self.store.create_download_url(
                key,
                expires_seconds=60,
            )
            self.assertTrue(signed.startswith(("http://", "https://")))
        finally:
            self.store.delete(key)
        with self.assertRaises(ClientError):
            self.client.head_object(
                Bucket=self.settings.bucket,
                Key=key,
            )


if __name__ == "__main__":
    unittest.main()
