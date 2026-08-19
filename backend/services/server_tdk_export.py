from __future__ import annotations

import hashlib
from typing import Protocol

from config import AppConfig
from knowledge_agent.assets import KnowledgeAsset
from knowledge_agent.object_storage import (
    ARTICLE_DOCX_CONTENT_TYPE,
    TDK_DOCX_ARTIFACT_KIND,
)
from models import TaskRecord
from services.access_control import ActorIdentity
from services.llm import LLMClient
from services.server_llm_settings import ServerLlmClientFactory
from services.tdk import (
    TdkGenerationError,
    build_tdk_docx_bytes,
    generate_tdk_metadata,
)


MAX_SERVER_TDK_DOCX_BYTES = 2 * 1024 * 1024


class ServerTdkError(ValueError):
    """The current Server Task cannot produce a valid TDK document."""


class ServerTdkUnavailable(RuntimeError):
    """TDK generation is unavailable without exposing provider details."""


class ServerTdkObjectService(Protocol):
    """Private-object operation required by Server TDK export."""

    def upload_tdk_docx(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        asset_id: str,
        data: bytes,
    ) -> KnowledgeAsset: ...


class TdkChatClient(Protocol):
    @property
    def ready(self) -> bool: ...

    def chat(
        self,
        messages: list[dict[str, object]],
        temperature: float = 0.7,
        max_tokens: int = 1800,
    ) -> str: ...


class ServerTdkDocxExport:
    """Generate and store TDK metadata without a server-local Task path."""

    def __init__(
        self,
        *,
        config: AppConfig,
        objects: ServerTdkObjectService,
        llm: TdkChatClient | None = None,
        llm_factory: ServerLlmClientFactory | None = None,
    ) -> None:
        self._config = config
        self._objects = objects
        self._llm = llm
        self._llm_factory = llm_factory

    def generate(
        self,
        *,
        actor: ActorIdentity,
        project_id: str,
        task: TaskRecord,
    ) -> TaskRecord:
        if not task.docx_asset_id.strip():
            raise ServerTdkError(
                "the Server article Word document must be exported first"
            )
        client = self._llm
        if client is None and self._llm_factory is not None:
            client = self._llm_factory.client(actor.organization_id)
        if client is None:
            client = LLMClient(self._config)
        if getattr(client, "ready", True) is False:
            raise ServerTdkUnavailable(
                "TDK generation is temporarily unavailable"
            )
        try:
            metadata = generate_tdk_metadata(
                self._config,
                task,
                llm=client,  # type: ignore[arg-type]
            )
        except TdkGenerationError as exc:
            raise ServerTdkError(str(exc)) from exc
        except RuntimeError as exc:
            raise ServerTdkUnavailable(
                "TDK generation is temporarily unavailable"
            ) from exc

        data = build_tdk_docx_bytes(metadata)
        if not data or len(data) > MAX_SERVER_TDK_DOCX_BYTES:
            raise ServerTdkError(
                "generated TDK Word document exceeds the delivery limit"
            )
        digest = hashlib.sha256(data).hexdigest()
        asset = self._objects.upload_tdk_docx(
            actor=actor,
            project_id=project_id,
            asset_id=f"asset_{digest}",
            data=data,
        )
        if (
            asset.content_hash != digest
            or asset.content_type != ARTICLE_DOCX_CONTENT_TYPE
            or str(asset.metadata.get("artifact_kind") or "")
            != TDK_DOCX_ARTIFACT_KIND
        ):
            raise ServerTdkError(
                "stored TDK Word document identity is inconsistent"
            )

        task.tdk = metadata
        task.tdk_path = ""
        task.tdk_asset_id = asset.asset_id
        task.tdk_content_hash = asset.content_hash
        task.tdk_filename = "D.docx"
        task.delivery_package_path = ""
        task.workflow_error = None
        return task


__all__ = [
    "MAX_SERVER_TDK_DOCX_BYTES",
    "ServerTdkDocxExport",
    "ServerTdkError",
    "ServerTdkObjectService",
    "ServerTdkUnavailable",
    "TdkChatClient",
]
