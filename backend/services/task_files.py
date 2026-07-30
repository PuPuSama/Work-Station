from __future__ import annotations

from pathlib import Path

from config import AppConfig
from models import TaskRecord


class TaskDirectoryError(RuntimeError):
    """Raised when a task directory is missing, unsafe, or cannot be opened."""


def resolve_task_directory(config: AppConfig, task: TaskRecord) -> Path:
    output_root = config.output_root.expanduser().resolve()
    directory = Path(task.task_dir).expanduser().resolve()
    try:
        directory.relative_to(output_root)
    except ValueError as exc:
        raise TaskDirectoryError(
            f"Task directory is outside the configured output root: {directory}"
        ) from exc
    if not directory.exists() or not directory.is_dir():
        raise TaskDirectoryError(f"Task directory does not exist: {directory}")
    return directory
