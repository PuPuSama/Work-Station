from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config.yaml"


@dataclass(frozen=True)
class AppConfig:
    topic_library: Path
    knowledge_base: Path
    output_root: Path
    data_file: Path
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

    @property
    def current_week_folder(self) -> str:
        start, end = current_work_week()
        return self.week_name_format.format(
            start_month=start.month,
            start_day=start.day,
            end_month=end.month,
            end_day=end.day,
            owner=self.week_owner,
        )

    @property
    def current_week_path(self) -> Path:
        return self.output_root / self.current_week_folder


def current_work_week(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def load_config() -> AppConfig:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    paths = raw["paths"]
    article = raw["article"]
    prompts = raw.get("prompts", {})
    docx = raw["docx_format"]
    styles = docx["styles"]
    llm = raw.get("llm", {})
    week = raw["week_folder"]

    return AppConfig(
        topic_library=Path(paths["topic_library"]),
        knowledge_base=Path(paths["knowledge_base"]),
        output_root=Path(paths["output_root"]),
        data_file=Path(paths["data_file"]),
        week_owner=str(week["owner"]),
        week_name_format=str(week["name_format"]),
        language=str(article.get("language", "English")),
        title_candidates=int(article.get("title_candidates", 10)),
        default_word_count=int(article.get("default_word_count", 1200)),
        ai_pass_threshold=float(article.get("ai_pass_threshold", 30)),
        humanize_prompt_path=Path(
            prompts.get(
                "humanize",
                ROOT_DIR.parent / "降ai提示词-未测试效果版.txt",
            )
        ),
        docx_font=str(docx.get("font", "Times New Roman")),
        title_1_size=float(styles["title_1"]["size_pt"]),
        title_2_size=float(styles["title_2"]["size_pt"]),
        title_3_size=float(styles["title_3"]["size_pt"]),
        body_size=float(styles["body"]["size_pt"]),
        llm_provider=str(llm.get("provider", "openai_compatible")),
        llm_base_url=str(llm.get("base_url", "https://api.openai.com/v1")).rstrip("/"),
        llm_model=str(llm.get("model", "")),
    )


def public_config(config: AppConfig) -> dict[str, Any]:
    # Integration secrets stay server-side. The UI only needs a readiness flag.
    load_dotenv(ROOT_DIR / ".env")
    load_dotenv(ROOT_DIR / "backend" / ".env")
    return {
        "topic_library": str(config.topic_library),
        "knowledge_base": str(config.knowledge_base),
        "output_root": str(config.output_root),
        "data_file": str(config.data_file),
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
        },
        "integrations": {
            "tavily_ready": bool(os.environ.get("TAVILY_API_KEY", "").strip()),
        },
    }
