"""Разбор RSS и Atom в одинаковый список записей.

Лента Jackett — обычный RSS: адрес берётся из config.yaml (jackett_rss_url),
а разбор остаётся здесь. Внешние сайты отдельно не открываются.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import feedparser

from potok.config import MetadataPatterns

logger = logging.getLogger(__name__)

_TORRENT_TYPE = "application/x-bittorrent"
_TORRENT_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+\.torrent\b", re.IGNORECASE)
_TITLE_BRACKET = re.compile(r"\[([^\[\]]+)\]")
_YEAR_TOKEN = re.compile(r"^(?:19|20)\d{2}$")
_QUALITY_CHUNK = re.compile(
    r"(?i)(?:4k|uhd|fhd|(?<![0-9a-zа-яё])hd(?![0-9a-zа-яё])|"
    r"(?<![0-9a-zа-яё])sd(?![0-9a-zа-яё])|"
    r"web[\s-]?dl|webrip|bdrip|hdrip|hdtv|blu[\s-]?ray|bdremux|"
    r"dvdrip|remux|dcprip|2160p|1080p|720p|480p|576p|"
    r"x264|x265|h[\s.]?264|h[\s.]?265|(?<![0-9a-zа-яё])avc(?![0-9a-zа-яё])|"
    r"hevc|(?<![0-9a-zа-яё])hdr\d*\+?(?![0-9a-zа-яё])|"
    r"(?<![0-9a-zа-яё])(?:mvo|dvo|avo|dub|sub|dolby)(?![0-9a-zа-яё])|"
    r"rezka|(?<![0-9a-zа-яё])(?:studio|profile)(?![0-9a-zа-яё]))"
)
_DIGITS = re.compile(r"^\d+$")
_LOOKALIKE = str.maketrans(
    {
        "A": "А",
        "a": "а",
        "B": "В",
        "C": "С",
        "c": "с",
        "E": "Е",
        "e": "е",
        "H": "Н",
        "K": "К",
        "k": "к",
        "M": "М",
        "m": "м",
        "O": "О",
        "o": "о",
        "P": "Р",
        "p": "р",
        "T": "Т",
        "X": "Х",
        "x": "х",
        "Y": "У",
        "y": "у",
    }
)
_NOT_GENRE = frozenset({"ремейк", "экранизация", "реальные события"})
_GENRE_ALIAS = {"фэнтази": "фэнтези", "исторический": "история"}
_COUNTRIES = frozenset(
    {
        "австралия",
        "австрия",
        "азербайджан",
        "албания",
        "алжир",
        "аргентина",
        "армения",
        "афганистан",
        "бангладеш",
        "беларусь",
        "белоруссия",
        "бельгия",
        "болгария",
        "босния и герцеговина",
        "бразилия",
        "великобритания",
        "венгрия",
        "венесуэла",
        "вьетнам",
        "гдр",
        "германия",
        "гонконг",
        "греция",
        "грузия",
        "дания",
        "египет",
        "израиль",
        "индия",
        "индонезия",
        "иордания",
        "ирак",
        "иран",
        "ирландия",
        "исландия",
        "испания",
        "италия",
        "казахстан",
        "камбоджа",
        "канада",
        "катар",
        "кипр",
        "киргизия",
        "китай",
        "кндр",
        "колумбия",
        "корея",
        "корея южная",
        "куба",
        "латвия",
        "ливан",
        "литва",
        "люксембург",
        "македония",
        "малайзия",
        "мальта",
        "мексика",
        "молдова",
        "монако",
        "монголия",
        "марокко",
        "нидерланды",
        "новая зеландия",
        "норвегия",
        "оаэ",
        "пакистан",
        "перу",
        "польша",
        "португалия",
        "россия",
        "румыния",
        "саудовская аравия",
        "северная македония",
        "сербия",
        "сингапур",
        "сирия",
        "словакия",
        "словения",
        "ссср",
        "сша",
        "таджикистан",
        "таиланд",
        "тайвань",
        "тунис",
        "туркменистан",
        "турция",
        "узбекистан",
        "украина",
        "уругвай",
        "филиппины",
        "финляндия",
        "франция",
        "фрг",
        "хорватия",
        "черногория",
        "чехия",
        "чехословакия",
        "чили",
        "швейцария",
        "швеция",
        "эстония",
        "юар",
        "югославия",
        "южная корея",
        "япония",
    }
)
_ITEM_BLOCK = re.compile(r"<item\b[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)
_SEEDERS = re.compile(
    r"""<torznab:attr\s+name=["']seeders["']\s+value=["'](\d+)["']""",
    re.IGNORECASE,
)
_TOPIC_ID = re.compile(r"[?&]t=(\d+)")
_GIB = 1024**3


@dataclass(frozen=True)
class FeedItem:
    guid: str
    feed_name: str
    title: str
    link: str
    published: datetime | None
    torrent_url: str | None
    rating: float | None
    quality: str | None
    genre: str | None
    overview: str | None = None
    poster_url: str | None = None
    seeders: int | None = None
    size_bytes: int | None = None
    topic_url: str | None = None
    kinopoisk_url: str | None = None
    runtime_minutes: int | None = None
    movie_title: str | None = None
    country: str | None = None


def parse_feed(payload: bytes, *, feed_name: str, patterns: MetadataPatterns) -> list[FeedItem]:
    """Разобрать уже скачанное тело ленты. Сеть здесь не используется."""
    parsed = feedparser.parse(payload)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        detail = getattr(parsed, "bozo_exception", None)
        logger.warning("Лента %s не разобралась: %s", feed_name, detail)
        return []

    items: list[FeedItem] = []
    seeders = _seeders_by_item(payload)
    for index, entry in enumerate(parsed.entries):
        peers = seeders[index] if index < len(seeders) else None
        item = _entry_to_item(entry, feed_name=feed_name, patterns=patterns, seeders=peers)
        if item is not None:
            items.append(item)
    logger.info("Лента %s: записей %s", feed_name, len(items))
    return items


def _entry_to_item(
    entry: object,
    *,
    feed_name: str,
    patterns: MetadataPatterns,
    seeders: int | None,
) -> FeedItem | None:
    title = _text(entry, "title")
    link = _text(entry, "link")
    guid = _text(entry, "id") or _text(entry, "guid") or link or title
    if not guid or not title:
        logger.debug("Пропуск записи без названия или идентификатора в ленте %s", feed_name)
        return None

    text = "\n".join(
        part for part in (title, _text(entry, "summary"), _text(entry, "description")) if part
    )
    rating = _rating(text, patterns.rating)
    genre = _group(text, patterns.genre) or genres_from_title(title)
    return FeedItem(
        guid=f"{feed_name}:{guid}",
        feed_name=feed_name,
        title=title,
        link=link,
        published=_published(entry),
        torrent_url=_torrent_url(entry, text),
        rating=rating,
        quality=_group(text, patterns.quality),
        genre=genre,
        seeders=seeders,
        size_bytes=_whole_text(entry.get("size") if hasattr(entry, "get") else None),
        topic_url=rutracker_topic(_text(entry, "comments")),
    )


def format_gigabytes(size_bytes: int | None) -> str:
    """Размер раздачи в гигабайтах. Пустое значение — «нет»."""
    if size_bytes is None or size_bytes < 0:
        return "нет"
    return f"{size_bytes / _GIB:.2f} ГБ"


def rutracker_topic(url: str) -> str | None:
    """Ссылка на описание раздачи.

    Jackett читает зеркало rutracker.ru. Там тема открывается так:
    http://rutracker.ru/viewtopic.php?t=НОМЕР
    Тот же номер на rutracker.org/forum/ — другая, часто пустая страница.
    """
    text = url.strip()
    if not text.startswith(("http://", "https://")):
        return None
    host = (urlsplit(text).hostname or "").casefold()
    found = _TOPIC_ID.search(text)
    if found and "rutracker" in host:
        return f"http://rutracker.ru/viewtopic.php?t={found.group(1)}"
    return text


def _seeders_by_item(payload: bytes) -> list[int | None]:
    """feedparser оставляет только последний torznab:attr, поэтому сиды читаем из XML."""
    text = payload.decode("utf-8", errors="replace")
    found: list[int | None] = []
    for block in _ITEM_BLOCK.findall(text):
        match = _SEEDERS.search(block)
        found.append(int(match.group(1)) if match else None)
    return found


def _whole_text(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def genres_from_title(title: str) -> str | None:
    """Жанры из квадратных скобок названия, без года, страны и качества."""
    found = _TITLE_BRACKET.search(title)
    if found is None:
        return None
    text = genre_text(found.group(1))
    return text or None


def genre_parts(value: str | None, *, pretty: bool = False) -> list[str]:
    """Куски поля «жанр»: без стран, качества и повторов из-за регистра."""
    names: list[str] = []
    seen: set[str] = set()
    for piece in (value or "").replace("/", ",").split(","):
        chunk = piece.split("|", 1)[0].split("+", 1)[0].strip()
        name = _fix_lookalikes(" ".join(chunk.split()).strip(" .-"))
        name = _GENRE_ALIAS.get(name.casefold(), name)
        if _skip_genre(name):
            continue
        label = _pretty_genre(name) if pretty else name
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(label)
    return names


def genre_text(value: str | None) -> str:
    """Жанры одной строкой для карточки и списка фильтра."""
    return ", ".join(genre_parts(value, pretty=True))


def _skip_genre(name: str) -> bool:
    if not name or _YEAR_TOKEN.match(name) or _DIGITS.match(name):
        return True
    folded = name.casefold()
    if folded in _COUNTRIES or folded in _NOT_GENRE:
        return True
    return _QUALITY_CHUNK.search(name) is not None


def _fix_lookalikes(name: str) -> str:
    """«Kомедия» с латинской K — то же слово, что «Комедия»."""
    cyrillic = any("\u0400" <= char <= "\u04ff" for char in name)
    latin = any(("A" <= char <= "Z") or ("a" <= char <= "z") for char in name)
    if cyrillic and latin:
        return name.translate(_LOOKALIKE)
    return name


def _pretty_genre(name: str) -> str:
    """Первая буква заглавная, чтобы «боевик» и «Боевик» стали одной строкой."""
    for index, char in enumerate(name):
        if not char.isalpha():
            continue
        if char.isupper():
            return name
        return name[:index] + char.upper() + name[index + 1 :]
    return name


def magnet_link(url: str | None) -> str | None:
    """Вернуть ссылку, если это magnet, а не адрес файла .torrent."""
    if not url:
        return None
    text = url.strip()
    lowered = text.lower()
    if len(text) > 4000 or not lowered.startswith("magnet:?"):
        return None
    if "xt=urn:btih:" not in lowered:
        return None
    return text


def _torrent_url(entry: object, text: str) -> str | None:
    for enclosure in _list(entry, "enclosures"):
        found = _href_if_torrent(enclosure)
        if found:
            return found
    for link in _list(entry, "links"):
        found = _href_if_torrent(link)
        if found:
            return found
    match = _TORRENT_IN_TEXT.search(text)
    if match:
        return match.group(0)
    return None


def _href_if_torrent(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    href = raw.get("href") or raw.get("url")
    if not isinstance(href, str) or not href.strip():
        return None
    href = href.strip()
    kind = str(raw.get("type") or "").split(";", 1)[0].strip().lower()
    path = href.split("?", 1)[0].lower()
    if kind == _TORRENT_TYPE or path.endswith(".torrent"):
        return href
    return None


def _rating(text: str, pattern: re.Pattern[str] | None) -> float | None:
    raw = _group(text, pattern)
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        logger.debug("Число рейтинга не разобралось: %s", raw)
        return None


def _group(text: str, pattern: re.Pattern[str] | None) -> str | None:
    if pattern is None:
        return None
    match = pattern.search(text)
    if match is None:
        return None
    if match.lastindex:
        for index in range(1, match.lastindex + 1):
            group = match.group(index)
            if group and group.strip():
                return group.strip()
    whole = match.group(0).strip()
    return whole or None


def _published(entry: object) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
            except (OverflowError, OSError, ValueError):
                logger.debug("Дата записи не разобралась: %s", parsed)
    return None


def _text(entry: object, key: str) -> str:
    value = entry.get(key, "") if hasattr(entry, "get") else getattr(entry, key, "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _list(entry: object, key: str) -> list:
    value = entry.get(key, []) if hasattr(entry, "get") else getattr(entry, key, [])
    if isinstance(value, list):
        return value
    return []
