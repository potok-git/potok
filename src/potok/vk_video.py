"""Загрузка видео в VK Video: video.save, отправка файла, ожидание обработки."""

from __future__ import annotations

import http.client
import json
import logging
import re
import ssl
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from potok.config import VkSettings
from potok.rss import genre_parts

logger = logging.getLogger(__name__)

_API_ROOT = "https://api.vk.com/method"
_RETRYABLE_API = frozenset({1, 6, 9, 10, 29})
_RETRYABLE_HTTP = frozenset({500, 502, 503, 504})
_HINTS = {
    5: "Токен недействителен. Выпустите новый в настройках приложения VK, право: video.",
    6: "Слишком много запросов в секунду.",
    9: "VK ограничил частоту запросов.",
    10: "Внутренняя ошибка VK.",
    14: "VK запросил капчу. Через API её не пройти, попробуйте позже.",
    15: "Нет доступа. У токена должно быть право video.",
    204: "Нет доступа к этому видео.",
    214: "Нельзя опубликовать на стене. Оставьте vk.wallpost: false.",
    13000: "Загрузка запрещена: у сообщества есть страйки.",
}
_TITLE_LIMIT = 128
_DESCRIPTION_LIMIT = 5000
_ALBUM_TITLE_LIMIT = 128
_MAGNET_TEXT = re.compile(r"magnet:\?[^\s<>\"']+", re.IGNORECASE)


class VkError(Exception):
    """VK не принял запрос или не успел обработать видео."""

    def __init__(self, message: str, *, retryable: bool = False, code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class VkStopped(VkError):
    """Загрузку остановили с портала, пока файл ещё уходил в VK."""


@dataclass(frozen=True)
class SavedVideo:
    upload_url: str
    owner_id: int
    video_id: int
    access_key: str | None
    title: str


@dataclass(frozen=True)
class PublishedVideo:
    owner_id: int
    video_id: int
    access_key: str | None
    player_url: str
    page_url: str


@dataclass(frozen=True)
class VkAlbum:
    """Плейлист VK Video. В API это альбом: video.getAlbums."""

    album_id: int
    title: str


@dataclass(frozen=True)
class VkCatalogItem:
    """Один ролик из video.get: название, описание и ссылка на страницу."""

    owner_id: int
    video_id: int
    title: str
    description: str
    page_url: str
    player_url: str
    duration_seconds: int
    processing: bool
    image_url: str = ""


class MultipartFile:
    """Тело multipart/form-data. Файл читается кусками и не попадает в память целиком."""

    def __init__(self, path: Path, chunk_bytes: int) -> None:
        self.path = path
        self.chunk_bytes = chunk_bytes
        self.file_size = path.stat().st_size
        boundary = uuid.uuid4().hex
        self.content_type = f"multipart/form-data; boundary={boundary}"
        self._head = (
            f"--{boundary}\r\n"
            f"Content-Disposition: {_disposition(path.name)}\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"\r\n"
        ).encode("ascii")
        self._tail = f"\r\n--{boundary}--\r\n".encode("ascii")
        self.content_length = len(self._head) + self.file_size + len(self._tail)

    def iter_parts(self, on_progress: Callable[[int, int], None]) -> Iterator[bytes]:
        yield self._head
        sent = 0
        with self.path.open("rb") as handle:
            while True:
                block = handle.read(self.chunk_bytes)
                if not block:
                    break
                sent += len(block)
                on_progress(sent, self.file_size)
                yield block
        yield self._tail


class VkVideoClient:
    """Один ролик: получить адрес, отправить файл, дождаться ссылки плеера."""

    def __init__(
        self,
        settings: VkSettings,
        token: str,
        *,
        transport: _Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.token = token.strip()
        self.transport = transport or UrllibTransport(settings)
        self.sleep = sleep
        self.now = now
        if not self.token:
            raise VkError("Пустой токен VK.")

    def upload_and_wait(
        self,
        path: Path,
        *,
        title: str,
        description: str = "",
        on_progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_saved: Callable[[SavedVideo], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> PublishedVideo:
        self._check_file(path)
        title = _clip(title, _TITLE_LIMIT, fallback="Видео")
        description = _clip(_public_description(description), _DESCRIPTION_LIMIT, fallback="")
        saved: SavedVideo | None = None
        for attempt in range(1, self.settings.upload_attempts + 1):
            saved = None
            try:
                if should_stop is not None and should_stop():
                    raise VkStopped("Загрузка остановлена.")
                saved = self.save_video(title, description)
                if on_saved is not None:
                    on_saved(saved)
                logger.info(
                    "VK video.save: видео %s_%s, попытка %s из %s.",
                    saved.owner_id,
                    saved.video_id,
                    attempt,
                    self.settings.upload_attempts,
                )
                self.upload_file(
                    saved,
                    path,
                    on_progress=on_progress,
                    should_stop=should_stop,
                )
                break
            except VkError as exc:
                stopped = isinstance(exc, VkStopped) or (should_stop is not None and should_stop())
                if stopped and saved is not None:
                    self._delete_quiet(saved)
                if stopped:
                    if isinstance(exc, VkStopped):
                        raise
                    raise VkStopped("Загрузка остановлена.") from exc
                if saved is not None:
                    logger.error(
                        "Загрузка %s_%s не удалась: %s",
                        saved.owner_id,
                        saved.video_id,
                        exc,
                    )
                if not exc.retryable or attempt >= self.settings.upload_attempts:
                    raise
                pause = min(30.0, 2.0 ** (attempt - 1))
                logger.warning("Повтор загрузки через %.0f с: %s", pause, exc)
                self.sleep(pause)
        if saved is None:
            raise VkError("VK не вернул адрес загрузки.")
        if on_status is not None:
            on_status("processing")
        try:
            player = self.wait_for_player(saved, should_stop=should_stop)
        except VkStopped:
            self._delete_quiet(saved)
            raise
        page = page_url(saved.owner_id, saved.video_id, saved.access_key)
        logger.info("VK плеер: %s", player)
        logger.info("VK страница: %s", page)
        return PublishedVideo(
            owner_id=saved.owner_id,
            video_id=saved.video_id,
            access_key=saved.access_key,
            player_url=player,
            page_url=page,
        )

    def list_videos(self, *, limit: int = 200) -> tuple[VkCatalogItem, ...]:
        """Все ролики владельца токена или сообщества из vk.group_id."""
        limit = max(1, min(limit, 500))
        found: list[VkCatalogItem] = []
        offset = 0
        while len(found) < limit:
            batch = min(200, limit - len(found))
            form = {"count": str(batch), "offset": str(offset)}
            if self.settings.group_id is not None:
                form["owner_id"] = str(-self.settings.group_id)
            payload = self._api("video.get", form)
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, dict):
                    parsed = _catalog_item(item)
                    if parsed is not None:
                        found.append(parsed)
            offset += len(items)
            total = payload.get("count")
            if isinstance(total, int) and not isinstance(total, bool) and offset >= total:
                break
            if len(items) < batch:
                break
        return tuple(found)

    def get_video(self, owner_id: int, video_id: int) -> VkCatalogItem | None:
        """Один ролик по номеру. None — VK его больше не показывает."""
        payload = self._api("video.get", {"videos": f"{owner_id}_{video_id}"})
        items = payload.get("items")
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict):
                parsed = _catalog_item(item)
                if parsed is not None:
                    return parsed
        return None

    def drop_magnet_descriptions(self) -> int:
        """Убрать magnet-ссылки из описаний уже загруженных роликов.

        Название не меняется. Ролики без такой ссылки не трогаются.
        Возвращает, сколько описаний перезаписано.
        """
        changed = 0
        offset = 0
        while True:
            form = {"count": "200", "offset": str(offset)}
            if self.settings.group_id is not None:
                form["owner_id"] = str(-self.settings.group_id)
            payload = self._api("video.get", form)
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                parsed = _catalog_item(item)
                if parsed is None or _MAGNET_TEXT.search(parsed.description) is None:
                    continue
                self.edit_video(
                    parsed.owner_id,
                    parsed.video_id,
                    title=parsed.title,
                    description=_public_description(parsed.description),
                )
                changed += 1
                self.sleep(0.4)
            offset += len(items)
            total = payload.get("count")
            if isinstance(total, int) and not isinstance(total, bool) and offset >= total:
                break
            if len(items) < 200:
                break
        return changed

    def edit_video(self, owner_id: int, video_id: int, *, title: str, description: str) -> None:
        """Поменять название и описание. Метод VK: video.edit."""
        _check_ids(owner_id, video_id)
        self._call(
            "video.edit",
            {
                "owner_id": str(owner_id),
                "video_id": str(video_id),
                "name": _clip(title, _TITLE_LIMIT, fallback="Видео"),
                "desc": _clip(_public_description(description), _DESCRIPTION_LIMIT, fallback=""),
            },
        )

    def place_in_playlists(
        self,
        owner_id: int,
        video_id: int,
        names: tuple[str, ...],
        *,
        access_key: str | None = None,
    ) -> tuple[str, ...]:
        """Положить ролик в плейлисты. Нет плейлиста с таким именем — создать."""
        _check_ids(owner_id, video_id)
        if not names:
            return ()
        albums: dict[str, int] = {}
        for album in self.list_albums(owner_id):
            albums.setdefault(album.title.casefold(), album.album_id)
        placed: list[str] = []
        errors: list[VkError] = []
        for name in names:
            try:
                album_id = albums.get(name.casefold())
                if album_id is None:
                    album_id = self.add_album(owner_id, name)
                    albums[name.casefold()] = album_id
                    logger.info("VK плейлист создан: «%s» (%s).", name, album_id)
                else:
                    logger.info("VK плейлист уже есть: «%s» (%s).", name, album_id)
                self.add_to_album(
                    owner_id,
                    video_id,
                    album_id,
                    access_key=access_key,
                )
                placed.append(name)
            except VkError as exc:
                if _already_in_album(exc):
                    logger.info("Видео уже лежит в плейлисте «%s».", name)
                    placed.append(name)
                    continue
                logger.warning("Плейлист «%s»: %s", name, exc)
                errors.append(exc)
        if not placed and errors:
            raise errors[-1]
        return tuple(placed)

    def list_albums(self, owner_id: int) -> tuple[VkAlbum, ...]:
        """Плейлисты владельца. Для сообщества owner_id отрицательный."""
        if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id == 0:
            raise VkError("owner_id плейлиста VK должен быть числом и не нулём.")
        found: list[VkAlbum] = []
        offset = 0
        while offset < 1000:
            payload = self._api(
                "video.getAlbums",
                {"owner_id": str(owner_id), "count": "100", "offset": str(offset)},
            )
            items = payload.get("items")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if isinstance(item, dict):
                    album = _album(item)
                    if album is not None:
                        found.append(album)
            offset += len(items)
            total = payload.get("count")
            if isinstance(total, int) and not isinstance(total, bool) and offset >= total:
                break
            if len(items) < 100:
                break
        return tuple(found)

    def add_album(self, owner_id: int, title: str) -> int:
        """Создать плейлист. Для сообщества передаётся group_id."""
        form = {"title": _clip(title, _ALBUM_TITLE_LIMIT, fallback="Плейлист")}
        if owner_id < 0:
            form["group_id"] = str(-owner_id)
        return _album_id(self._call("video.addAlbum", form))

    def add_to_album(
        self,
        owner_id: int,
        video_id: int,
        album_id: int,
        *,
        access_key: str | None = None,
    ) -> None:
        """Добавить уже загруженный ролик в плейлист. Метод VK: video.addToAlbum."""
        _check_ids(owner_id, video_id)
        if isinstance(album_id, bool) or not isinstance(album_id, int) or album_id < 1:
            raise VkError("album_id плейлиста VK должен быть положительным числом.")
        form = {
            "target_id": str(owner_id),
            "album_id": str(album_id),
            "owner_id": str(owner_id),
            "video_id": str(video_id),
        }
        if access_key:
            form["access_key"] = access_key
        self._call("video.addToAlbum", form)

    def delete_video(self, owner_id: int, video_id: int) -> None:
        """Удалить ролик. Метод VK: video.delete."""
        _check_ids(owner_id, video_id)
        self._call(
            "video.delete",
            {"owner_id": str(owner_id), "video_id": str(video_id)},
        )

    def save_video(self, title: str, description: str) -> SavedVideo:
        form: dict[str, str] = {
            "name": title,
            "wallpost": "1" if self.settings.wallpost else "0",
            "is_private": "1" if self.settings.is_private else "0",
            "privacy_view": self.settings.privacy_view,
        }
        description = _public_description(description)
        if description:
            form["description"] = description
        if self.settings.group_id is not None:
            form["group_id"] = str(self.settings.group_id)
        payload = self._api("video.save", form)
        upload_url = payload.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
            raise VkError("video.save не вернул https-адрес загрузки.")
        access_key = payload.get("access_key")
        saved = SavedVideo(
            upload_url=upload_url,
            owner_id=_as_int(payload.get("owner_id"), "owner_id"),
            video_id=_as_int(payload.get("video_id"), "video_id"),
            access_key=access_key if isinstance(access_key, str) and access_key else None,
            title=title,
        )
        return self._apply_privacy(saved)

    def _apply_privacy(self, saved: SavedVideo) -> SavedVideo:
        """video.save не всегда применяет приватность, поэтому дублируем её в video.edit."""
        edited = self._call(
            "video.edit",
            {
                "owner_id": str(saved.owner_id),
                "video_id": str(saved.video_id),
                "name": saved.title,
                "privacy_view": self.settings.privacy_view,
            },
        )
        fresh_key = edited.get("access_key") if isinstance(edited, dict) else None
        if isinstance(fresh_key, str) and fresh_key:
            saved = SavedVideo(
                upload_url=saved.upload_url,
                owner_id=saved.owner_id,
                video_id=saved.video_id,
                access_key=fresh_key,
                title=saved.title,
            )
        logger.info(
            "VK приватность %s_%s: %s",
            saved.owner_id,
            saved.video_id,
            self.settings.privacy_view,
        )
        return saved

    def upload_file(
        self,
        saved: SavedVideo,
        path: Path,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        stream = MultipartFile(path, self.settings.chunk_bytes)
        host = urlparse(saved.upload_url).hostname or "vk"
        logger.info(
            "VK: отправляю %s (%s) на %s кусками по %s.",
            path.name,
            _fmt_bytes(stream.file_size),
            host,
            _fmt_bytes(self.settings.chunk_bytes),
        )
        next_mark = 5

        def report(sent: int, total: int) -> None:
            nonlocal next_mark
            if should_stop is not None and should_stop():
                raise VkStopped("Загрузка остановлена.")
            if on_progress is not None:
                on_progress(sent, total)
            percent = sent * 100 // total if total else 100
            if percent < next_mark and sent != total:
                return
            logger.info(
                "VK: отправлено %s из %s (%s%%).",
                _fmt_bytes(sent),
                _fmt_bytes(total),
                percent,
            )
            next_mark = percent + 5

        payload = self.transport.upload(
            saved.upload_url,
            content_type=stream.content_type,
            content_length=stream.content_length,
            chunks=stream.iter_parts(report),
            should_stop=should_stop,
        )
        logger.info("VK: сервер принял файл, видео %s_%s.", saved.owner_id, saved.video_id)
        _check_upload_payload(payload, stream.file_size)
        return payload

    def wait_for_player(
        self,
        saved: SavedVideo,
        should_stop: Callable[[], bool] | None = None,
    ) -> str:
        deadline = self.now() + self.settings.poll_timeout_seconds
        attempt = 0
        unseen = False
        videos = f"{saved.owner_id}_{saved.video_id}"
        if saved.access_key:
            videos = f"{videos}_{saved.access_key}"
        while True:
            if should_stop is not None and should_stop():
                raise VkStopped("Загрузка остановлена.")
            attempt += 1
            item: dict | None = None
            try:
                payload = self._api(
                    "video.get",
                    {
                        "owner_id": str(saved.owner_id),
                        "videos": videos,
                        "count": "1",
                    },
                )
                item = _first_item(payload)
            except VkError as exc:
                if not exc.retryable:
                    raise
                logger.warning("VK video.get, попытка %s: %s", attempt, exc)
            player = _ready_player(item) if item is not None else None
            if player:
                logger.info(
                    "VK обработал %s_%s за %s попыток.",
                    saved.owner_id,
                    saved.video_id,
                    attempt,
                )
                return player
            unseen = item is None
            if item is not None and _is_processing(item):
                state = "ещё обрабатывается"
            elif item is None:
                state = "пока не видно в video.get"
            else:
                state = "плеер ещё не готов"
            if self.now() >= deadline:
                hint = ""
                if unseen:
                    hint = " Проверьте токен и vk.is_private: скрытое видео без ключа не отдаётся."
                raise VkError(
                    f"VK не закончил обработку {saved.owner_id}_{saved.video_id} "
                    f"за {self.settings.poll_timeout_seconds:.0f} с.{hint}"
                )
            logger.info(
                "VK %s_%s %s, попытка %s. Следующая проверка через %.0f с.",
                saved.owner_id,
                saved.video_id,
                state,
                attempt,
                self.settings.poll_interval_seconds,
            )
            self.sleep(self.settings.poll_interval_seconds)

    def _check_file(self, path: Path) -> None:
        if not path.exists():
            raise VkError(f"Файл не найден: {path}")
        if not path.is_file():
            raise VkError(f"Нужен файл, а не папка: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise VkError(f"Файл пустой: {path}")
        if size > self.settings.max_bytes:
            raise VkError(
                f"Файл {_fmt_bytes(size)} больше лимита {_fmt_bytes(self.settings.max_bytes)} "
                "(настройка vk.max_bytes)."
            )

    def _delete_quiet(self, saved: SavedVideo) -> None:
        try:
            self.delete_video(saved.owner_id, saved.video_id)
        except VkError as exc:
            logger.warning(
                "Не удалось удалить незавершённое видео %s_%s: %s",
                saved.owner_id,
                saved.video_id,
                exc,
            )

    def _api(self, method: str, form: dict[str, str]) -> dict:
        response = self._call(method, form)
        if not isinstance(response, dict):
            raise VkError(f"VK {method} вернул ответ без объекта response.")
        return response

    def _call(self, method: str, form: dict[str, str]) -> object:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Potok/0.3",
            "Accept": "application/json",
        }
        body = {**form, "v": self.settings.api_version}
        last = VkError(f"VK {method} не ответил.")
        for attempt in range(1, self.settings.api_retries + 1):
            try:
                payload = self.transport.request_api(method, body, headers)
            except VkError as exc:
                if not exc.retryable or attempt >= self.settings.api_retries:
                    raise
                pause = min(30.0, 2.0 ** (attempt - 1))
                logger.warning("VK %s: %s Повтор через %.0f с.", method, exc, pause)
                self.sleep(pause)
                continue
            error = payload.get("error")
            if not isinstance(error, dict):
                if "response" not in payload:
                    raise VkError(f"VK {method} вернул ответ без поля response.")
                return payload.get("response")
            code = error.get("error_code")
            code_int = code if isinstance(code, int) and not isinstance(code, bool) else None
            message = _api_message(method, code_int, str(error.get("error_msg") or "ошибка"))
            retryable = code_int in _RETRYABLE_API
            last = VkError(message, retryable=retryable, code=code_int)
            if not retryable or attempt >= self.settings.api_retries:
                raise last
            pause = min(30.0, 2.0 ** (attempt - 1))
            logger.warning("VK %s, код %s. Повтор через %.0f с.", method, code_int, pause)
            self.sleep(pause)
        raise last


class _Transport:
    def request_api(self, method: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        raise NotImplementedError

    def upload(
        self,
        url: str,
        *,
        content_type: str,
        content_length: int,
        chunks: Iterable[bytes],
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        del should_stop
        raise NotImplementedError


class UrllibTransport(_Transport):
    def __init__(self, settings: VkSettings) -> None:
        self.settings = settings

    def request_api(self, method: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        if not re.fullmatch(r"video\.[A-Za-z0-9_]+", method):
            raise VkError(f"Неизвестный метод VK: {method}")
        data = urlencode(form).encode("ascii")
        request = Request(f"{_API_ROOT}/{method}", data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.settings.api_timeout_seconds) as response:
                raw = response.read(2_000_000)
        except HTTPError as exc:
            detail = exc.read(2_000).decode("utf-8", errors="replace")
            retryable = exc.code in _RETRYABLE_HTTP or exc.code == 429
            raise VkError(
                f"VK {method}: HTTP {exc.code}. {detail[:180]}",
                retryable=retryable,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise VkError(f"Нет связи с API VK ({method}): {exc}", retryable=True) from exc
        return _decode_json(raw, f"API {method}")

    def upload(
        self,
        url: str,
        *,
        content_type: str,
        content_length: int,
        chunks: Iterable[bytes],
        should_stop: Callable[[], bool] | None = None,
    ) -> dict:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise VkError("Адрес загрузки от VK должен начинаться с https://.")
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=self.settings.upload_idle_timeout_seconds,
            context=context,
        )
        cancelled = threading.Event()

        def watch() -> None:
            while not cancelled.wait(0.4):
                try:
                    if should_stop is not None and should_stop():
                        conn.close()
                        return
                except OSError:
                    return

        threading.Thread(target=watch, name="vk-upload-stop", daemon=True).start()
        try:
            try:
                status, raw = _post_stream(
                    conn,
                    target,
                    content_type,
                    content_length,
                    chunks,
                )
            except VkError:
                raise
            except (TimeoutError, OSError, http.client.HTTPException) as exc:
                if should_stop is not None and should_stop():
                    raise VkStopped("Загрузка остановлена.") from exc
                raise VkError(
                    f"Связь с сервером загрузки VK оборвалась: {exc}",
                    retryable=True,
                ) from exc
        finally:
            cancelled.set()
            conn.close()
        if status in _RETRYABLE_HTTP:
            raise VkError(f"Сервер загрузки VK ответил {status}.", retryable=True)
        if status == 413:
            raise VkError("Сервер VK отклонил файл как слишком большой.")
        if status != 200:
            text = raw.decode("utf-8", errors="replace")[:180]
            raise VkError(f"Сервер загрузки VK ответил {status}. {text}")
        return _decode_json(raw, "сервера загрузки")


def playlist_names(genre: str | None, year: int | None) -> tuple[str, ...]:
    """Имена плейлистов: по одному на жанр, вид «2026 - Комедия»."""
    if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2099:
        return ()
    names: list[str] = []
    seen: set[str] = set()
    for text in genre_parts(genre):
        title = _clip(f"{year} - {text}", _ALBUM_TITLE_LIMIT, fallback="")
        key = title.casefold()
        if not title or key in seen:
            continue
        seen.add(key)
        names.append(title)
    return tuple(names)


def _check_ids(owner_id: int, video_id: int) -> None:
    if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id == 0:
        raise VkError("owner_id видео VK должен быть числом и не нулём.")
    if isinstance(video_id, bool) or not isinstance(video_id, int) or video_id < 1:
        raise VkError("video_id видео VK должен быть положительным числом.")


def _album(item: dict) -> VkAlbum | None:
    raw_id = item.get("id", item.get("album_id"))
    title = item.get("title")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id < 1:
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    return VkAlbum(album_id=raw_id, title=" ".join(title.split()))


def _album_id(response: object) -> int:
    if isinstance(response, int) and not isinstance(response, bool) and response > 0:
        return response
    if isinstance(response, dict):
        for key in ("album_id", "id"):
            value = response.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    raise VkError("video.addAlbum не вернул номер плейлиста.")


def _already_in_album(exc: VkError) -> bool:
    if exc.code == 800:
        return True
    text = str(exc).casefold()
    return "already" in text or "уже" in text


def _catalog_item(item: dict) -> VkCatalogItem | None:
    owner_id = item.get("owner_id")
    video_id = item.get("id")
    if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id == 0:
        return None
    if isinstance(video_id, bool) or not isinstance(video_id, int) or video_id < 1:
        return None
    access = item.get("access_key")
    access_key = access if isinstance(access, str) and access else None
    player = item.get("player")
    player_url = player if isinstance(player, str) and player.startswith("https://") else ""
    title = item.get("title")
    description = item.get("description")
    duration = item.get("duration")
    seconds = duration if isinstance(duration, int) and not isinstance(duration, bool) else 0
    return VkCatalogItem(
        owner_id=owner_id,
        video_id=video_id,
        title=title if isinstance(title, str) else "",
        description=description if isinstance(description, str) else "",
        page_url=page_url(owner_id, video_id, access_key),
        player_url=player_url,
        duration_seconds=max(seconds, 0),
        processing=_is_processing(item),
        image_url=_cover_url(item),
    )


def _cover_url(item: dict) -> str:
    """Самая широкая https-картинка из image, иначе из first_frame."""
    for key in ("image", "first_frame"):
        images = item.get(key)
        if not isinstance(images, list):
            continue
        best_url = ""
        best_width = -1
        for image in images:
            if not isinstance(image, dict):
                continue
            url = image.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            width = image.get("width")
            pixels = width if isinstance(width, int) and not isinstance(width, bool) else 0
            if pixels > best_width:
                best_url = url
                best_width = pixels
        if best_url:
            return best_url
    return ""


def page_url(owner_id: int, video_id: int, access_key: str | None) -> str:
    base = f"https://vk.com/video{owner_id}_{video_id}"
    if access_key:
        return f"{base}?access_key={quote(access_key, safe='')}"
    return base


def _post_stream(
    conn: http.client.HTTPConnection,
    target: str,
    content_type: str,
    content_length: int,
    chunks: Iterable[bytes],
) -> tuple[int, bytes]:
    conn.putrequest("POST", target)
    conn.putheader("Content-Type", content_type)
    conn.putheader("Content-Length", str(content_length))
    conn.putheader("Connection", "close")
    conn.putheader("User-Agent", "Potok/0.3")
    conn.endheaders()
    for chunk in chunks:
        _send_all(conn, chunk)
    response = conn.getresponse()
    try:
        return response.status, response.read(2_000_000)
    finally:
        response.close()


def _send_all(conn: http.client.HTTPConnection, data: bytes) -> None:
    """Отправить все байты. http.client.send в Python 3.13 возвращает None и шлёт всё сразу."""
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        written = conn.send(view[sent:])
        if written is None:
            return
        if written == 0:
            raise VkError("Соединение с сервером VK оборвалось.", retryable=True)
        sent += written


def _check_upload_payload(payload: dict, file_size: int) -> None:
    error = payload.get("error")
    if isinstance(error, str) and error:
        raise VkError(f"Сервер загрузки VK: {error}")
    if isinstance(error, dict):
        raise VkError(f"Сервер загрузки VK: {error.get('error_msg') or error}")
    if "error_code" in payload and "size" not in payload:
        raise VkError(
            f"Сервер загрузки VK: {payload.get('error_msg') or payload.get('error_code')}"
        )
    if "size" not in payload:
        logger.warning("Ответ сервера загрузки без поля size. Жду обработку.")
        return
    remote = payload.get("size")
    if isinstance(remote, bool) or not isinstance(remote, int):
        raise VkError("Сервер загрузки VK вернул нечисловой size.")
    if remote != file_size:
        raise VkError(
            f"VK сохранил {_fmt_bytes(remote)} из {_fmt_bytes(file_size)}. "
            "Файл загрузился не целиком.",
            retryable=True,
        )


def _decode_json(raw: bytes, source: str) -> dict:
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    if not text:
        raise VkError(f"Пустой ответ {source}.", retryable=True)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VkError(f"Ответ {source} не JSON: {text[:180]}") from exc
    if not isinstance(payload, dict):
        raise VkError(f"Ответ {source} должен быть объектом JSON.")
    return payload


def _api_message(method: str, code: int | None, msg: str) -> str:
    hint = _HINTS.get(code or 0, "")
    prefix = f"VK {method}"
    if code is None:
        text = f"{prefix}: {msg}"
    else:
        text = f"{prefix}: код {code}, {msg}"
    if hint:
        return f"{text} {hint}"
    return text


def _as_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VkError(f"VK не прислал число {field}.")
    return value


def _first_item(payload: dict) -> dict | None:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    return first


def _is_processing(item: dict) -> bool:
    for key in ("processing", "converting"):
        if item.get(key) in (1, True, "1"):
            return True
    return False


def _ready_player(item: dict) -> str | None:
    if _is_processing(item):
        return None
    player = item.get("player")
    if isinstance(player, str) and player.startswith(("http://", "https://")):
        return player
    return None


def _disposition(name: str) -> str:
    original = Path(name).name or "video.mp4"
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._") or "video.mp4"
    encoded = quote(original, safe="")
    return f'form-data; name="video_file"; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'


def _public_description(text: str) -> str:
    """Текст описания для VK без magnet-ссылки."""
    cleaned = _MAGNET_TEXT.sub("", text)
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


def _clip(value: str, limit: int, *, fallback: str) -> str:
    text = " ".join(value.split())
    if not text:
        return fallback
    return text[:limit]


def _fmt_bytes(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} ГБ"
    if size >= 1024**2:
        return f"{size / 1024**2:.1f} МБ"
    if size >= 1024:
        return f"{size / 1024:.1f} КБ"
    return f"{size} Б"
