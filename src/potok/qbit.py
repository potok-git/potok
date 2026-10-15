"""Клиент qBittorrent: список раздач, пауза, продолжение и удаление."""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from potok.config import StreamingSettings
from potok.stream import StreamError

logger = logging.getLogger(__name__)

_HASH_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_MAX_BODY = 2_000_000
_UNKNOWN_ETA = 8_640_000


class QbitClient:
    """Говорит с WebAPI qBittorrent. Пароль берёт из окружения, если он задан."""

    def __init__(self, settings: StreamingSettings, *, opener=None) -> None:
        self.settings = settings
        self._opener = opener or urlopen
        self._sid = ""

    def list_torrents(self) -> list[dict]:
        payload = self._json("GET", "/api/v2/torrents/info")
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise StreamError("qBittorrent вернул список в неожиданном виде.")
        return [item for item in payload if isinstance(item, dict)]

    def torrent_files(self, infohash: str) -> list[dict]:
        """Файлы одной раздачи. progress от 0 до 1, name — путь внутри папки загрузки."""
        digest = _hash(infohash)
        payload = self._json("GET", f"/api/v2/torrents/files?hash={digest}")
        if not isinstance(payload, list):
            raise StreamError("qBittorrent не отдал список файлов раздачи.")
        return [item for item in payload if isinstance(item, dict)]

    def add_torrent_file(self, filename: str, content: bytes, *, title: str = "") -> dict:
        if not content or len(content) > _MAX_BODY:
            raise StreamError("Файл .torrent пустой или больше 2 МБ.")
        fields = [("paused", "false"), ("stopped", "false")]
        if title.strip():
            fields.append(("rename", " ".join(title.split())[:200]))
        body, content_type = _multipart(fields, "torrents", filename, content)
        self._request("POST", "/api/v2/torrents/add", body, content_type)
        logger.info("Торрент «%s» добавлен в qBittorrent.", title or filename)
        return {"ok": True, "title": title or filename}

    def add_magnet(self, link: str, *, title: str = "") -> dict:
        clean = link.strip()
        if not clean.lower().startswith("magnet:?"):
            raise StreamError("В qBittorrent можно отправить только magnet-ссылку.")
        fields = {"urls": clean, "paused": "false", "stopped": "false"}
        if title.strip():
            fields["rename"] = " ".join(title.split())[:200]
        self._form("/api/v2/torrents/add", fields)
        logger.info("Magnet «%s» добавлен в qBittorrent.", title or "раздача")
        return {"ok": True, "title": title or "раздача"}

    def stop(self, infohash: str) -> None:
        self._hashes("/api/v2/torrents/stop", infohash, fallback="/api/v2/torrents/pause")

    def stop_all(self) -> int:
        """Пауза всем раздачам. hashes=all; на старых версиях — pause."""
        count = len(self.list_torrents())
        try:
            self._form("/api/v2/torrents/stop", {"hashes": "all"})
        except StreamError as exc:
            if "404" not in str(exc):
                raise
            self._form("/api/v2/torrents/pause", {"hashes": "all"})
        return count

    def start(self, infohash: str) -> None:
        self._hashes("/api/v2/torrents/start", infohash, fallback="/api/v2/torrents/resume")

    def remove(self, infohash: str, *, delete_files: bool = False) -> None:
        """Убрать раздачу. delete_files=True стирает и скачанные файлы в qBittorrent."""
        digest = _hash(infohash)
        flag = "true" if delete_files else "false"
        self._form("/api/v2/torrents/delete", {"hashes": digest, "deleteFiles": flag})

    def move_in_queue(self, infohash: str, direction: str) -> bool:
        """Поднять или опустить раздачу. True — очередь qBittorrent до этого была выключена."""
        if direction not in {"up", "down"}:
            raise StreamError("Порядок можно только поднять или опустить.")
        digest = _hash(infohash)
        enabled = False
        current = self._torrent(digest)
        priority = current.get("priority") if current else None
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            self._form(
                "/api/v2/app/setPreferences",
                {"json": json.dumps({"queueing_enabled": True})},
            )
            enabled = True
        path = "/api/v2/torrents/increasePrio"
        if direction == "down":
            path = "/api/v2/torrents/decreasePrio"
        self._form(path, {"hashes": digest})
        return enabled

    def _torrent(self, digest: str) -> dict | None:
        for item in self.list_torrents():
            if str(item.get("hash") or "").lower() == digest:
                return item
        return None

    def _hashes(self, path: str, infohash: str, *, fallback: str) -> None:
        digest = _hash(infohash)
        try:
            self._form(path, {"hashes": digest})
        except StreamError as exc:
            if "404" not in str(exc):
                raise
            self._form(fallback, {"hashes": digest})

    def _form(self, path: str, fields: dict[str, str]) -> None:
        self._request(
            "POST",
            path,
            urlencode(fields).encode("utf-8"),
            "application/x-www-form-urlencoded",
        )

    def _json(self, method: str, path: str) -> object:
        raw = self._request(method, path)
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise StreamError("qBittorrent вернул не JSON.") from exc

    def _request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        content_type: str | None = None,
        *,
        allow_login: bool = True,
    ) -> bytes:
        url = self.settings.base_url.rstrip("/") + path
        request = Request(url, data=data, method=method)
        request.add_header("Referer", self.settings.base_url.rstrip("/") + "/")
        if self._sid:
            request.add_header("Cookie", f"SID={self._sid}")
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with self._opener(request, timeout=self.settings.timeout_seconds) as response:
                self._remember_cookie(response)
                return _read(response)
        except HTTPError as exc:
            if exc.code in {401, 403} and allow_login and self._login():
                return self._request(method, path, data, content_type, allow_login=False)
            if exc.code == 409 and path.endswith("/torrents/add"):
                logger.info("Раздача уже есть в qBittorrent, повторно не добавляю.")
                return b"Ok."
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise StreamError(f"qBittorrent ответил {exc.code}. {detail[:180]}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise StreamError(f"Нет связи с qBittorrent: {exc}") from exc

    def _login(self) -> bool:
        user = os.environ.get(self.settings.username_env, "").strip()
        password = os.environ.get(self.settings.password_env, "").strip()
        if not user or not password:
            return False
        raw = self._request(
            "POST",
            "/api/v2/auth/login",
            urlencode({"username": user, "password": password}).encode("utf-8"),
            "application/x-www-form-urlencoded",
            allow_login=False,
        )
        if raw.strip() not in {b"Ok.", b"Ok"}:
            raise StreamError("qBittorrent не принял логин или пароль.")
        return bool(self._sid)

    def _remember_cookie(self, response: object) -> None:
        headers = getattr(response, "headers", None)
        if headers is None:
            return
        cookie = headers.get("Set-Cookie") or ""
        marker = "SID="
        if marker not in cookie:
            return
        self._sid = cookie.split(marker, 1)[1].split(";", 1)[0].strip()


def _hash(infohash: str) -> str:
    text = infohash.strip().lower()
    if not _HASH_RE.fullmatch(text):
        raise StreamError("Hash должен быть 40 шестнадцатеричных знаков.")
    return text


def _read(response: object) -> bytes:
    read = getattr(response, "read", None)
    if read is None:
        return b""
    return read(_MAX_BODY)


def _multipart(
    fields: list[tuple[str, str]],
    file_field: str,
    filename: str,
    content: bytes,
) -> tuple[bytes, str]:
    boundary = "----potokqbit"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        "Content-Type: application/x-bittorrent\r\n\r\n"
    )
    chunks.append(header.encode() + content + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def eta_text(seconds: object) -> str:
    """Сколько ещё качать. qBittorrent ставит очень большое число, если срок неизвестен."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        return "неизвестно"
    if seconds < 0 or seconds >= _UNKNOWN_ETA:
        return "неизвестно"
    minutes, _rest = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"
