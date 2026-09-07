"""Durable generated copy with bounded, independent post-generation checks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from models import AICheck, GenerationCheckpoint, KnowledgeCoverageCheck, TaskRecord
from services.access_control import (
    ActorIdentity, PostgresProjectAccessRepository, ProjectAccessService,
    ProjectAccessDenied,
)
from services.job_queue import JobCancelled, JobConflict
from services.server_task_commands import PostgresAuditedTaskWriter
from storage import RevisionConflictError, content_hash, now_iso


def resume_generated_copy(task: TaskRecord, job: dict[str, Any]) -> bool:
    checkpoint = task.generation_checkpoint
    if checkpoint is None or checkpoint.job_id != str(job.get("id") or ""):
        return False
    article = task.humanized_article if job["operation"] == "humanize" else task.initial_article
    if (checkpoint.operation != job["operation"]
            or checkpoint.source_revision != int(job.get("source_revision") or 0)
            or checkpoint.result_revision != task.revision
            or checkpoint.article_hash != content_hash(article)):
        raise JobConflict("saved generation revision changed")
    return True


def checkpoint_generated_copy(task: TaskRecord, job: dict[str, Any]) -> None:
    article = task.humanized_article if job["operation"] == "humanize" else task.initial_article
    pending_check = AICheck(
        article_hash=content_hash(article),
        report="正文已保存，正在等待或执行自动检测。",
    )
    if job["operation"] == "humanize":
        task.final_ai_check = pending_check
    else:
        task.initial_ai_check = pending_check
        task.zero_gpt_report = pending_check.report
    if task.knowledge_coverage.status != "not_checked":
        task.knowledge_coverage.status = "stale"
        task.knowledge_coverage.message = "正文已更新，正在等待或执行知识库复检。"
    task.generation_checkpoint = GenerationCheckpoint(
        job_id=str(job["id"]), operation=str(job["operation"]),
        source_revision=int(job.get("source_revision") or 0),
        result_revision=task.revision + 1, article_hash=content_hash(article),
    )


def finish_generation_checks(
    task: TaskRecord, job: dict[str, Any], *, engine: Any, audit: Any,
    ai_rate: Any, knowledge_coverage: Any, cancelled: Callable[[], bool],
) -> int:
    if not resume_generated_copy(task, job):
        raise JobConflict("saved generation is unavailable")
    checkpoint = task.generation_checkpoint
    assert checkpoint is not None
    actor = ActorIdentity(str(job["organization_id"]), str(job["requested_by_user_id"]))
    project_id = str(job["project_id"])
    if cancelled():
        raise JobCancelled("generation checks cancelled; generated copy was retained")
    try:
        ProjectAccessService(PostgresProjectAccessRepository(engine)).require(
            actor, project_id, "article.edit",
        )
    except ProjectAccessDenied as exc:
        raise JobConflict("job actor is not authorized") from exc
    if checkpoint.completed:
        return task.revision
    humanized = job["operation"] == "humanize"
    article = task.humanized_article if humanized else task.initial_article

    def detect() -> AICheck:
        check = AICheck(
            confirmed=False, provider="zerogpt", checked_at=now_iso(),
            article_hash=checkpoint.article_hash,
            report="ZeroGPT 自动检测未运行：服务端尚未配置 API Key。",
        )
        try:
            if ai_rate is not None and ai_rate.ready:
                result = ai_rate.detect(article)
                check.score = result.ai_percentage
                check.report = result.report
        except Exception:
            check.report = "ZeroGPT 自动检测暂时不可用，正文已保存，请重试检测或人工确认。"
        return check

    def coverage() -> KnowledgeCoverageCheck:
        if knowledge_coverage is None:
            return task.knowledge_coverage.model_copy(deep=True)
        # The coverage service mutates its input; never share that object with
        # the detector or merge its other fields over a concurrent user edit.
        copy = task.model_copy(deep=True)
        try:
            knowledge_coverage.evaluate_task(
                copy, organization_id=actor.organization_id,
                user_id=actor.user_id, project_id=project_id,
            )
            return copy.knowledge_coverage
        except Exception:
            return KnowledgeCoverageCheck(
                status="unavailable", checked_at=now_iso(),
                message="知识库复检暂时不可用，正文已保存，请单独重试检查。",
            )

    # At most two checks per active job; the existing global/project queue
    # limits still bound jobs. No nested generation or unbounded executor.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="article-check") as pool:
        ai_future = pool.submit(detect)
        coverage_future = pool.submit(coverage)
        check = ai_future.result()
        coverage_check = coverage_future.result()
    if cancelled():
        raise JobCancelled("generation checks cancelled; generated copy was retained")
    if humanized:
        task.final_ai_check = check
    else:
        task.initial_ai_check = check
        task.zero_gpt_report = check.report
    task.knowledge_coverage = coverage_check
    checkpoint.completed = True
    checkpoint.result_revision = task.revision + 1
    try:
        saved = PostgresAuditedTaskWriter(
            engine, organization_id=actor.organization_id, project_id=project_id, audit=audit,
        ).put(
            task, expected_revision=task.revision, actor=actor,
            action="article.generation.checked",
            details={"ai_score_recorded": check.score is not None,
                     "knowledge_coverage_status": coverage_check.status},
        )
    except ProjectAccessDenied as exc:
        raise JobConflict("job actor is not authorized") from exc
    except RevisionConflictError as exc:
        raise JobConflict("saved generation revision changed") from exc
    return saved.revision
