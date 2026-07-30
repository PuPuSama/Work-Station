from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from knowledge_agent.schema import (
    knowledge_assets,
    snapshot_assets,
    source_snapshots,
)
from server_schema import article_tasks, object_orphan_observations
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
    decide_project_permission,
)
from services.audit_log import AuditEvent, AuditEventWriter, PostgresAuditEventWriter
from services.object_store import (
    ObjectMetadata,
    ObjectStore,
    ObjectStoreError,
    build_project_object_prefix,
)


DEFAULT_ORPHAN_GRACE = timedelta(days=7)
MINIMUM_ORPHAN_GRACE = timedelta(hours=24)
MINIMUM_ORPHAN_SIGHTINGS = 2


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _managed_key(uri: object, *, bucket: str, prefix: str) -> str | None:
    if not isinstance(uri, str) or not uri.strip():
        return None
    parsed = urlsplit(uri.strip())
    if (
        parsed.scheme != "s3"
        or parsed.netloc != bucket
        or parsed.query
        or parsed.fragment
    ):
        return None
    key = parsed.path.lstrip("/")
    if not key.startswith(prefix):
        return None
    return key


def _collect_task_asset_ids(value: object) -> set[str]:
    """Collect durable Task asset identities without trusting arbitrary values."""

    found: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.endswith("asset_id") and isinstance(child, str) and child.strip():
                found.add(child.strip())
            elif key.endswith("asset_ids") and isinstance(child, list):
                found.update(
                    item.strip()
                    for item in child
                    if isinstance(item, str) and item.strip()
                )
            else:
                found.update(_collect_task_asset_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_task_asset_ids(child))
    return found


def _fingerprint(item: ObjectMetadata) -> str:
    material = "\0".join(
        (
            item.key,
            item.etag,
            str(item.byte_size),
            item.last_modified.astimezone(timezone.utc).isoformat(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ObjectOrphanCandidate:
    key: str
    byte_size: int
    registered_asset_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    sighting_count: int
    eligible: bool


@dataclass(frozen=True)
class ObjectOrphanInventory:
    organization_id: str
    project_id: str
    scanned_object_count: int
    live_object_count: int
    candidates: tuple[ObjectOrphanCandidate, ...]

    @property
    def eligible_count(self) -> int:
        return sum(candidate.eligible for candidate in self.candidates)


@dataclass(frozen=True)
class ObjectOrphanCleanupReport:
    organization_id: str
    project_id: str
    eligible_count: int
    retired_registered_asset_count: int
    deleted_object_count: int
    object_delete_failure_count: int


@dataclass(frozen=True)
class _ReferenceState:
    live_keys: frozenset[str]
    asset_ids_by_key: Mapping[str, tuple[str, ...]]


class ProjectObjectOrphanReconciler:
    """Inventory and explicitly clean continuously unreferenced private objects.

    Reconciliation is deliberately project scoped. A candidate must be absent
    from snapshot URI references, snapshot asset links, and every persisted
    Task ``*_asset_id`` field. Cleanup additionally requires two observations,
    an unchanged provider fingerprint, and a grace period.
    """

    def __init__(
        self,
        engine: Engine,
        store: ObjectStore,
        *,
        bucket: str,
        grace_period: timedelta = DEFAULT_ORPHAN_GRACE,
        access_repository: PostgresProjectAccessRepository | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        if grace_period < MINIMUM_ORPHAN_GRACE:
            raise ValueError("orphan grace_period must be at least 24 hours")
        self._engine = engine
        self._store = store
        self._bucket = _required_text(bucket, "bucket")
        self._grace_period = grace_period
        self._access_repository = (
            access_repository or PostgresProjectAccessRepository(engine)
        )
        self._access = ProjectAccessService(self._access_repository)
        self._audit = audit or PostgresAuditEventWriter()

    def observe(
        self,
        actor: ActorIdentity,
        project_id: str,
        *,
        observed_at: datetime | None = None,
    ) -> ObjectOrphanInventory:
        normalized_project_id = _required_text(project_id, "project_id")
        self._access.require(actor, normalized_project_id, "knowledge.delete")
        now = _utc(observed_at or datetime.now(timezone.utc), "observed_at")
        prefix = build_project_object_prefix(
            actor.organization_id,
            normalized_project_id,
        )
        listed = self._store.list(prefix=prefix.rstrip("/"))
        with self._engine.begin() as connection:
            state = self._load_reference_state(
                connection,
                actor,
                normalized_project_id,
                prefix=prefix,
                lock=False,
            )
            candidate_items = {
                item.key: item
                for item in listed
                if item.key not in state.live_keys
            }
            self._record_observations(
                connection,
                actor,
                normalized_project_id,
                candidate_items,
                state.asset_ids_by_key,
                now,
            )
            rows = connection.execute(
                sa.select(object_orphan_observations)
                .where(
                    object_orphan_observations.c.organization_id
                    == actor.organization_id,
                    object_orphan_observations.c.project_id
                    == normalized_project_id,
                )
                .order_by(object_orphan_observations.c.object_key)
            ).mappings().all()
        candidates = tuple(
            self._candidate_from_row(row, now=now) for row in rows
        )
        return ObjectOrphanInventory(
            organization_id=actor.organization_id,
            project_id=normalized_project_id,
            scanned_object_count=len(listed),
            live_object_count=len(listed) - len(candidate_items),
            candidates=candidates,
        )

    def cleanup(
        self,
        actor: ActorIdentity,
        project_id: str,
        *,
        confirm_project_id: str,
        observed_at: datetime | None = None,
    ) -> ObjectOrphanCleanupReport:
        normalized_project_id = _required_text(project_id, "project_id")
        if (
            _required_text(confirm_project_id, "confirm_project_id")
            != normalized_project_id
        ):
            raise ValueError("confirm_project_id must exactly match project_id")
        now = _utc(observed_at or datetime.now(timezone.utc), "observed_at")
        inventory = self.observe(
            actor,
            normalized_project_id,
            observed_at=now,
        )
        eligible_keys = {
            candidate.key for candidate in inventory.candidates if candidate.eligible
        }
        if not eligible_keys:
            return ObjectOrphanCleanupReport(
                organization_id=actor.organization_id,
                project_id=normalized_project_id,
                eligible_count=0,
                retired_registered_asset_count=0,
                deleted_object_count=0,
                object_delete_failure_count=0,
            )

        prefix = build_project_object_prefix(
            actor.organization_id,
            normalized_project_id,
        )
        currently_listed = {
            item.key: item
            for item in self._store.list(prefix=prefix.rstrip("/"))
        }
        retired_asset_count = 0
        keys_to_delete: list[str] = []
        with self._engine.begin() as connection:
            facts = self._access_repository.lock_project_access_in_connection(
                connection,
                actor,
                normalized_project_id,
            )
            if not decide_project_permission(facts, "knowledge.delete").allowed:
                raise ProjectAccessDenied("project access denied")
            state = self._load_reference_state(
                connection,
                actor,
                normalized_project_id,
                prefix=prefix,
                lock=True,
            )
            observations = {
                str(row["object_key"]): row
                for row in connection.execute(
                    sa.select(object_orphan_observations)
                    .where(
                        object_orphan_observations.c.organization_id
                        == actor.organization_id,
                        object_orphan_observations.c.project_id
                        == normalized_project_id,
                        object_orphan_observations.c.object_key.in_(eligible_keys),
                    )
                    .with_for_update()
                ).mappings()
            }
            for key in sorted(eligible_keys):
                item = currently_listed.get(key)
                row = observations.get(key)
                if (
                    item is None
                    or row is None
                    or key in state.live_keys
                    or str(row["fingerprint"]) != _fingerprint(item)
                    or int(row["sighting_count"]) < MINIMUM_ORPHAN_SIGHTINGS
                    or _utc(row["first_seen_at"], "first_seen_at")
                    > now - self._grace_period
                ):
                    continue
                asset_ids = state.asset_ids_by_key.get(key, ())
                if asset_ids:
                    retired_asset_count += connection.execute(
                        knowledge_assets.delete().where(
                            knowledge_assets.c.project_id == normalized_project_id,
                            knowledge_assets.c.asset_id.in_(asset_ids),
                        )
                    ).rowcount
                connection.execute(
                    object_orphan_observations.delete().where(
                        object_orphan_observations.c.organization_id
                        == actor.organization_id,
                        object_orphan_observations.c.project_id
                        == normalized_project_id,
                        object_orphan_observations.c.object_key == key,
                    )
                )
                keys_to_delete.append(key)
            if keys_to_delete:
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=f"orphan-cleanup-{uuid.uuid4().hex}",
                        actor_user_id=actor.user_id,
                        project_id=normalized_project_id,
                        action="knowledge.objects.orphans.retired",
                        target_type="project",
                        target_id=normalized_project_id,
                        details={
                            "eligible_object_count": len(keys_to_delete),
                            "registered_asset_count": retired_asset_count,
                            "grace_seconds": int(
                                self._grace_period.total_seconds()
                            ),
                        },
                    ),
                )

        deleted_count = 0
        failure_count = 0
        for key in keys_to_delete:
            try:
                self._store.delete(key)
            except ObjectStoreError:
                # The DB reference is already retired. A failed provider delete
                # remains an unregistered object and must age through a new
                # observation window before another explicit cleanup attempt.
                failure_count += 1
            else:
                deleted_count += 1
        return ObjectOrphanCleanupReport(
            organization_id=actor.organization_id,
            project_id=normalized_project_id,
            eligible_count=len(keys_to_delete),
            retired_registered_asset_count=retired_asset_count,
            deleted_object_count=deleted_count,
            object_delete_failure_count=failure_count,
        )

    def _load_reference_state(
        self,
        connection: Connection,
        actor: ActorIdentity,
        project_id: str,
        *,
        prefix: str,
        lock: bool,
    ) -> _ReferenceState:
        asset_statement = sa.select(
            knowledge_assets.c.asset_id,
            knowledge_assets.c.artifact_uri,
        ).where(knowledge_assets.c.project_id == project_id)
        link_statement = sa.select(snapshot_assets.c.asset_id).where(
            snapshot_assets.c.project_id == project_id
        )
        snapshot_statement = sa.select(
            source_snapshots.c.raw_artifact_uri,
            source_snapshots.c.normalized_artifact_uri,
        ).where(source_snapshots.c.project_id == project_id)
        task_statement = sa.select(article_tasks.c.payload).where(
            article_tasks.c.organization_id == actor.organization_id,
            article_tasks.c.project_id == project_id,
        )
        if lock:
            asset_statement = asset_statement.with_for_update()
            link_statement = link_statement.with_for_update()
            snapshot_statement = snapshot_statement.with_for_update()
            task_statement = task_statement.with_for_update()
        asset_rows = connection.execute(asset_statement).mappings().all()
        linked_asset_ids = set(connection.execute(link_statement).scalars())
        task_asset_ids: set[str] = set()
        for payload in connection.execute(task_statement).scalars():
            task_asset_ids.update(_collect_task_asset_ids(payload))
        referenced_asset_ids = linked_asset_ids | task_asset_ids

        live_keys: set[str] = set()
        for row in connection.execute(snapshot_statement).mappings():
            for column in ("raw_artifact_uri", "normalized_artifact_uri"):
                key = _managed_key(
                    row[column],
                    bucket=self._bucket,
                    prefix=prefix,
                )
                if key is not None:
                    live_keys.add(key)

        asset_ids_by_key: dict[str, list[str]] = {}
        for row in asset_rows:
            key = _managed_key(
                row["artifact_uri"],
                bucket=self._bucket,
                prefix=prefix,
            )
            if key is None:
                continue
            asset_id = str(row["asset_id"])
            asset_ids_by_key.setdefault(key, []).append(asset_id)
            if asset_id in referenced_asset_ids:
                live_keys.add(key)
        return _ReferenceState(
            live_keys=frozenset(live_keys),
            asset_ids_by_key={
                key: tuple(sorted(asset_ids))
                for key, asset_ids in asset_ids_by_key.items()
            },
        )

    def _record_observations(
        self,
        connection: Connection,
        actor: ActorIdentity,
        project_id: str,
        candidates: Mapping[str, ObjectMetadata],
        asset_ids_by_key: Mapping[str, tuple[str, ...]],
        now: datetime,
    ) -> None:
        existing = {
            str(row["object_key"]): row
            for row in connection.execute(
                sa.select(object_orphan_observations).where(
                    object_orphan_observations.c.organization_id
                    == actor.organization_id,
                    object_orphan_observations.c.project_id == project_id,
                )
            ).mappings()
        }
        for key, item in candidates.items():
            fingerprint = _fingerprint(item)
            previous = existing.get(key)
            stable = previous is not None and previous["fingerprint"] == fingerprint
            values = {
                "fingerprint": fingerprint,
                "byte_size": item.byte_size,
                "object_last_modified_at": item.last_modified,
                "registered_asset_count": len(asset_ids_by_key.get(key, ())),
                "first_seen_at": (
                    previous["first_seen_at"] if stable else now
                ),
                "last_seen_at": now,
                "sighting_count": (
                    int(previous["sighting_count"]) + 1 if stable else 1
                ),
            }
            if previous is None:
                connection.execute(
                    object_orphan_observations.insert().values(
                        organization_id=actor.organization_id,
                        project_id=project_id,
                        object_key=key,
                        **values,
                    )
                )
            else:
                connection.execute(
                    object_orphan_observations.update()
                    .where(
                        object_orphan_observations.c.organization_id
                        == actor.organization_id,
                        object_orphan_observations.c.project_id == project_id,
                        object_orphan_observations.c.object_key == key,
                    )
                    .values(**values)
                )
        resolved = set(existing) - set(candidates)
        if resolved:
            connection.execute(
                object_orphan_observations.delete().where(
                    object_orphan_observations.c.organization_id
                    == actor.organization_id,
                    object_orphan_observations.c.project_id == project_id,
                    object_orphan_observations.c.object_key.in_(resolved),
                )
            )

    def _candidate_from_row(
        self,
        row: Mapping[str, object],
        *,
        now: datetime,
    ) -> ObjectOrphanCandidate:
        first_seen = _utc(
            row["first_seen_at"],  # type: ignore[arg-type]
            "first_seen_at",
        )
        last_seen = _utc(row["last_seen_at"], "last_seen_at")  # type: ignore[arg-type]
        sightings = int(row["sighting_count"])
        return ObjectOrphanCandidate(
            key=str(row["object_key"]),
            byte_size=int(row["byte_size"]),
            registered_asset_count=int(row["registered_asset_count"]),
            first_seen_at=first_seen,
            last_seen_at=last_seen,
            sighting_count=sightings,
            eligible=(
                sightings >= MINIMUM_ORPHAN_SIGHTINGS
                and first_seen <= now - self._grace_period
            ),
        )


__all__ = [
    "DEFAULT_ORPHAN_GRACE",
    "MINIMUM_ORPHAN_GRACE",
    "MINIMUM_ORPHAN_SIGHTINGS",
    "ObjectOrphanCandidate",
    "ObjectOrphanCleanupReport",
    "ObjectOrphanInventory",
    "ProjectObjectOrphanReconciler",
]
