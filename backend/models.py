from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


STATUS_NEW = "new"
STATUS_TITLES_READY = "titles_ready"
STATUS_TITLE_SELECTED = "title_selected"
STATUS_OUTLINE_READY = "outline_ready"
STATUS_DRAFT_READY = "draft_ready"
STATUS_DOCX_EXPORTED = "docx_exported"


class Product(BaseModel):
    name: str = ""
    url: str = ""
    image_path: str = ""
    description: str = ""


class TaskRecord(BaseModel):
    id: str
    week_folder: str
    customer: str
    topic_index: int
    topic: str
    competitor_keyword: str = ""
    competitor_blog: str = ""
    status: str = STATUS_NEW
    task_dir: str
    title_candidates: list[str] = Field(default_factory=list)
    selected_title: str = ""
    outline: str = ""
    article: str = ""
    products: list[Product] = Field(default_factory=list)
    docx_path: str = ""
    zero_gpt_report: str = ""
    created_at: str
    updated_at: str


class SelectTitleRequest(BaseModel):
    title: str


class OutlineUpdateRequest(BaseModel):
    outline: str


class ArticleUpdateRequest(BaseModel):
    article: str


class ProductsUpdateRequest(BaseModel):
    products: list[Product]


class ZeroGptReportRequest(BaseModel):
    report: str


class GenerateArticleRequest(BaseModel):
    word_count: int | None = None


class DashboardSummary(BaseModel):
    week_folder: str
    week_path: str
    customer_count: int
    task_count: int
    completed_count: int
    status_counts: dict[str, int]
    llm_ready: bool


class ApiMessage(BaseModel):
    message: str
    data: Any | None = None
