"""Процент отправки видео в VK. Файл общий для портала и рабочего контейнера."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from potok.config import data_dir_for
from potok.qbit import eta_text

_KEEP_SECONDS = 60 * 60
_LOCK = threading.Lock()
_STATUSES = frozenset({"uploading", "processing", "done", "error", "stopped"})


def board_path(database: Path) -> Path:
    return data_dir_for(database) / "vk_uploads.json"


def order_path(database: Path) -> Path:
    return data_dir_for(database) / "vk_order.json"


class UploadOrder:
    """Порядок, в котором докачанные фильмы уходят в VK. Верхний — первый."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def arranged(self, hashes: list[str]) -> list[str]:
        """Сохранённый порядок. Новые hash добавляются в конец, лишние выпадают."""
        known = [_hash(item) for item in hashes]
        saved = self._read()
        ordered = [item for item in saved if item in known]
        for item in known:
            if item not in ordered:
                ordered.append(item)
        return ordered

    def apply(self, hashes: list[str]) -> list[str]:
        ordered = self.arranged(hashes)
        if ordered != self._read():
            self._write(ordered)
        return ordered

    def move(self, hashes: list[str], infohash: str, direction: str) -> list[str]:
        if direction not in {"up", "down"}:
            raise ValueError("Порядок можно только поднять или опустить.")
        digest = _hash(infohash)
        ordered = self.arranged(hashes)
        if digest not in ordered:
            return ordered
        index = ordered.index(digest)
        target = index - 1 if direction == "up" else index + 1
        if target < 0 or target >= len(ordered):
            return ordered
        ordered[index], ordered[target] = ordered[target], ordered[index]
        self._write(ordered)
        return ordered

    def forget(self, infohash: str) -> None:
        """Убрать раздачу из сохранённого порядка. Если её там нет, файл не меняется."""
        digest = _hash(infohash)
        saved = self._read()
        if digest not in saved:
            return
        self._write([item for item in saved if item != digest])

    def _read(self) -> list[str]:
        with _LOCK, _file_lock(self.path):
            return self._load()

    def _write(self, hashes: list[str]) -> None:
        with _LOCK, _file_lock(self.path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"hashes": hashes}, ensure_ascii=False)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self.path)

    def _load(self) -> list[str]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw = payload.get("hashes") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []
        found: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            digest = item.lower()
            if _HASH_RE.fullmatch(digest) and digest not in found:
                found.append(digest)
        return found


_HASH_RE = re.compile(r"^[a-f0-9]{40}$")


def _hash(infohash: str) -> str:
    text = infohash.strip().lower()
    if not _HASH_RE.fullmatch(text):
        raise ValueError("Hash должен быть 40 шестнадцатеричных знаков.")
    return text


class UploadBoard:
    """Список текущих отправок. Остановка — флаг в том же файле."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def begin(self, *, name: str, title: str, vk_uuid: str = "") -> str:
        job_id = secrets.token_hex(6)
        job = {
            "id": job_id,
            "name": name,
            "title": title,
            "vk_uuid": vk_uuid,
            "percent": 0,
            "sent": 0,
            "total": 0,
            "status": "uploading",
            "message": "",
            "page_url": "",
            "owner_id": None,
            "video_id": None,
            "stop": False,
            "updated": time.time(),
        }
        self._update(lambda jobs: jobs.append(job))
        return job_id

    def tick(self, job_id: str, sent: int, total: int) -> None:
        percent = sent * 100 // total if total else 0
        now = time.time()

        def change(jobs: list[dict]) -> None:
            job = _find(jobs, job_id)
            if job is None or job.get("stop"):
                return
            updated = job.get("updated")
            elapsed = now - updated if isinstance(updated, (int, float)) else 0.0
            if job.get("percent") == percent and sent != total and elapsed < 2:
                return
            previous = job.get("sent")
            if (
                isinstance(previous, int)
                and not isinstance(previous, bool)
                and elapsed >= 0.5
                and sent >= previous
            ):
                job["speed"] = int((sent - previous) / elapsed)
            job["sent"] = sent
            job["total"] = total
            job["percent"] = 100 if sent == total else percent
            job["status"] = "uploading"
            job["updated"] = now

        self._update(change)

    def remember(self, job_id: str, owner_id: int, video_id: int) -> None:
        def change(jobs: list[dict]) -> None:
            job = _find(jobs, job_id)
            if job is None:
                return
            job["owner_id"] = owner_id
            job["video_id"] = video_id
            job["updated"] = time.time()

        self._update(change)

    def set_status(
        self,
        job_id: str,
        status: str,
        *,
        message: str | None = None,
        page_url: str | None = None,
    ) -> None:
        if status not in _STATUSES:
            raise ValueError("Неизвестный статус отправки в VK.")

        def change(jobs: list[dict]) -> None:
            job = _find(jobs, job_id)
            if job is None:
                return
            if job.get("stop") and status in {"uploading", "processing", "done"}:
                job["status"] = "stopped"
                if not str(job.get("message") or "").strip():
                    job["message"] = "Остановлено"
                job["updated"] = time.time()
                return
            job["status"] = status
            if message is not None:
                job["message"] = message
            if page_url is not None:
                job["page_url"] = page_url
            if status == "done":
                job["percent"] = 100
                job["stop"] = False
            job["updated"] = time.time()

        self._update(change)

    def is_stop_requested(self, job_id: str) -> bool:
        with _LOCK, _file_lock(self.path):
            job = _find(self._read(), job_id)
        return bool(job and job.get("stop"))

    def request_stop(self, job_id: str) -> dict | None:
        found: dict | None = None

        def change(jobs: list[dict]) -> None:
            nonlocal found
            job = _find(jobs, job_id)
            if job is None:
                return
            if job.get("status") not in {"done", "error", "stopped"}:
                job["stop"] = True
                job["updated"] = time.time()
            found = dict(job)

        self._update(change)
        return found

    def settle_stops(self, *, wait_seconds: float = 15) -> None:
        """Если остановка не дошла до отправителя, строка не должна висеть вечно."""
        now = time.time()

        def change(jobs: list[dict]) -> None:
            for job in jobs:
                if not job.get("stop"):
                    continue
                if job.get("status") not in {"uploading", "processing"}:
                    continue
                updated = job.get("updated")
                if not isinstance(updated, (int, float)) or isinstance(updated, bool):
                    updated = 0
                if now - float(updated) < wait_seconds:
                    continue
                job["status"] = "stopped"
                if not str(job.get("message") or "").strip():
                    job["message"] = "Остановлено"
                job["updated"] = now

        self._update(change)

    def drop(self, job_id: str) -> dict | None:
        """Убрать строку очереди. Живую отправку без флага остановки не трогает."""
        found: dict | None = None

        def change(jobs: list[dict]) -> None:
            nonlocal found
            job = _find(jobs, job_id)
            if job is None:
                return
            status = job.get("status")
            if status in {"uploading", "processing"} and not job.get("stop"):
                return
            found = dict(job)
            jobs.remove(job)

        self._update(change)
        return found

    def job(self, job_id: str) -> dict | None:
        """Служебные поля, включая номер ролика VK. Для кнопки «Остановить»."""
        with _LOCK, _file_lock(self.path):
            found = _find(self._read(), job_id)
        return dict(found) if found is not None else None

    def jobs(self) -> list[dict]:
        with _LOCK, _file_lock(self.path):
            rows = [_public(job) for job in self._read()]
        rows.sort(key=lambda item: item["updated"], reverse=True)
        return rows

    def _update(self, change: Callable[[list[dict]], None]) -> None:
        with _LOCK, _file_lock(self.path):
            jobs = self._read()
            change(jobs)
            self._write(jobs)

    def _read(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return []
        return [job for job in jobs if isinstance(job, dict) and isinstance(job.get("id"), str)]

    def _write(self, jobs: list[dict]) -> None:
        fresh = _prune(jobs)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"jobs": fresh}, ensure_ascii=False)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)


@contextmanager
def _file_lock(path: Path):
    """Короткий замок на файл, чтобы портал и worker не затёрли друг друга."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    handle: int | None = None
    for _ in range(100):
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink()
                    continue
            except OSError:
                pass
            time.sleep(0.02)
    if handle is None:
        try:
            lock.unlink()
        except OSError:
            pass
        handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    try:
        yield
    finally:
        os.close(handle)
        try:
            lock.unlink()
        except OSError:
            pass


def _find(jobs: list[dict], job_id: str) -> dict | None:
    for job in jobs:
        if job.get("id") == job_id:
            return job
    return None


def _prune(jobs: list[dict]) -> list[dict]:
    cutoff = time.time() - _KEEP_SECONDS
    kept: list[dict] = []
    for job in jobs:
        status = job.get("status")
        updated = job.get("updated")
        if (
            status in {"done", "error", "stopped"}
            and isinstance(updated, (int, float))
            and updated < cutoff
        ):
            continue
        kept.append(job)
    return kept[-30:]


def _public(job: dict) -> dict:
    status = str(job.get("status") or "")
    if job.get("stop") and status in {"uploading", "processing"}:
        status = "stopping"
    percent = job.get("percent")
    if isinstance(percent, bool) or not isinstance(percent, int):
        percent = 0
    updated = job.get("updated")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        updated = 0
    speed = job.get("speed")
    if isinstance(speed, bool) or not isinstance(speed, int) or speed < 0:
        speed = 0
    return {
        "id": job.get("id"),
        "name": str(job.get("name") or ""),
        "title": str(job.get("title") or ""),
        "vk_uuid": str(job.get("vk_uuid") or ""),
        "percent": max(0, min(percent, 100)),
        "status": status,
        "message": str(job.get("message") or ""),
        "page_url": str(job.get("page_url") or ""),
        "updated": updated,
        "speed": speed,
        "eta": _eta(job.get("sent"), job.get("total"), speed),
        "owner_id": _number(job.get("owner_id")),
        "video_id": _number(job.get("video_id")),
    }


def _eta(sent: object, total: object, speed: int) -> str:
    """Сколько ещё отправлять: остаток файла делим на текущую скорость."""
    if (
        isinstance(sent, bool)
        or not isinstance(sent, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or speed < 1
        or sent < 0
        or total <= sent
    ):
        return ""
    seconds = (total - sent + speed - 1) // speed
    text = eta_text(seconds)
    if text == "неизвестно":
        return ""
    return text


def _number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
