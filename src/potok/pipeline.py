"""Скачать ленты, отфильтровать и поставить новые записи на согласование.

VPN здесь не включается и не выключается: туннель постоянный,
а через него идут только домены из настройки.
"""

from __future__ import annotations

import hashlib
import logging
import os
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from potok.config import AppConfig, ConfigError, CredentialSettings, FilterSettings
from potok.filters import accept, choose_copy
from potok.jackett import JackettError, ensure_jackett, feed_sources
from potok.rss import FeedItem, genres_from_title, magnet_link, parse_feed
from potok.store import APPROVED, SeenStore, StoredItem
from potok.tmdb import kinopoisk_search_url, release_key
from potok.vpn import VpnManager

logger = logging.getLogger(__name__)

_RSS_MAX_BYTES = 5_000_000
_TORRENT_MAX_BYTES = 2_000_000
_HIDDEN_QUERY = frozenset({"apikey", "api_key", "token", "password", "pass"})

# Уведомление: запись и номер строки в базе.
Notify = Callable[[FeedItem, int], None]
# Номер страницы, сколько страниц всего, сколько новых фильмов уже записано.
PageProgress = Callable[[int, int, int], None]


class FetchError(Exception):
    """Ленту или файл метаданных скачать не удалось."""


@dataclass(frozen=True)
class FetchReport:
    accepted: tuple[FeedItem, ...]
    skipped_seen: int
    skipped_filter: int
    failed: int


def run_fetch(
    config: AppConfig,
    vpn: VpnManager,
    *,
    now: datetime | None = None,
    download=None,
    notify: Notify | None = None,
    on_page: PageProgress | None = None,
) -> FetchReport:
    """Включить VPN, забрать ленты и поставить подходящие записи в ожидание.

    Новая запись остаётся в статусе «ожидает». Файл .torrent скачивается здесь
    только у тех записей, которые уже одобрены в Telegram.
    """
    username, password = resolve_credentials(config.credentials)
    fetcher = download or download_bytes
    report_failed = 0
    accepted: list[FeedItem] = []
    queued: list[tuple[FeedItem, int]] = []
    skipped_seen = 0
    skipped_filter = 0

    try:
        ensure_jackett(config)
    except JackettError as exc:
        logger.error("%s", exc)
        raise FetchError(str(exc)) from exc

    feeds = feed_sources(config)
    if not feeds:
        raise FetchError(
            "Нет ленты. Вставьте ссылку Jackett в jackett_rss_url или добавьте адрес в rss.feeds."
        )

    logger.debug("Опрос ленты не переключает VPN %s: маршруты к доменам постоянные.", vpn.interface)
    with SeenStore(config.database_path) as store:
        report_failed += _download_approved(
            store,
            directory=config.rss.metadata_dir,
            timeout=config.rss.timeout_seconds,
            user_agent=config.rss.user_agent,
            username=username,
            password=password,
            download=fetcher,
        )
        total = len(feeds)
        collected: list[FeedItem] = []
        for index, feed in enumerate(feeds, start=1):
            if on_page is not None:
                on_page(index, total, len(accepted))
            try:
                payload = fetcher(
                    feed.url,
                    timeout=config.rss.timeout_seconds,
                    user_agent=config.rss.user_agent,
                    username=username,
                    password=password,
                    max_bytes=_RSS_MAX_BYTES,
                )
            except FetchError as exc:
                report_failed += 1
                logger.error("Лента %s: %s", feed.name, exc)
                continue

            parsed = parse_feed(
                payload,
                feed_name=feed.name,
                patterns=config.filters.patterns,
            )
            for item in parsed:
                if not _quality_ok(item, config.filters):
                    if store.has(item.guid):
                        skipped_seen += 1
                    else:
                        skipped_filter += 1
                    continue
                collected.append(item)
            if on_page is not None:
                on_page(index, total, len(accepted))
        grouped: dict[str, list[FeedItem]] = {}
        for item in collected:
            grouped.setdefault(release_key(item.title), []).append(item)
        fresh_seen, fresh_filtered, fresh_accepted, fresh_queued = _queue_movies(
            store,
            grouped,
            config,
            now=now,
        )
        skipped_seen += fresh_seen
        skipped_filter += fresh_filtered
        accepted.extend(fresh_accepted)
        queued.extend(fresh_queued)
        store.collapse_pending(config.filters.quality_keep)
        if on_page is not None and total:
            on_page(total, total, len(accepted))

    _send_notifications(notify, queued)

    return FetchReport(
        accepted=tuple(accepted),
        skipped_seen=skipped_seen,
        skipped_filter=skipped_filter,
        failed=report_failed,
    )


def _queue_movies(
    store: SeenStore,
    grouped: dict[str, list[FeedItem]],
    config: AppConfig,
    *,
    now: datetime | None,
) -> tuple[int, int, list[FeedItem], list[tuple[FeedItem, int]]]:
    """Поставить в очередь одну копию фильма с выбранным качеством."""
    skipped_seen = 0
    skipped_filter = 0
    accepted: list[FeedItem] = []
    queued: list[tuple[FeedItem, int]] = []
    quality_keep = config.filters.quality_keep
    for key, items in grouped.items():
        if store.has_decided_release(key):
            store.drop_pending_release(key)
            skipped_seen += len(items)
            continue
        pending = store.pending_feeds(key)
        by_guid = {item.guid: item for item in pending}
        for item in items:
            by_guid[item.guid] = item
        winner = choose_copy(list(by_guid.values()), quality_keep)
        feed_winner = next((item for item in items if item.guid == winner.guid), None)
        skipped_filter += len(items) - (0 if feed_winner is None else 1)
        if feed_winner is None:
            store.keep_pending_release(key, winner, quality_keep=quality_keep)
            continue
        if store.pending_release_ready(key, feed_winner):
            skipped_seen += 1
            continue
        item = describe_item(feed_winner, config)
        if not accept(item, config.filters, now=now):
            skipped_filter += 1
            continue
        outcome = store.keep_pending_release(key, item, quality_keep=quality_keep)
        if outcome is None:
            skipped_seen += 1
            continue
        row_id, created = outcome
        if not created:
            skipped_seen += 1
            continue
        accepted.append(item)
        queued.append((item, row_id))
        logger.info(
            "Ждёт решения: %s | %s | рейтинг %s | сиды %s | %s",
            item.title,
            item.quality or "качество нет",
            item.rating if item.rating is not None else "нет",
            item.seeders if item.seeders is not None else "нет",
            item.topic_url or item.link,
        )
    return skipped_seen, skipped_filter, accepted, queued


def deliver_approved(
    config: AppConfig,
    vpn: VpnManager,
    item: StoredItem,
    *,
    download=None,
) -> Path | None:
    """Скачать .torrent уже одобренной записи. Без одобрения файл не качается."""
    if item.status != APPROVED:
        raise FetchError("Метаданные скачиваются только после одобрения.")
    if not item.torrent_url:
        logger.info("У «%s» нет адреса .torrent, скачивать нечего.", item.title)
        return None
    if magnet_link(item.torrent_url):
        logger.info("У «%s» magnet-ссылка, файл .torrent не скачиваю.", item.title)
        return None

    username, password = resolve_credentials(config.credentials)
    fetcher = download or download_bytes
    feed_item = FeedItem(
        guid=item.guid,
        feed_name=item.feed_name,
        title=item.title,
        link=item.link,
        published=None,
        torrent_url=item.torrent_url,
        rating=item.rating,
        quality=item.quality,
        genre=item.genre,
    )
    logger.debug("Файл .torrent качается по постоянным маршрутам VPN %s.", vpn.interface)
    return _save_torrent(
        feed_item,
        directory=config.rss.metadata_dir,
        timeout=config.rss.timeout_seconds,
        user_agent=config.rss.user_agent,
        username=username,
        password=password,
        download=fetcher,
    )


def _cover_title(title: str, year: str | None) -> str:
    """Название с обложки: как в TMDB, с годом в скобках."""
    text = " ".join(title.split())
    if year and year not in text:
        text = f"{text} ({year})"
    return text[:128]


def _quality_ok(item: FeedItem, filters: FilterSettings) -> bool:
    """Пустой шаблон качества пропускает всё. Иначе остаётся только то, что шаблон нашёл."""
    if filters.patterns.quality is None:
        return True
    return bool(item.quality)


def describe_item(item: FeedItem, config: AppConfig) -> FeedItem:
    """Дописать жанр из названия и, если задан ключ, рейтинг, описание и постер из TMDB."""
    from potok.tmdb import MovieNotFound, TmdbError, search_movie

    genre = item.genre or genres_from_title(item.title)
    page = item.kinopoisk_url or kinopoisk_search_url(item.title)
    key = os.environ.get(config.tmdb.api_key_env, "").strip()
    if not key:
        return replace(item, genre=genre, kinopoisk_url=page)
    try:
        movie = search_movie(key, item.title, timeout=config.tmdb.timeout_seconds)
    except MovieNotFound as exc:
        logger.info("TMDB для «%s»: %s", item.title, exc)
        return replace(
            item,
            genre=genre,
            overview="",
            kinopoisk_url=page,
            runtime_minutes=item.runtime_minutes if item.runtime_minutes is not None else 0,
            country=item.country if item.country is not None else "",
        )
    except TmdbError as exc:
        logger.info("TMDB для «%s»: %s", item.title, exc)
        return replace(
            item,
            genre=genre,
            overview="",
            kinopoisk_url=page,
            runtime_minutes=item.runtime_minutes if item.runtime_minutes is not None else 0,
        )
    rating = item.rating if item.rating is not None else movie.rating
    runtime = movie.runtime_minutes if movie.runtime_minutes is not None else 0
    cover = _cover_title(movie.title, movie.year) if movie.poster_url else item.movie_title
    return replace(
        item,
        genre=genre,
        rating=rating,
        overview=movie.overview,
        poster_url=movie.poster_url,
        kinopoisk_url=movie.kinopoisk_url or page,
        runtime_minutes=runtime,
        movie_title=cover,
        country=item.country if movie.country is None else movie.country,
    )


def fill_pending_details(config: AppConfig, *, limit: int = 100) -> int:
    """Дописать постер, описание и длительность у записей в очереди."""
    filled = 0
    with SeenStore(config.database_path) as store:
        waiting = store.list_pending_without_details(limit=limit)
        for item in waiting:
            described = describe_item(
                FeedItem(
                    guid=item.guid,
                    feed_name=item.feed_name,
                    title=item.title,
                    link=item.link,
                    published=None,
                    torrent_url=item.torrent_url,
                    rating=item.rating,
                    quality=item.quality,
                    genre=item.genre,
                    overview=item.overview,
                    poster_url=item.poster_url,
                    seeders=item.seeders,
                    size_bytes=item.size_bytes,
                    topic_url=item.topic_url,
                    kinopoisk_url=item.kinopoisk_url,
                    runtime_minutes=item.runtime_minutes,
                    movie_title=item.movie_title,
                    country=item.country,
                ),
                config,
            )
            learned_runtime = item.runtime_minutes is None and described.runtime_minutes is not None
            learned_title = bool(described.movie_title) and not (item.movie_title or "").strip()
            learned_country = item.country is None and described.country is not None
            if (
                item.overview is not None
                and not learned_runtime
                and not learned_title
                and not learned_country
            ):
                continue
            store.update_details(
                item.row_id,
                rating=described.rating,
                genre=described.genre,
                overview=described.overview,
                poster_url=described.poster_url,
                kinopoisk_url=described.kinopoisk_url,
                runtime_minutes=described.runtime_minutes,
                movie_title=described.movie_title,
                country=described.country,
            )
            filled += 1
    return filled


def resolve_credentials(settings: CredentialSettings) -> tuple[str | None, str | None]:
    """Прочитать логин и пароль из окружения. В YAML лежат только имена переменных."""
    username = _env_value(settings.username_env)
    password = _env_value(settings.password_env)
    if (username is None) != (password is None):
        raise ConfigError(
            "Для ленты с паролем задайте обе переменные: "
            f"{settings.username_env} и {settings.password_env}."
        )
    return username, password


def download_bytes(
    url: str,
    *,
    timeout: float,
    user_agent: str,
    username: str | None,
    password: str | None,
    max_bytes: int,
) -> bytes:
    """Скачать один http(s)-адрес и не читать больше max_bytes."""
    scheme = url.split(":", 1)[0].lower()
    safe_url = redact_url(url)
    if scheme not in {"http", "https"}:
        raise FetchError(f"Адрес должен начинаться с http:// или https://: {safe_url}")

    request = Request(url, headers={"User-Agent": user_agent})
    if username is not None and password is not None:
        token = b64encode(f"{username}:{password}".encode()).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")

    try:
        with urlopen(request, timeout=timeout) as response:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(f"Ответ больше {max_bytes} байт, загрузка остановлена.")
                chunks.append(chunk)
    except HTTPError as exc:
        raise FetchError(f"Сервер ответил {exc.code} для {safe_url}") from exc
    except URLError as exc:
        raise FetchError(f"Не удалось открыть {safe_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError(f"Превышено ожидание {timeout:.0f} с для {safe_url}") from exc

    return b"".join(chunks)


def _save_torrent(
    item: FeedItem,
    *,
    directory: Path,
    timeout: float,
    user_agent: str,
    username: str | None,
    password: str | None,
    download,
) -> Path:
    assert item.torrent_url is not None
    payload = download(
        item.torrent_url,
        timeout=timeout,
        user_agent=user_agent,
        username=username,
        password=password,
        max_bytes=_TORRENT_MAX_BYTES,
    )
    directory.mkdir(parents=True, exist_ok=True)
    target = torrent_path(directory, item.torrent_url)
    target.write_bytes(payload)
    logger.info("Метаданные сохранены: %s", target)
    return target


def clear_download_queue(config: AppConfig) -> int:
    """Удалить очередь загрузки и файлы .torrent этих записей."""
    with SeenStore(config.database_path) as store:
        items = store.clear_queue()
    for item in items:
        if not item.torrent_url:
            continue
        path = torrent_path(config.rss.metadata_dir, item.torrent_url)
        if path.is_file():
            path.unlink()
            logger.info("Удалён файл очереди: %s", path)
    return len(items)


def redact_url(url: str) -> str:
    """Убрать ключ и пароль из адреса, прежде чем писать его в журнал."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(name.lower() in _HIDDEN_QUERY and value for name, value in pairs):
        return url
    cleaned = [
        (name, "***" if name.lower() in _HIDDEN_QUERY and value else value) for name, value in pairs
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(cleaned, safe="*"), parts.fragment)
    )


def torrent_path(directory: Path, torrent_url: str) -> Path:
    digest = hashlib.sha256(torrent_url.encode("utf-8")).hexdigest()[:16]
    return directory / f"{digest}.torrent"


def _download_approved(
    store: SeenStore,
    *,
    directory: Path,
    timeout: float,
    user_agent: str,
    username: str | None,
    password: str | None,
    download,
) -> int:
    """Скачать .torrent у одобренных записей, если файла ещё нет."""
    failed = 0
    for item in store.list_status(APPROVED):
        if not item.torrent_url or magnet_link(item.torrent_url):
            continue
        target = torrent_path(directory, item.torrent_url)
        if target.is_file():
            continue
        feed_item = FeedItem(
            guid=item.guid,
            feed_name=item.feed_name,
            title=item.title,
            link=item.link,
            published=None,
            torrent_url=item.torrent_url,
            rating=item.rating,
            quality=item.quality,
            genre=item.genre,
        )
        try:
            _save_torrent(
                feed_item,
                directory=directory,
                timeout=timeout,
                user_agent=user_agent,
                username=username,
                password=password,
                download=download,
            )
        except FetchError as exc:
            failed += 1
            logger.error("%s: %s", item.title, exc)
    return failed


def _send_notifications(notify: Notify | None, queued: list[tuple[FeedItem, int]]) -> None:
    if notify is None or not queued:
        return
    for item, row_id in queued:
        try:
            notify(item, row_id)
        except Exception:
            logger.exception("Не удалось отправить уведомление в Telegram: %s", item.title)


def _env_value(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value
