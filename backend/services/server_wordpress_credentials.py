"""Project-scoped WordPress credentials with server-side encryption."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import os
import uuid

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from knowledge_agent.schema import wordpress_project_credentials
from services.access_control import (
    ActorIdentity,
    PostgresProjectAccessRepository,
    ProjectAccessDenied,
    ProjectAccessService,
)
from services.audit_log import AuditEvent, AuditEventWriter, PostgresAuditEventWriter


WORDPRESS_CREDENTIALS_KEY_ENV = "ARTICLE_AGENT_WORDPRESS_CREDENTIALS_KEY"
WORDPRESS_CREDENTIALS_KEY_VERSION = "v1"


class WordPressCredentialsConflict(RuntimeError):
    """A credential update used a stale credential revision."""


class WordPressCredentialsUnavailable(RuntimeError):
    """Credentials could not be read or persisted safely."""


@dataclass(frozen=True, slots=True)
class ServerWordPressCredentials:
    project_id: str
    username: str
    app_password: str = field(repr=False)
    revision: int = 0
    updated_at: str = ""


def _cipher() -> Fernet:
    secret = os.getenv(WORDPRESS_CREDENTIALS_KEY_ENV, "").strip()
    if len(secret) < 32:
        raise WordPressCredentialsUnavailable(
            "WordPress 凭据加密密钥未配置或长度不足。"
        )
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("article-agent/wordpress/" + secret).encode()).digest()
    )
    return Fernet(key)


def encrypt_wordpress_app_password(value: str) -> str:
    password = str(value or "").strip()
    if not password:
        raise ValueError("WordPress Application Password 不能为空。")
    if len(password) > 1024:
        raise ValueError("WordPress Application Password 不能超过 1024 个字符。")
    return _cipher().encrypt(password.encode("utf-8")).decode("ascii")


def decrypt_wordpress_app_password(value: str) -> str:
    try:
        return _cipher().decrypt(str(value or "").encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise WordPressCredentialsUnavailable(
            "WordPress 凭据无法解密，请重新保存该项目的连接。"
        ) from exc


def _normalize_username(value: str) -> str:
    username = " ".join(str(value or "").split())
    if not username:
        raise ValueError("WordPress 用户名不能为空。")
    if len(username) > 200:
        raise ValueError("WordPress 用户名不能超过 200 个字符。")
    return username


class PostgresServerWordPressCredentials:
    def __init__(
        self,
        engine: Engine,
        *,
        access: ProjectAccessService | None = None,
        audit: AuditEventWriter | None = None,
    ) -> None:
        self._engine = engine
        self._access = access or ProjectAccessService(
            PostgresProjectAccessRepository(engine)
        )
        self._audit = audit or PostgresAuditEventWriter()

    @staticmethod
    def _from_row(row: sa.RowMapping) -> ServerWordPressCredentials:
        return ServerWordPressCredentials(
            project_id=str(row["project_id"]),
            username=str(row["username"]),
            app_password=decrypt_wordpress_app_password(
                str(row["app_password_ciphertext"])
            ),
            revision=int(row["revision"]),
            updated_at=str(row["updated_at"] or ""),
        )

    @staticmethod
    def _select():
        return sa.select(
            wordpress_project_credentials.c.project_id,
            wordpress_project_credentials.c.username,
            wordpress_project_credentials.c.app_password_ciphertext,
            wordpress_project_credentials.c.revision,
            wordpress_project_credentials.c.updated_at,
        )

    def get(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
    ) -> ServerWordPressCredentials | None:
        try:
            self._access.require(actor, project_id, "project.view")
            with self._engine.connect() as connection:
                row = connection.execute(
                    self._select().where(
                        wordpress_project_credentials.c.project_id == project_id,
                        wordpress_project_credentials.c.organization_id
                        == actor.organization_id,
                    )
                ).mappings().one_or_none()
        except ProjectAccessDenied:
            raise
        except (WordPressCredentialsUnavailable, SQLAlchemyError) as exc:
            if isinstance(exc, WordPressCredentialsUnavailable):
                raise
            raise WordPressCredentialsUnavailable(
                "WordPress 项目凭据暂时不可用。"
            ) from exc
        return None if row is None else self._from_row(row)

    def save(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        username: str,
        app_password: str | None,
        expected_revision: int,
    ) -> ServerWordPressCredentials:
        self._access.require(actor, project_id, "article.edit")
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("WordPress 凭据 Revision 无效。")
        normalized_username = _normalize_username(username)
        try:
            with self._engine.begin() as connection:
                row = connection.execute(
                    self._select()
                    .where(
                        wordpress_project_credentials.c.project_id == project_id,
                        wordpress_project_credentials.c.organization_id
                        == actor.organization_id,
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if row is None:
                    if expected_revision != 0:
                        raise WordPressCredentialsConflict(
                            "WordPress 凭据已被其他成员创建，请重新载入。"
                        )
                    if not str(app_password or "").strip():
                        raise ValueError(
                            "首次配置 WordPress 连接时必须填写 Application Password。"
                        )
                    encrypted = encrypt_wordpress_app_password(app_password)
                    result = connection.execute(
                        wordpress_project_credentials.insert()
                        .values(
                            project_id=project_id,
                            organization_id=actor.organization_id,
                            username=normalized_username,
                            app_password_ciphertext=encrypted,
                            key_version=WORDPRESS_CREDENTIALS_KEY_VERSION,
                            revision=1,
                        )
                        .returning(*self._select().selected_columns)
                    )
                    saved = result.mappings().one()
                    action = "project.wordpress_credentials.created"
                else:
                    current = self._from_row(row)
                    if current.revision != expected_revision:
                        raise WordPressCredentialsConflict(
                            "WordPress 凭据已被其他成员更新，请重新载入。"
                        )
                    password_supplied = bool(str(app_password or "").strip())
                    if normalized_username != current.username and not password_supplied:
                        raise ValueError(
                            "修改 WordPress 用户名时必须同时填写新的 Application Password。"
                        )
                    encrypted = (
                        encrypt_wordpress_app_password(app_password)
                        if password_supplied
                        else row["app_password_ciphertext"]
                    )
                    result = connection.execute(
                        wordpress_project_credentials.update()
                        .where(
                            wordpress_project_credentials.c.project_id == project_id,
                            wordpress_project_credentials.c.organization_id
                            == actor.organization_id,
                            wordpress_project_credentials.c.revision
                            == expected_revision,
                        )
                        .values(
                            username=normalized_username,
                            app_password_ciphertext=encrypted,
                            key_version=WORDPRESS_CREDENTIALS_KEY_VERSION,
                            revision=expected_revision + 1,
                            updated_at=sa.func.now(),
                        )
                        .returning(*self._select().selected_columns)
                    )
                    saved = result.mappings().one_or_none()
                    if saved is None:
                        raise WordPressCredentialsConflict(
                            "WordPress 凭据已被其他成员更新，请重新载入。"
                        )
                    action = "project.wordpress_credentials.updated"
                self._audit.append(
                    connection,
                    AuditEvent(
                        organization_id=actor.organization_id,
                        event_id=str(uuid.uuid4()),
                        actor_user_id=actor.user_id,
                        project_id=project_id,
                        action=action,
                        target_type="wordpress_credentials",
                        target_id=project_id,
                        details={"username_changed": True},
                    ),
                )
        except (WordPressCredentialsConflict, ProjectAccessDenied, ValueError):
            raise
        except (WordPressCredentialsUnavailable, SQLAlchemyError) as exc:
            if isinstance(exc, WordPressCredentialsUnavailable):
                raise
            raise WordPressCredentialsUnavailable(
                "WordPress 项目凭据暂时不可用。"
            ) from exc
        return self._from_row(saved)


__all__ = [
    "PostgresServerWordPressCredentials",
    "ServerWordPressCredentials",
    "WORDPRESS_CREDENTIALS_KEY_ENV",
    "WordPressCredentialsConflict",
    "WordPressCredentialsUnavailable",
    "decrypt_wordpress_app_password",
    "encrypt_wordpress_app_password",
]
