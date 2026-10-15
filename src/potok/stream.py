"""Клиент TorrServer: библиотека, настройки и ссылки на просмотр."""

from __future__ import annotations

import json
import logging
import os
import re
from base64 import b64encode
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from potok.config import AppConfig, StreamingSettings

logger = logging.getLogger(__name__)

_HASH_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_MAGNET_RE = re.compile(
    r"^magnet:\?xt=urn:btih:([a-fA-F0-9]{40}|[A-Za-z2-7]{32})(&[A-Za-z0-9._~%=:+-]*)?$",
    re.IGNORECASE,
)
_TORRENT_BAD = re.compile(r'[\x00-\x1f\\/:"*?<>|]')
_ACTIVE_STATS = frozenset({2, 3})
_STAT_NAMES = {
    0: "added",
    1: "getting_info",
    2: "preload",
    3: "working",
    4: "closed",
    5: "in_database",
}
_INT_SETTINGS = {
    "cacheSize": (8 * 1024 * 1024, 8 * 1024**3),
    "connectionsLimit": (10, 5000),
    "downloadRateLimit": (0, 1_000_000),
    "uploadRateLimit": (0, 1_000_000),
    "readerReadAHead": (5, 100),
    "preloadCache": (0, 100),
    "peersListenPort": (0, 65535),
}
_BOOL_SETTINGS = frozenset(
    {
        "useDisk",
        "removeCacheOnDrop",
        "enableDLNA",
        "disableUpload",
        "responsiveMode",
        "enableDebug",
    }
)
_MAX_TORRENT_BYTES = 2_000_000
_MAX_BODY = 2_000_000


class StreamError(Exception):
    """TorrServer не ответил или отклонил запрос."""


def validate_source_link(link: str) -> str:
    """Оставить только magnet, http(s) или infohash. Путь к файлу снаружи не принимаем."""
    text = link.strip()
    if _HASH_RE.fullmatch(text) or _MAGNET_RE.fullmatch(text):
        return text
    parsed = urlparse(text)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and len(text) <= 2000
    ):
        return text
    raise StreamError("Источник должен быть magnet-ссылкой, адресом http(s) или infohash.")


def torrent_basename(raw: str) -> str | None:
    """Имя файла .torrent без папки. Скобки и запятые из названия Rutracker остаются."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name.lower().endswith(".torrent"):
        return None
    if name.startswith(".") or len(name) > 200:
        return None
    if _TORRENT_BAD.search(name):
        return None
    return name


def validate_settings_patch(patch: object) -> dict:
    """Проверить поля, которые портал имеет право менять у TorrServer."""
    if not isinstance(patch, dict) or not patch:
        raise StreamError("Передайте непустой словарь настроек.")
    cleaned: dict = {}
    for key, value in patch.items():
        if key in _INT_SETTINGS:
            low, high = _INT_SETTINGS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise StreamError(f"{key} должен быть целым числом от {low} до {high}.")
            cleaned[key] = value
            continue
        if key in _BOOL_SETTINGS:
            if not isinstance(value, bool):
                raise StreamError(f"{key} должен быть true или false.")
            cleaned[key] = value
            continue
        if key == "torrentsSavePath":
            cleaned[key] = _save_path(value)
            continue
        raise StreamError(f"Поле {key} менять через портал нельзя.")
    return cleaned


def playback_links(public_url: str, item: dict) -> dict:
    """Ссылки плеера. HLS — m3u8 через GStreamer, поток — прямой HTTP TorrServer."""
    infohash = str(item.get("hash") or "").lower()
    title = str(item.get("title") or item.get("name") or infohash or "video")
    file_id = _file_id(item)
    base = public_url.rstrip("/")
    name = quote(title, safe="")
    return {
        "hash": infohash,
        "title": title,
        "stat": item.get("stat"),
        "stat_name": _stat_name(item),
        "download_speed": item.get("download_speed") or 0,
        "upload_speed": item.get("upload_speed") or 0,
        "size": item.get("torrent_size") or 0,
        "hls_url": f"{base}/gst/{infohash}/master.m3u8?index={file_id}",
        "stream_url": f"{base}/stream/{name}?link={infohash}&index={file_id}&play",
        "playlist_url": f"{base}/playlist?hash={infohash}",
        "player_url": f"{base}/play/{infohash}/{file_id}",
    }


def is_active(item: dict) -> bool:
    stat = item.get("stat")
    if stat in _ACTIVE_STATS:
        return True
    return str(item.get("stat_string") or "") in {"TorrentPreload", "TorrentWorking"}


class StreamClient:
    """Говорит с TorrServer по HTTP. Секреты в журнал не пишет."""

    def __init__(self, settings: StreamingSettings, *, opener=None) -> None:
        self.settings = settings
        self._opener = opener or urlopen

    def add_source(self, link: str, *, title: str = "") -> dict:
        clean = validate_source_link(link)
        payload = self._post(
            "/torrents",
            {
                "action": "add",
                "link": clean,
                "title": _one_line(title),
                "save_to_db": True,
            },
        )
        return payload if isinstance(payload, dict) else {"ok": True}

    def add_torrent_file(self, filename: str, content: bytes, *, title: str = "") -> dict:
        if not content or len(content) > _MAX_TORRENT_BYTES:
            raise StreamError("Файл .torrent пустой или больше 2 МБ.")
        safe_name = torrent_basename(filename) or "item.torrent"
        body, content_type = _multipart(
            [("title", _one_line(title)), ("save", "true")],
            "file",
            safe_name,
            content,
        )
        payload = self._request("POST", "/torrent/upload", body, content_type)
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        if isinstance(payload, dict):
            return payload
        return {"ok": True}

    def remove_source(self, infohash: str) -> None:
        if not _HASH_RE.fullmatch(infohash):
            raise StreamError("Hash должен быть 40 шестнадцатеричных знаков.")
        self._post("/torrents", {"action": "rem", "hash": infohash.lower()})

    def list_sources(self) -> list[dict]:
        payload = self._post("/torrents", {"action": "list"})
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise StreamError("TorrServer вернул список в неожиданном виде.")
        return [item for item in payload if isinstance(item, dict)]

    def active_sources(self) -> list[dict]:
        return [item for item in self.list_sources() if is_active(item)]

    def get_settings(self) -> dict:
        payload = self._post("/settings", {"action": "get"})
        if not isinstance(payload, dict):
            raise StreamError("TorrServer не вернул настройки.")
        return payload

    def update_settings(self, patch: dict) -> dict:
        """Сначала читаем настройки, потом меняем только проверенные поля и записываем целиком."""
        cleaned = validate_settings_patch(patch)
        current = self.get_settings()
        current.update(cleaned)
        self._post("/settings", {"action": "set", "sets": current})
        return current

    def _post(self, path: str, payload: dict) -> object:
        body = json.dumps(payload).encode("utf-8")
        return self._request("POST", path, body, "application/json")

    def _request(self, method: str, path: str, body: bytes, content_type: str) -> object:
        url = self.settings.base_url.rstrip("/") + path
        request = Request(url, data=body, method=method)
        request.add_header("Content-Type", content_type)
        request.add_header("Accept", "application/json")
        user = os.environ.get(self.settings.username_env, "").strip()
        password = os.environ.get(self.settings.password_env, "").strip()
        if user and password:
            token = b64encode(f"{user}:{password}".encode()).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with self._opener(request, timeout=self.settings.timeout_seconds) as response:
                raw = response.read(_MAX_BODY)
        except HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise StreamError(f"TorrServer ответил {exc.code}. {detail[:180]}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise StreamError(f"Нет связи с TorrServer: {exc}") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise StreamError("TorrServer вернул не JSON.") from exc


def register_downloaded(config: AppConfig, torrent: Path, title: str) -> dict | None:
    """Положить .torrent в qBittorrent. Выключенный клиент ничего не делает."""
    from potok.qbit import QbitClient

    settings = config.streaming
    if not settings.enabled or not settings.auto_add:
        return None
    if not torrent.is_file():
        raise StreamError(f"Файл для qBittorrent не найден: {torrent.name}")
    content = torrent.read_bytes()
    return QbitClient(settings).add_torrent_file(torrent.name, content, title=title)


def register_magnet(config: AppConfig, link: str, title: str) -> dict | None:
    """Поставить magnet в qBittorrent. Выключенный клиент ничего не делает."""
    from potok.qbit import QbitClient
    from potok.rss import magnet_link

    settings = config.streaming
    if not settings.enabled or not settings.auto_add:
        return None
    clean = magnet_link(link)
    if clean is None:
        raise StreamError("Это не magnet-ссылка.")
    return QbitClient(settings).add_magnet(clean, title=title)


def _file_id(item: dict) -> int:
    files = item.get("file_stats")
    if isinstance(files, list) and files and isinstance(files[0], dict):
        file_id = files[0].get("id")
        if isinstance(file_id, int) and file_id > 0:
            return file_id
    return 1


def _stat_name(item: dict) -> str:
    stat = item.get("stat")
    if isinstance(stat, int):
        return _STAT_NAMES.get(stat, "unknown")
    text = str(item.get("stat_string") or "").strip()
    return text or "unknown"


def _save_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StreamError("torrentsSavePath должен быть путём внутри контейнера.")
    text = value.strip()
    if not text.startswith("/opt/ts/") or ".." in text.split("/"):
        raise StreamError("torrentsSavePath должен начинаться с /opt/ts/.")
    return text


def _one_line(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:200]


def _multipart(
    fields: list[tuple[str, str]],
    file_field: str,
    filename: str,
    content: bytes,
) -> tuple[bytes, str]:
    boundary = "----potokstream"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            "Content-Type: application/x-bittorrent\r\n\r\n"
        ).encode()
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
