from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from services.deployment_readiness import (  # noqa: E402
    DatabaseReadiness,
    ServerCutoverCapabilities,
    run_deployment_preflight,
)
from services.recovery_evidence import (  # noqa: E402
    MAX_RECOVERY_EVIDENCE_BYTES,
    RECOVERY_EVIDENCE_SCHEMA_V1,
    REQUIRED_DATABASE_RESTORE_CHECK_IDS,
    RecoveryEvidenceError,
    VerifiedRecoveryEvidence,
    load_verified_recovery_evidence,
    recovery_evidence_key_id,
    verify_recovery_evidence_bytes,
)


NOW = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
RELEASE_COMMIT = "1" * 40
ALEMBIC_HEAD = "20260826_0033"

COMPLETE_ENVIRONMENT = {
    "ARTICLE_AGENT_SERVER_MODE": "true",
    "ARTICLE_AGENT_SERVER_SESSION_SECRET": "s" * 32,
    "ARTICLE_AGENT_OIDC_ISSUER": "https://identity.test/tenant",
    "ARTICLE_AGENT_OIDC_CLIENT_ID": "article-agent",
    "ARTICLE_AGENT_OIDC_CLIENT_SECRET": "private-oidc-secret",
    "ARTICLE_AGENT_OIDC_REDIRECT_URI": (
        "https://app.test/api/auth/oidc/callback"
    ),
    "ARTICLE_AGENT_DATABASE_URL": "postgresql+psycopg://user:secret@db/app",
    "EMBEDDING_BASE_URL": "https://embedding.test/v1",
    "EMBEDDING_API_KEY": "private-embedding-key",
    "EMBEDDING_MODEL": "text-embedding-3-small",
    "EMBEDDING_DIMENSIONS": "1536",
    "ARTICLE_AGENT_OBJECT_STORE_BUCKET": "private-bucket",
    "ARTICLE_AGENT_OBJECT_STORE_REGION": "us-east-1",
    "ARTICLE_AGENT_OBJECT_STORE_ENDPOINT": "https://objects.test",
    "ARTICLE_AGENT_OBJECT_STORE_ACCESS_KEY": "private-access-key",
    "ARTICLE_AGENT_OBJECT_STORE_SECRET_KEY": "private-object-secret",
    "ARTICLE_AGENT_OBJECT_STORE_SSE": "AES256",
}


class _ReadyStore:
    def check_ready(self) -> None:
        return None


def _public_key_base64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _payload() -> dict[str, object]:
    return {
        "release_commit": RELEASE_COMMIT,
        "alembic_head": ALEMBIC_HEAD,
        "started_at": "2026-08-06T10:00:00Z",
        "completed_at": "2026-08-06T11:00:00Z",
        "expires_at": "2026-08-07T11:00:00Z",
        "operator": "operator-secret@example.test",
        "reviewer": "reviewer-secret@example.test",
        "evidence_bundle_sha256": "a" * 64,
        "database_restore": {
            "dump_sha256": "b" * 64,
            "source_manifest_sha256": "c" * 64,
            "restored_manifest_sha256": "c" * 64,
            "checks": {
                check_id: True
                for check_id in REQUIRED_DATABASE_RESTORE_CHECK_IDS
            },
        },
        "object_restore": {
            "inventory_sha256": "d" * 64,
            "sample_manifest_sha256": "e" * 64,
            "sample_count": 4,
            "matched_count": 4,
            "samples_by_kind": {
                "product_primary": {
                    "sample_count": 1,
                    "matched_count": 1,
                },
                "product_gallery": {
                    "sample_count": 1,
                    "matched_count": 1,
                },
                "private_document": {
                    "sample_count": 1,
                    "matched_count": 1,
                },
                "normalized_artifact": {
                    "sample_count": 1,
                    "matched_count": 1,
                },
            },
        },
        "recovery_objectives": {
            "target_rpo_seconds": 3600,
            "observed_rpo_seconds": 1800,
            "target_rto_seconds": 7200,
            "observed_rto_seconds": 3600,
        },
    }


def _signed_bytes(
    private_key: Ed25519PrivateKey,
    payload: dict[str, object],
) -> bytes:
    signed = {
        "schema_version": RECOVERY_EVIDENCE_SCHEMA_V1,
        "signature_algorithm": "Ed25519",
        "signing_key_id": recovery_evidence_key_id(
            private_key.public_key()
        ),
        "payload": payload,
    }
    canonical = json.dumps(
        signed,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = base64.urlsafe_b64encode(
        private_key.sign(canonical)
    ).decode("ascii").rstrip("=")
    return json.dumps(
        {**signed, "signature": signature},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = _public_key_base64(self.private_key)

    def verify(self, payload: dict[str, object]):
        return verify_recovery_evidence_bytes(
            _signed_bytes(self.private_key, payload),
            trusted_public_key_base64=self.public_key,
            expected_release_commit=RELEASE_COMMIT,
            expected_alembic_head=ALEMBIC_HEAD,
            now=NOW,
        )

    def assert_invalid(
        self,
        data: bytes,
        *,
        public_key: str | None = None,
        release_commit: str = RELEASE_COMMIT,
        alembic_head: str = ALEMBIC_HEAD,
    ) -> None:
        with self.assertRaisesRegex(
            RecoveryEvidenceError,
            r"^recovery evidence is invalid$",
        ):
            verify_recovery_evidence_bytes(
                data,
                trusted_public_key_base64=(
                    public_key if public_key is not None else self.public_key
                ),
                expected_release_commit=release_commit,
                expected_alembic_head=alembic_head,
                now=NOW,
            )

    def test_valid_signed_envelope_passes_all_recovery_gates(self) -> None:
        verified = self.verify(_payload())
        self.assertEqual(
            verified.public_values(),
            {
                "identity": True,
                "database_restore": True,
                "object_restore": True,
                "recovery_objectives": True,
            },
        )

        report = run_deployment_preflight(
            environment=COMPLETE_ENVIRONMENT,
            database_probe=lambda: DatabaseReadiness(
                revision=ALEMBIC_HEAD,
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: _ReadyStore(),
            identity_provider_probe=lambda settings: None,
            capabilities=ServerCutoverCapabilities(
                True,
                True,
                True,
                True,
                True,
                True,
            ),
            recovery_evidence=verified,
        )
        by_id = {check.check_id: check for check in report.checks}
        self.assertTrue(report.ready)
        for check_id in (
            "recovery_evidence_identity",
            "database_restore",
            "object_restore",
            "recovery_objectives",
        ):
            self.assertTrue(by_id[check_id].passed)

    def test_signed_evidence_does_not_flip_missing_code_capabilities(
        self,
    ) -> None:
        verified = self.verify(_payload())
        report = run_deployment_preflight(
            environment=COMPLETE_ENVIRONMENT,
            database_probe=lambda: DatabaseReadiness(
                revision=ALEMBIC_HEAD,
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: _ReadyStore(),
            identity_provider_probe=lambda settings: None,
            capabilities=ServerCutoverCapabilities(),
            recovery_evidence=verified,
        )
        by_id = {check.check_id: check for check in report.checks}
        self.assertFalse(report.ready)
        self.assertFalse(by_id["server_cutover"].passed)
        self.assertTrue(by_id["recovery_evidence_identity"].passed)
        self.assertTrue(by_id["database_restore"].passed)
        self.assertTrue(by_id["object_restore"].passed)
        self.assertTrue(by_id["recovery_objectives"].passed)

    def test_preflight_rejects_duck_typed_recovery_evidence(self) -> None:
        class FakeRecoveryEvidence:
            database_restore_passed = True
            object_restore_passed = True
            recovery_objectives_passed = True

        report = run_deployment_preflight(
            environment=COMPLETE_ENVIRONMENT,
            database_probe=lambda: DatabaseReadiness(
                revision=ALEMBIC_HEAD,
                vector_extension="0.8.1",
            ),
            object_store_factory=lambda settings: _ReadyStore(),
            identity_provider_probe=lambda settings: None,
            capabilities=ServerCutoverCapabilities(
                True,
                True,
                True,
                True,
                True,
                True,
            ),
            recovery_evidence=FakeRecoveryEvidence(),  # type: ignore[arg-type]
        )
        by_id = {check.check_id: check for check in report.checks}
        self.assertFalse(report.ready)
        for check_id in (
            "recovery_evidence_identity",
            "database_restore",
            "object_restore",
            "recovery_objectives",
        ):
            self.assertFalse(by_id[check_id].passed)

    def test_tamper_and_wrong_signature_key_are_rejected(self) -> None:
        data = json.loads(_signed_bytes(self.private_key, _payload()))
        data["payload"]["operator"] = "tampered@example.test"
        tampered = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.assert_invalid(tampered)

        other_key = Ed25519PrivateKey.generate()
        self.assert_invalid(
            _signed_bytes(self.private_key, _payload()),
            public_key=_public_key_base64(other_key),
        )

    def test_expired_future_and_overlong_windows_are_rejected(self) -> None:
        mutations = (
            ("expires_at", "2026-08-06T12:00:00Z"),
            ("completed_at", "2026-08-06T12:01:00Z"),
            ("started_at", "2026-08-04T10:00:00Z"),
            ("expires_at", "2026-08-14T11:00:01Z"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                payload = _payload()
                payload[field] = value
                self.assert_invalid(_signed_bytes(self.private_key, payload))

    def test_wrong_or_non_sha1_commit_and_wrong_head_are_rejected(self) -> None:
        data = _signed_bytes(self.private_key, _payload())
        self.assert_invalid(data, release_commit="2" * 40)
        self.assert_invalid(data, release_commit="2" * 64)
        self.assert_invalid(data, alembic_head="20260806_9999")

    def test_incomplete_or_mistyped_database_checks_are_rejected(self) -> None:
        payload = _payload()
        checks = payload["database_restore"]["checks"]
        checks.pop(next(iter(checks)))
        self.assert_invalid(_signed_bytes(self.private_key, payload))

        payload = _payload()
        payload["database_restore"]["checks"][
            "schema_and_vector"
        ] = 1
        self.assert_invalid(_signed_bytes(self.private_key, payload))

    def test_database_failure_is_a_valid_independent_gate_result(self) -> None:
        failed_check = _payload()
        failed_check["database_restore"]["checks"][
            "schema_and_vector"
        ] = False
        self.assertFalse(self.verify(failed_check).database_restore_passed)

        manifest_mismatch = _payload()
        manifest_mismatch["database_restore"][
            "restored_manifest_sha256"
        ] = "f" * 64
        self.assertFalse(
            self.verify(manifest_mismatch).database_restore_passed
        )

    def test_object_mismatch_and_recovery_target_miss_are_independent(self) -> None:
        object_mismatch = _payload()
        object_mismatch["object_restore"]["matched_count"] = 3
        self.assertFalse(
            self.verify(object_mismatch).object_restore_passed
        )

        objective_miss = _payload()
        objective_miss["recovery_objectives"][
            "observed_rpo_seconds"
        ] = 3601
        objective_miss["recovery_objectives"][
            "observed_rto_seconds"
        ] = 7201
        self.assertFalse(
            self.verify(objective_miss).recovery_objectives_passed
        )

    def test_missing_object_kind_and_unknown_fields_are_rejected(self) -> None:
        missing_kind = _payload()
        missing_kind["object_restore"]["samples_by_kind"].pop(
            "normalized_artifact"
        )
        self.assert_invalid(_signed_bytes(self.private_key, missing_kind))

        extra = _payload()
        extra["private_note"] = "must-not-be-accepted"
        self.assert_invalid(_signed_bytes(self.private_key, extra))

    def test_duplicate_json_key_is_rejected(self) -> None:
        data = _signed_bytes(self.private_key, _payload())
        duplicate = data.replace(
            b'"schema_version":',
            b'"schema_version":"duplicate","schema_version":',
            1,
        )
        self.assert_invalid(duplicate)

    def test_oversize_nan_and_non_boolean_or_count_types_are_rejected(
        self,
    ) -> None:
        self.assert_invalid(b"{" + b" " * MAX_RECOVERY_EVIDENCE_BYTES)

        valid = _signed_bytes(self.private_key, _payload()).decode("utf-8")
        nan_data = valid.replace(
            '"target_rpo_seconds":3600',
            '"target_rpo_seconds":NaN',
        ).encode("utf-8")
        self.assert_invalid(nan_data)

        payload = _payload()
        payload["object_restore"]["sample_count"] = True
        self.assert_invalid(_signed_bytes(self.private_key, payload))

        oversized_integer = b'{"value":' + b"1" * 10_000 + b"}"
        self.assert_invalid(oversized_integer)

    def test_only_canonical_base64_raw_public_key_is_accepted(self) -> None:
        data = _signed_bytes(self.private_key, _payload())
        pem = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        self.assert_invalid(data, public_key=pem)
        self.assert_invalid(data, public_key=self.public_key.rstrip("="))
        self.assert_invalid(data, public_key="非 ASCII 公钥")

    def test_verified_result_rejects_ordinary_construction(self) -> None:
        with self.assertRaisesRegex(
            RecoveryEvidenceError,
            r"^recovery evidence is invalid$",
        ):
            VerifiedRecoveryEvidence(
                database_restore_passed=True,
                object_restore_passed=True,
                recovery_objectives_passed=True,
                _verification_token=object(),
            )

    def test_non_datetime_now_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            RecoveryEvidenceError,
            r"^recovery evidence is invalid$",
        ):
            verify_recovery_evidence_bytes(
                _signed_bytes(self.private_key, _payload()),
                trusted_public_key_base64=self.public_key,
                expected_release_commit=RELEASE_COMMIT,
                expected_alembic_head=ALEMBIC_HEAD,
                now=object(),  # type: ignore[arg-type]
            )

    def test_public_results_and_errors_do_not_expose_private_values(self) -> None:
        verified = self.verify(_payload())
        public = str(verified.public_values())
        for secret in (
            "operator-secret",
            "reviewer-secret",
            "a" * 64,
            "b" * 64,
            RELEASE_COMMIT,
            ALEMBIC_HEAD,
        ):
            self.assertNotIn(secret, public)

        invalid = _payload()
        invalid["operator"] = "same-secret@example.test"
        invalid["reviewer"] = "same-secret@example.test"
        try:
            self.verify(invalid)
        except RecoveryEvidenceError as exc:
            error = str(exc)
        else:
            self.fail("invalid evidence unexpectedly passed")
        self.assertEqual(error, "recovery evidence is invalid")
        self.assertNotIn("same-secret", error)

    def test_file_loader_rejects_missing_and_accepts_bounded_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.json"
            with self.assertRaises(RecoveryEvidenceError):
                load_verified_recovery_evidence(
                    path,
                    trusted_public_key_base64=self.public_key,
                    expected_release_commit=RELEASE_COMMIT,
                    expected_alembic_head=ALEMBIC_HEAD,
                    now=NOW,
                )
            path.write_bytes(_signed_bytes(self.private_key, _payload()))
            verified = load_verified_recovery_evidence(
                path,
                trusted_public_key_base64=self.public_key,
                expected_release_commit=RELEASE_COMMIT,
                expected_alembic_head=ALEMBIC_HEAD,
                now=NOW,
            )
        self.assertTrue(verified.database_restore_passed)


if __name__ == "__main__":
    unittest.main()
