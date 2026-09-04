from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values


def _application_root(environment: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environment is None else environment
    configured = str(source.get("ARTICLE_AGENT_ROOT", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT_DIR = _application_root()


def _config_path(
    environment: Mapping[str, str] | None = None,
    *,
    root: Path | None = None,
) -> Path:
    source = os.environ if environment is None else environment
    application_root = root or _application_root(source)
    configured = str(source.get("ARTICLE_AGENT_CONFIG", "") or "").strip()
    path = Path(configured or "config.yaml").expanduser()
    return (
        path.resolve()
        if path.is_absolute()
        else (application_root / path).resolve()
    )


CONFIG_PATH = _config_path(root=ROOT_DIR)
PROJECT_SCOPE = "全部项目"


def application_root() -> Path:
    """Return the current application root after environment initialization."""

    return _application_root()


def initialize_environment(
    environment: MutableMapping[str, str] | None = None,
    *,
    app_root: Path | None = None,
) -> Path:
    """Load dotenv files once at an application or CLI boundary.

    Process variables always win.  Without an explicit environment file, the
    repository root ``.env`` wins over the legacy ``backend/.env`` for keys
    which are not already present.  ``ARTICLE_AGENT_ENV_FILE`` is deliberately
    read before the default candidates so it can select exactly one file.
    """

    target = os.environ if environment is None else environment
    root = (app_root or _application_root(target)).expanduser().resolve()
    selected = str(target.get("ARTICLE_AGENT_ENV_FILE", "") or "").strip()
    if selected:
        selected_path = Path(selected).expanduser()
        if not selected_path.is_absolute():
            selected_path = root / selected_path
        selected_path = selected_path.resolve()
        if not selected_path.is_file():
            raise FileNotFoundError(
                "ARTICLE_AGENT_ENV_FILE does not exist: "
                f"{selected_path}"
            )
        candidates = (selected_path,)
    else:
        candidates = (root / ".env", root / "backend" / ".env")

    for path in candidates:
        if not path.is_file():
            continue
        for key, value in dotenv_values(path).items():
            if not key or value is None or key in target:
                continue
            target[key] = value

    effective_root = (app_root or _application_root(target)).expanduser().resolve()
    effective_config_path = _config_path(target, root=effective_root)
    if environment is None:
        global ROOT_DIR, CONFIG_PATH
        ROOT_DIR = effective_root
        CONFIG_PATH = effective_config_path
    return effective_root


def _deep_merge(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(previous, value)
        else:
            merged[key] = value
    return merged


def _load_yaml_config(
    path: Path,
    *,
    ancestry: tuple[Path, ...] = (),
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if resolved in ancestry:
        chain = " -> ".join(str(item) for item in (*ancestry, resolved))
        raise ValueError(f"configuration extends cycle: {chain}")
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"configuration must be a mapping: {resolved}")
    document = dict(raw)
    parent_reference = document.pop("extends", None)
    if parent_reference is None:
        return document
    if not isinstance(parent_reference, str) or not parent_reference.strip():
        raise ValueError(
            "configuration extends must be a non-empty path: "
            f"{resolved}"
        )
    parent_path = (resolved.parent / parent_reference).resolve()
    parent = _load_yaml_config(parent_path, ancestry=(*ancestry, resolved))
    return _deep_merge(parent, document)


def _environment_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _environment_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        parsed = default
    else:
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer value") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _configured_path(value: object, *, root: Path | None = None) -> Path:
    path = Path(str(value or "")).expanduser()
    application_root = root or ROOT_DIR
    return (
        path.resolve()
        if path.is_absolute()
        else (application_root / path).resolve()
    )


@dataclass(frozen=True)
class AppConfig:
    topic_library: Path
    knowledge_base: Path
    output_root: Path
    week_owner: str
    week_name_format: str
    language: str
    title_candidates: int
    default_word_count: int
    ai_pass_threshold: float
    humanize_prompt_path: Path
    docx_font: str
    title_1_size: float
    title_2_size: float
    title_3_size: float
    body_size: float
    llm_provider: str
    llm_base_url: str
    llm_model: str
    llm_reasoning_effort: str
    llm_available_models: tuple[str, ...]
    llm_available_reasoning_efforts: tuple[str, ...]
    llm_runtime_override: bool
    knowledge_agent_enabled: bool
    workflow_assistant_enabled: bool
    workflow_assistant_attachments_enabled: bool
    workflow_assistant_project_changes_enabled: bool
    workflow_assistant_gap_fill_enabled: bool
    workflow_assistant_max_concurrency: int
    workflow_assistant_soft_budget_tokens: int
    global_job_concurrency: int
    project_job_concurrency: int
    database_pool_size: int = 20
    database_max_overflow: int = 20
    database_pool_timeout_seconds: int = 60
    database_pool_recycle_seconds: int = 300

    @property
    def current_week_folder(self) -> str:
        """Compatibility label for clients which still call this a week.

        Tasks are now persistent project records.  Keeping the old response
        field avoids breaking deployed frontends while removing dates from
        task identity and filesystem layout.
        """

        return PROJECT_SCOPE

    @property
    def current_week_path(self) -> Path:
        """Compatibility path for the project-wide, non-weekly workspace."""

        return self.output_root


def load_config() -> AppConfig:
    root = _application_root()
    config_path = _config_path(root=root)
    raw = _load_yaml_config(config_path)
    paths = raw["paths"]
    article = raw["article"]
    prompts = raw.get("prompts", {})
    docx = raw["docx_format"]
    styles = docx["styles"]
    llm = raw.get("llm", {})
    features = raw.get("features", {})
    workflow_assistant = raw.get("workflow_assistant", {}) or {}
    server_jobs = raw.get("server_jobs", {}) or {}
    legacy_week = raw.get("week_folder", {})
    configured_model = str(llm.get("model", "")).strip()
    configured_reasoning_effort = str(
        llm.get("reasoning_effort", "xhigh")
    ).strip()
    available_models = tuple(
        dict.fromkeys(
            [
                configured_model,
                *[
                    str(item).strip()
                    for item in llm.get(
                        "available_models",
                        ["gpt-5.6-sol", "gpt-5.6-terra"],
                    )
                ],
            ]
        )
    )
    available_models = tuple(item for item in available_models if item)
    available_reasoning_efforts = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in llm.get(
                "available_reasoning_efforts",
                ["low", "medium", "high", "xhigh"],
            )
            if str(item).strip()
        )
    )
    if configured_reasoning_effort not in available_reasoning_efforts:
        available_reasoning_efforts = (
            configured_reasoning_effort,
            *available_reasoning_efforts,
        )

    project_job_concurrency = _environment_int(
        "ARTICLE_AGENT_PROJECT_JOB_CONCURRENCY",
        int(server_jobs.get("project_concurrency", 5)),
        minimum=1,
        maximum=32,
    )
    return AppConfig(
        topic_library=_configured_path(paths["topic_library"], root=root),
        knowledge_base=_configured_path(paths["knowledge_base"], root=root),
        output_root=_configured_path(paths["output_root"], root=root),
        # Compatibility-only fields. Task identity and paths no longer use
        # owner/date formatting.
        week_owner=str(legacy_week.get("owner", "")),
        week_name_format=str(legacy_week.get("name_format", "")),
        language=str(article.get("language", "English")),
        title_candidates=int(article.get("title_candidates", 10)),
        default_word_count=int(article.get("default_word_count", 1200)),
        ai_pass_threshold=float(article.get("ai_pass_threshold", 30)),
        humanize_prompt_path=_configured_path(
            prompts.get(
                "humanize",
                root.parent / "降ai提示词-未测试效果版.txt",
            ),
            root=root,
        ),
        docx_font=str(docx.get("font", "Times New Roman")),
        title_1_size=float(styles["title_1"]["size_pt"]),
        title_2_size=float(styles["title_2"]["size_pt"]),
        title_3_size=float(styles["title_3"]["size_pt"]),
        body_size=float(styles["body"]["size_pt"]),
        llm_provider=str(llm.get("provider", "openai_compatible")),
        llm_base_url=str(llm.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
        llm_model=configured_model,
        llm_reasoning_effort=configured_reasoning_effort,
        llm_available_models=available_models,
        llm_available_reasoning_efforts=available_reasoning_efforts,
        llm_runtime_override=False,
        knowledge_agent_enabled=_environment_bool(
            "KNOWLEDGE_AGENT_ENABLED",
            bool(features.get("knowledge_agent_enabled", False)),
        ),
        workflow_assistant_enabled=_environment_bool(
            "WORKFLOW_ASSISTANT_ENABLED",
            bool(features.get("workflow_assistant_enabled", False)),
        ),
        workflow_assistant_attachments_enabled=_environment_bool(
            "WORKFLOW_ASSISTANT_ATTACHMENTS_ENABLED",
            bool(features.get("workflow_assistant_attachments_enabled", False)),
        ),
        workflow_assistant_project_changes_enabled=_environment_bool(
            "WORKFLOW_ASSISTANT_PROJECT_CHANGES_ENABLED",
            bool(features.get("workflow_assistant_project_changes_enabled", False)),
        ),
        workflow_assistant_gap_fill_enabled=_environment_bool(
            "WORKFLOW_ASSISTANT_GAP_FILL_ENABLED",
            bool(features.get("workflow_assistant_gap_fill_enabled", False)),
        ),
        workflow_assistant_max_concurrency=_environment_int(
            "WORKFLOW_ASSISTANT_MAX_CONCURRENCY",
            int(workflow_assistant.get("max_concurrency", 5)),
            minimum=1,
            maximum=32,
        ),
        workflow_assistant_soft_budget_tokens=_environment_int(
            "WORKFLOW_ASSISTANT_SOFT_BUDGET_TOKENS",
            int(workflow_assistant.get("soft_budget_warning_tokens", 24000)),
            minimum=1000,
            maximum=1_000_000,
        ),
        global_job_concurrency=_environment_int(
            "ARTICLE_AGENT_GLOBAL_JOB_CONCURRENCY",
            int(server_jobs.get("global_concurrency", 8)),
            minimum=1,
            maximum=128,
        ),
        project_job_concurrency=project_job_concurrency,
        database_pool_size=_environment_int(
            "ARTICLE_AGENT_DB_POOL_SIZE",
            20,
            minimum=5,
            maximum=100,
        ),
        database_max_overflow=_environment_int(
            "ARTICLE_AGENT_DB_MAX_OVERFLOW",
            20,
            minimum=0,
            maximum=100,
        ),
        database_pool_timeout_seconds=_environment_int(
            "ARTICLE_AGENT_DB_POOL_TIMEOUT_SECONDS",
            60,
            minimum=5,
            maximum=300,
        ),
        database_pool_recycle_seconds=_environment_int(
            "ARTICLE_AGENT_DB_POOL_RECYCLE_SECONDS",
            300,
            minimum=60,
            maximum=3600,
        ),
    )


def available_with_current(
    current: str,
    configured: tuple[str, ...],
) -> tuple[str, ...]:
    if not current or current in configured:
        return configured
    return (current, *configured)


def load_runtime_config() -> AppConfig:
    """Load static configuration plus process-level environment overrides."""

    base = load_config()
    environment_model = os.environ.get("LLM_MODEL", "").strip()
    environment_reasoning_effort = os.environ.get(
        "LLM_REASONING_EFFORT",
        "",
    ).strip()
    environment_base_url = os.environ.get("LLM_BASE_URL", "").strip()
    if not (
        environment_model
        or environment_reasoning_effort
        or environment_base_url
    ):
        return base
    return replace(
        base,
        llm_model=environment_model or base.llm_model,
        llm_reasoning_effort=(
            environment_reasoning_effort or base.llm_reasoning_effort
        ),
        llm_base_url=(environment_base_url or base.llm_base_url).rstrip("/"),
        llm_available_models=available_with_current(
            environment_model or base.llm_model,
            base.llm_available_models,
        ),
        llm_available_reasoning_efforts=available_with_current(
            environment_reasoning_effort or base.llm_reasoning_effort,
            base.llm_available_reasoning_efforts,
        ),
    )


def public_config(config: AppConfig) -> dict[str, Any]:
    # Integration secrets stay server-side. The UI only needs a readiness flag.
    features: dict[str, bool] = {
        "knowledge_agent_enabled": config.knowledge_agent_enabled,
    }
    # Keep the legacy public response byte-for-byte stable while the new
    # assistant is disabled.  Once explicitly enabled, expose its readiness
    # flag to the workspace UI.
    if config.workflow_assistant_enabled:
        features["workflow_assistant_enabled"] = True
        features["workflow_assistant_attachments_enabled"] = bool(
            config.workflow_assistant_attachments_enabled
        )
        features["workflow_assistant_project_changes_enabled"] = bool(
            config.workflow_assistant_project_changes_enabled
        )
        features["workflow_assistant_gap_fill_enabled"] = bool(
            config.workflow_assistant_gap_fill_enabled
        )
    payload = {
        "topic_library": str(config.topic_library),
        "knowledge_base": str(config.knowledge_base),
        "output_root": str(config.output_root),
        "current_week_folder": config.current_week_folder,
        "current_week_path": str(config.current_week_path),
        "article": {
            "language": config.language,
            "title_candidates": config.title_candidates,
            "default_word_count": config.default_word_count,
            "ai_pass_threshold": config.ai_pass_threshold,
        },
        "prompts": {
            "humanize": str(config.humanize_prompt_path),
        },
        "docx_format": {
            "font": config.docx_font,
            "title_1_size": config.title_1_size,
            "title_2_size": config.title_2_size,
            "title_3_size": config.title_3_size,
            "body_size": config.body_size,
        },
        "llm": {
            "provider": config.llm_provider,
            "base_url": config.llm_base_url,
            "model": config.llm_model,
            "reasoning_effort": config.llm_reasoning_effort,
            "available_models": list(config.llm_available_models),
            "available_reasoning_efforts": list(
                config.llm_available_reasoning_efforts
            ),
        },
        "integrations": {
            "tavily_ready": bool(os.environ.get("TAVILY_API_KEY", "").strip()),
            # Only expose readiness; never expose the provider key itself.
            "zerogpt_ready": bool(
                (
                    os.environ.get("ARTICLE_AGENT_ZEROGPT_API_KEY", "")
                    or os.environ.get("ZEROGPT_API_KEY", "")
                ).strip()
            ),
        },
        "features": features,
    }
    if config.workflow_assistant_enabled:
        payload["workflow_assistant"] = {
            "max_concurrency": int(
                getattr(config, "workflow_assistant_max_concurrency", 5)
            ),
            "soft_budget_warning_tokens": int(
                getattr(config, "workflow_assistant_soft_budget_tokens", 24000)
            ),
        }
    return payload
