from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)


RECOVERY_EVIDENCE_SCHEMA_V1 = "article-agent.recovery-evidence.v1"
MAX_RECOVERY_EVIDENCE_BYTES = 64 * 1024
MAX_RECOVERY_EVIDENCE_VALIDITY = timedelta(days=7)
MAX_RECOVERY_DRILL_DURATION = timedelta(hours=24)

REQUIRED_DATABASE_RESTORE_CHECK_IDS = frozenset(
    {
        "schema_and_vector",
        "required_relations",
        "workspace_session_versions",
        "tenant_foreign_keys",
        "audit_append_only",
        "snapshot_pointer_constraints",
        "snapshot_review_receipts",
        "snapshot_review_append_only",
        "task_job_manifest",
    }
)
REQUIRED_OBJECT_SAMPLE_KINDS = frozenset(
    {
        "product_primary",
        "product_gallery",
        "private_document",
        "normalized_artifact",
    }
)

_FULL_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_KEY_ID = re.compile(r"ed25519:[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:+/-]{0,254}\Z")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]{86}\Z")


class RecoveryEvidenceError(ValueError):
    """Recovery evidence failed closed without exposing its contents."""

    def __init__(self) -> None:
        super().__init__("recovery evidence is invalid")


@dataclass(frozen=True, slots=True)
class RecoveryEvidenceEnvelopeV1:
    """Private parsed V1 envelope used only during signature verification."""

    signed_values: Mapping[str, object]
    signature: bytes


@dataclass(frozen=True, slots=True, init=False)
class VerifiedRecoveryEvidence:
    """Safe gate results with no identities, hashes, paths, or timestamps."""

    database_restore_passed: bool
    object_restore_passed: bool
    recovery_objectives_passed: bool

    def __init__(
        self,
        *,
        database_restore_passed: bool,
        object_restore_passed: bool,
        recovery_objectives_passed: bool,
        _verification_token: object,
    ) -> None:
        if _verification_token is not _VERIFICATION_TOKEN:
            raise RecoveryEvidenceError()
        object.__setattr__(
            self,
            "database_restore_passed",
            database_restore_passed,
        )
        object.__setattr__(
            self,
            "object_restore_passed",
            object_restore_passed,
        )
        object.__setattr__(
            self,
            "recovery_objectives_passed",
            recovery_objectives_passed,
        )

    def public_values(self) -> dict[str, bool]:
        return {
            "identity": True,
            "database_restore": self.database_restore_passed,
            "object_restore": self.object_restore_passed,
            "recovery_objectives": self.recovery_objectives_passed,
        }


_VERIFICATION_TOKEN = object()


def _invalid(*_args: object, **_kwargs: object) -> NoReturn:
    raise RecoveryEvidenceError()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryEvidenceError()
        result[key] = value
    return result


def _mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise RecoveryEvidenceError()
    return value


def _string(
    value: object,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RecoveryEvidenceError()
    if "\r" in value or "\n" in value:
        raise RecoveryEvidenceError()
    if pattern is not None and pattern.fullmatch(value) is None:
        raise RecoveryEvidenceError()
    return value


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise RecoveryEvidenceError()
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise RecoveryEvidenceError()
    return value


def _utc_datetime(value: object) -> datetime:
    text = _string(value, maximum=40)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (binascii.Error, ValueError) as exc:
        raise RecoveryEvidenceError() from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RecoveryEvidenceError()
    return parsed.astimezone(timezone.utc)


def _signature(value: object) -> bytes:
    encoded = _string(value, maximum=86, pattern=_BASE64URL)
    try:
        decoded = base64.urlsafe_b64decode(encoded + "==")
    except ValueError as exc:
        raise RecoveryEvidenceError() from exc
    if len(decoded) != 64:
        raise RecoveryEvidenceError()
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != encoded:
        raise RecoveryEvidenceError()
    return decoded


def _public_key(value: str | bytes) -> Ed25519PublicKey:
    if not isinstance(value, (str, bytes)):
        raise RecoveryEvidenceError()
    try:
        encoded = value.encode("ascii") if isinstance(value, str) else value
    except UnicodeEncodeError as exc:
        raise RecoveryEvidenceError() from exc
    if not encoded or len(encoded) > 64:
        raise RecoveryEvidenceError()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RecoveryEvidenceError() from exc
    if len(raw) != 32 or base64.b64encode(raw) != encoded:
        raise RecoveryEvidenceError()
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise RecoveryEvidenceError() from exc


def recovery_evidence_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "ed25519:" + hashlib.sha256(raw).hexdigest()


def _canonical(values: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            values,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (
        TypeError,
        UnicodeEncodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise RecoveryEvidenceError() from exc


def _parse_envelope(data: bytes) -> RecoveryEvidenceEnvelopeV1:
    if (
        not isinstance(data, bytes)
        or not data
        or len(data) > MAX_RECOVERY_EVIDENCE_BYTES
    ):
        raise RecoveryEvidenceError()
    try:
        values = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_invalid,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise RecoveryEvidenceError() from exc
    envelope = _mapping(
        values,
        frozenset(
            {
                "schema_version",
                "signature_algorithm",
                "signing_key_id",
                "payload",
                "signature",
            }
        ),
    )
    schema = _string(envelope["schema_version"], maximum=64)
    algorithm = _string(envelope["signature_algorithm"], maximum=32)
    key_id = _string(
        envelope["signing_key_id"],
        maximum=72,
        pattern=_KEY_ID,
    )
    if schema != RECOVERY_EVIDENCE_SCHEMA_V1 or algorithm != "Ed25519":
        raise RecoveryEvidenceError()
    payload = _mapping(
        envelope["payload"],
        frozenset(
            {
                "release_commit",
                "alembic_head",
                "started_at",
                "completed_at",
                "expires_at",
                "operator",
                "reviewer",
                "evidence_bundle_sha256",
                "database_restore",
                "object_restore",
                "recovery_objectives",
            }
        ),
    )
    return RecoveryEvidenceEnvelopeV1(
        signed_values={
            "schema_version": schema,
            "signature_algorithm": algorithm,
            "signing_key_id": key_id,
            "payload": payload,
        },
        signature=_signature(envelope["signature"]),
    )


def _database_restore(value: object) -> bool:
    database = _mapping(
        value,
        frozenset(
            {
                "dump_sha256",
                "source_manifest_sha256",
                "restored_manifest_sha256",
                "checks",
            }
        ),
    )
    _string(database["dump_sha256"], maximum=64, pattern=_SHA256)
    source = _string(
        database["source_manifest_sha256"],
        maximum=64,
        pattern=_SHA256,
    )
    restored = _string(
        database["restored_manifest_sha256"],
        maximum=64,
        pattern=_SHA256,
    )
    checks = _mapping(database["checks"], REQUIRED_DATABASE_RESTORE_CHECK_IDS)
    check_results = tuple(_boolean(result) for result in checks.values())
    checks_ready = all(check_results)
    return source == restored and checks_ready


def _object_restore(value: object) -> bool:
    restored = _mapping(
        value,
        frozenset(
            {
                "inventory_sha256",
                "sample_manifest_sha256",
                "sample_count",
                "matched_count",
                "samples_by_kind",
            }
        ),
    )
    _string(restored["inventory_sha256"], maximum=64, pattern=_SHA256)
    _string(restored["sample_manifest_sha256"], maximum=64, pattern=_SHA256)
    sample_count = _integer(
        restored["sample_count"],
        minimum=1,
        maximum=100_000,
    )
    matched_count = _integer(
        restored["matched_count"],
        minimum=0,
        maximum=100_000,
    )
    by_kind = _mapping(
        restored["samples_by_kind"],
        REQUIRED_OBJECT_SAMPLE_KINDS,
    )
    total_samples = 0
    total_matches = 0
    kinds_ready = True
    for raw_counts in by_kind.values():
        counts = _mapping(
            raw_counts,
            frozenset({"sample_count", "matched_count"}),
        )
        kind_samples = _integer(
            counts["sample_count"],
            minimum=1,
            maximum=100_000,
        )
        kind_matches = _integer(
            counts["matched_count"],
            minimum=0,
            maximum=100_000,
        )
        total_samples += kind_samples
        total_matches += kind_matches
        kinds_ready = kinds_ready and kind_samples == kind_matches
    return (
        sample_count == total_samples
        and matched_count == total_matches
        and sample_count == matched_count
        and kinds_ready
    )


def _recovery_objectives(value: object) -> bool:
    objectives = _mapping(
        value,
        frozenset(
            {
                "target_rpo_seconds",
                "observed_rpo_seconds",
                "target_rto_seconds",
                "observed_rto_seconds",
            }
        ),
    )
    target_rpo = _integer(
        objectives["target_rpo_seconds"],
        minimum=1,
        maximum=31_536_000,
    )
    observed_rpo = _integer(
        objectives["observed_rpo_seconds"],
        minimum=0,
        maximum=31_536_000,
    )
    target_rto = _integer(
        objectives["target_rto_seconds"],
        minimum=1,
        maximum=31_536_000,
    )
    observed_rto = _integer(
        objectives["observed_rto_seconds"],
        minimum=0,
        maximum=31_536_000,
    )
    return observed_rpo <= target_rpo and observed_rto <= target_rto


def verify_recovery_evidence_bytes(
    data: bytes,
    *,
    trusted_public_key_base64: str | bytes,
    expected_release_commit: str,
    expected_alembic_head: str,
    now: datetime | None = None,
) -> VerifiedRecoveryEvidence:
    """Verify one signed artifact without retaining its private payload."""

    expected_commit = _string(
        expected_release_commit,
        maximum=40,
        pattern=_FULL_COMMIT,
    )
    expected_head = _string(expected_alembic_head, maximum=64)
    envelope = _parse_envelope(data)
    public_key = _public_key(trusted_public_key_base64)
    signed = envelope.signed_values
    if signed["signing_key_id"] != recovery_evidence_key_id(public_key):
        raise RecoveryEvidenceError()
    try:
        public_key.verify(envelope.signature, _canonical(signed))
    except InvalidSignature as exc:
        raise RecoveryEvidenceError() from exc

    payload = _mapping(
        signed["payload"],
        frozenset(
            {
                "release_commit",
                "alembic_head",
                "started_at",
                "completed_at",
                "expires_at",
                "operator",
                "reviewer",
                "evidence_bundle_sha256",
                "database_restore",
                "object_restore",
                "recovery_objectives",
            }
        ),
    )
    release_commit = _string(
        payload["release_commit"],
        maximum=40,
        pattern=_FULL_COMMIT,
    )
    alembic_head = _string(payload["alembic_head"], maximum=64)
    if release_commit != expected_commit or alembic_head != expected_head:
        raise RecoveryEvidenceError()
    started = _utc_datetime(payload["started_at"])
    completed = _utc_datetime(payload["completed_at"])
    expires = _utc_datetime(payload["expires_at"])
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise RecoveryEvidenceError()
    current = current.astimezone(timezone.utc)
    if not (
        started < completed <= current < expires
        and completed - started <= MAX_RECOVERY_DRILL_DURATION
        and expires - completed <= MAX_RECOVERY_EVIDENCE_VALIDITY
    ):
        raise RecoveryEvidenceError()
    operator = _string(
        payload["operator"],
        maximum=255,
        pattern=_IDENTITY,
    )
    reviewer = _string(
        payload["reviewer"],
        maximum=255,
        pattern=_IDENTITY,
    )
    if operator.casefold() == reviewer.casefold():
        raise RecoveryEvidenceError()
    _string(
        payload["evidence_bundle_sha256"],
        maximum=64,
        pattern=_SHA256,
    )
    database_ready = _database_restore(payload["database_restore"])
    object_ready = _object_restore(payload["object_restore"])
    objectives_ready = _recovery_objectives(payload["recovery_objectives"])
    return VerifiedRecoveryEvidence(
        database_restore_passed=database_ready,
        object_restore_passed=object_ready,
        recovery_objectives_passed=objectives_ready,
        _verification_token=_VERIFICATION_TOKEN,
    )


def load_verified_recovery_evidence(
    path: str | Path,
    *,
    trusted_public_key_base64: str | bytes,
    expected_release_commit: str,
    expected_alembic_head: str,
    now: datetime | None = None,
) -> VerifiedRecoveryEvidence:
    """Read one bounded evidence file and return only safe gate results."""

    try:
        evidence_path = Path(path)
        if not evidence_path.is_file():
            raise RecoveryEvidenceError()
        size = evidence_path.stat().st_size
        if size <= 0 or size > MAX_RECOVERY_EVIDENCE_BYTES:
            raise RecoveryEvidenceError()
        data = evidence_path.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise RecoveryEvidenceError() from exc
    return verify_recovery_evidence_bytes(
        data,
        trusted_public_key_base64=trusted_public_key_base64,
        expected_release_commit=expected_release_commit,
        expected_alembic_head=expected_alembic_head,
        now=now,
    )


__all__ = [
    "MAX_RECOVERY_EVIDENCE_BYTES",
    "RECOVERY_EVIDENCE_SCHEMA_V1",
    "REQUIRED_DATABASE_RESTORE_CHECK_IDS",
    "REQUIRED_OBJECT_SAMPLE_KINDS",
    "RecoveryEvidenceError",
    "VerifiedRecoveryEvidence",
    "load_verified_recovery_evidence",
    "recovery_evidence_key_id",
    "verify_recovery_evidence_bytes",
]
