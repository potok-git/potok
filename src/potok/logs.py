"""Журнал worker: запись в файл и чтение последних строк для портала."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

from potok.config import data_dir_for


def worker_log_path(database: Path) -> Path:
    """Общий файл журнала. В Docker путь можно задать переменной POTOK_LOG_FILE."""
    env = os.environ.get("POTOK_LOG_FILE", "").strip()
    if env:
        return Path(env)
    return data_dir_for(database) / "logs" / "worker.log"


def attach_log_file(path: Path) -> None:
    """Дублировать журнал в файл, чтобы портал мог его показывать."""
    root = logging.getLogger()
    resolved = path.resolve()
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == resolved:
            return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)


def tail_lines(path: Path, limit: int = 200) -> list[str]:
    if limit < 1 or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-limit:]


def count_errors(path: Path, *, limit_bytes: int = 2_000_000) -> int:
    """Сколько строк с уровнем ERROR в хвосте журнала."""
    return len(error_lines(path, limit=10_000, limit_bytes=limit_bytes))


def error_lines(path: Path, *, limit: int = 8, limit_bytes: int = 2_000_000) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if len(data) > limit_bytes:
        data = data[-limit_bytes:]
    text = data.decode("utf-8", errors="replace")
    found = [line for line in text.splitlines() if _is_error(line)]
    return found[-limit:]


def iter_events(path: Path, *, follow: bool) -> Iterator[str]:
    """События для страницы журнала. follow=False отдаёт только уже записанные строки."""
    for line in tail_lines(path, 200):
        yield _sse(line)
    if not follow:
        return
    import time

    position = path.stat().st_size if path.is_file() else 0
    while True:
        time.sleep(1)
        if not path.is_file():
            yield ": ping\n\n"
            continue
        size = path.stat().st_size
        if size < position:
            position = 0
        if size == position:
            yield ": ping\n\n"
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            handle.seek(position)
            chunk = handle.read()
            position = handle.tell()
        for line in chunk.splitlines():
            yield _sse(line)


def _is_error(line: str) -> bool:
    return " ERROR " in f" {line} " or line.startswith("ERROR")


def _sse(line: str) -> str:
    clean = line.replace("\r", " ").replace("\n", " ")
    if len(clean) > 2000:
        clean = clean[:2000] + "…"
    return f"data: {clean}\n\n"
