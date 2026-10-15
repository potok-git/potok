"""После aria2c: загрузить видео в VK и отправить ссылку плеера в Telegram."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from potok.config import AppConfig, ConfigError
from potok.pipeline import torrent_path
from potok.rss import genres_from_title
from potok.store import SeenStore, StoredItem
from potok.stream import StreamError, register_downloaded, register_magnet
from potok.titles import is_manual_video, release_title, source_name
from potok.tmdb import MovieInfo, TmdbError, movie_query, search_movie
from potok.vk_uploads import UploadBoard, UploadOrder, board_path, order_path
from potok.vk_video import PublishedVideo, VkError, VkStopped, VkVideoClient, playlist_names

logger = logging.getLogger(__name__)

_VIDEO_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".wmv",
    ".ts",
    ".m2ts",
}
_SKIP_SUFFIXES = {".aria2", ".part", ".torrent"}
_YEAR_TOKEN = re.compile(r"(?:19|20)\d{2}")
_PLAYLIST_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)


class DownloadError(Exception):
    """aria2c не скачал видео."""


class NotifyError(Exception):
    """Telegram не принял сообщение. Ссылка плеера при этом уже может быть готова."""

    def __init__(self, message: str, *, player_urls: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.player_urls = player_urls


@dataclass(frozen=True)
class PublishResult:
    player_urls: tuple[str, ...]


def deliver_and_notify(
    config: AppConfig,
    source: Path,
    *,
    title: str,
    description: str = "",
    row_id: int | None = None,
    run_aria2: Callable[[list[str], float | None], None] | None = None,
    upload=None,
    notify=None,
) -> PublishResult:
    """Скачать раздачу через aria2c, загрузить ролики в VK и написать в Telegram.

    source — путь к .torrent или к уже скачанному видео. Для готового видео
    aria2c не запускается.
    """
    token, chat_id = _telegram(config)
    if source.suffix.lower() == ".torrent":
        result = publish_torrent(
            config,
            source,
            title=title,
            description=description,
            row_id=row_id,
            run_aria2=run_aria2,
            upload=upload,
        )
    else:
        result = publish_file(
            config,
            source,
            title=title,
            description=description,
            row_id=row_id,
            upload=upload,
        )
    movie = _lookup_movie(config, title)
    stored = _stored_row(config, row_id) or _find_stored(config, title)
    from potok.bot import deliver_notice, uploaded_view

    text, poster, markup = uploaded_view(
        title=title,
        description=description,
        stored=stored,
        movie=movie,
        urls=result.player_urls,
    )
    timeout = config.telegram.timeout_seconds

    def default_notify(body: str) -> None:
        deliver_notice(token, chat_id, body, poster, timeout, reply_markup=markup)

    sender = notify or default_notify
    try:
        sender(text)
    except NotifyError as exc:
        logger.error(
            "Ссылка VK готова, но Telegram её не принял: %s",
            " ".join(result.player_urls),
        )
        raise NotifyError(str(exc), player_urls=result.player_urls) from exc
    logger.info("Ссылка отправлена в Telegram.")
    return result


_COMPLETE_STATES = frozenset(
    {
        "uploading",
        "stalledUP",
        "pausedUP",
        "queuedUP",
        "forcedUP",
        "stoppedUP",
    }
)
_ACTIVE_UPLOADS = frozenset({"uploading", "processing", "stopping"})
_HOLD_SECONDS = 60.0
_SEEDING_STATES = frozenset({"uploading", "stalledUP", "queuedUP", "forcedUP"})
SEED_AFTER_COMPLETE_SECONDS = 30 * 60
KEEP_DOWNLOAD_SECONDS = 10 * 24 * 60 * 60


def publish_finished(config: AppConfig, *, client=None, upload=None) -> int:
    """Отправить в VK видео из раздач, которые qBittorrent уже докачал.

    После скачивания раздача ещё 30 минут раздаётся, потом встаёт на паузу
    и только тогда уходит в VK. За один вызов берётся одна раздача. Пока на
    VK что-то уходит, новая отправка не стартует. Уже отправленные файлы
    повторно не берутся. Загрузка старше 10 дней удаляется с диска, если
    видео уже успешно лежит в VK.
    """
    if not config.streaming.enabled or not vk_ready(config):
        return 0
    board = UploadBoard(board_path(config.database_path))
    qbit = client if client is not None else _qbit_client(config)
    try:
        torrents = qbit.list_torrents()
    except StreamError as exc:
        logger.warning("Не вижу список qBittorrent: %s", exc)
        return 0
    root = config.delivery.download_dir
    sent, hold, _playlists, completed = _load_sent(config)
    _absorb_finished_names(board, sent)
    seen = _load_seen(config)
    torrents = _in_vk_order(config, torrents, seen)
    uploaded = 0

    def persist() -> None:
        _save_sent(config, sent, hold, completed=completed)

    try:
        if _board_busy(board):
            return 0
        uploaded = _publish_ready(
            config,
            qbit,
            torrents,
            board,
            root,
            sent,
            hold,
            seen,
            completed,
            upload,
            persist,
        )
        persist()
        return uploaded
    finally:
        _purge_old_downloads(config, qbit, torrents, sent, time.time())


def _publish_ready(
    config: AppConfig,
    qbit,
    torrents: list[dict],
    board: UploadBoard,
    root: Path,
    sent: set[str],
    hold: dict[str, float],
    seen: list[StoredItem],
    completed: dict[str, float],
    upload,
    persist: Callable[[], None],
) -> int:
    uploaded = 0
    for torrent in torrents:
        if not _torrent_complete(torrent):
            continue
        infohash = str(torrent.get("hash") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{40}", infohash):
            continue
        title = str(torrent.get("name") or infohash)
        action = _seed_action(torrent, completed, time.time())
        if action == "remember":
            persist()
            logger.info("«%s» докачалась, раздаю ещё 30 минут.", title)
            continue
        if action == "wait":
            continue
        if action == "pause":
            try:
                qbit.stop(infohash)
            except StreamError as exc:
                logger.warning("Не поставил «%s» на паузу: %s", title, exc)
                continue
            logger.info("«%s» раздавалась 30 минут. Ставлю на паузу и отдаю в VK.", title)
        try:
            files = qbit.torrent_files(infohash)
        except StreamError as exc:
            logger.warning("Файлы «%s» не прочитались: %s", title, exc)
            continue
        videos = _finished_videos(root, files)
        if not videos:
            marker = f"empty:{infohash}"
            if marker not in sent:
                logger.info("В раздаче «%s» нет готового видео, в VK не отправляю.", title)
                _mark_sent(sent, hold, marker)
                persist()
            continue
        series = _playlist_for_files(config, infohash, files)
        row = _row_for_hash(infohash, seen)
        if row is not None and row.vk_url and not series:
            for path in videos:
                _mark_sent(sent, hold, _relative(root, path))
            persist()
            continue
        pending = [
            path
            for path in videos
            if _relative(root, path) not in sent
            and f"name:{path.name}" not in sent
            and not is_manual_video(config.rss.metadata_dir, path.name)
        ]
        if series:
            pending.sort(key=lambda path: path.as_posix().casefold())
        if not pending:
            continue
        film_title = row.title if row is not None else title
        description = row.link if row is not None else ""
        row_id = row.row_id if row is not None else None
        attempted = False
        for path in pending:
            if _board_busy(board):
                persist()
                return uploaded
            now = time.time()
            key = _relative(root, path)
            if hold.get(key, 0) > now:
                continue
            if not attempted:
                logger.info("Раздача «%s» докачалась, отправляю в VK.", film_title)
            attempted = True
            try:
                result = publish_file(
                    config,
                    path,
                    title=film_title,
                    description=description,
                    row_id=row_id,
                    upload=upload,
                    series_playlist=series,
                    resolve_series=False,
                )
            except VkStopped:
                logger.info("Отправка «%s» остановлена, повторно сам не начну.", path.name)
                _mark_sent(sent, hold, key)
                _mark_sent(sent, hold, f"name:{path.name}")
                persist()
                return uploaded
            except (VkError, ConfigError) as exc:
                logger.error("Не отправил «%s» в VK: %s", path.name, exc)
                hold[key] = now + _HOLD_SECONDS
                persist()
                return uploaded
            _mark_sent(sent, hold, key)
            _mark_sent(sent, hold, f"name:{path.name}")
            persist()
            uploaded += 1
            _notify_vk(config, film_title, description, result, stored=row)
        if attempted:
            return uploaded
    return uploaded


def publish_pending(config: AppConfig) -> int:
    """Догрузить одобренные записи, у которых ещё нет ссылки VK."""
    with SeenStore(config.database_path) as store:
        items = store.list_approved_without_vk()
    if not items:
        logger.info("Нет одобренных записей без ссылки VK.")
        return 0
    failures = 0
    for item in items:
        if not item.torrent_url:
            continue
        path = torrent_path(config.rss.metadata_dir, item.torrent_url)
        if not path.is_file():
            failures += 1
            logger.error("Нет файла %s для «%s». Запустите fetch ещё раз.", path.name, item.title)
            continue
        try:
            deliver_and_notify(
                config,
                path,
                title=item.title,
                description=item.link,
                row_id=item.row_id,
            )
        except (DownloadError, VkError, NotifyError, ConfigError) as exc:
            failures += 1
            logger.error("%s: %s", item.title, exc)
    return failures


def publish_torrent(
    config: AppConfig,
    torrent: Path,
    *,
    title: str,
    description: str = "",
    row_id: int | None = None,
    run_aria2: Callable[[list[str], float | None], None] | None = None,
    upload=None,
) -> PublishResult:
    existing = _stored_urls(config, row_id)
    if existing:
        logger.info("У записи уже есть ссылка VK, повторно не загружаю.")
        return PublishResult(player_urls=existing)
    if not torrent.is_file():
        raise DownloadError(f"Файл не найден: {torrent}")
    _require_vk(config)
    directory = config.delivery.download_dir
    directory.mkdir(parents=True, exist_ok=True)
    videos = download_with_aria2(
        torrent,
        directory,
        binary=config.delivery.aria2_bin,
        timeout=_aria_timeout(config),
        run_aria2=run_aria2,
    )
    _remember_stream(config, torrent, title)
    return _upload_videos(
        config,
        videos,
        title=title,
        description=description,
        row_id=row_id,
        upload=upload,
    )


def publish_file(
    config: AppConfig,
    path: Path,
    *,
    title: str,
    description: str = "",
    row_id: int | None = None,
    upload=None,
    series_playlist: str | None = None,
    resolve_series: bool = True,
) -> PublishResult:
    code = series_playlist
    if not code and resolve_series:
        code = _series_playlist_for_filename(config, path.name)
    existing = _stored_urls(config, row_id)
    if existing and not code:
        logger.info("У записи уже есть ссылка VK, повторно не загружаю.")
        return PublishResult(player_urls=existing)
    _require_vk(config)
    return _upload_videos(
        config,
        (path,),
        title=title,
        description=description,
        row_id=row_id,
        upload=upload,
        series_playlist=code,
    )


def download_with_aria2(
    source: Path,
    directory: Path,
    *,
    binary: str,
    timeout: float | None,
    run_aria2: Callable[[list[str], float | None], None] | None = None,
) -> tuple[Path, ...]:
    """Скачать торрент или адрес. Вернуть новые видеофайлы, крупные первыми."""
    directory.mkdir(parents=True, exist_ok=True)
    before = _snapshot(directory)
    command = build_aria2_command(binary, str(source), directory)
    logger.info("Скачиваю через aria2c в %s", directory)
    runner = run_aria2 or _run_aria2
    runner(command, timeout)
    changed = _changed_files(before, _snapshot(directory))
    if not changed:
        raise DownloadError("aria2c завершился, но новых файлов нет.")
    selected = _select_videos(changed)
    logger.info("aria2c скачал файлов: %s, к загрузке в VK: %s.", len(changed), len(selected))
    return tuple(selected)


def build_aria2_command(binary: str, source: str, directory: Path) -> list[str]:
    return [
        binary,
        f"--dir={directory}",
        "--seed-time=0",
        "--summary-interval=30",
        "--console-log-level=notice",
        "--continue=true",
        "--max-tries=5",
        "--retry-wait=5",
        "--allow-overwrite=false",
        source,
    ]


def send_vk_card(
    config: AppConfig,
    *,
    title: str,
    description: str,
    page_url: str,
    stored: StoredItem | None = None,
) -> None:
    """Карточка в Telegram: постер, те же поля, что у очереди, и кнопка «Смотреть во VK»."""
    token, chat_id = _telegram(config)
    movie = _lookup_movie(config, title)
    row = stored or _find_stored(config, title)
    from potok.bot import deliver_notice, uploaded_view

    link = page_url.strip()
    text, poster, markup = uploaded_view(
        title=title,
        description=description,
        stored=row,
        movie=movie,
        urls=(link,) if link else (),
    )
    deliver_notice(
        token,
        chat_id,
        text,
        poster,
        config.telegram.timeout_seconds,
        reply_markup=markup,
    )


def message_text(title: str, player_urls: tuple[str, ...]) -> str:
    lines = [title, *player_urls]
    text = "\n".join(lines)
    if len(text) <= 4000:
        return text
    return text[:3990] + "…"


def send_telegram(token: str, chat_id: str, text: str, timeout: float) -> None:
    """Отправить текст методом sendMessage. Адрес с токеном в журнал не пишется."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = Request(url, data=data, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_000)
    except HTTPError as exc:
        detail = exc.read(1_000).decode("utf-8", errors="replace")
        raise NotifyError(f"Telegram ответил {exc.code}. {detail[:180]}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise NotifyError(f"Нет связи с Telegram: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise NotifyError("Telegram вернул не JSON.") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        description = ""
        if isinstance(payload, dict):
            description = str(payload.get("description") or "")
        raise NotifyError(description or "Telegram отклонил сообщение.")
    logger.info("Telegram принял сообщение, чат %s.", chat_id)


def _upload_videos(
    config: AppConfig,
    videos: tuple[Path, ...],
    *,
    title: str,
    description: str,
    row_id: int | None,
    upload,
    series_playlist: str | None = None,
) -> PublishResult:
    urls: list[str] = []
    uuids: list[str] = []
    shared = _series_code(series_playlist)
    if shared is None and len(videos) > 1:
        shared = _series_playlist_for_filename(config, videos[0].name) or str(uuid.uuid4())
        logger.info("В раздаче несколько видео, плейлист VK: %s.", shared)
    for index, video in enumerate(videos, start=1):
        video_title = title if len(videos) == 1 else f"{title} — {video.name}"
        logger.info("Загружаю в VK %s из %s: %s", index, len(videos), video.name)
        published, video_uuid = _upload_one(
            config,
            video,
            title=video_title,
            description=description,
            row_id=row_id,
            upload=upload,
            series_playlist=shared,
        )
        urls.append(published.player_url)
        if video_uuid:
            uuids.append(video_uuid)
        logger.info("Плеер готов: %s", published.player_url)
        _mark_uploaded_file(config, video)
    if row_id is not None:
        for player_url in urls:
            _remember_player(config, row_id, player_url)
        for video_uuid in uuids:
            _remember_uuid(config, row_id, video_uuid)
        logger.info("Ссылка VK записана в базу, строка %s.", row_id)
    return PublishResult(player_urls=tuple(urls))


def _upload_one(
    config: AppConfig,
    path: Path,
    *,
    title: str,
    description: str,
    row_id: int | None,
    upload,
    series_playlist: str | None = None,
) -> tuple[PublishedVideo, str | None]:
    if upload is not None:
        published = upload(path, title=title, description=description)
        if not isinstance(published, PublishedVideo):
            raise VkError("Загрузчик не вернул ссылку плеера.")
        return published, None
    return _upload_with_progress(
        config,
        path,
        title=title,
        description=description,
        row_id=row_id,
        series_playlist=series_playlist,
    )


def _upload_with_progress(
    config: AppConfig,
    path: Path,
    *,
    title: str,
    description: str,
    row_id: int | None,
    series_playlist: str | None = None,
) -> tuple[PublishedVideo, str]:
    """Отправить файл и писать процент в data/vk_uploads.json, чтобы портал его показал.

    Во VK имя ролика — UUID этой загрузки, не название фильма.
    """
    token = _require_vk(config)
    board = UploadBoard(board_path(config.database_path))
    vk_name = str(uuid.uuid4())
    linked = row_id
    if linked is None:
        found = row_for_filename(config, path.name)
        if found is not None:
            linked = found.row_id
    _remember_uuid(config, linked, vk_name)
    job_id = board.begin(name=path.name, title=title, vk_uuid=vk_name)
    client = VkVideoClient(config.vk, token)
    names = _playlist_names(
        config,
        title=title,
        filename=path.name,
        row_id=linked,
        series_playlist=series_playlist,
    )
    logger.info("В VK «%s» уйдёт под именем %s.", title, vk_name)
    try:
        published = client.upload_and_wait(
            path,
            title=vk_name,
            description=description,
            on_progress=lambda sent, total: board.tick(job_id, sent, total),
            should_stop=lambda: board.is_stop_requested(job_id),
            on_saved=lambda saved: _remember_saved(
                config,
                board,
                job_id,
                linked,
                vk_name,
                saved.owner_id,
                saved.video_id,
            ),
            on_status=lambda status: board.set_status(job_id, status),
        )
    except VkStopped:
        board.set_status(job_id, "stopped", message="Остановлено")
        raise
    except VkError as exc:
        board.set_status(job_id, "error", message=str(exc))
        raise
    note = _place_playlists(client, published, names)
    board.set_status(job_id, "done", message=note, page_url=published.page_url)
    if linked is not None:
        _remember_player(config, linked, published.player_url)
    return published, vk_name


def _place_playlists(
    client: VkVideoClient,
    published: PublishedVideo,
    names: tuple[str, ...],
) -> str:
    """Видео уже в VK. Сбой плейлиста не отменяет загрузку."""
    if not names:
        return "Загружено"
    try:
        placed = client.place_in_playlists(
            published.owner_id,
            published.video_id,
            names,
            access_key=published.access_key,
        )
    except VkError as exc:
        logger.warning("Видео в VK есть, плейлист не обновлён: %s", exc)
        return "Загружено, плейлист не обновлён"
    if not placed:
        return "Загружено"
    return "Загружено в " + ", ".join(f"«{name}»" for name in placed)


def _playlist_names(
    config: AppConfig,
    *,
    title: str,
    filename: str,
    row_id: int | None,
    series_playlist: str | None = None,
) -> tuple[str, ...]:
    """«Год - жанр», а для сериала ещё плейлист с кодом этой раздачи."""
    genre = _stored_genre(config, title, row_id)
    source = source_name(config.rss.metadata_dir, filename) or ""
    if not genre:
        genre = genres_from_title(title) or genres_from_title(filename) or genres_from_title(source)
    year = _year_of(title) or _year_of(filename) or _year_of(source)
    names = list(playlist_names(genre, year))
    code = _series_code(series_playlist)
    if code and code.casefold() not in {name.casefold() for name in names}:
        names.insert(0, code)
    if names:
        logger.info("Плейлисты VK: %s.", ", ".join(names))
    else:
        logger.info("Плейлист VK не выбран: для «%s» нет жанра или года.", title)
    return tuple(names)


def _series_code(value: str | None) -> str | None:
    text = (value or "").strip()
    if _PLAYLIST_ID.fullmatch(text):
        return text.lower()
    return None


def _video_file_count(files: list[dict]) -> int:
    count = 0
    for item in files:
        name = item.get("name")
        if isinstance(name, str) and Path(name).suffix.lower() in _VIDEO_SUFFIXES:
            count += 1
    return count


def _playlist_for_files(config: AppConfig, infohash: str, files: list[dict]) -> str | None:
    """Один код плейлиста на раздачу, если в ней больше одного видео."""
    if _video_file_count(files) < 2:
        return None
    key = infohash.lower()
    sent, hold, playlists, _completed = _load_sent(config)
    found = playlists.get(key)
    if found:
        return found
    found = str(uuid.uuid4())
    playlists[key] = found
    _save_sent(config, sent, hold, playlists)
    logger.info("Раздача с несколькими видео, плейлист VK: %s.", found)
    return found


def _series_playlist_for_filename(config: AppConfig, filename: str) -> str | None:
    """Найти раздачу по имени файла и вернуть код плейлиста, если видео в ней несколько."""
    if not config.streaming.enabled:
        return None
    needle = Path(filename).name
    if not needle:
        return None
    try:
        client = _qbit_client(config)
        torrents = client.list_torrents()
    except StreamError as exc:
        logger.warning("Не проверил число видео в раздаче: %s", exc)
        return None
    for torrent in torrents:
        infohash = str(torrent.get("hash") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{40}", infohash):
            continue
        try:
            files = client.torrent_files(infohash)
        except StreamError:
            continue
        names = {Path(str(item.get("name") or "")).name for item in files}
        if needle not in names:
            continue
        return _playlist_for_files(config, infohash, files)
    return None


def _stored_genre(config: AppConfig, title: str, row_id: int | None) -> str | None:
    """Жанр из базы: сначала по номеру записи, иначе по точному названию."""
    with SeenStore(config.database_path) as store:
        if row_id is not None:
            item = store.get(row_id)
            if item is not None and item.genre and item.genre.strip():
                return item.genre.strip()
        return store.genre_for_title(title)


def _year_of(text: str) -> int | None:
    if not text.strip():
        return None
    _, year = movie_query(text)
    if year is not None:
        return year
    found = _YEAR_TOKEN.search(text)
    if found is None:
        return None
    return int(found.group(0))


def _stored_urls(config: AppConfig, row_id: int | None) -> tuple[str, ...]:
    if row_id is None:
        return ()
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
    if item is None or not item.vk_url:
        return ()
    return tuple(line for line in item.vk_url.splitlines() if line.strip())


def _stored_row(config: AppConfig, row_id: int | None) -> StoredItem | None:
    if row_id is None:
        return None
    with SeenStore(config.database_path) as store:
        return store.get(row_id)


def _find_stored(config: AppConfig, title: str) -> StoredItem | None:
    """Найти фильм в базе по названию раздачи или по имени из TMDB."""
    text = title.strip()
    short = release_title(text)
    if not text and not short:
        return None
    with SeenStore(config.database_path) as store:
        rows = store.list_seen()
    for row in rows:
        if text and text in {(row.title or "").strip(), (row.movie_title or "").strip()}:
            return row
        if short and short == release_title(row.title or ""):
            return row
    return None


def _lookup_movie(config: AppConfig, title: str) -> MovieInfo | None:
    """Спросить TMDB только после того, как ссылка VK уже есть. Сбой не отменяет выгрузку."""
    key = os.environ.get(config.tmdb.api_key_env, "").strip()
    if not key:
        logger.info(
            "Ключ TMDB не задан (%s). В Telegram уйдёт ссылка VK без обложки.",
            config.tmdb.api_key_env,
        )
        return None
    try:
        return search_movie(key, title, timeout=config.tmdb.timeout_seconds)
    except TmdbError as exc:
        logger.warning("TMDB не дал описание для «%s»: %s", title, exc)
        return None


def _require_vk(config: AppConfig) -> str:
    token = os.environ.get(config.vk.token_env, "").strip()
    if not token:
        raise ConfigError(
            f"Задайте токен VK в переменной {config.vk.token_env}. "
            "Нужно право video. В config.yaml токен не пишите."
        )
    return token


def _telegram(config: AppConfig) -> tuple[str, str]:
    token = os.environ.get(config.telegram.token_env, "").strip()
    chat_id = os.environ.get(config.telegram.chat_id_env, "").strip()
    if not token:
        raise ConfigError(f"Задайте токен бота в переменной {config.telegram.token_env}.")
    if not chat_id:
        raise ConfigError(f"Задайте id чата в переменной {config.telegram.chat_id_env}.")
    return token, chat_id


def queue_link(config: AppConfig, link: str, title: str) -> None:
    """Поставить magnet в qBittorrent. Файл .torrent для этого не скачивается."""
    try:
        added = register_magnet(config, link, title)
    except StreamError as exc:
        logger.warning("В qBittorrent не добавлено «%s»: %s", title, exc)
        return
    if added is None:
        logger.info("qBittorrent выключен, «%s» в загрузку не ставлю.", title)


def queue_download(config: AppConfig, torrent: Path, title: str) -> None:
    """Поставить .torrent в qBittorrent сразу после одобрения.

    Токен VK здесь не нужен: фильм может качаться долго, а выкладка во
    ВКонтакте начнётся отдельно, когда токен задан.
    """
    _remember_stream(config, torrent, title)


def _remember_stream(config: AppConfig, torrent: Path, title: str) -> None:
    """Отдать .torrent в qBittorrent. Сбой здесь не отменяет загрузку в VK."""
    try:
        added = register_downloaded(config, torrent, title)
    except StreamError as exc:
        logger.warning("В qBittorrent не добавлено «%s»: %s", title, exc)
        return
    if added is None:
        logger.info("qBittorrent выключен, «%s» в загрузку не ставлю.", title)


def _aria_timeout(config: AppConfig) -> float | None:
    timeout = config.delivery.aria2_timeout_seconds
    if timeout <= 0:
        return None
    return timeout


def _run_aria2(command: list[str], timeout: float | None) -> None:
    logger.info("Запуск: %s", " ".join(command))
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise DownloadError(
            f"Не удалось запустить {command[0]}: {exc}. "
            "Установите aria2c или укажите delivery.aria2_bin."
        ) from exc
    assert proc.stdout is not None

    def reader() -> None:
        for line in proc.stdout:
            logger.info("aria2c: %s", line.rstrip())

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        thread.join(timeout=5)
        limit = "без ограничения" if timeout is None else f"{timeout:.0f} с"
        raise DownloadError(f"aria2c не закончил за {limit}.") from None
    thread.join(timeout=5)
    if code != 0:
        raise DownloadError(f"aria2c завершился с кодом {code}.")


def _snapshot(root: Path) -> dict[Path, tuple[int, int]]:
    found: dict[Path, tuple[int, int]] = {}
    if not root.exists():
        return found
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        found[path.resolve()] = (stat.st_size, stat.st_mtime_ns)
    return found


def _changed_files(
    before: dict[Path, tuple[int, int]],
    after: dict[Path, tuple[int, int]],
) -> list[Path]:
    changed = [
        path
        for path, stamp in after.items()
        if before.get(path) != stamp and path.suffix.lower() not in _SKIP_SUFFIXES
    ]
    changed.sort(key=lambda path: path.stat().st_size, reverse=True)
    return changed


def _select_videos(files: list[Path]) -> list[Path]:
    videos = [
        path for path in files if path.suffix.lower() in _VIDEO_SUFFIXES and path.stat().st_size > 0
    ]
    if videos:
        return videos
    usable = [path for path in files if path.stat().st_size > 0]
    if not usable:
        raise DownloadError("aria2c скачал только пустые или служебные файлы.")
    logger.warning("Среди новых файлов нет видео. Беру самый большой: %s", usable[0].name)
    return [usable[0]]


def vk_ready(config: AppConfig) -> bool:
    return bool(os.environ.get(config.vk.token_env, "").strip())


def _remember_player(config: AppConfig, row_id: int, player_url: str) -> None:
    if not player_url.strip():
        return
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
        if item is None:
            return
        parts = [part.strip() for part in (item.vk_url or "").splitlines() if part.strip()]
        if player_url not in parts:
            parts.append(player_url)
        store.set_vk_url(row_id, "\n".join(parts))


def _remember_saved(
    config: AppConfig,
    board: UploadBoard,
    job_id: str,
    row_id: int | None,
    vk_uuid: str,
    owner_id: int,
    video_id: int,
) -> None:
    """Запомнить номер ролика и в очереди, и в базе. Иначе карточка пропадёт после «Убрать»."""
    board.remember(job_id, owner_id, video_id)
    _remember_clip(config, row_id, owner_id, video_id, vk_uuid)


def _remember_clip(
    config: AppConfig,
    row_id: int | None,
    owner_id: int,
    video_id: int,
    vk_uuid: str,
) -> None:
    if row_id is None:
        return
    marker = f"video{owner_id}_{video_id}"
    line = f"{marker} {vk_uuid}".strip()
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
        if item is None:
            return
        parts = [part.strip() for part in (item.vk_uuid or "").splitlines() if part.strip()]
        if any(marker in part for part in parts):
            return
        parts.append(line)
        store.set_vk_uuid(row_id, "\n".join(parts))


def _remember_uuid(config: AppConfig, row_id: int | None, vk_uuid: str) -> None:
    """Привязать код ролика к фильму в базе, не затирая уже записанные коды."""
    if row_id is None or not vk_uuid.strip():
        return
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
        if item is None:
            return
        parts = [part.strip() for part in (item.vk_uuid or "").splitlines() if part.strip()]
        if vk_uuid not in parts:
            parts.append(vk_uuid)
        store.set_vk_uuid(row_id, "\n".join(parts))


def row_for_filename(config: AppConfig, filename: str) -> StoredItem | None:
    """Найти фильм в базе по имени файла: раздача qBittorrent содержит этот файл."""
    return rows_by_filename(config, {filename}).get(Path(filename).name)


def rows_by_filename(
    config: AppConfig,
    names: set[str],
    rows: list[StoredItem] | None = None,
) -> dict[str, StoredItem]:
    """Имя файла ролика → запись в базе. Один проход по раздачам qBittorrent."""
    wanted = {Path(name).name for name in names if Path(name).name}
    if not wanted or not config.streaming.enabled:
        return {}
    from potok.qbit import QbitClient

    try:
        client = QbitClient(config.streaming)
        torrents = client.list_torrents()
    except StreamError:
        return {}
    seen = rows if rows is not None else _load_seen(config)
    found: dict[str, StoredItem] = {}
    for torrent in torrents:
        if wanted <= set(found):
            break
        infohash = str(torrent.get("hash") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{40}", infohash):
            continue
        try:
            files = client.torrent_files(infohash)
        except StreamError:
            continue
        file_names = {Path(str(item.get("name") or "")).name for item in files}
        hit = wanted & file_names
        if not hit:
            continue
        row = _row_for_hash(infohash, seen)
        if row is None:
            continue
        for name in hit:
            found.setdefault(name, row)
    return found


def bind_uploaded_codes(
    config: AppConfig,
    rows: list[StoredItem] | None = None,
) -> dict[str, StoredItem]:
    """Связать уже отправленные ролики с фильмом в базе, чтобы карточка нашла обложку."""
    jobs = UploadBoard(board_path(config.database_path)).jobs()
    found = rows_by_filename(
        config,
        {str(job.get("name") or "") for job in jobs},
        rows,
    )
    for job in jobs:
        row = found.get(str(job.get("name") or ""))
        vk_uuid = str(job.get("vk_uuid") or "")
        if row is not None and vk_uuid:
            _remember_uuid(config, row.row_id, vk_uuid)
    return found


def _qbit_client(config: AppConfig):
    from potok.qbit import QbitClient

    return QbitClient(config.streaming)


def _board_busy(board: UploadBoard) -> bool:
    return any(job.get("status") in _ACTIVE_UPLOADS for job in board.jobs())


def waiting_vk_hashes(torrents: list[dict], seen: list[StoredItem]) -> list[str]:
    """Докачанные раздачи без ссылки VK. Такой фильм ещё поедет в очередь выгрузки."""
    found: list[str] = []
    for torrent in torrents:
        if not _torrent_complete(torrent):
            continue
        infohash = str(torrent.get("hash") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{40}", infohash):
            continue
        row = _row_for_hash(infohash, seen)
        if row is not None and (row.vk_url or "").strip():
            continue
        found.append(infohash)
    return found


def _in_vk_order(config: AppConfig, torrents: list[dict], seen: list[StoredItem]) -> list[dict]:
    order = UploadOrder(order_path(config.database_path)).apply(waiting_vk_hashes(torrents, seen))
    rank = {infohash: index for index, infohash in enumerate(order)}
    return sorted(
        torrents,
        key=lambda item: rank.get(str(item.get("hash") or "").lower(), len(rank)),
    )


def _torrent_complete(item: dict) -> bool:
    if str(item.get("state") or "") not in _COMPLETE_STATES:
        return False
    progress = item.get("progress")
    if isinstance(progress, bool) or not isinstance(progress, (int, float)):
        return False
    return float(progress) >= 0.999


def seed_remaining_seconds(item: dict, now: float) -> int | None:
    """Сколько секунд ещё раздавать. None — торрент не в этом окне."""
    if str(item.get("state") or "") not in _SEEDING_STATES or not _torrent_complete(item):
        return None
    stamp = _unix(item.get("completion_on"))
    if stamp is None:
        return None
    return max(0, int(SEED_AFTER_COMPLETE_SECONDS - (now - stamp)))


def _seed_action(torrent: dict, completed: dict[str, float], now: float) -> str:
    """wait — ещё раздаём; remember — запомнили старт; pause — время вышло; ready — уже пауза."""
    if str(torrent.get("state") or "") not in _SEEDING_STATES:
        return "ready"
    stamp = _unix(torrent.get("completion_on"))
    if stamp is None:
        infohash = str(torrent.get("hash") or "").lower()
        stamp = completed.get(infohash)
        if stamp is None and re.fullmatch(r"[a-f0-9]{40}", infohash):
            completed[infohash] = now
            return "remember"
    if stamp is None or now - stamp < SEED_AFTER_COMPLETE_SECONDS:
        return "wait"
    return "pause"


def _unix(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number <= 0:
        return None
    return number


def _completed_marks(raw: object) -> dict[str, float]:
    found: dict[str, float] = {}
    if not isinstance(raw, dict):
        return found
    for key, value in raw.items():
        stamp = _unix(value)
        if not isinstance(key, str) or stamp is None:
            continue
        if re.fullmatch(r"[a-f0-9]{40}", key.lower()):
            found[key.lower()] = stamp
    return found


def _purge_old_downloads(
    config: AppConfig,
    qbit,
    torrents: list[dict],
    sent: set[str],
    now: float,
) -> None:
    """Удалить видео и .torrent, если загрузке больше 10 дней и VK уже принял ролики."""
    root = config.delivery.download_dir
    busy = _busy_upload_names(config)
    for torrent in torrents:
        try:
            _purge_one(config, qbit, torrent, sent, busy, root, now)
        except Exception:
            logger.exception("Не разобрал старую загрузку")


def _busy_upload_names(config: AppConfig) -> set[str]:
    names: set[str] = set()
    for job in UploadBoard(board_path(config.database_path)).jobs():
        name = str(job.get("name") or "")
        if name and job.get("status") in _ACTIVE_UPLOADS:
            names.add(name)
    return names


def _purge_one(
    config: AppConfig,
    qbit,
    torrent: dict,
    sent: set[str],
    busy: set[str],
    root: Path,
    now: float,
) -> None:
    if not _torrent_complete(torrent):
        return
    started = _unix(torrent.get("added_on")) or _unix(torrent.get("completion_on"))
    if started is None or now - started < KEEP_DOWNLOAD_SECONDS:
        return
    infohash = str(torrent.get("hash") or "").lower()
    if not re.fullmatch(r"[a-f0-9]{40}", infohash):
        return
    title = str(torrent.get("name") or infohash)
    try:
        files = qbit.torrent_files(infohash)
    except StreamError as exc:
        logger.warning("Файлы «%s» не прочитались, старое видео не трогаю: %s", title, exc)
        return
    videos = _declared_videos(root, files)
    if not videos or any(path.name in busy for path in videos):
        return
    if not all(_already_sent(sent, root, path) for path in videos):
        return
    for path in videos:
        if path.is_file() and not _delete_file(path):
            logger.warning("«%s» не удалился с диска, попробую позже.", path.name)
            return
        _prune_empty_dirs(path.parent, root)
    removed = _delete_metadata_torrents(config, infohash)
    try:
        qbit.remove(infohash)
    except StreamError as exc:
        logger.warning("Не убрал раздачу «%s» из qBittorrent: %s", title, exc)
        return
    logger.info(
        "«%s» старше 10 дней и уже в VK. Удалил видео и файлов .torrent: %s.",
        title,
        removed,
    )


def _declared_videos(root: Path, files: list[dict]) -> list[Path]:
    found: list[Path] = []
    for item in files:
        name = item.get("name")
        if not isinstance(name, str):
            continue
        progress = item.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            if float(progress) < 0.999:
                continue
        path = _inside(root, name)
        if path is None or path.suffix.lower() not in _VIDEO_SUFFIXES:
            continue
        found.append(path)
    return found


def _already_sent(sent: set[str], root: Path, path: Path) -> bool:
    return _relative(root, path) in sent or f"name:{path.name}" in sent


def _delete_file(path: Path) -> bool:
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("Не удалил %s: %s", path, exc)
    return not path.exists()


def _prune_empty_dirs(start: Path, root: Path) -> None:
    try:
        root_resolved = root.resolve()
    except OSError:
        return
    current = start
    while True:
        try:
            resolved = current.resolve()
            resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            return
        if resolved == root_resolved:
            return
        try:
            current.rmdir()
        except OSError:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _delete_metadata_torrents(config: AppConfig, infohash: str) -> int:
    paths: dict[Path, Path] = {}
    digest = infohash.lower()
    directory = config.rss.metadata_dir
    try:
        with SeenStore(config.database_path) as store:
            rows = store.list_seen()
    except OSError:
        rows = []
    for row in rows:
        url = row.torrent_url or ""
        if digest not in url.lower():
            continue
        path = torrent_path(directory, url)
        if path.is_file():
            paths[path.resolve()] = path
    if directory.is_dir():
        for path in directory.glob("*.torrent"):
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            if _torrent_info_hash(blob) == digest:
                paths[path.resolve()] = path
    removed = 0
    for path in paths.values():
        if _delete_file(path):
            removed += 1
    return removed


def _torrent_info_hash(blob: bytes) -> str | None:
    marker = b"4:info"
    start = blob.find(marker)
    if start < 0:
        return None
    value_at = start + len(marker)
    end = _bencode_end(blob, value_at)
    if end is None:
        return None
    return hashlib.sha1(blob[value_at:end], usedforsecurity=False).hexdigest()


def _bencode_end(blob: bytes, index: int) -> int | None:
    if index >= len(blob):
        return None
    kind = blob[index]
    if kind == ord("i"):
        end = blob.find(b"e", index)
        if end < 0:
            return None
        return end + 1
    if kind in {ord("d"), ord("l")}:
        cursor = index + 1
        while cursor < len(blob) and blob[cursor] != ord("e"):
            if kind == ord("d"):
                cursor = _bencode_end(blob, cursor) or 0
                if cursor == 0:
                    return None
            nxt = _bencode_end(blob, cursor)
            if nxt is None:
                return None
            cursor = nxt
        if cursor >= len(blob):
            return None
        return cursor + 1
    colon = blob.find(b":", index)
    if colon < 0 or colon == index:
        return None
    try:
        length = int(blob[index:colon])
    except ValueError:
        return None
    if length < 0:
        return None
    end = colon + 1 + length
    if end > len(blob):
        return None
    return end


def _finished_videos(root: Path, files: list[dict]) -> list[Path]:
    found: list[Path] = []
    for item in files:
        name = item.get("name")
        if not isinstance(name, str):
            continue
        progress = item.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            if float(progress) < 0.999:
                continue
        path = _inside(root, name)
        if path is None or path.suffix.lower() not in _VIDEO_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        found.append(path)
    found.sort(key=lambda path: path.stat().st_size, reverse=True)
    return found


def _inside(root: Path, relative: str) -> Path | None:
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    path = root.joinpath(*parts)
    return path


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _load_seen(config: AppConfig) -> list[StoredItem]:
    with SeenStore(config.database_path) as store:
        return store.list_seen()


def _row_for_hash(infohash: str, rows: list[StoredItem]) -> StoredItem | None:
    needle = infohash.lower()
    for row in rows:
        if needle in (row.torrent_url or "").lower():
            return row
    return None


def _sent_path(config: AppConfig) -> Path:
    return config.data_dir() / "vk_sent.json"


def _load_sent(
    config: AppConfig,
) -> tuple[set[str], dict[str, float], dict[str, str], dict[str, float]]:
    path = _sent_path(config)
    if not path.is_file():
        return set(), {}, {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), {}, {}, {}
    if not isinstance(payload, dict):
        return set(), {}, {}, {}
    files = payload.get("files")
    hold = payload.get("hold")
    sent = {item for item in files if isinstance(item, str)} if isinstance(files, list) else set()
    waits: dict[str, float] = {}
    if isinstance(hold, dict):
        for key, value in hold.items():
            if (
                isinstance(key, str)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                waits[key] = float(value)
    playlists: dict[str, str] = {}
    raw = payload.get("playlists")
    if isinstance(raw, dict):
        for key, value in raw.items():
            code = _series_code(value if isinstance(value, str) else None)
            if isinstance(key, str) and re.fullmatch(r"[a-f0-9]{40}", key.lower()) and code:
                playlists[key.lower()] = code
    completed = _completed_marks(payload.get("completed"))
    return sent, waits, playlists, completed


def _save_sent(
    config: AppConfig,
    sent: set[str],
    hold: dict[str, float],
    playlists: dict[str, str] | None = None,
    completed: dict[str, float] | None = None,
) -> None:
    path = _sent_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if playlists is None or completed is None:
        _, _, stored_playlists, stored_completed = _load_sent(config)
        if playlists is None:
            playlists = stored_playlists
        if completed is None:
            completed = stored_completed
    fresh = {key: value for key, value in hold.items() if key not in sent}
    text = json.dumps(
        {
            "files": sorted(sent),
            "hold": fresh,
            "playlists": playlists,
            "completed": completed,
        },
        ensure_ascii=False,
    )
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _mark_sent(sent: set[str], hold: dict[str, float], key: str) -> None:
    sent.add(key)
    hold.pop(key, None)


def _absorb_finished_names(board: UploadBoard, sent: set[str]) -> None:
    """Ручная отправка уже закончилась или её остановили — саму не повторять."""
    for job in board.jobs():
        name = str(job.get("name") or "")
        if name and job.get("status") == "done":
            sent.add(f"name:{name}")


def _mark_uploaded_file(config: AppConfig, video: Path) -> None:
    sent, hold, _playlists, _completed = _load_sent(config)
    _mark_sent(sent, hold, _relative(config.delivery.download_dir, video))
    _mark_sent(sent, hold, f"name:{video.name}")
    _save_sent(config, sent, hold)


def already_sent_to_vk(config: AppConfig, path: Path) -> bool:
    """Файл уже есть в vk_sent.json — по пути или по имени."""
    sent, _, _, _ = _load_sent(config)
    return _relative(config.delivery.download_dir, path) in sent or f"name:{path.name}" in sent


def torrent_info_hash(blob: bytes) -> str | None:
    """Хеш раздачи из содержимого файла .torrent. Если файл битый — None."""
    return _torrent_info_hash(blob)


def discard_queued_files(
    config: AppConfig,
    infohash: str,
    files: list[dict],
    *,
    content_path: str = "",
) -> tuple[int, int]:
    """Удалить видео этой раздачи и файлы .torrent. Возвращает (видео, torrent)."""
    digest = infohash.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{40}", digest):
        raise ValueError("Неизвестная раздача.")
    root = config.delivery.download_dir
    videos = _declared_videos(root, files)
    extra = _video_under_root(root, content_path)
    if extra is not None and extra not in videos:
        videos.append(extra)
    removed_videos = 0
    for path in videos:
        if not path.is_file():
            continue
        if not _delete_file(path):
            raise ValueError(f"Не удалось удалить видео «{path.name}».")
        removed_videos += 1
        _prune_empty_dirs(path.parent, root)
    return removed_videos, _delete_metadata_torrents(config, digest)


def _video_under_root(root: Path, content_path: str) -> Path | None:
    """Путь qBittorrent, если это видео внутри папки загрузок."""
    text = content_path.strip()
    if not text:
        return None
    path = Path(text)
    if path.suffix.lower() not in _VIDEO_SUFFIXES:
        return None
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return path


def _notify_vk(
    config: AppConfig,
    title: str,
    description: str,
    result: PublishResult,
    stored: StoredItem | None = None,
) -> None:
    link = result.player_urls[0] if result.player_urls else ""
    try:
        send_vk_card(
            config,
            title=title,
            description=description,
            page_url=link,
            stored=stored,
        )
    except (ConfigError, NotifyError) as exc:
        logger.warning("Видео в VK есть, в Telegram не отправилось: %s", exc)
