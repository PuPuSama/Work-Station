from __future__ import annotations

from collections import Counter

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config import load_config, public_config
from models import (
    ApiMessage,
    ArticleUpdateRequest,
    DashboardSummary,
    GenerateArticleRequest,
    OutlineUpdateRequest,
    ProductsUpdateRequest,
    SelectTitleRequest,
    STATUS_DOCX_EXPORTED,
    STATUS_DRAFT_READY,
    STATUS_OUTLINE_READY,
    STATUS_TITLE_SELECTED,
    STATUS_TITLES_READY,
    TaskRecord,
    ZeroGptReportRequest,
)
from services.docx_export import export_task_docx
from services.generator import generate_article, generate_outline, generate_titles, humanize_article
from services.llm import LLMClient
from services.product_crawler import recommend_products
from services.topics import scan_topic_library
from storage import TaskStore, write_json_artifact, write_text_artifact


app = FastAPI(title="Article Workflow Agent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def config():
    return load_config()


def store() -> TaskStore:
    return TaskStore(config())


def get_task_or_404(task_id: str) -> TaskRecord:
    try:
        return store().get(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}") from None


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def read_config() -> dict:
    return public_config(config())


@app.get("/api/dashboard", response_model=DashboardSummary)
def dashboard() -> DashboardSummary:
    cfg = config()
    tasks = store().load()
    current_week_tasks = [task for task in tasks if task.week_folder == cfg.current_week_folder]
    status_counts = dict(Counter(task.status for task in current_week_tasks))
    return DashboardSummary(
        week_folder=cfg.current_week_folder,
        week_path=str(cfg.current_week_path),
        customer_count=len({task.customer for task in current_week_tasks}),
        task_count=len(current_week_tasks),
        completed_count=sum(1 for task in current_week_tasks if task.status == STATUS_DOCX_EXPORTED),
        status_counts=status_counts,
        llm_ready=LLMClient(cfg).ready,
    )


@app.post("/api/init-week", response_model=ApiMessage)
def init_week() -> ApiMessage:
    cfg = config()
    scanned = scan_topic_library(cfg)
    tasks = store().upsert_many(scanned)
    current_week_count = sum(1 for task in tasks if task.week_folder == cfg.current_week_folder)
    return ApiMessage(
        message=f"Initialized {current_week_count} tasks for {cfg.current_week_folder}.",
        data={"week_folder": cfg.current_week_folder, "task_count": current_week_count},
    )


@app.get("/api/tasks", response_model=list[TaskRecord])
def list_tasks(customer: str | None = None, status: str | None = None) -> list[TaskRecord]:
    cfg = config()
    tasks = [task for task in store().load() if task.week_folder == cfg.current_week_folder]
    if customer:
        tasks = [task for task in tasks if task.customer == customer]
    if status:
        tasks = [task for task in tasks if task.status == status]
    return sorted(tasks, key=lambda task: (task.customer, task.topic_index))


@app.get("/api/tasks/{task_id}", response_model=TaskRecord)
def read_task(task_id: str) -> TaskRecord:
    return get_task_or_404(task_id)


@app.post("/api/tasks/{task_id}/titles", response_model=TaskRecord)
def create_titles(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.title_candidates = generate_titles(config(), task)
    task.status = STATUS_TITLES_READY
    write_text_artifact(task, "title_candidates.md", "\n".join(f"{i + 1}. {title}" for i, title in enumerate(task.title_candidates)))
    return store().put(task)


@app.post("/api/tasks/{task_id}/select-title", response_model=TaskRecord)
def select_title(task_id: str, request: SelectTitleRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.selected_title = request.title.strip()
    task.status = STATUS_TITLE_SELECTED
    write_text_artifact(task, "selected_title.txt", task.selected_title)
    return store().put(task)


@app.put("/api/tasks/{task_id}/products", response_model=TaskRecord)
def update_products(task_id: str, request: ProductsUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.products = request.products
    write_json_artifact(task, "products.json", [product.model_dump() for product in task.products])
    return store().put(task)


@app.post("/api/tasks/{task_id}/products/auto", response_model=TaskRecord)
def auto_products(task_id: str, limit: int = 3) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.products = recommend_products(config(), task, max(1, min(limit, 6)))
    write_json_artifact(task, "products.json", [product.model_dump() for product in task.products])
    return store().put(task)


@app.post("/api/tasks/{task_id}/outline", response_model=TaskRecord)
def create_outline(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.outline = generate_outline(config(), task)
    task.status = STATUS_OUTLINE_READY
    write_text_artifact(task, "outline.md", task.outline)
    return store().put(task)


@app.put("/api/tasks/{task_id}/outline", response_model=TaskRecord)
def update_outline(task_id: str, request: OutlineUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.outline = request.outline
    task.status = STATUS_OUTLINE_READY
    write_text_artifact(task, "outline.md", task.outline)
    return store().put(task)


@app.post("/api/tasks/{task_id}/article", response_model=TaskRecord)
def create_article(task_id: str, request: GenerateArticleRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.article = generate_article(config(), task, request.word_count)
    task.status = STATUS_DRAFT_READY
    write_text_artifact(task, "draft.md", task.article)
    return store().put(task)


@app.put("/api/tasks/{task_id}/article", response_model=TaskRecord)
def update_article(task_id: str, request: ArticleUpdateRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.article = request.article
    task.status = STATUS_DRAFT_READY
    write_text_artifact(task, "final.md", task.article)
    return store().put(task)


@app.put("/api/tasks/{task_id}/zerogpt", response_model=TaskRecord)
def update_zero_gpt_report(task_id: str, request: ZeroGptReportRequest) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.zero_gpt_report = request.report
    write_text_artifact(task, "zerogpt_report.txt", task.zero_gpt_report)
    return store().put(task)


@app.post("/api/tasks/{task_id}/humanize", response_model=TaskRecord)
def humanize(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    task.article = humanize_article(config(), task)
    task.status = STATUS_DRAFT_READY
    write_text_artifact(task, "final.md", task.article)
    return store().put(task)


@app.post("/api/tasks/{task_id}/export-docx", response_model=TaskRecord)
def export_docx(task_id: str) -> TaskRecord:
    task = get_task_or_404(task_id)
    output = export_task_docx(config(), task)
    task.docx_path = str(output)
    task.status = STATUS_DOCX_EXPORTED
    return store().put(task)
