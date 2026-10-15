"""Очередь решений с портала. Её читает Telegram-бот и делает ту же работу, что по кнопке."""

from __future__ import annotations

import json
from pathlib import Path

from potok.store import APPROVED, REJECTED

_STATUSES = frozenset({APPROVED, REJECTED})


def jobs_path(directory: Path) -> Path:
    return directory / "portal_jobs.jsonl"


def enqueue(directory: Path, row_id: int, status: str) -> None:
    """Добавить решение. Бот заберёт строку и скачает файл, если запись одобрена."""
    if status not in _STATUSES:
        raise ValueError("Решение может быть только approved или rejected.")
    if row_id < 1:
        raise ValueError("Номер записи должен быть больше нуля.")
    path = jobs_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"row_id": row_id, "status": status}, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def drain_jobs(path: Path) -> list[dict[str, object]]:
    """Забрать все строки и очистить файл. Битые строки пропускаются."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        path.write_text("", encoding="utf-8")
    except OSError:
        return []
    jobs: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        row_id = payload.get("row_id")
        status = payload.get("status")
        if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id < 1:
            continue
        if not isinstance(status, str) or status not in _STATUSES:
            continue
        jobs.append({"row_id": row_id, "status": status})
    return jobs
