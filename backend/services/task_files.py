from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath

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


def open_task_directory(config: AppConfig, task: TaskRecord) -> Path:
    directory = resolve_task_directory(config, task)
    if sys.platform != "win32":
        raise TaskDirectoryError("Opening task folders is supported only on Windows.")

    explorer = PureWindowsPath(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
    try:
        subprocess.Popen(
            [str(explorer), str(directory)],
            close_fds=True,
        )
    except OSError as explorer_error:
        raise TaskDirectoryError(
            f"Unable to open task directory: {directory} ({explorer_error})"
        ) from explorer_error
    return directory
