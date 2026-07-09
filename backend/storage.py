from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from config import AppConfig
from models import TaskRecord


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class TaskStore:
    def __init__(self, config: AppConfig):
        self.path = config.data_file
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[TaskRecord]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [TaskRecord.model_validate(item) for item in raw]

    def save(self, tasks: Iterable[TaskRecord]) -> None:
        records = [task.model_dump(mode="json") for task in tasks]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def get(self, task_id: str) -> TaskRecord:
        for task in self.load():
            if task.id == task_id:
                return task
        raise KeyError(task_id)

    def put(self, task: TaskRecord) -> TaskRecord:
        task.updated_at = now_iso()
        tasks = self.load()
        replaced = False
        for index, existing in enumerate(tasks):
            if existing.id == task.id:
                tasks[index] = task
                replaced = True
                break
        if not replaced:
            tasks.append(task)
        self.save(tasks)
        return task

    def upsert_many(self, incoming: Iterable[TaskRecord]) -> list[TaskRecord]:
        existing = {task.id: task for task in self.load()}
        for task in incoming:
            if task.id in existing:
                previous = existing[task.id]
                task.status = previous.status
                task.title_candidates = previous.title_candidates
                task.selected_title = previous.selected_title
                task.outline = previous.outline
                task.article = previous.article
                task.products = previous.products
                task.docx_path = previous.docx_path
                task.zero_gpt_report = previous.zero_gpt_report
                task.created_at = previous.created_at
            existing[task.id] = task
        tasks = list(existing.values())
        self.save(tasks)
        return tasks


def write_text_artifact(task: TaskRecord, filename: str, content: str) -> Path:
    path = Path(task.task_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_json_artifact(task: TaskRecord, filename: str, content: object) -> Path:
    path = Path(task.task_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
