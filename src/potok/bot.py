"""Telegram-бот: кнопки одобрения и команды наблюдения."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from potok.config import AppConfig, ConfigError, TelegramSettings, load_config
from potok.jobs import drain_jobs, jobs_path
from potok.logs import count_errors, worker_log_path
from potok.pipeline import (
    FetchError,
    FetchReport,
    Notify,
    deliver_approved,
    fill_pending_details,
    run_fetch,
    torrent_path,
)
from potok.publish import (
    DownloadError,
    NotifyError,
    deliver_and_notify,
    publish_finished,
    queue_download,
    queue_link,
    vk_ready,
    waiting_vk_hashes,
)
from potok.qbit import QbitClient
from potok.rss import (
    FeedItem,
    format_gigabytes,
    genre_parts,
    genre_text,
    genres_from_title,
    magnet_link,
    rutracker_topic,
)
from potok.secrets import SecretError, SecretStore
from potok.settings_io import SettingsError, save_vk_id
from potok.store import APPROVED, PENDING, REJECTED, Decision, SeenStore, StoredItem
from potok.stream import StreamError, torrent_basename
from potok.titles import release_title
from potok.tmdb import MovieInfo, country_text, movie_query
from potok.vk_oauth import (
    EXPIRES_NAME,
    REMINDED_NAME,
    VkOAuthError,
    blank_authorize_url,
    describe_token,
    is_token_paste,
    save_vk_token,
    should_remind_token,
)
from potok.vk_uploads import UploadOrder, order_path
from potok.vk_video import VkCatalogItem, VkError, VkVideoClient
from potok.vpn import VpnError, VpnManager

logger = logging.getLogger(__name__)

router = Router()
_TORRENT_LIMIT = 2_000_000
PAGE_SIZE = 10
_BUTTON_LIMIT = 60
_KIND_TAIL = re.compile(r"\s*:\s*(?:фильм|мультфильм|сериал|аниме)\s*$", re.IGNORECASE)

_CHAT_RE = re.compile(r"-?\d+")
_VK_ID_PREFIX = re.compile(r"^(?:app|club|public)(\d+)$", re.IGNORECASE)
_APP_ID_HELP = (
    "Чтобы открыть страницу разрешения VK, нужен ID приложения «Поток». "
    "Откройте приложение во ВКонтакте. В коротком адресе будет app123456 — "
    "пришлите только число, без букв app."
)
_GROUP_ID_HELP = (
    "Нужен ID сообщества, куда класть видео. Откройте сообщество. "
    "В адресе будет club123456 или public123456 — пришлите только число, "
    "без club и public."
)
_vk_wait_field: str | None = None
_STATUS_RU = {
    "pending": "ожидает",
    "approved": "одобрено",
    "rejected": "отклонено",
}
MENU_DATA = "menu"
BOT_COMMANDS = [
    BotCommand(command="status", description="Сводка по базе и VPN"),
    BotCommand(command="pending", description="Что ждёт решения"),
    BotCommand(command="vk", description="Фильмы, загруженные во VK"),
    BotCommand(command="token", description="Токен VK: срок и обновление"),
    BotCommand(command="update", description="Обновить список из ленты"),
    BotCommand(command="clear", description="Очистить очередь загрузки"),
]


def menu_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="Меню", callback_data=MENU_DATA)]


def with_menu(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Добавить «Меню» последней строкой на всю ширину."""
    return InlineKeyboardMarkup(inline_keyboard=[*rows, menu_row()])


def menu_markup() -> InlineKeyboardMarkup:
    return with_menu([])


def resolve_telegram(settings: TelegramSettings) -> tuple[str, int]:
    """Прочитать токен и id чата из окружения. В YAML лежат только имена переменных."""
    token = os.environ.get(settings.token_env, "").strip()
    raw_chat = os.environ.get(settings.chat_id_env, "").strip()
    if not token:
        raise ConfigError(
            f"Задайте токен бота в переменной {settings.token_env}. "
            "Его присылает @BotFather. В config.yaml токен не пишите."
        )
    if not _CHAT_RE.fullmatch(raw_chat):
        raise ConfigError(
            f"Задайте числовой id чата в переменной {settings.chat_id_env}. "
            "Напишите боту @userinfobot — он покажет ваш Id."
        )
    return token, int(raw_chat)


def notifier_from_env(config: AppConfig) -> Notify | None:
    """Собрать отправку карточки, если токен и чат заданы. Иначе записи просто ждут в базе."""
    token = os.environ.get(config.telegram.token_env, "").strip()
    raw_chat = os.environ.get(config.telegram.chat_id_env, "").strip()
    if not token and not raw_chat:
        logger.info(
            "Telegram не настроен (%s и %s). Новые записи останутся в статусе «ожидает».",
            config.telegram.token_env,
            config.telegram.chat_id_env,
        )
        return None
    token, chat_id = resolve_telegram(config.telegram)

    def notify(item: FeedItem, row_id: int) -> None:
        asyncio.run(send_card(token, chat_id, item, row_id))

    return notify


def parse_callback(data: str) -> tuple[str, int] | None:
    """Разобрать нажатие кнопки: yes:12 → approved, no:12 → rejected."""
    action, separator, raw_id = data.partition(":")
    if separator != ":" or not raw_id.isdigit():
        return None
    if action == "yes":
        return APPROVED, int(raw_id)
    if action == "no":
        return REJECTED, int(raw_id)
    return None


def approval_keyboard(
    row_id: int,
    *,
    topic_url: str | None = None,
    kinopoisk_url: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"yes:{row_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"no:{row_id}"),
        ]
    ]
    links = [
        button
        for button in (
            _url_button("Рутрекер", rutracker_topic(topic_url or "") or topic_url),
            _url_button("Кинопоиск", kinopoisk_url),
        )
        if button is not None
    ]
    if links:
        rows.append(links)
    return with_menu(rows)


def _url_button(text: str, url: str | None) -> InlineKeyboardButton | None:
    if not url or not url.startswith(("http://", "https://")):
        return None
    return InlineKeyboardButton(text=text, url=url)


def movie_button_label(item: StoredItem) -> str:
    """Кнопка списка: год и название, без жанра и качества."""
    year, name, _genre = _movie_facts(item)
    prefix = "" if year is None else f"{year} "
    room = _BUTTON_LIMIT - len(prefix)
    if room < 1:
        return (prefix + name)[:_BUTTON_LIMIT]
    if len(name) > room:
        name = name[: room - 1].rstrip() + "…"
    return f"{prefix}{name}"


def _movie_facts(item: StoredItem) -> tuple[int | None, str, str]:
    raw = (item.movie_title or "").strip() or release_title(item.title) or item.title
    name, year = movie_query(raw)
    if year is None:
        _, year = movie_query(item.title)
    name = " ".join(name.split()) or f"Фильм {item.row_id}"
    name = _KIND_TAIL.sub("", name).strip() or name
    genre = (item.genre or "").strip() or (genres_from_title(item.title) or "")
    genre = " ".join(genre.replace("/", ", ").split())
    return year, name, genre


def _pending_order(item: StoredItem) -> tuple[int, str, int]:
    year, name, _genre = _movie_facts(item)
    return (-(year or 0), name.casefold(), item.row_id)


def pending_keyboard(
    items: list[StoredItem],
    *,
    offset: int,
    total: int,
    genre_index: int | None = None,
) -> InlineKeyboardMarkup:
    """Десять кнопок фильмов и листание по десяткам."""
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=movie_button_label(item),
                    callback_data=f"open:{item.row_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=_pending_page_data(max(offset - PAGE_SIZE, 0), genre_index),
            )
        )
    if offset + len(items) < total:
        nav.append(
            InlineKeyboardButton(
                text="▶️ Дальше",
                callback_data=_pending_page_data(offset + PAGE_SIZE, genre_index),
            )
        )
    if nav:
        rows.append(nav)
    return with_menu(rows)


def _pending_page_data(offset: int, genre_index: int | None) -> str:
    if genre_index is None:
        return f"page:{offset}"
    return f"gpage:{genre_index}:{offset}"


def format_card(
    item: FeedItem | StoredItem,
    *,
    waiting: bool = True,
    heading: str | None = None,
) -> str:
    rating = "нет" if item.rating is None else f"{item.rating:g}"
    seeds = "нет" if item.seeders is None else str(item.seeders)
    lines = [
        heading or ("Новая запись ждёт решения" if waiting else "Запись"),
        "",
        f"Название: {item.title}",
        f"Рейтинг: {rating}",
        f"Качество: {item.quality or 'нет'}",
        f"Жанр: {genre_text(item.genre) or 'нет'}",
        f"Страна: {country_text(item.country)}",
        f"Сиды: {seeds}",
        f"Размер: {format_gigabytes(item.size_bytes)}",
    ]
    used = len("\n".join(lines))
    overview = (item.overview or "").strip()
    room = 1000 - used - 2
    if overview and room > 40:
        if len(overview) > room:
            overview = overview[: room - 1].rstrip() + "…"
        lines.extend(["", overview])
    if waiting:
        lines.extend(["", "После кнопки «Одобрить» видео скачается и загрузится в VK."])
    text = "\n".join(lines)
    if len(text) > 1000:
        return text[:999].rstrip() + "…"
    return text


def format_dashboard(numbers: dict[str, int], token_line: str) -> str:
    """Цифры как плитки портала и строка про токен VK."""
    return "\n".join(
        [
            "Дашборд Потока",
            "",
            f"Ожидают одобрения: {numbers.get('pending', 0)}",
            f"Одобрено: {numbers.get('approved', 0)}",
            f"Отклонено: {numbers.get('rejected', 0)}",
            f"Загружаются: {numbers.get('loading', 0)}",
            f"Выгружаются в ВК: {numbers.get('exporting', 0)}",
            f"Ошибки в журнале: {numbers.get('errors', 0)}",
            token_line,
        ]
    )


def dashboard_numbers(config: AppConfig) -> dict[str, int]:
    with SeenStore(config.database_path) as store:
        counts = store.counts()
        loading = len(store.list_approved_without_vk())
        seen = store.list_seen()
    return {
        "pending": counts.get("pending", 0),
        "approved": counts.get("approved", 0),
        "rejected": counts.get("rejected", 0),
        "loading": loading,
        "exporting": _exporting_count(config, seen),
        "errors": count_errors(worker_log_path(config.database_path)),
    }


def _exporting_count(config: AppConfig, seen: list[StoredItem]) -> int:
    if not config.streaming.enabled:
        return 0
    try:
        torrents = QbitClient(config.streaming).list_torrents()
    except StreamError:
        return 0
    waiting = waiting_vk_hashes(torrents, seen)
    return len(UploadOrder(order_path(config.database_path)).arranged(waiting))


def format_status(counts: dict[str, int], vpn_text: str, token_line: str | None = None) -> str:
    lines = [
        "Состояние Потока",
        "",
        f"Ожидают решения: {counts.get('pending', 0)}",
        f"Одобрено: {counts.get('approved', 0)}",
        f"Отклонено: {counts.get('rejected', 0)}",
        vpn_text,
    ]
    if token_line:
        lines.append(token_line)
    return "\n".join(lines)


def parse_vk_numeric_id(text: str) -> int | None:
    """Число ID приложения или сообщества. Буквы app/club/public можно не писать."""
    compact = text.strip().replace(" ", "")
    match = _VK_ID_PREFIX.fullmatch(compact)
    if match:
        compact = match.group(1)
    if not compact.isdigit():
        return None
    value = int(compact)
    if value <= 0 or value > 2_000_000_000:
        return None
    return value


def token_keyboard(*, app_id: int, api_version: str, has_token: bool) -> InlineKeyboardMarkup:
    url = blank_authorize_url(app_id=app_id, api_version=api_version)
    label = "Обновить" if has_token else "Добавить"
    return with_menu([[InlineKeyboardButton(text=label, url=url)]])


def has_vk_token(config: AppConfig, store: SecretStore | None = None) -> bool:
    stored = ""
    if store is not None:
        stored = store.get(config.vk.token_env) or ""
    return bool(stored.strip() or os.environ.get(config.vk.token_env, "").strip())


def vk_token_status(
    config: AppConfig, store: SecretStore | None = None, *, now: float | None = None
):
    secrets = store
    expires: str | None = None
    try:
        if secrets is None:
            secrets = SecretStore(config.data_dir())
        expires = secrets.get(EXPIRES_NAME)
        present = has_vk_token(config, secrets)
    except SecretError:
        present = has_vk_token(config, None)
        expires = None
    return describe_token(
        has_token=present,
        expires_at=expires,
        now=time.time() if now is None else now,
    )


def format_vk_token_message(
    config: AppConfig,
    *,
    has_token: bool,
    status_message: str = "",
    remind: bool = False,
    expired: bool = False,
) -> tuple[str, InlineKeyboardMarkup | None, str | None]:
    """Текст /token, кнопка-ссылка и какое поле ждать дальше."""
    if remind and expired:
        head = (
            "Токен VK пора продлить. Нажмите «Обновить», разрешите доступ "
            "и пришлите сюда адрес пустой страницы."
        )
    elif remind:
        head = (
            "Токен VK истечёт примерно через час. Нажмите «Обновить», "
            "разрешите доступ и пришлите сюда адрес пустой страницы."
        )
    else:
        head = status_message
    wait: str | None = None
    markup: InlineKeyboardMarkup | None = menu_markup()
    extra = ""
    if config.vk.app_id is None:
        extra = _APP_ID_HELP
        wait = "app_id"
    else:
        markup = token_keyboard(
            app_id=config.vk.app_id,
            api_version=config.vk.api_version,
            has_token=has_token,
        )
        if config.vk.group_id is None:
            extra = _GROUP_ID_HELP
            wait = "group_id"
    text = head if not extra else f"{head}\n\n{extra}"
    return text, markup, wait


def vk_token_prompt(config: AppConfig) -> tuple[str, InlineKeyboardMarkup | None, str | None]:
    status = vk_token_status(config)
    return format_vk_token_message(
        config,
        has_token=status.kind != "missing",
        status_message=status.message,
    )


def set_vk_wait(field: str | None) -> None:
    global _vk_wait_field
    _vk_wait_field = field


def vk_wait_field() -> str | None:
    return _vk_wait_field


def vk_reminder(
    config: AppConfig,
    store: SecretStore,
    *,
    now: float,
) -> tuple[str, InlineKeyboardMarkup | None, str] | None:
    """Текст напоминания или None, если писать рано или уже писали про этот срок."""
    expires = store.get(EXPIRES_NAME)
    if not has_vk_token(config, store) or not should_remind_token(
        expires_at=expires,
        reminded_at=store.get(REMINDED_NAME),
        now=now,
    ):
        return None
    if expires is None:
        return None
    expired = expires.isdigit() and int(expires) - int(now) <= 0
    text, markup, wait = format_vk_token_message(
        config,
        has_token=True,
        remind=True,
        expired=expired,
    )
    set_vk_wait(wait)
    return text, markup, expires


async def remind_vk_token(config: AppConfig, bot: Bot, *, now: float | None = None) -> bool:
    """Написать в чат один раз, когда до конца токена остался час или срок уже вышел."""
    try:
        store = SecretStore(config.data_dir())
        payload = vk_reminder(config, store, now=time.time() if now is None else now)
        if payload is None:
            return False
        text, markup, expires = payload
        _token, chat_id = resolve_telegram(config.telegram)
        del _token
        await bot.send_message(chat_id, text, reply_markup=markup)
        store.set_many({REMINDED_NAME: expires})
    except (SecretError, ConfigError, TelegramAPIError) as exc:
        logger.error("Напоминание про токен VK: %s", exc)
        return False
    return True


def format_pending(
    items: list[StoredItem],
    *,
    offset: int,
    total: int,
    genre: str | None = None,
) -> str:
    if total <= 0 or not items:
        if genre:
            return f"Сейчас нет записей жанра «{genre}», которые ждут решения."
        return "Сейчас нет записей, которые ждут решения."
    page = offset // PAGE_SIZE + 1
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    lines = [f"Ждут решения: {total}"]
    if genre:
        lines.append(f"Жанр: {genre}")
    lines.extend(
        [
            f"Страница {page} из {pages}",
            "",
            "Нажмите фильм — пришлю карточку с описанием и постером.",
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class VkEntry:
    """Один фильм в списке «загружено во VK»."""

    item: StoredItem
    watch_url: str
    owner_id: int | None = None
    video_id: int | None = None

    @property
    def key(self) -> str:
        if self.owner_id is not None and self.video_id is not None:
            return f"vid:{self.owner_id}_{self.video_id}"
        return f"row:{self.item.row_id}"


def vk_watch_keyboard(
    urls: tuple[str, ...],
    *,
    topic_url: str | None = None,
    kinopoisk_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Кнопки просмотра. Ссылка больше не пишется текстом в карточке."""
    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        button
        for index, url in enumerate(urls, start=1)
        if (
            button := _url_button(
                "Смотреть во VK" if len(urls) == 1 else f"Смотреть во VK {index}",
                url,
            )
        )
        is not None
    ]
    for button in buttons:
        rows.append([button])
    links = [
        button
        for button in (
            _url_button("Рутрекер", rutracker_topic(topic_url or "") or topic_url),
            _url_button("Кинопоиск", kinopoisk_url),
        )
        if button is not None
    ]
    if links:
        rows.append(links)
    return with_menu(rows)


def uploaded_view(
    *,
    title: str,
    description: str = "",
    stored: StoredItem | None = None,
    movie: MovieInfo | None = None,
    poster_url: str | None = None,
    urls: tuple[str, ...] = (),
) -> tuple[str, str | None, InlineKeyboardMarkup]:
    """Карточка готового ролика: те же поля, что у очереди, и кнопка VK."""
    rating = None if movie is None else movie.rating
    quality = _quality_from_title(title)
    genre = genres_from_title(title)
    country = None if movie is None else movie.country
    seeders = None
    size = None
    overview = (description or "").strip()
    if overview.startswith(("http://", "https://", "magnet:")):
        overview = ""
    topic = None
    kinopoisk = None if movie is None else movie.kinopoisk_url
    poster = poster_url
    display = title.strip() or "Видео"
    if movie is not None:
        year = f" ({movie.year})" if movie.year else ""
        display = f"{movie.title}{year}"
        overview = movie.overview.strip() or overview
        poster = movie.poster_url or poster
    if stored is not None:
        if movie is None:
            named = (
                (stored.movie_title or "").strip() or release_title(stored.title) or stored.title
            )
            display = named or display
        if stored.rating is not None:
            rating = stored.rating
        quality = stored.quality or quality
        genre = stored.genre or genres_from_title(stored.title) or genre
        country = stored.country or country
        seeders = stored.seeders
        size = stored.size_bytes
        overview = (stored.overview or "").strip() or overview
        poster = stored.poster_url or poster
        topic = stored.topic_url
        kinopoisk = stored.kinopoisk_url or kinopoisk
    card = FeedItem(
        guid="vk",
        feed_name="vk",
        title=display,
        link="",
        published=None,
        torrent_url=None,
        rating=rating,
        quality=quality,
        genre=genre,
        overview=overview or None,
        poster_url=poster,
        seeders=seeders,
        size_bytes=size,
        topic_url=topic,
        kinopoisk_url=kinopoisk,
        country=country,
    )
    text = format_card(card, waiting=False, heading="Загружено во VK")
    markup = vk_watch_keyboard(urls, topic_url=topic, kinopoisk_url=kinopoisk)
    return text, poster, markup


def _quality_from_title(title: str) -> str | None:
    found = re.search(r"\b(2160p|1080p|720p|480p)\b", title, re.IGNORECASE)
    if found is None:
        return None
    return found.group(1).lower()


def vk_entries(
    rows: list[StoredItem],
    videos: tuple[VkCatalogItem, ...] | None = None,
) -> list[VkEntry]:
    """Список для меню. Если VK ответил — все его ролики, иначе фильмы из базы со ссылкой."""
    if videos is None:
        return [_entry_from_row(row) for row in rows if (row.vk_url or "").strip()]
    found: list[VkEntry] = []
    for video in videos:
        if video.processing:
            continue
        watch = video.player_url if video.player_url.startswith("https://") else video.page_url
        if not watch.startswith("https://"):
            continue
        stored = _row_for_video(rows, video)
        found.append(
            VkEntry(
                item=stored or _stub_item(video),
                watch_url=watch,
                owner_id=video.owner_id,
                video_id=video.video_id,
            )
        )
    return found


def _entry_from_row(row: StoredItem) -> VkEntry:
    urls = [line.strip() for line in (row.vk_url or "").splitlines() if line.strip()]
    watch = next((line for line in urls if line.startswith("https://")), urls[0] if urls else "")
    return VkEntry(item=row, watch_url=watch)


def _stub_item(video: VkCatalogItem) -> StoredItem:
    title = video.title.strip() or f"Видео {video.video_id}"
    return StoredItem(
        row_id=0,
        guid=f"vk:{video.owner_id}_{video.video_id}",
        feed_name="vk",
        title=title,
        link=video.page_url,
        torrent_url=None,
        first_seen="",
        status=APPROVED,
        rating=None,
        quality=_quality_from_title(title),
        genre=genres_from_title(title),
        overview=video.description.strip() or None,
        poster_url=video.image_url or None,
        movie_title=title,
    )


def _row_for_video(rows: list[StoredItem], video: VkCatalogItem) -> StoredItem | None:
    marker = f"video{video.owner_id}_{video.video_id}"
    title = video.title.strip()
    for row in rows:
        blob = f"{row.vk_uuid or ''}\n{row.vk_url or ''}"
        if marker in blob:
            return row
    short = release_title(title)
    for row in rows:
        if title and title in {(row.title or "").strip(), (row.movie_title or "").strip()}:
            return row
        if short and short == release_title(row.title or ""):
            return row
    return None


def load_vk_entries(config: AppConfig) -> tuple[list[VkEntry], str]:
    """Прочитать ролики VK. Если API молчит — взять фильмы, у которых в базе уже есть ссылка."""
    with SeenStore(config.database_path) as store:
        rows = store.list_seen()
    note = ""
    videos: tuple[VkCatalogItem, ...] | None = None
    if vk_ready(config):
        token = os.environ.get(config.vk.token_env, "").strip()
        try:
            videos = VkVideoClient(config.vk, token).list_videos(limit=500)
        except VkError as exc:
            logger.warning("Список VK не прочитался: %s", exc)
            note = "VK сейчас не ответил. Показываю фильмы из базы."
    return vk_entries(rows, videos), note


def format_vk_list(total: int, *, offset: int, note: str = "") -> str:
    if total <= 0:
        text = "Во VK пока нет загруженных фильмов."
        return f"{text}\n{note}" if note else text
    page = offset // PAGE_SIZE + 1
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    lines = [
        f"Загружено во VK: {total}",
        f"Страница {page} из {pages}",
        "",
        "Нажмите фильм — пришлю карточку с описанием и постером.",
    ]
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def vk_list_keyboard(
    entries: list[VkEntry],
    *,
    offset: int,
    total: int,
) -> InlineKeyboardMarkup:
    """Десять кнопок фильмов и листание, как в очереди ожидания."""
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries:
        if entry.owner_id is not None and entry.video_id is not None:
            data = f"vkvid:{entry.owner_id}_{entry.video_id}"
        else:
            data = f"vkopen:{entry.item.row_id}"
        rows.append([InlineKeyboardButton(text=movie_button_label(entry.item), callback_data=data)])
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"vkpage:{max(offset - PAGE_SIZE, 0)}",
            )
        )
    if offset + len(entries) < total:
        nav.append(
            InlineKeyboardButton(
                text="▶️ Дальше",
                callback_data=f"vkpage:{offset + PAGE_SIZE}",
            )
        )
    if nav:
        rows.append(nav)
    return with_menu(rows)


def vk_view(
    entries: list[VkEntry],
    offset: int,
    *,
    note: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    ordered = sorted(entries, key=lambda entry: (*_pending_order(entry.item), entry.key))
    total = len(ordered)
    if total <= 0:
        page_offset = 0
        page: list[VkEntry] = []
    else:
        last = ((total - 1) // PAGE_SIZE) * PAGE_SIZE
        page_offset = max(0, min((max(offset, 0) // PAGE_SIZE) * PAGE_SIZE, last))
        page = ordered[page_offset : page_offset + PAGE_SIZE]
    return (
        format_vk_list(total, offset=page_offset, note=note),
        vk_list_keyboard(page, offset=page_offset, total=total),
    )


def pending_genre_names(items: list[StoredItem]) -> list[str]:
    names: set[str] = set()
    for item in items:
        names.update(genre_parts(item.genre, pretty=True))
    return sorted(names, key=str.casefold)


def _filter_pending(items: list[StoredItem], genre: str | None) -> list[StoredItem]:
    if not genre:
        return items
    return [item for item in items if genre in genre_parts(item.genre, pretty=True)]


def load_pending_items(db_path: Path) -> list[StoredItem]:
    with SeenStore(db_path) as store:
        total = store.counts().get("pending", 0)
        if total <= 0:
            return []
        return sorted(store.list_pending(limit=total, offset=0), key=_pending_order)


def pending_view(
    db_path: Path,
    offset: int,
    *,
    genre: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст меню и кнопки одной страницы очереди."""
    raw = load_pending_items(db_path)
    names = pending_genre_names(raw)
    items = _filter_pending(raw, genre)
    total = len(items)
    if total <= 0:
        page_offset = 0
        page: list[StoredItem] = []
        genre_index = None
    else:
        last = ((total - 1) // PAGE_SIZE) * PAGE_SIZE
        page_offset = max(0, min((max(offset, 0) // PAGE_SIZE) * PAGE_SIZE, last))
        page = items[page_offset : page_offset + PAGE_SIZE]
        genre_index = names.index(genre) if genre and genre in names else None
    return (
        format_pending(page, offset=page_offset, total=total, genre=genre),
        pending_keyboard(page, offset=page_offset, total=total, genre_index=genre_index),
    )


def genres_keyboard(names: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"genre:{index}")]
        for index, name in enumerate(names)
    ]
    rows.append([InlineKeyboardButton(text="Все жанры", callback_data="pending:all")])
    return with_menu(rows)


def root_menu_keyboard() -> InlineKeyboardMarkup:
    return with_menu(
        [
            [InlineKeyboardButton(text="Дашборд", callback_data="menu:dash")],
            [InlineKeyboardButton(text="Ожидают", callback_data="menu:pending")],
            [InlineKeyboardButton(text="Загрузки", callback_data="menu:loads")],
            [InlineKeyboardButton(text="ВК", callback_data="menu:vk")],
        ]
    )


def pending_menu_keyboard() -> InlineKeyboardMarkup:
    return with_menu(
        [
            [InlineKeyboardButton(text="Список", callback_data="pending:list")],
            [InlineKeyboardButton(text="Жанры", callback_data="genres")],
            [InlineKeyboardButton(text="Все жанры", callback_data="pending:all")],
            [InlineKeyboardButton(text="Обновить ленту", callback_data="update")],
            [InlineKeyboardButton(text="Очистить очередь", callback_data="clear")],
        ]
    )


def loads_menu_keyboard() -> InlineKeyboardMarkup:
    return with_menu(
        [
            [InlineKeyboardButton(text="Пауза всем", callback_data="pause")],
            [InlineKeyboardButton(text="Прислать торрент", callback_data="asktorrent")],
        ]
    )


def vk_section_keyboard() -> InlineKeyboardMarkup:
    return with_menu(
        [
            [InlineKeyboardButton(text="Просмотр", callback_data="vk:list")],
            [InlineKeyboardButton(text="Бот", callback_data="vk:token")],
        ]
    )


def format_clear_result(removed: int) -> str:
    if removed == 0:
        return "Неодобренных записей нет."
    return f"Из очереди убраны не одобренные. Удалено: {removed}. Одобренные остались."


def format_refresh_result(report: FetchReport) -> str:
    return "\n".join(
        [
            "Лента обновлена.",
            f"Новых в очереди: {len(report.accepted)}",
            f"Уже были в базе: {report.skipped_seen}",
            f"Не прошли фильтр: {report.skipped_filter}",
            f"Ошибок: {report.failed}",
        ]
    )


def format_settings(config: AppConfig) -> str:
    if config.filters.min_rating is None:
        rating = "выключен"
    else:
        rating = f"от {config.filters.min_rating:g}"
    if config.filters.max_age_days is None:
        age = "выключен"
    else:
        age = f"не старше {config.filters.max_age_days} дн."
    names = ", ".join(feed.name for feed in config.rss.feeds) or "список пуст"
    quality_keep = {
        "max": "максимальное",
        "average": "среднее",
        "min": "минимальное",
    }.get(config.filters.quality_keep, "максимальное")
    return "\n".join(
        [
            "Настройки",
            "",
            f"Рейтинг: {rating}",
            f"Возраст записей: {age}",
            f"Одинаковые фильмы: {quality_keep} качество",
            f"Ленты ({len(config.rss.feeds)}): {names}",
            f"База: {config.database_path}",
        ]
    )


def vpn_text(vpn: VpnManager) -> str:
    names = ", ".join(vpn.route_names())
    try:
        if vpn.is_up():
            return f"VPN {vpn.interface}: включён. Через туннель: {names}."
        return f"VPN {vpn.interface}: выключен. Когда включится, через туннель пойдут: {names}."
    except VpnError as exc:
        return f"VPN {vpn.interface}: не удалось проверить ({exc})."


_VPN_REFRESH_SECONDS = 600


def _vpn_for(config: AppConfig) -> VpnManager:
    return VpnManager(
        interface=config.vpn.interface,
        config_path=config.vpn.config_path,
        timeout_seconds=config.vpn.command_timeout_seconds,
        domains=config.routing.vpn_domains,
    )


async def _keep_split_vpn(config: AppConfig, config_path: Path | None) -> None:
    """Держать туннель включённым и обновлять адреса доменов раз в 10 минут."""
    previous = ""
    while True:
        current = config
        if config_path is not None:
            try:
                current = load_config(config_path)
            except ConfigError as exc:
                logger.error("%s", exc)
                await asyncio.sleep(_VPN_REFRESH_SECONDS)
                continue
        try:
            text = await asyncio.to_thread(_vpn_for(current).ensure)
        except VpnError as exc:
            logger.error("Маршруты VPN: %s", exc)
        else:
            if text != previous:
                logger.info("%s", text)
                previous = text
        await asyncio.sleep(_VPN_REFRESH_SECONDS)


async def send_card(token: str, chat_id: int, item: FeedItem | StoredItem, row_id: int) -> None:
    bot = Bot(token=token)
    text = format_card(item)
    markup = approval_keyboard(
        row_id,
        topic_url=item.topic_url,
        kinopoisk_url=item.kinopoisk_url,
    )
    try:
        if item.poster_url:
            await bot.send_photo(
                chat_id,
                photo=item.poster_url,
                caption=text,
                reply_markup=markup,
            )
            return
        await bot.send_message(chat_id, text, reply_markup=markup)
    finally:
        await bot.session.close()


def deliver_notice(
    token: str,
    chat_id: str,
    caption: str,
    poster_url: str | None,
    timeout: float,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправить карточку. Если есть обложка — методом send_photo, иначе текстом."""
    try:
        asyncio.run(_deliver_notice(token, chat_id, caption, poster_url, timeout, reply_markup))
    except NotifyError:
        raise
    except Exception as exc:
        raise NotifyError("Не удалось отправить сообщение в Telegram.") from exc


async def _deliver_notice(
    token: str,
    chat_id: str,
    caption: str,
    poster_url: str | None,
    timeout: float,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    bot = Bot(token=token, session=AiohttpSession(timeout=timeout))
    try:
        if poster_url:
            await bot.send_photo(
                chat_id,
                photo=poster_url,
                caption=caption,
                reply_markup=reply_markup,
            )
            logger.info("Telegram принял обложку, чат %s.", chat_id)
            return
        await bot.send_message(chat_id, caption, reply_markup=reply_markup)
        logger.info("Telegram принял сообщение, чат %s.", chat_id)
    except TelegramBadRequest as exc:
        if not poster_url:
            raise NotifyError(f"Telegram отклонил сообщение: {exc}") from exc
        logger.warning("Обложка не отправилась, пишу текстом: %s", exc)
        try:
            await bot.send_message(chat_id, caption, reply_markup=reply_markup)
        except TelegramBadRequest as second:
            raise NotifyError(f"Telegram отклонил сообщение: {second}") from second
    finally:
        await bot.session.close()


async def _release_telegram(bot: Bot | None) -> None:
    """Закрыть текущее соединение с Telegram.

    Нужно только если туннель пересобирают целиком. Обычный опрос ленты
    маршрут по умолчанию не меняет: закрывать сессию тогда нельзя, иначе
    aiogram пишет ошибку Server disconnected и кнопки на минуту пропадают.
    """
    if bot is None:
        return
    await bot.session.close()


async def process_portal_jobs(
    config: AppConfig,
    already: set[int] | None = None,
) -> int:
    """Выполнить решения, которые нажали на сайте. Та же база, что у кнопок бота."""
    handled = 0
    for job in drain_jobs(jobs_path(config.data_dir())):
        if job["status"] != APPROVED:
            continue
        row_id = job["row_id"]
        if not isinstance(row_id, int):
            continue
        with SeenStore(config.database_path) as store:
            item = store.get(row_id)
        if item is None or item.status != APPROVED:
            continue
        link = magnet_link(item.torrent_url)
        if link:
            await asyncio.to_thread(queue_link, config, link, item.title)
            if already is not None:
                already.add(row_id)
            handled += 1
            logger.info("«%s» поставлен в qBittorrent по magnet-ссылке.", item.title)
            continue
        vpn = VpnManager(
            interface=config.vpn.interface,
            config_path=config.vpn.config_path,
            timeout_seconds=config.vpn.command_timeout_seconds,
        )
        try:
            path = await asyncio.to_thread(deliver_approved, config, vpn, item)
        except (VpnError, FetchError) as exc:
            logger.error("Портал: не скачалось «%s»: %s", item.title, exc)
            continue
        handled += 1
        if path is None:
            continue
        await asyncio.to_thread(queue_download, config, path, item.title)
        if already is not None:
            already.add(row_id)
        if not vk_ready(config):
            logger.info(
                "«%s» поставлен в qBittorrent. Токен VK не задан, выкладку пропускаю.",
                item.title,
            )
            continue
        await _publish(config, item, path)
    return handled


def queue_saved_torrents(config: AppConfig, already: set[int]) -> int:
    """Поставить в qBittorrent одобренные фильмы, у которых .torrent уже лежит на диске.

    Повторно одну и ту же запись не отправляет. Так подхватываются фильмы,
    одобренные раньше, когда загрузка ещё ждала токен VK.
    """
    with SeenStore(config.database_path) as store:
        items = store.list_status(APPROVED)
    queued = 0
    for item in items:
        if item.row_id in already or not item.torrent_url:
            continue
        link = magnet_link(item.torrent_url)
        if link:
            queue_link(config, link, item.title)
            already.add(item.row_id)
            queued += 1
            continue
        path = torrent_path(config.rss.metadata_dir, item.torrent_url)
        if not path.is_file():
            continue
        queue_download(config, path, item.title)
        already.add(item.row_id)
        queued += 1
    return queued


async def _portal_loop(
    config: AppConfig,
    config_path: Path | None,
    bot: Bot | None = None,
) -> None:
    """Подхватывать кнопки сайта и по интервалу из настроек перечитывать ленту."""
    last_fetch = 0.0
    already_queued: set[int] = set()
    upload_task: asyncio.Task | None = None
    while True:
        await asyncio.sleep(3)
        current = config
        if config_path is not None:
            try:
                current = load_config(config_path)
            except ConfigError as exc:
                logger.error("Настройки не прочитались: %s", exc)
                continue
        try:
            SecretStore(current.data_dir()).apply_to_environ()
        except SecretError as exc:
            logger.error("%s", exc)
        if bot is not None:
            await remind_vk_token(current, bot)
        try:
            await process_portal_jobs(current, already_queued)
            await asyncio.to_thread(queue_saved_torrents, current, already_queued)
        except sqlite3.OperationalError as exc:
            logger.error("База не открылась: %s. Следующая попытка через 30 с.", exc)
            await asyncio.sleep(30)
        except Exception:
            logger.exception("Не удалось обработать решение с портала")
        if upload_task is not None and upload_task.done():
            failed = upload_task.exception()
            if failed is not None:
                logger.error("Отправка готового торрента в VK: %s", failed)
            upload_task = None
        if upload_task is None and vk_ready(current) and current.streaming.enabled:
            upload_task = asyncio.create_task(asyncio.to_thread(publish_finished, current))
        interval = current.rss.poll_interval_minutes * 60
        if time.monotonic() - last_fetch < interval:
            continue
        if not current.jackett.rss_url and not current.rss.feeds:
            continue
        last_fetch = time.monotonic()
        vpn = VpnManager(
            interface=current.vpn.interface,
            config_path=current.vpn.config_path,
            timeout_seconds=current.vpn.command_timeout_seconds,
        )
        logger.info("Опрос ленты по расписанию, каждые %s мин.", current.rss.poll_interval_minutes)
        try:
            await asyncio.to_thread(
                run_fetch,
                current,
                vpn,
                notify=notifier_from_env(current),
            )
        except (FetchError, VpnError, ConfigError) as exc:
            logger.error("Опрос ленты остановлен: %s", exc)


async def run_watch(config: AppConfig, *, config_path: Path | None = None) -> None:
    """Опрашивать ленту без Telegram. Настройки читаются заново перед каждым кругом."""
    logger.info(
        "Служба ленты запущена. Опрос каждые %s мин. Telegram для этого не нужен.",
        config.rss.poll_interval_minutes,
    )
    refresher = asyncio.create_task(_keep_split_vpn(config, config_path))
    try:
        await _portal_loop(config, config_path)
    finally:
        refresher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresher


async def run_bot(config: AppConfig, *, config_path: Path | None = None) -> None:
    """Слушать Telegram, пока процесс не остановят."""
    token, chat_id = resolve_telegram(config.telegram)
    bot = Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.set_my_commands(BOT_COMMANDS)
    gate = _OwnerOnly(chat_id)
    dispatcher.message.middleware(gate)
    dispatcher.callback_query.middleware(gate)
    logger.info(
        "Бот запущен. Команды принимает только пользователь %s. Лента опрашивается каждые %s мин.",
        chat_id,
        config.rss.poll_interval_minutes,
    )
    refresher = asyncio.create_task(_keep_split_vpn(config, config_path))
    watcher = asyncio.create_task(_portal_loop(config, config_path, bot))
    try:
        await dispatcher.start_polling(
            bot,
            allowed_chat_id=chat_id,
            db_path=config.database_path,
            app_config=config,
            config_path=config_path,
        )
    finally:
        refresher.cancel()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresher
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def is_allowed(user_id: int | None, owner_id: int) -> bool:
    """True только для пользователя, чей id записан в POTOK_TELEGRAM_CHAT_ID."""
    return user_id is not None and user_id == owner_id


class _OwnerOnly(BaseMiddleware):
    """Чужие сообщения не доходят до команд и не получают ответ."""

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        user_id = user.id if user is not None else None
        if is_allowed(user_id, self.owner_id) or _group_torrent(event, self.owner_id):
            return await handler(event, data)
        logger.info("Чужой пользователь %s проигнорирован.", user_id)
        if isinstance(event, CallbackQuery):
            await event.answer()
        return None


def _group_torrent(event: object, allowed_chat_id: int) -> bool:
    """В группе, чей id записан в настройках, принимаем только файл .torrent."""
    if not isinstance(event, Message) or event.document is None:
        return False
    return accepts_torrent(
        None,
        event.chat.id,
        event.document.file_name,
        allowed_chat_id,
    )


def _user_id(message: Message) -> int | None:
    if message.from_user is None:
        return None
    return message.from_user.id


def _word(text: str | None) -> str:
    return (text or "").strip().casefold()


@router.message(CommandStart())
async def cmd_start(message: Message, allowed_chat_id: int) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    await message.answer(
        "Поток присылает новые записи из ленты.\n"
        "«Одобрить» — скачать видео и загрузить его в VK. «Отклонить» — пропустить.\n"
        "Ссылка на плеер придёт отдельным сообщением.\n\n"
        "/status — сколько записей в каждом статусе\n"
        "/pending — что ещё ждёт решения\n"
        "/vk — фильмы, уже загруженные во VK\n"
        "/token — срок токена VK и страница разрешения\n"
        "/update — заново прочитать ленту, уже загруженное не добавлять\n"
        "/clear — удалить очередь загрузки\n\n"
        "Кнопка «Меню» внизу сообщений открывает дашборд, ожидание, загрузки и ВК.\n"
        "Можно написать словами: обновить, очистить или токен.\n"
        "Можно прислать файл .torrent: бот скачает видео и загрузит его в VK.",
        reply_markup=menu_markup(),
    )


@router.message(Command("token"))
@router.message(F.text.func(lambda text: _word(text) == "токен"))
async def cmd_token(
    message: Message,
    allowed_chat_id: int,
    app_config: AppConfig,
) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    text, markup, wait = vk_token_prompt(app_config)
    set_vk_wait(wait)
    await message.answer(text, reply_markup=markup)


@router.message(F.text.func(lambda text: bool(text) and is_token_paste(text)))
async def on_vk_token_paste(
    message: Message,
    allowed_chat_id: int,
    app_config: AppConfig,
) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    try:
        store = SecretStore(app_config.data_dir())
        note = save_vk_token(
            message.text or "",
            token_env=app_config.vk.token_env,
            api_version=app_config.vk.api_version,
            timeout=app_config.vk.api_timeout_seconds,
            store=store,
        )
    except (VkOAuthError, SecretError) as exc:
        await message.answer(str(exc), reply_markup=menu_markup())
        return
    logger.info("Токен VK сохранён из Telegram.")
    with contextlib.suppress(TelegramAPIError):
        await message.delete()
    await message.answer(note, reply_markup=menu_markup())


@router.message(F.text.func(lambda text: _waiting_vk_id(text)))
async def on_vk_id_number(
    message: Message,
    allowed_chat_id: int,
    app_config: AppConfig,
    dispatcher: Dispatcher,
    config_path: Path | None = None,
) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    field = vk_wait_field()
    value = parse_vk_numeric_id(message.text or "")
    if field is None or value is None:
        return
    if config_path is None:
        await message.answer(
            "Не могу записать ID: не найден файл настроек.",
            reply_markup=menu_markup(),
        )
        return
    try:
        save_vk_id(config_path, field, value)
        loaded = load_config(config_path)
    except (SettingsError, ConfigError, OSError) as exc:
        await message.answer(f"Не удалось записать ID.\n{exc}", reply_markup=menu_markup())
        return
    dispatcher.workflow_data["app_config"] = loaded
    if field == "app_id":
        await message.answer(f"ID приложения записан: {value}.", reply_markup=menu_markup())
    else:
        await message.answer(f"ID сообщества записан: {value}.", reply_markup=menu_markup())
    text, markup, wait = vk_token_prompt(loaded)
    set_vk_wait(wait)
    await message.answer(text, reply_markup=markup)


def _waiting_vk_id(text: str | None) -> bool:
    if vk_wait_field() is None or not text:
        return False
    if is_token_paste(text):
        return False
    return parse_vk_numeric_id(text) is not None


@router.message(Command("status"))
async def cmd_status(
    message: Message,
    allowed_chat_id: int,
    db_path: Path,
    app_config: AppConfig,
) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    vpn = VpnManager(
        interface=app_config.vpn.interface,
        config_path=app_config.vpn.config_path,
        timeout_seconds=app_config.vpn.command_timeout_seconds,
        domains=app_config.routing.vpn_domains,
    )
    with SeenStore(db_path) as store:
        text = format_status(
            store.counts(),
            vpn_text(vpn),
            token_line=vk_token_status(app_config).message,
        )
    await message.answer(text, reply_markup=menu_markup())


@router.message(Command("pending"))
async def cmd_pending(message: Message, allowed_chat_id: int, db_path: Path) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    text, markup = pending_view(db_path, 0)
    await message.answer(text, reply_markup=markup)


@router.message(Command("vk"))
@router.message(F.text.func(lambda text: _word(text) in {"вк", "vk"}))
async def cmd_vk(message: Message, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    text, markup = await _vk_page(app_config, 0)
    await message.answer(text, reply_markup=markup)


@router.message(Command("clear"))
@router.message(F.text.func(lambda text: _word(text) == "очистить"))
async def cmd_clear(message: Message, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    removed = drop_unapproved(app_config)
    await message.answer(format_clear_result(removed), reply_markup=menu_markup())


@router.message(Command("update"))
@router.message(F.text.func(lambda text: _word(text) == "обновить"))
async def cmd_update(
    message: Message,
    allowed_chat_id: int,
    app_config: AppConfig,
) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    await message.answer(
        "Читаю ленту. Одобренные повторно не добавлю.",
        reply_markup=menu_markup(),
    )
    report = await _fetch_report(app_config)
    if isinstance(report, str):
        await message.answer(report, reply_markup=menu_markup())
        return
    await message.answer(format_refresh_result(report), reply_markup=menu_markup())


@router.message(Command("settings"))
async def cmd_settings(message: Message, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(_user_id(message), allowed_chat_id):
        return
    await message.answer(format_settings(app_config), reply_markup=menu_markup())


@router.message(F.document)
async def on_torrent_file(
    message: Message,
    allowed_chat_id: int,
    app_config: AppConfig,
    bot: Bot,
) -> None:
    """Файл .torrent из чата: сохранить, скачать видео через aria2c и загрузить в VK."""
    document = message.document
    if document is None or not accepts_torrent(
        _user_id(message),
        message.chat.id,
        document.file_name,
        allowed_chat_id,
    ):
        return
    name = torrent_basename(document.file_name or "")
    if name is None:
        await message.answer("Пришлите файл с расширением .torrent.", reply_markup=menu_markup())
        return
    if document.file_size is not None and document.file_size > _TORRENT_LIMIT:
        await message.answer(
            "Файл .torrent больше 2 МБ, такой не беру.",
            reply_markup=menu_markup(),
        )
        return
    if not vk_ready(app_config):
        await message.answer(
            f"Токен VK не задан ({app_config.vk.token_env}), видео не загружаю.",
            reply_markup=menu_markup(),
        )
        return
    folder = app_config.rss.metadata_dir
    folder.mkdir(parents=True, exist_ok=True)
    target = _fresh_torrent(folder, name)
    try:
        await bot.download(document, destination=target)
    except (TelegramAPIError, OSError) as exc:
        target.unlink(missing_ok=True)
        logger.error("Не удалось забрать .torrent из Telegram: %s", exc)
        await message.answer(
            f"Не удалось забрать файл из Telegram.\n{exc}",
            reply_markup=menu_markup(),
        )
        return
    try:
        size = target.stat().st_size
    except OSError as exc:
        await message.answer(f"Файл не сохранился.\n{exc}", reply_markup=menu_markup())
        return
    if size <= 0 or size > _TORRENT_LIMIT:
        target.unlink(missing_ok=True)
        await message.answer("Файл пустой или больше 2 МБ.", reply_markup=menu_markup())
        return
    title = release_title(name) or Path(name).stem
    await message.answer(
        f"Принял «{title}».\nСкачиваю видео и загружаю в VK. Ссылка придёт отдельным сообщением.",
        reply_markup=menu_markup(),
    )
    await _publish_torrent(app_config, target, title)


def accepts_torrent(
    user_id: int | None,
    chat_id: int,
    file_name: str | None,
    allowed_chat_id: int,
) -> bool:
    """Файл .torrent можно прислать владельцу или в чат, чей id записан в настройках."""
    if is_allowed(user_id, allowed_chat_id):
        return bool(file_name and file_name.lower().endswith(".torrent"))
    if chat_id != allowed_chat_id:
        return False
    return bool(file_name and file_name.lower().endswith(".torrent"))


def _fresh_torrent(folder: Path, name: str) -> Path:
    target = folder / name
    if not target.exists():
        return target
    stem = Path(name).stem
    for index in range(2, 100):
        candidate = folder / f"{stem}-{index}.torrent"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}-{time.time_ns()}.torrent"


def drop_unapproved(config: AppConfig) -> int:
    """Убрать из очереди ожидающие и отклонённые. Одобренные не трогать."""
    with SeenStore(config.database_path) as store:
        return store.delete_unapproved()


async def _fetch_report(config: AppConfig) -> FetchReport | str:
    vpn = VpnManager(
        interface=config.vpn.interface,
        config_path=config.vpn.config_path,
        timeout_seconds=config.vpn.command_timeout_seconds,
    )
    try:
        report = await asyncio.to_thread(
            run_fetch,
            config,
            vpn,
            notify=notifier_from_env(config),
        )
        await asyncio.to_thread(fill_pending_details, config)
    except (FetchError, VpnError, ConfigError) as exc:
        return f"Не удалось обновить ленту.\n{exc}"
    return report


def dashboard_view(config: AppConfig) -> tuple[str, InlineKeyboardMarkup]:
    numbers = dashboard_numbers(config)
    token_line = vk_token_status(config).message
    return format_dashboard(numbers, token_line), root_menu_keyboard()


def _root_menu_text() -> str:
    return "Меню Потока\n\nВыберите раздел: дашборд, ожидание, загрузки или ВК."


async def _edit_menu(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    try:
        if query.message.photo:
            await query.message.edit_caption(caption=text, reply_markup=markup)
        else:
            await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        logger.info("Меню не изменилось: %s", exc)


@router.callback_query(F.data == "menu")
async def on_menu(query: CallbackQuery, allowed_chat_id: int) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    await query.answer()
    if isinstance(query.message, Message):
        await query.message.answer(_root_menu_text(), reply_markup=root_menu_keyboard())


@router.callback_query(F.data == "menu:dash")
async def on_menu_dash(query: CallbackQuery, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    text, markup = dashboard_view(app_config)
    await _edit_menu(query, text, markup)


@router.callback_query(F.data == "menu:pending")
async def on_menu_pending(query: CallbackQuery, allowed_chat_id: int) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    await _edit_menu(
        query,
        "Ожидают решения\n\nСписок фильмов, жанры, обновление ленты и очистка очереди.",
        pending_menu_keyboard(),
    )


@router.callback_query(F.data == "menu:loads")
async def on_menu_loads(query: CallbackQuery, allowed_chat_id: int) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    await _edit_menu(
        query,
        "Загрузки\n\nМожно поставить все раздачи на паузу или прислать файл .torrent.",
        loads_menu_keyboard(),
    )


@router.callback_query(F.data == "menu:vk")
async def on_menu_vk(query: CallbackQuery, allowed_chat_id: int) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    await _edit_menu(
        query,
        "ВК\n\nПросмотр загруженных фильмов или экран токена бота.",
        vk_section_keyboard(),
    )


@router.callback_query(F.data.in_({"pending:list", "pending:all"}))
async def on_pending_list(query: CallbackQuery, allowed_chat_id: int, db_path: Path) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    text, markup = pending_view(db_path, 0)
    await _edit_menu(query, text, markup)


@router.callback_query(F.data == "genres")
async def on_genres(query: CallbackQuery, allowed_chat_id: int, db_path: Path) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    names = pending_genre_names(load_pending_items(db_path))
    if not names:
        await _edit_menu(
            query,
            "В очереди сейчас нет жанров.",
            pending_menu_keyboard(),
        )
        return
    await _edit_menu(query, "Выберите жанр из очереди ожидания.", genres_keyboard(names))


@router.callback_query(F.data.regexp(r"^genre:\d+$"))
async def on_genre(query: CallbackQuery, allowed_chat_id: int, db_path: Path) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    raw = (query.data or "").partition(":")[2]
    index = int(raw) if raw.isdigit() else -1
    names = pending_genre_names(load_pending_items(db_path))
    if index < 0 or index >= len(names):
        await query.answer("Такого жанра уже нет", show_alert=True)
        return
    text, markup = pending_view(db_path, 0, genre=names[index])
    await _edit_menu(query, text, markup)


@router.callback_query(F.data.regexp(r"^gpage:\d+:\d+$"))
async def on_genre_page(query: CallbackQuery, allowed_chat_id: int, db_path: Path) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    _, genre_raw, offset_raw = (query.data or "").split(":", 2)
    genre_index = int(genre_raw) if genre_raw.isdigit() else -1
    offset = int(offset_raw) if offset_raw.isdigit() else 0
    names = pending_genre_names(load_pending_items(db_path))
    if genre_index < 0 or genre_index >= len(names):
        await query.answer("Такого жанра уже нет", show_alert=True)
        return
    text, markup = pending_view(db_path, offset, genre=names[genre_index])
    await _edit_menu(query, text, markup)


@router.callback_query(F.data == "pause")
async def on_pause_all(query: CallbackQuery, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    if not app_config.streaming.enabled:
        await _edit_menu(
            query,
            "qBittorrent выключен в настройках. Паузу поставить нельзя.",
            loads_menu_keyboard(),
        )
        return
    try:
        stopped = await asyncio.to_thread(QbitClient(app_config.streaming).stop_all)
    except StreamError as exc:
        await _edit_menu(
            query,
            f"Не удалось поставить раздачи на паузу.\n{exc}",
            loads_menu_keyboard(),
        )
        return
    await _edit_menu(
        query,
        f"Все раздачи в qBittorrent на паузе. Остановлено: {stopped}.",
        loads_menu_keyboard(),
    )


@router.callback_query(F.data == "asktorrent")
async def on_ask_torrent(query: CallbackQuery, allowed_chat_id: int) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    await _edit_menu(
        query,
        "Пришлите файл .torrent в этот чат. Бот скачает видео и загрузит его в VK.",
        loads_menu_keyboard(),
    )


@router.callback_query(F.data == "vk:list")
async def on_vk_list(query: CallbackQuery, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    text, markup = await _vk_page(app_config, 0)
    await _edit_menu(query, text, markup)


@router.callback_query(F.data == "vk:token")
async def on_vk_bot_token(
    query: CallbackQuery,
    allowed_chat_id: int,
    app_config: AppConfig,
) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    text, markup, wait = vk_token_prompt(app_config)
    set_vk_wait(wait)
    await _edit_menu(query, text, markup or menu_markup())


@router.callback_query(F.data.regexp(r"^vkpage:\d+$"))
async def on_vk_page(query: CallbackQuery, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    raw = (query.data or "").partition(":")[2]
    offset = int(raw) if raw.isdigit() else 0
    text, markup = await _vk_page(app_config, offset)
    await query.answer()
    if not isinstance(query.message, Message):
        return
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        logger.info("Список VK не изменился: %s", exc)


@router.callback_query(F.data.regexp(r"^vkopen:\d+$"))
async def on_vk_open(query: CallbackQuery, allowed_chat_id: int, db_path: Path) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    raw = (query.data or "").partition(":")[2]
    if not raw.isdigit():
        await query.answer("Не понял кнопку")
        return
    with SeenStore(db_path) as store:
        item = store.get(int(raw))
    if item is None or not (item.vk_url or "").strip():
        await query.answer("Этого фильма уже нет во VK", show_alert=True)
        return
    urls = tuple(line.strip() for line in item.vk_url.splitlines() if line.strip())
    await query.answer()
    if isinstance(query.message, Message):
        text, poster, markup = uploaded_view(title=item.title, stored=item, urls=urls)
        await _send_photo_or_text(query.message, text, poster, markup)


@router.callback_query(F.data.regexp(r"^vkvid:-?\d+_\d+$"))
async def on_vk_video(query: CallbackQuery, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    owner_text, _, video_text = (query.data or "").partition(":")[2].partition("_")
    if not _signed_id(owner_text) or not video_text.isdigit():
        await query.answer("Не понял кнопку")
        return
    try:
        video, stored = await asyncio.to_thread(
            _vk_video_card,
            app_config,
            int(owner_text),
            int(video_text),
        )
    except VkError as exc:
        await query.answer(str(exc)[:180], show_alert=True)
        return
    if video is None:
        await query.answer("Этого видео уже нет во VK", show_alert=True)
        return
    watch = video.player_url if video.player_url.startswith("https://") else video.page_url
    await query.answer()
    if isinstance(query.message, Message):
        text, poster, markup = uploaded_view(
            title=video.title,
            description=video.description,
            stored=stored,
            poster_url=video.image_url or None,
            urls=(watch,) if watch.startswith("https://") else (),
        )
        await _send_photo_or_text(query.message, text, poster, markup)


@router.callback_query(F.data.regexp(r"^page:\d+$"))
async def on_page(query: CallbackQuery, allowed_chat_id: int, db_path: Path) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    raw = (query.data or "").partition(":")[2]
    offset = int(raw) if raw.isdigit() else 0
    text, markup = pending_view(db_path, offset)
    await query.answer()
    if not isinstance(query.message, Message):
        return
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        logger.info("Список не изменился: %s", exc)


@router.callback_query(F.data.regexp(r"^open:\d+$"))
async def on_open(query: CallbackQuery, allowed_chat_id: int, db_path: Path) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    raw = (query.data or "").partition(":")[2]
    if not raw.isdigit():
        await query.answer("Не понял кнопку")
        return
    with SeenStore(db_path) as store:
        item = store.get(int(raw))
    if item is None or item.status != PENDING:
        await query.answer("Этой записи уже нет в очереди", show_alert=True)
        return
    await query.answer()
    if isinstance(query.message, Message):
        await _send_decision_card(query.message, item)


@router.callback_query(F.data == "clear")
async def on_clear(query: CallbackQuery, allowed_chat_id: int, app_config: AppConfig) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    removed = drop_unapproved(app_config)
    await query.answer("Очищено")
    if query.message is not None:
        await query.message.answer(format_clear_result(removed), reply_markup=menu_markup())


@router.callback_query(F.data == "update")
async def on_update(
    query: CallbackQuery,
    allowed_chat_id: int,
    app_config: AppConfig,
) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return
    await query.answer("Обновляю ленту")
    if query.message is not None:
        await query.message.answer(
            "Читаю ленту. Одобренные повторно не добавлю.",
            reply_markup=menu_markup(),
        )
    report = await _fetch_report(app_config)
    if query.message is None:
        return
    if isinstance(report, str):
        await query.message.answer(report, reply_markup=menu_markup())
        return
    await query.message.answer(format_refresh_result(report), reply_markup=menu_markup())


@router.callback_query(F.data.regexp(r"^(yes|no):\d+$"))
async def on_decision(
    query: CallbackQuery,
    allowed_chat_id: int,
    db_path: Path,
    app_config: AppConfig,
) -> None:
    if not is_allowed(query.from_user.id, allowed_chat_id):
        await query.answer()
        return

    parsed = parse_callback(query.data or "")
    if parsed is None:
        await query.answer("Не понял кнопку")
        return
    status, row_id = parsed

    with SeenStore(db_path) as store:
        decision = store.decide(row_id, status)
    if decision is None:
        await query.answer("Запись не найдена", show_alert=True)
        return
    if not decision.changed:
        label = _STATUS_RU.get(decision.item.status, decision.item.status)
        await query.answer(f"Уже решено: {label}")
        return

    if status == REJECTED:
        await query.answer("Отклонено")
        await _replace_card(query, decision, "Отклонено. Файл метаданных не скачивается.")
        return

    link = magnet_link(decision.item.torrent_url)
    if link:
        await query.answer("Одобрено, ставлю в загрузку…")
        await asyncio.to_thread(queue_link, app_config, link, decision.item.title)
        await _replace_card(
            query,
            decision,
            "Одобрено. Magnet-ссылка поставлена в qBittorrent, файл .torrent не нужен.",
        )
        return

    await query.answer("Одобрено, скачиваю метаданные…")
    saved = await _download(query, app_config, decision)
    if not saved.ok:
        return
    if saved.path is None:
        await _replace_card(
            query,
            decision,
            "Одобрено. Адреса .torrent у записи нет, скачивать нечего.",
        )
        return
    await asyncio.to_thread(queue_download, app_config, saved.path, decision.item.title)
    if not vk_ready(app_config):
        await _replace_card(
            query,
            decision,
            f"Одобрено. Загрузка в qBittorrent уже началась: {saved.path.name}\n"
            f"Токен VK не задан ({app_config.vk.token_env}), во ВКонтакте не выкладываю.",
        )
        return
    await _replace_card(
        query,
        decision,
        f"Одобрено. Загрузка в qBittorrent уже началась: {saved.path.name}\n"
        "Дальше загружаю видео в VK. Ссылка придёт отдельным сообщением.",
    )
    await _publish(app_config, decision.item, saved.path)


async def _publish_torrent(config: AppConfig, torrent: Path, title: str) -> None:
    try:
        await asyncio.to_thread(
            deliver_and_notify,
            config,
            torrent,
            title=title,
        )
    except (DownloadError, VkError, NotifyError, ConfigError) as exc:
        logger.error("Публикация «%s» не удалась: %s", title, exc)
        await _tell_error(config, title, exc)


@dataclass(frozen=True)
class _Saved:
    ok: bool
    path: Path | None = None


async def _publish(config: AppConfig, item: StoredItem, torrent: Path) -> None:
    """Долгий шаг после одобрения: aria2c, VK, затем обложка и ссылка плеера."""
    try:
        await asyncio.to_thread(
            deliver_and_notify,
            config,
            torrent,
            title=item.title,
            description=item.link,
            row_id=item.row_id,
        )
    except (DownloadError, VkError, NotifyError, ConfigError) as exc:
        logger.error("Публикация «%s» не удалась: %s", item.title, exc)
        await _tell_error(config, item.title, exc)


async def _tell_error(config: AppConfig, title: str, exc: Exception) -> None:
    urls = exc.player_urls if isinstance(exc, NotifyError) else ()
    extra = "\n" + "\n".join(urls) if urls else ""
    text = f"Не удалось опубликовать «{title}».\n{exc}{extra}"
    token = os.environ.get(config.telegram.token_env, "").strip()
    chat_id = os.environ.get(config.telegram.chat_id_env, "").strip()
    if not token or not chat_id:
        return
    try:
        await asyncio.to_thread(
            deliver_notice,
            token,
            chat_id,
            text[:4000],
            None,
            config.telegram.timeout_seconds,
            menu_markup(),
        )
    except NotifyError:
        logger.exception("Не удалось отправить ошибку в Telegram")


async def _download(
    query: CallbackQuery,
    config: AppConfig,
    decision: Decision,
) -> _Saved:
    """Скачать метаданные. ok=False — файл не сохранился, решение в базе уже «одобрено»."""
    vpn = VpnManager(
        interface=config.vpn.interface,
        config_path=config.vpn.config_path,
        timeout_seconds=config.vpn.command_timeout_seconds,
    )
    try:
        path = await asyncio.to_thread(deliver_approved, config, vpn, decision.item)
    except (VpnError, FetchError) as exc:
        logger.error("После одобрения не скачалось: %s", exc)
        await _replace_card(
            query,
            decision,
            "Одобрено. Файл сейчас не скачался — повтор будет при следующем fetch.\n"
            + str(exc)[:300],
        )
        return _Saved(ok=False)
    return _Saved(ok=True, path=path)


def _signed_id(text: str) -> bool:
    body = text[1:] if text.startswith("-") else text
    return bool(body) and body.isdigit()


async def _vk_page(config: AppConfig, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    entries, note = await asyncio.to_thread(load_vk_entries, config)
    return vk_view(entries, offset, note=note)


def _vk_video_card(
    config: AppConfig,
    owner_id: int,
    video_id: int,
) -> tuple[VkCatalogItem | None, StoredItem | None]:
    if not vk_ready(config):
        raise VkError("Токен VK не задан.")
    token = os.environ.get(config.vk.token_env, "").strip()
    video = VkVideoClient(config.vk, token).get_video(owner_id, video_id)
    if video is None:
        return None, None
    with SeenStore(config.database_path) as store:
        stored = _row_for_video(store.list_seen(), video)
    return video, stored


async def _send_photo_or_text(
    message: Message,
    text: str,
    poster: str | None,
    markup: InlineKeyboardMarkup,
) -> None:
    if poster:
        try:
            await message.answer_photo(photo=poster, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            logger.info("Постер не открылся, пишу текстом: %s", exc)
    await message.answer(text, reply_markup=markup)


async def _send_decision_card(message: Message, item: StoredItem) -> None:
    """Отдельное сообщение: постер, описание и кнопки решения."""
    text = format_card(item)
    markup = approval_keyboard(
        item.row_id,
        topic_url=item.topic_url,
        kinopoisk_url=item.kinopoisk_url,
    )
    if item.poster_url:
        try:
            await message.answer_photo(photo=item.poster_url, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            logger.info("Постер не открылся, пишу текстом: %s", exc)
    await message.answer(text, reply_markup=markup)


async def _replace_card(query: CallbackQuery, decision: Decision, note: str) -> None:
    if not isinstance(query.message, Message):
        return
    text = f"{format_card(decision.item, waiting=False)}\n\n{note}"
    if len(text) > 1024:
        text = text[:1023].rstrip() + "…"
    try:
        if query.message.photo:
            await query.message.edit_caption(caption=text, reply_markup=menu_markup())
        else:
            await query.message.edit_text(text, reply_markup=menu_markup())
    except TelegramBadRequest as exc:
        logger.info("Сообщение не изменилось: %s", exc)
