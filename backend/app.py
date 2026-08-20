from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
)
from dotenv import load_dotenv
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import (
    ROOT_DIR,
    load_runtime_config,
)
from models import (
    ApiMessage,
    AuthLoginRequest,
)
from services.llm import LLMClient
from services.object_store import S3ObjectStore, S3ObjectStoreSettings
from services.access_control import (
    PostgresProjectAccessRepository,
    ProjectAccessService,
)
from services.actor_sessions import (
    PostgresActorSessionRepository,
    PostgresActorSessionRevocationService,
)
from services.external_identity import (
    ExternalIdentityNotAuthorized,
    PostgresExternalIdentityRepository,
)
from services.external_identity_provisioning import (
    PostgresExternalIdentityProvisioningService,
)
from services.workspace_invitations import (
    PostgresWorkspaceInvitationService,
)
from services.oidc_identity import (
    OidcProviderSettings,
    OidcProviderUnavailable,
    OidcVerificationError,
)
from services.oidc_login import (
    OIDC_STATE_COOKIE_NAME,
    WORKSPACE_INVITATION_COOKIE_NAME,
    OidcLoginService,
    OidcLoginStateError,
)
from services.project_directory import PostgresProjectDirectory
from services.project_memberships import PostgresProjectMembershipService
from services.project_deletion import PostgresProjectDeletionService
from services.server_project_metadata import PostgresServerProjectMetadata
from services.server_private_document_ingestion import (
    PostgresServerPrivateDocumentIngestion,
)
from services.server_snapshot_evidence import (
    PostgresServerSnapshotEvidenceService,
)
from services.server_knowledge_commands import (
    PostgresServerKnowledgeCommands,
)
from services.team_administration import PostgresTeamAdministrationService
from services.workspace_users import PostgresWorkspaceUserService
from services.server_auth import (
    SERVER_AUTH_COOKIE_NAME,
    load_server_actor_session_codec,
    server_mode_enabled,
)
from services.server_request_security import (
    ServerRequestSecurity,
    ServerRequestUnauthenticated,
    server_http_route_available,
)
from services.server_project_tasks import ServerProjectTaskStoreFactory
from services.server_project_prompts import (
    ServerProjectPromptServiceFactory,
)
from services.server_task_writing_settings import (
    ServerTaskWritingSettingsServiceFactory,
)
from services.server_project_catalog import PostgresServerProjectCatalog
from services.server_product_selection import (
    PostgresConfirmedProductSelection,
)
from services.server_product_rediscovery import (
    ServerProductRediscoveryHandler,
    ServerProductRediscoveryRegistry,
    create_product_sync_factory,
)
from services.server_product_generation import (
    LlmServerProductProvider,
    ServerProductGenerationHandler,
    ServerProductGenerationRegistry,
)
from services.server_knowledge_research import (
    ServerKnowledgeResearchRegistry,
    create_server_research_execution,
)
from services.server_outline_generation import (
    LlmServerOutlineProvider,
    ServerOutlineGenerationHandler,
    ServerOutlineGenerationRegistry,
)
from services.server_title_generation import (
    LlmServerTitleProvider,
    ServerTitleGenerationHandler,
    ServerTitleGenerationRegistry,
)
from services.server_article_generation import (
    LlmServerArticleProvider,
    ServerArticleGenerationHandler,
    ServerArticleGenerationRegistry,
)
from services.server_link_restoration import (
    LlmServerLinkRestorationProvider,
    ServerLinkRestorationHandler,
    ServerLinkRestorationRegistry,
)
from services.server_seo_review_generation import (
    LlmServerSeoReviewProvider,
    ServerSeoReviewGenerationHandler,
    ServerSeoReviewGenerationRegistry,
)
from services.server_humanize_generation import (
    LlmServerHumanizeProvider,
    ServerHumanizeGenerationHandler,
    ServerHumanizeGenerationRegistry,
)
from services.server_llm_settings import (
    PostgresServerLlmSettings,
    ServerLlmClientFactory,
)
from services.zerogpt import ZeroGPTClient
from services.server_job_control import PostgresServerJobControlService
from knowledge_agent.assets import PostgresKnowledgeAssetRepository
from knowledge_agent.catalog import PostgresProductCatalogRepository
from knowledge_agent.http import router as knowledge_agent_router
from knowledge_agent.embedding import OpenAICompatibleEmbeddingProvider
from knowledge_agent.object_storage import ProjectKnowledgeObjectService
from knowledge_agent.research_chat import LlmResearchAnswerProvider
from knowledge_agent.retention import prune_expired_research_details
from knowledge_agent.runtime import create_knowledge_runtime
from knowledge_agent.repository import PostgresKnowledgeRepository
from knowledge_agent.settings import load_knowledge_agent_settings
from server_project_http import router as server_project_router
from server_admin_http import router as server_admin_router
from server_team_http import router as server_team_router
from server_identity_http import router as server_identity_router
from server_invitation_http import router as server_invitation_router
from server_job_http import router as server_job_router
from server_prompt_http import router as server_prompt_router
from server_llm_settings_http import router as server_llm_settings_router
from workflow_assistant.context import WorkflowAssistantContextResolver
from workflow_assistant.adapters import WorkflowAssistantServiceAdapters
from workflow_assistant.attachment_http import (
    router as workflow_assistant_attachment_router,
)
from workflow_assistant.attachment_review import AttachmentReviewWorkflowService
from workflow_assistant.import_adapters import build_default_import_executor
from workflow_assistant.attachment_review_http import (
    router as workflow_assistant_attachment_review_router,
)
from workflow_assistant.attachment_repository import PostgresAttachmentRepository
from workflow_assistant.attachment_retention import AttachmentRetentionRunner
from workflow_assistant.attachments import AttachmentService
from workflow_assistant.execution import WorkflowExecutionCoordinator
from workflow_assistant.http import router as workflow_assistant_router
from workflow_assistant.planner import StructuredWorkflowPlanner
from workflow_assistant.repository import PostgresWorkflowAssistantRepository
from workflow_assistant.retention import prune_expired_assistant_conversations
from workflow_assistant.runner import WorkflowAssistantRunner
from workflow_assistant.tools import WorkflowToolRegistry


load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / "backend" / ".env")


@asynccontextmanager
async def app_lifespan(application: FastAPI):
    cfg = config()
    server_engine = None
    previous_article_agent_config = getattr(
        application.state,
        "article_agent_config",
        None,
    )
    previous_workflow_assistant_repository = getattr(
        application.state,
        "workflow_assistant_repository",
        None,
    )
    previous_workflow_assistant_context = getattr(
        application.state,
        "workflow_assistant_context",
        None,
    )
    previous_workflow_assistant_planner = getattr(
        application.state,
        "workflow_assistant_planner",
        None,
    )
    previous_workflow_assistant_adapters = getattr(
        application.state,
        "workflow_assistant_adapters",
        None,
    )
    previous_workflow_assistant_tools = getattr(
        application.state,
        "workflow_assistant_tools",
        None,
    )
    previous_workflow_assistant_coordinator = getattr(
        application.state,
        "workflow_assistant_coordinator",
        None,
    )
    previous_workflow_assistant_runner = getattr(
        application.state,
        "workflow_assistant_runner",
        None,
    )
    previous_workflow_assistant_attachment_service = getattr(
        application.state,
        "workflow_assistant_attachment_service",
        None,
    )
    previous_workflow_assistant_attachment_retention = getattr(
        application.state,
        "workflow_assistant_attachment_retention",
        None,
    )
    previous_workflow_assistant_attachment_review = getattr(
        application.state,
        "workflow_assistant_attachment_review_workflow",
        None,
    )
    previous_server_mode = getattr(
        application.state,
        "server_mode_enabled",
        None,
    )
    previous_server_security = getattr(
        application.state,
        "server_request_security",
        None,
    )
    previous_server_project_task_store_factory = getattr(
        application.state,
        "server_project_task_store_factory",
        None,
    )
    previous_server_project_prompt_service_factory = getattr(
        application.state,
        "server_project_prompt_service_factory",
        None,
    )
    previous_server_task_writing_settings_service_factory = getattr(
        application.state,
        "server_task_writing_settings_service_factory",
        None,
    )
    previous_server_project_directory = getattr(
        application.state,
        "server_project_directory",
        None,
    )
    previous_server_project_metadata = getattr(
        application.state,
        "server_project_metadata",
        None,
    )
    previous_server_project_memberships = getattr(
        application.state,
        "server_project_memberships",
        None,
    )
    previous_server_project_deletion = getattr(
        application.state,
        "server_project_deletion",
        None,
    )
    previous_server_project_object_service = getattr(
        application.state,
        "server_project_object_service",
        None,
    )
    previous_server_private_document_ingestion = getattr(
        application.state,
        "server_private_document_ingestion",
        None,
    )
    previous_server_snapshot_evidence = getattr(
        application.state,
        "server_snapshot_evidence",
        None,
    )
    previous_server_project_catalog = getattr(
        application.state,
        "server_project_catalog",
        None,
    )
    previous_server_confirmed_product_selection = getattr(
        application.state,
        "server_confirmed_product_selection",
        None,
    )
    previous_server_product_rediscovery = getattr(
        application.state,
        "server_product_rediscovery",
        None,
    )
    previous_server_product_generation = getattr(
        application.state,
        "server_product_generation",
        None,
    )
    previous_server_knowledge_research = getattr(
        application.state,
        "server_knowledge_research",
        None,
    )
    previous_server_outline_generation = getattr(
        application.state,
        "server_outline_generation",
        None,
    )
    previous_server_title_generation = getattr(
        application.state,
        "server_title_generation",
        None,
    )
    previous_server_article_generation = getattr(
        application.state,
        "server_article_generation",
        None,
    )
    previous_server_link_restoration = getattr(
        application.state,
        "server_link_restoration",
        None,
    )
    previous_server_seo_review_generation = getattr(
        application.state,
        "server_seo_review_generation",
        None,
    )
    previous_server_humanize_generation = getattr(
        application.state,
        "server_humanize_generation",
        None,
    )
    previous_server_job_control = getattr(
        application.state,
        "server_job_control",
        None,
    )
    previous_server_oidc_login = getattr(
        application.state,
        "server_oidc_login",
        None,
    )
    previous_server_actor_session_revocation = getattr(
        application.state,
        "server_actor_session_revocation",
        None,
    )
    previous_server_workspace_users = getattr(
        application.state,
        "server_workspace_users",
        None,
    )
    previous_server_team_administration = getattr(
        application.state,
        "server_team_administration",
        None,
    )
    previous_server_external_identity_provisioning = getattr(
        application.state,
        "server_external_identity_provisioning",
        None,
    )
    previous_server_workspace_invitations = getattr(
        application.state,
        "server_workspace_invitations",
        None,
    )
    previous_server_llm_settings = getattr(
        application.state,
        "server_llm_settings",
        None,
    )
    previous_server_llm_client_factory = getattr(
        application.state,
        "server_llm_client_factory",
        None,
    )
    server_product_rediscovery = None
    server_product_generation = None
    server_knowledge_research = None
    server_outline_generation = None
    server_title_generation = None
    server_article_generation = None
    server_link_restoration = None
    server_seo_review_generation = None
    server_humanize_generation = None
    workflow_assistant_repository = None
    workflow_assistant_adapters = None
    workflow_assistant_tools = None
    workflow_assistant_coordinator = None
    workflow_assistant_runner = None
    workflow_assistant_attachment_retention = None
    workflow_assistant_attachment_review = None
    server_oidc_login = None
    server_mode = server_mode_enabled()
    application.state.server_mode_enabled = server_mode
    application.state.article_agent_config = cfg
    application.state.workflow_assistant_repository = None
    application.state.workflow_assistant_context = None
    application.state.workflow_assistant_planner = None
    application.state.workflow_assistant_adapters = None
    application.state.workflow_assistant_tools = None
    application.state.workflow_assistant_coordinator = None
    application.state.workflow_assistant_runner = None
    application.state.workflow_assistant_attachment_service = None
    application.state.workflow_assistant_attachment_retention = None
    application.state.workflow_assistant_attachment_review_workflow = None
    application.state.server_request_security = None
    application.state.server_project_task_store_factory = None
    application.state.server_project_prompt_service_factory = None
    application.state.server_task_writing_settings_service_factory = None
    application.state.server_project_directory = None
    application.state.server_project_metadata = None
    application.state.server_project_memberships = None
    application.state.server_project_deletion = None
    application.state.server_project_object_service = None
    application.state.server_private_document_ingestion = None
    application.state.server_snapshot_evidence = None
    application.state.server_project_catalog = None
    application.state.server_confirmed_product_selection = None
    application.state.server_product_rediscovery = None
    application.state.server_product_generation = None
    application.state.server_knowledge_research = None
    application.state.server_outline_generation = None
    application.state.server_title_generation = None
    application.state.server_article_generation = None
    application.state.server_link_restoration = None
    application.state.server_seo_review_generation = None
    application.state.server_humanize_generation = None
    application.state.server_job_control = None
    application.state.server_oidc_login = None
    application.state.server_actor_session_revocation = None
    application.state.server_workspace_users = None
    application.state.server_team_administration = None
    application.state.server_external_identity_provisioning = None
    application.state.server_workspace_invitations = None
    application.state.server_llm_settings = None
    application.state.server_llm_client_factory = None
    if server_mode:
        codec = load_server_actor_session_codec()
        server_settings = load_knowledge_agent_settings(
            enabled=False,
            require_ready=False,
        )
        database_url = server_settings.database_url or ""
        if not database_url:
            raise RuntimeError(
                "ARTICLE_AGENT_DATABASE_URL is required in server mode"
            )
        server_engine = sa.create_engine(
            database_url,
            pool_pre_ping=True,
        )
        server_access_repository = PostgresProjectAccessRepository(
            server_engine
        )
        server_access = ProjectAccessService(server_access_repository)
        server_actor_sessions = PostgresActorSessionRepository(server_engine)
        application.state.server_actor_session_revocation = (
            PostgresActorSessionRevocationService(server_engine)
        )
        application.state.server_workspace_users = (
            PostgresWorkspaceUserService(server_engine)
        )
        application.state.server_team_administration = (
            PostgresTeamAdministrationService(server_engine)
        )
        application.state.server_external_identity_provisioning = (
            PostgresExternalIdentityProvisioningService(server_engine)
        )
        application.state.server_workspace_invitations = (
            PostgresWorkspaceInvitationService(server_engine)
        )
        server_llm_settings = PostgresServerLlmSettings(server_engine)
        server_llm_client_factory = ServerLlmClientFactory(
            cfg,
            server_llm_settings,
        )
        application.state.server_llm_settings = server_llm_settings
        application.state.server_llm_client_factory = server_llm_client_factory
        application.state.server_request_security = ServerRequestSecurity(
            codec=codec,
            access=server_access,
            sessions=server_actor_sessions,
        )
        if cfg.workflow_assistant_enabled:
            workflow_assistant_repository = PostgresWorkflowAssistantRepository(
                server_engine
            )
            prune_expired_assistant_conversations(workflow_assistant_repository)
            application.state.workflow_assistant_repository = workflow_assistant_repository
            application.state.workflow_assistant_context = (
                WorkflowAssistantContextResolver(server_engine)
            )
            application.state.workflow_assistant_planner = (
                StructuredWorkflowPlanner(
                    cfg,
                    access=server_access,
                    llm_factory=server_llm_client_factory,
                )
            )
        oidc_settings = OidcProviderSettings.from_environment()
        if oidc_settings is not None:
            server_oidc_login = OidcLoginService.create(
                settings=oidc_settings,
                identities=PostgresExternalIdentityRepository(
                    server_engine
                ),
                codec=codec,
                invitations=application.state.server_workspace_invitations,
            )
            application.state.server_oidc_login = server_oidc_login
        application.state.server_project_task_store_factory = (
            ServerProjectTaskStoreFactory(server_engine, cfg)
        )
        application.state.server_project_prompt_service_factory = (
            ServerProjectPromptServiceFactory(server_engine)
        )
        application.state.server_task_writing_settings_service_factory = (
            ServerTaskWritingSettingsServiceFactory(server_engine, cfg)
        )
        application.state.server_project_directory = (
            PostgresProjectDirectory(server_engine)
        )
        application.state.server_project_metadata = (
            PostgresServerProjectMetadata(server_engine)
        )
        application.state.server_project_memberships = (
            PostgresProjectMembershipService(server_engine)
        )
        application.state.server_project_deletion = (
            PostgresProjectDeletionService(server_engine)
        )
        application.state.server_confirmed_product_selection = (
            PostgresConfirmedProductSelection(server_engine)
        )
        application.state.server_project_catalog = (
            PostgresServerProjectCatalog(server_engine)
        )
        application.state.server_job_control = (
            PostgresServerJobControlService(
                server_engine,
                access_repository=server_access_repository,
            )
        )
        rediscovery_sync_factory = None
        server_object_store = None
        server_object_bucket = ""
        if os.environ.get(
            "ARTICLE_AGENT_OBJECT_STORE_BUCKET",
            "",
        ).strip():
            object_settings = S3ObjectStoreSettings.from_environment()
            server_object_store = S3ObjectStore(object_settings)
            server_object_bucket = object_settings.bucket
            application.state.server_project_object_service = (
                ProjectKnowledgeObjectService(
                    store=server_object_store,
                    bucket=object_settings.bucket,
                    repository=PostgresKnowledgeAssetRepository(
                        server_engine
                    ),
                    access=server_access,
                )
            )
            application.state.server_private_document_ingestion = (
                PostgresServerPrivateDocumentIngestion(
                    server_engine,
                    store=server_object_store,
                    bucket=object_settings.bucket,
                )
            )
            application.state.server_snapshot_evidence = (
                PostgresServerSnapshotEvidenceService(
                    engine=server_engine,
                    store=server_object_store,
                    bucket=object_settings.bucket,
                    access=server_access,
                )
            )
            rediscovery_sync_factory = create_product_sync_factory(
                server_engine,
                store=server_object_store,
                bucket=object_settings.bucket,
            )
            if server_engine is not None:
                attachment_service = AttachmentService(
                    repository=PostgresAttachmentRepository(server_engine),
                    store=server_object_store,
                )
                workflow_assistant_attachment_retention = (
                    AttachmentRetentionRunner(attachment_service)
                )
                workflow_assistant_attachment_retention.start()
                application.state.workflow_assistant_attachment_retention = (
                    workflow_assistant_attachment_retention
                )
                if (
                    cfg.workflow_assistant_enabled
                    and cfg.workflow_assistant_attachments_enabled
                ):
                    application.state.workflow_assistant_attachment_service = (
                        attachment_service
                    )
                    workflow_assistant_attachment_review = (
                        AttachmentReviewWorkflowService(
                            server_engine,
                            object_store=server_object_store,
                            llm_factory=server_llm_client_factory,
                            access=server_access,
                            import_executor=build_default_import_executor(
                                engine=server_engine,
                                access=server_access,
                                object_store=server_object_store,
                                ingestion=application.state.server_private_document_ingestion,
                            ),
                        )
                    )
                    workflow_assistant_attachment_review.start()
                    application.state.workflow_assistant_attachment_review_workflow = (
                        workflow_assistant_attachment_review
                    )
        product_provider = LlmServerProductProvider(
            cfg,
            llm_factory=server_llm_client_factory,
        )
        product_handler = (
            ServerProductGenerationHandler(
                server_engine,
                provider=product_provider,
            )
            if product_provider.ready
            else None
        )
        server_product_generation = ServerProductGenerationRegistry(
            server_engine,
            access=server_access,
            provider=product_provider,
            handler=product_handler,
        )
        server_product_generation.start_existing()
        application.state.server_product_generation = (
            server_product_generation
        )
        outline_provider = LlmServerOutlineProvider(
            cfg,
            llm_factory=server_llm_client_factory,
        )
        outline_handler = (
            ServerOutlineGenerationHandler(
                server_engine,
                provider=outline_provider,
            )
            if outline_provider.ready
            else None
        )
        server_outline_generation = ServerOutlineGenerationRegistry(
            server_engine,
            access=server_access,
            handler=outline_handler,
        )
        server_outline_generation.start_existing()
        application.state.server_outline_generation = (
            server_outline_generation
        )
        title_provider = LlmServerTitleProvider(
            cfg,
            llm_factory=server_llm_client_factory,
        )
        title_handler = (
            ServerTitleGenerationHandler(
                server_engine,
                provider=title_provider,
            )
            if title_provider.ready
            else None
        )
        server_title_generation = ServerTitleGenerationRegistry(
            server_engine,
            config=cfg,
            access=server_access,
            handler=title_handler,
        )
        server_title_generation.start_existing()
        application.state.server_title_generation = (
            server_title_generation
        )
        article_provider = LlmServerArticleProvider(
            cfg,
            llm_factory=server_llm_client_factory,
        )
        zerogpt_client = ZeroGPTClient()
        article_handler = (
            ServerArticleGenerationHandler(
                server_engine,
                provider=article_provider,
                ai_rate=zerogpt_client,
            )
            if article_provider.ready
            else None
        )
        server_article_generation = ServerArticleGenerationRegistry(
            server_engine,
            config=cfg,
            access=server_access,
            handler=article_handler,
        )
        server_article_generation.start_existing()
        application.state.server_article_generation = (
            server_article_generation
        )
        link_provider = LlmServerLinkRestorationProvider(
            cfg,
            llm_factory=server_llm_client_factory,
        )
        link_handler = (
            ServerLinkRestorationHandler(
                server_engine,
                provider=link_provider,
            )
            if link_provider.ready
            else None
        )
        server_link_restoration = ServerLinkRestorationRegistry(
            server_engine,
            access=server_access,
            handler=link_handler,
        )
        server_link_restoration.start_existing()
        application.state.server_link_restoration = (
            server_link_restoration
        )
        seo_review_provider = LlmServerSeoReviewProvider(
            cfg,
            llm_factory=server_llm_client_factory,
        )
        seo_review_handler = (
            ServerSeoReviewGenerationHandler(
                server_engine,
                provider=seo_review_provider,
            )
            if seo_review_provider.ready
            else None
        )
        server_seo_review_generation = (
            ServerSeoReviewGenerationRegistry(
                server_engine,
                access=server_access,
                handler=seo_review_handler,
            )
        )
        server_seo_review_generation.start_existing()
        application.state.server_seo_review_generation = (
            server_seo_review_generation
        )
        humanize_provider = LlmServerHumanizeProvider(
            cfg,
            llm_factory=server_llm_client_factory,
        )
        humanize_handler = (
            ServerHumanizeGenerationHandler(
                server_engine,
                provider=humanize_provider,
                ai_rate=zerogpt_client,
            )
            if humanize_provider.ready
            else None
        )
        server_humanize_generation = ServerHumanizeGenerationRegistry(
            server_engine,
            access=server_access,
            handler=humanize_handler,
        )
        server_humanize_generation.start_existing()
        application.state.server_humanize_generation = (
            server_humanize_generation
        )
    knowledge_runtime = None
    application.state.knowledge_agent_runtime = None
    application.state.knowledge_research_enqueue = None
    if cfg.knowledge_agent_enabled:
        knowledge_settings = load_knowledge_agent_settings(enabled=True)
        research_answer_provider = LlmResearchAnswerProvider(LLMClient(cfg))
        knowledge_runtime = create_knowledge_runtime(
            database_url=knowledge_settings.database_url or "",
            artifact_root=Path(
                os.environ.get(
                    "ARTICLE_AGENT_KNOWLEDGE_ROOT",
                    str(ROOT_DIR / "workspace" / "knowledge-agent"),
                )
            ),
            embedding_provider=OpenAICompatibleEmbeddingProvider.from_settings(
                knowledge_settings
            ),
            answer_provider=(
                research_answer_provider
                if research_answer_provider.ready
                else None
            ),
        )
        prune_expired_research_details(knowledge_runtime)
        application.state.knowledge_agent_runtime = knowledge_runtime
    if server_mode:
        rediscovery_handler = None
        if (
            knowledge_runtime is not None
            and knowledge_runtime.publication is not None
            and rediscovery_sync_factory is not None
        ):
            rediscovery_handler = ServerProductRediscoveryHandler(
                server_engine,
                sync_factory=rediscovery_sync_factory,
                commands=PostgresServerKnowledgeCommands(
                    server_engine,
                    repository=PostgresKnowledgeRepository(server_engine),
                    catalog=PostgresProductCatalogRepository(server_engine),
                    publication=knowledge_runtime.publication,
                ),
            )
        server_product_rediscovery = ServerProductRediscoveryRegistry(
            server_engine,
            access=server_access,
            handler=rediscovery_handler,
        )
        server_product_rediscovery.start_existing()
        application.state.server_product_rediscovery = (
            server_product_rediscovery
        )
        research_execution = None
        if (
            knowledge_runtime is not None
            and server_object_store is not None
        ):
            research_execution = create_server_research_execution(
                engine=server_engine,
                database_url=database_url,
                embedding_provider=knowledge_runtime.embedding_provider,
                store=server_object_store,
                bucket=server_object_bucket,
                access=server_access,
            )
        server_knowledge_research = ServerKnowledgeResearchRegistry(
            server_engine,
            access=server_access,
            execution=research_execution,
            access_repository=server_access_repository,
        )
        server_knowledge_research.start_existing()
        application.state.server_knowledge_research = (
            server_knowledge_research
        )
        if (
            cfg.workflow_assistant_enabled
            and workflow_assistant_repository is not None
            and server_engine is not None
        ):
            workflow_assistant_adapters = WorkflowAssistantServiceAdapters(
                engine=server_engine,
                config=cfg,
                task_factory=application.state.server_project_task_store_factory,
                context=application.state.workflow_assistant_context,
                plan_status=lambda plan_id, actor: {
                    "plan_id": plan_id,
                    "status": workflow_assistant_repository.get_plan(
                        actor=actor,
                        plan_id=plan_id,
                    ).status,
                },
                evidence_chat=(
                    None
                    if knowledge_runtime is None
                    else knowledge_runtime.research_chat
                ),
                product_selection=(
                    application.state.server_confirmed_product_selection
                ),
                project_catalog=application.state.server_project_catalog,
                title_generation=server_title_generation,
                product_generation=server_product_generation,
                outline_generation=server_outline_generation,
                article_generation=server_article_generation,
                humanize_generation=server_humanize_generation,
                link_restoration=server_link_restoration,
                seo_review_generation=server_seo_review_generation,
                knowledge_research=server_knowledge_research,
                object_service=application.state.server_project_object_service,
            )
            workflow_assistant_tools = WorkflowToolRegistry(
                access=application.state.workflow_assistant_context.access,
                handlers=workflow_assistant_adapters.handlers(),
            )
            workflow_assistant_coordinator = WorkflowExecutionCoordinator(
                repository=workflow_assistant_repository,
                access=application.state.workflow_assistant_context.access,
                tools=workflow_assistant_tools,
                max_concurrency=int(
                    getattr(cfg, "workflow_assistant_max_concurrency", 3)
                ),
                job_status_resolver=workflow_assistant_adapters.job_status,
            )
            workflow_assistant_runner = WorkflowAssistantRunner(
                repository=workflow_assistant_repository,
                coordinator=workflow_assistant_coordinator,
                database_url=database_url,
            )
            application.state.workflow_assistant_adapters = (
                workflow_assistant_adapters
            )
            application.state.workflow_assistant_tools = (
                workflow_assistant_tools
            )
            application.state.workflow_assistant_coordinator = (
                workflow_assistant_coordinator
            )
            application.state.workflow_assistant_runner = (
                workflow_assistant_runner
            )
            workflow_assistant_runner.start()
        # Project operations use their own PostgreSQL runners.
        application.state.job_queue = None
        application.state.batch_runner = None
        application.state.batch_runners = ()
        try:
            yield
        finally:
            shutdown_error: RuntimeError | None = None
            if workflow_assistant_attachment_review is not None:
                stop_report = workflow_assistant_attachment_review.stop()
                if stop_report.alive:
                    shutdown_error = RuntimeError(
                        "workflow assistant attachment review did not stop"
                    )
            if workflow_assistant_attachment_retention is not None:
                stop_report = workflow_assistant_attachment_retention.stop()
                if stop_report.alive:
                    shutdown_error = RuntimeError(
                        "workflow assistant attachment retention did not stop"
                    )
            if workflow_assistant_runner is not None:
                stop_report = workflow_assistant_runner.stop()
                if stop_report.alive:
                    shutdown_error = RuntimeError(
                        "workflow assistant runner did not stop"
                    )
            if server_product_rediscovery is not None:
                stop_report = server_product_rediscovery.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server product rediscovery did not drain"
                    )
            if server_product_generation is not None:
                stop_report = server_product_generation.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server product generation did not drain"
                    )
            if server_knowledge_research is not None:
                stop_report = server_knowledge_research.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server knowledge research did not drain"
                    )
            if server_outline_generation is not None:
                stop_report = server_outline_generation.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server outline generation did not drain"
                    )
            if server_title_generation is not None:
                stop_report = server_title_generation.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server title generation did not drain"
                    )
            if server_article_generation is not None:
                stop_report = server_article_generation.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server article generation did not drain"
                    )
            if server_link_restoration is not None:
                stop_report = server_link_restoration.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server link restoration did not drain"
                    )
            if server_seo_review_generation is not None:
                stop_report = server_seo_review_generation.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server SEO review generation did not drain"
                    )
            if server_humanize_generation is not None:
                stop_report = server_humanize_generation.stop()
                if not stop_report.drained:
                    shutdown_error = RuntimeError(
                        "server humanize generation did not drain"
                    )
            if server_oidc_login is not None:
                server_oidc_login.close()
            server_private_ingestion = getattr(
                application.state,
                "server_private_document_ingestion",
                None,
            )
            close_private_ingestion = getattr(
                server_private_ingestion,
                "close",
                None,
            )
            if callable(close_private_ingestion):
                close_private_ingestion()
            if knowledge_runtime is not None:
                knowledge_runtime.close()
            if server_engine is not None and shutdown_error is None:
                server_engine.dispose()
            application.state.knowledge_agent_runtime = None
            application.state.knowledge_research_enqueue = None
            application.state.server_mode_enabled = previous_server_mode
            application.state.article_agent_config = previous_article_agent_config
            application.state.workflow_assistant_repository = (
                previous_workflow_assistant_repository
            )
            application.state.workflow_assistant_context = (
                previous_workflow_assistant_context
            )
            application.state.workflow_assistant_planner = (
                previous_workflow_assistant_planner
            )
            application.state.workflow_assistant_adapters = (
                previous_workflow_assistant_adapters
            )
            application.state.workflow_assistant_tools = (
                previous_workflow_assistant_tools
            )
            application.state.workflow_assistant_coordinator = (
                previous_workflow_assistant_coordinator
            )
            application.state.workflow_assistant_runner = (
                previous_workflow_assistant_runner
            )
            application.state.workflow_assistant_attachment_service = (
                previous_workflow_assistant_attachment_service
            )
            application.state.workflow_assistant_attachment_retention = (
                previous_workflow_assistant_attachment_retention
            )
            application.state.workflow_assistant_attachment_review_workflow = (
                previous_workflow_assistant_attachment_review
            )
            application.state.server_request_security = (
                previous_server_security
            )
            application.state.server_project_task_store_factory = (
                previous_server_project_task_store_factory
            )
            application.state.server_project_prompt_service_factory = (
                previous_server_project_prompt_service_factory
            )
            application.state.server_task_writing_settings_service_factory = (
                previous_server_task_writing_settings_service_factory
            )
            application.state.server_project_directory = (
                previous_server_project_directory
            )
            application.state.server_project_metadata = (
                previous_server_project_metadata
            )
            application.state.server_project_memberships = (
                previous_server_project_memberships
            )
            application.state.server_project_deletion = (
                previous_server_project_deletion
            )
            application.state.server_project_object_service = (
                previous_server_project_object_service
            )
            application.state.server_private_document_ingestion = (
                previous_server_private_document_ingestion
            )
            application.state.server_snapshot_evidence = (
                previous_server_snapshot_evidence
            )
            application.state.server_project_catalog = (
                previous_server_project_catalog
            )
            application.state.server_confirmed_product_selection = (
                previous_server_confirmed_product_selection
            )
            application.state.server_product_rediscovery = (
                previous_server_product_rediscovery
            )
            application.state.server_product_generation = (
                previous_server_product_generation
            )
            application.state.server_knowledge_research = (
                previous_server_knowledge_research
            )
            application.state.server_outline_generation = (
                previous_server_outline_generation
            )
            application.state.server_title_generation = (
                previous_server_title_generation
            )
            application.state.server_article_generation = (
                previous_server_article_generation
            )
            application.state.server_link_restoration = (
                previous_server_link_restoration
            )
            application.state.server_seo_review_generation = (
                previous_server_seo_review_generation
            )
            application.state.server_humanize_generation = (
                previous_server_humanize_generation
            )
            application.state.server_job_control = previous_server_job_control
            application.state.server_oidc_login = (
                previous_server_oidc_login
            )
            application.state.server_actor_session_revocation = (
                previous_server_actor_session_revocation
            )
            application.state.server_workspace_users = (
                previous_server_workspace_users
            )
            application.state.server_team_administration = (
                previous_server_team_administration
            )
            application.state.server_external_identity_provisioning = (
                previous_server_external_identity_provisioning
            )
            application.state.server_workspace_invitations = (
                previous_server_workspace_invitations
            )
            application.state.server_llm_settings = previous_server_llm_settings
            application.state.server_llm_client_factory = (
                previous_server_llm_client_factory
            )
            if shutdown_error is not None:
                raise shutdown_error
        return

app = FastAPI(
    title="Article Workflow Agent",
    version="0.3.0",
    lifespan=app_lifespan,
)
app.include_router(knowledge_agent_router)
app.include_router(server_project_router)
app.include_router(server_admin_router)
app.include_router(server_team_router)
app.include_router(server_identity_router)
app.include_router(server_invitation_router)
app.include_router(server_job_router)
app.include_router(server_prompt_router)
app.include_router(server_llm_settings_router)
app.include_router(workflow_assistant_router)
app.include_router(workflow_assistant_attachment_router)
app.include_router(workflow_assistant_attachment_review_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_AUTH_PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/status",
    "/api/auth/oidc/start",
    "/api/auth/oidc/callback",
    "/api/auth/invitations/prepare",
    "/api/health",
}


def application_server_mode(application: FastAPI = app) -> bool:
    configured = getattr(
        application.state,
        "server_mode_enabled",
        None,
    )
    return (
        server_mode_enabled()
        if configured is None
        else bool(configured)
    )


def request_server_mode(request: Request) -> bool:
    return application_server_mode(request.app)


@app.middleware("http")
async def require_application_password(request: Request, call_next):
    if server_http_route_available(request.method, request.url.path):
        return await call_next(request)
    return JSONResponse(
        status_code=404,
        content={"detail": "Route is not available."},
    )


def auth_cookie_secure(request: Request) -> bool:
    configured = os.environ.get("APP_COOKIE_SECURE", "").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded_proto.split(",", 1)[0].strip() == "https"


@app.get("/api/auth/status", response_model=ApiMessage)
def auth_status(request: Request) -> ApiMessage:
    security = getattr(request.app.state, "server_request_security", None)
    oidc_login = getattr(request.app.state, "server_oidc_login", None)
    oidc_enabled = isinstance(oidc_login, OidcLoginService)
    authenticated = False
    actor = None
    if isinstance(security, ServerRequestSecurity):
        try:
            actor = security.authenticate(
                request.cookies.get(SERVER_AUTH_COOKIE_NAME, "")
            )
            authenticated = True
        except ServerRequestUnauthenticated:
            pass
    runtime_config = getattr(
        getattr(request.app, "state", None),
        "article_agent_config",
        None,
    )
    workflow_assistant_enabled = bool(
        runtime_config
        and getattr(runtime_config, "workflow_assistant_enabled", False)
    )
    return ApiMessage(
        message="Server authentication status.",
        data={
            "enabled": True,
            "authenticated": authenticated,
            "mode": "server",
            "login_available": oidc_enabled,
            "issuer": oidc_login.settings.issuer if oidc_enabled else None,
            "organization_id": actor.organization_id if actor is not None else None,
            "user_id": actor.user_id if actor is not None else None,
            "workflow_assistant_enabled": workflow_assistant_enabled,
            "workflow_assistant_attachments_enabled": bool(
                workflow_assistant_enabled
                and getattr(
                    runtime_config,
                    "workflow_assistant_attachments_enabled",
                    False,
                )
            ),
        },
    )

@app.post("/api/auth/login", response_model=ApiMessage)
def auth_login(request: Request, payload: AuthLoginRequest):
    del request, payload
    raise HTTPException(
        status_code=503,
        detail="Password login is unavailable; use the configured identity provider.",
    )

def _server_oidc_login(request: Request) -> OidcLoginService:
    if not request_server_mode(request):
        raise HTTPException(
            status_code=404,
            detail="Server OIDC login is not available.",
        )
    service = getattr(request.app.state, "server_oidc_login", None)
    if not isinstance(service, OidcLoginService):
        raise HTTPException(
            status_code=503,
            detail="Server identity provider is not configured.",
        )
    return service


def _oidc_failure(status_code: int) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content={"detail": "OIDC login failed."},
    )
    response.delete_cookie(
        OIDC_STATE_COOKIE_NAME,
        path="/api/auth/oidc",
    )
    response.delete_cookie(
        WORKSPACE_INVITATION_COOKIE_NAME,
        path="/api/auth/oidc",
    )
    return response


class WorkspaceInvitationPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invitation_token: str = Field(min_length=1, max_length=512)

    @field_validator("invitation_token")
    @classmethod
    def validate_invitation_token(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("invitation_token must not be blank")
        return normalized


@app.post("/api/auth/invitations/prepare")
def prepare_workspace_invitation(
    payload: WorkspaceInvitationPrepareRequest,
    request: Request,
) -> JSONResponse:
    service = _server_oidc_login(request)
    response = JSONResponse(
        content={
            "start_path": "/api/auth/oidc/start",
            "expires_seconds": service.settings.state_seconds,
        }
    )
    response.set_cookie(
        key=WORKSPACE_INVITATION_COOKIE_NAME,
        value=payload.invitation_token,
        max_age=service.settings.state_seconds,
        httponly=True,
        secure=auth_cookie_secure(request),
        samesite="lax",
        path="/api/auth/oidc",
    )
    return response


@app.get("/api/auth/oidc/start")
def oidc_login_start(request: Request) -> RedirectResponse:
    service = _server_oidc_login(request)
    try:
        attempt = service.begin(
            redirect_path=request.query_params.get("next"),
            invitation_token=request.cookies.get(
                WORKSPACE_INVITATION_COOKIE_NAME
            ),
        )
    except OidcLoginStateError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid login destination.",
        ) from exc
    except OidcProviderUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Identity provider is temporarily unavailable.",
        ) from exc
    response = RedirectResponse(
        url=attempt.authorization_url,
        status_code=307,
    )
    response.set_cookie(
        key=OIDC_STATE_COOKIE_NAME,
        value=attempt.state_cookie,
        max_age=attempt.max_age,
        httponly=True,
        secure=auth_cookie_secure(request),
        samesite="lax",
        path="/api/auth/oidc",
    )
    return response


@app.get("/api/auth/oidc/callback", response_model=None)
def oidc_login_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse | JSONResponse:
    service = _server_oidc_login(request)
    if (
        error is not None
        or code is None
        or not code.strip()
        or len(code) > 4096
        or state is None
        or not state.strip()
        or len(state) > 512
    ):
        return _oidc_failure(401)
    try:
        result = service.complete(
            code=code,
            state=state,
            state_cookie=request.cookies.get(
                OIDC_STATE_COOKIE_NAME,
                "",
            ),
            invitation_token=request.cookies.get(
                WORKSPACE_INVITATION_COOKIE_NAME
            ),
        )
    except OidcProviderUnavailable:
        return _oidc_failure(503)
    except (
        ExternalIdentityNotAuthorized,
        OidcLoginStateError,
        OidcVerificationError,
    ):
        return _oidc_failure(401)
    response = RedirectResponse(
        url=service.settings.post_login_url(result.redirect_path),
        status_code=303,
    )
    response.delete_cookie(
        OIDC_STATE_COOKIE_NAME,
        path="/api/auth/oidc",
    )
    response.delete_cookie(
        WORKSPACE_INVITATION_COOKIE_NAME,
        path="/api/auth/oidc",
    )
    response.set_cookie(
        key=SERVER_AUTH_COOKIE_NAME,
        value=result.actor_session,
        max_age=service.settings.session_seconds,
        httponly=True,
        secure=auth_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout", response_model=ApiMessage)
def auth_logout() -> JSONResponse:
    response = JSONResponse(
        content={
            "message": "已退出登录。",
            "data": {"enabled": True, "authenticated": False, "mode": "server"},
        }
    )
    response.delete_cookie(SERVER_AUTH_COOKIE_NAME, path="/")
    response.delete_cookie(OIDC_STATE_COOKIE_NAME, path="/api/auth/oidc")
    response.delete_cookie(WORKSPACE_INVITATION_COOKIE_NAME, path="/api/auth/oidc")
    return response

def config():
    return load_runtime_config()



@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
