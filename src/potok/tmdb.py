"""Поиск фильма в The Movie Database: обложка, описание, год и рейтинг.

Для России сайт закрыт на уровне DNS. Адреса themoviedb.org и tmdb.org
спрашиваем у Quad9 (9.9.9.9), как в рекомендации с отдельным DNS только
для этих доменов. Остальные имена резолвятся по-прежнему.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

QUAD9_SERVERS = ("9.9.9.9", "149.112.112.112")

SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
IMAGE_ROOT = "https://image.tmdb.org/t/p/w500"
_WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
_KINOPOISK_FILM = "https://www.kinopoisk.ru/film/{}/"
_KP_PROPERTY = "P2603"
LANGUAGE = "ru-RU"
_CAPTION_LIMIT = 1024

_EXT = re.compile(r"\.(mkv|mp4|avi|mov|webm|m4v|mpeg|mpg|wmv|ts|m2ts)$", re.IGNORECASE)
_YEAR_BRACKET = re.compile(r"\[((?:19|20)\d{2})")
_YEAR_PAREN = re.compile(r"\(((?:19|20)\d{2})\)")
_PARENS = re.compile(r"\([^)]*\)")
_QUALITY = re.compile(
    r"\b(?:2160p|1080p|720p|480p|WEB-DL|WEBRip|BDRip|DVDRip|HDRip|Blu-?Ray|HDTV)\b",
    re.IGNORECASE,
)
_GROUP = re.compile(r"\s+by\s+[A-Z0-9][A-Z0-9._-]*$")
_SPACES = re.compile(r"\s+")


class TmdbError(Exception):
    """TMDB не ответил или ключ не принят."""


class MovieNotFound(TmdbError):
    """По названию ничего не нашлось."""


@dataclass(frozen=True)
class MovieInfo:
    title: str
    year: str | None
    rating: float | None
    overview: str
    poster_url: str | None
    kinopoisk_url: str | None = None
    runtime_minutes: int | None = None
    country: str | None = None


def movie_query(title: str) -> tuple[str, int | None]:
    """Убрать из названия раздачи качество, скобки и расширение. Год вернуть отдельно."""
    text = _EXT.sub("", title.strip())
    year: int | None = None
    found = _YEAR_BRACKET.search(text) or _YEAR_PAREN.search(text)
    if found:
        year = int(found.group(1))
    cut = text.find(" [")
    if cut != -1:
        text = text[:cut]
    text = _PARENS.sub(" ", text)
    text = _QUALITY.sub(" ", text)
    text = _GROUP.sub("", text)
    text = _SPACES.sub(" ", text).strip(" .-_")
    text = _russian_part(text)
    return text, year


def _russian_part(text: str) -> str:
    """«Лего / Lego Star Wars» — для карточки берём русское имя до косой черты."""
    left, sep, _right = text.partition(" / ")
    if not sep:
        return text
    if any("\u0400" <= char <= "\u04ff" for char in left):
        return left.strip()
    return text


def release_key(title: str, quality: str | None = None) -> str:
    """Один фильм, без различия качества, студии и имени раздачи.

    Качество в ключ не входит: какую копию оставить, решает фильтр.
    """
    del quality
    query, year = movie_query(title)
    name = " ".join(query.casefold().split())
    year_text = "" if year is None else str(year)
    return f"{name}|{year_text}"


def kinopoisk_search_url(title: str) -> str:
    """Страница поиска, если точный id фильма неизвестен."""
    query, year = movie_query(title)
    text = query or title.strip()
    if year is not None:
        text = f"{text} {year}"
    return "https://www.kinopoisk.ru/index.php?kp_query=" + quote_plus(text)


def _tmdb_host(host: str) -> bool:
    name = host.rstrip(".").lower()
    return (
        name == "themoviedb.org"
        or name.endswith(".themoviedb.org")
        or name == "tmdb.org"
        or name.endswith(".tmdb.org")
    )


def _dns_query(host: str) -> bytes:
    header = b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in host.split("."))
    return header + labels + b"\x00\x00\x01\x00\x01"


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    end = offset
    seen = 0
    while seen < 20 and offset < len(data):
        seen += 1
        length = data[offset]
        if length == 0:
            if not jumped:
                end = offset + 1
            return ".".join(labels), end
        if (length & 0xC0) == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            offset = pointer
            jumped = True
            continue
        start = offset + 1
        labels.append(data[start : start + length].decode("ascii", errors="replace"))
        offset = start + length
        if not jumped:
            end = offset
    return ".".join(labels), end


def _parse_a(data: bytes) -> tuple[str | None, int]:
    """Первый IPv4-адрес из ответа DNS и его TTL."""
    if len(data) < 12:
        return None, 0
    questions = int.from_bytes(data[4:6], "big")
    answers = int.from_bytes(data[6:8], "big")
    offset = 12
    for _ in range(questions):
        _, offset = _read_name(data, offset)
        offset += 4
    for _ in range(answers):
        _, offset = _read_name(data, offset)
        if offset + 10 > len(data):
            return None, 0
        kind = int.from_bytes(data[offset : offset + 2], "big")
        ttl = int.from_bytes(data[offset + 4 : offset + 8], "big")
        size = int.from_bytes(data[offset + 8 : offset + 10], "big")
        offset += 10
        raw = data[offset : offset + size]
        offset += size
        if kind == 1 and len(raw) == 4:
            return socket.inet_ntoa(raw), ttl
    return None, 0


def _ask_quad9(host: str, server: str) -> tuple[str | None, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    try:
        sock.sendto(_dns_query(host), (server, 53))
        payload, _source = sock.recvfrom(2048)
    except OSError:
        return None, 0
    finally:
        sock.close()
    return _parse_a(payload)


def _query_quad9(host: str) -> tuple[str | None, int]:
    for server in QUAD9_SERVERS:
        address, ttl = _ask_quad9(host, server)
        if address:
            logger.info("Адрес %s через DNS %s: %s", host, server, address)
            return address, ttl
    logger.warning("DNS 9.9.9.9 не ответил для %s. Беру обычный DNS.", host)
    return None, 0


_CACHE: dict[str, tuple[str | None, float]] = {}


def quad9_address(host: str) -> str | None:
    """IP домена TMDB через 9.9.9.9. None — Quad9 не ответил, нужен обычный DNS."""
    now = time.monotonic()
    cached = _CACHE.get(host)
    if cached is not None and cached[1] > now:
        return cached[0]
    address, ttl = _query_quad9(host)
    life = 300 if address else 30
    if ttl:
        life = min(max(ttl, 30), 3600)
    _CACHE[host] = (address, now + life)
    return address


def _port_number(port: object) -> int:
    if port is None or port == "":
        return 0
    return int(port)


_SYSTEM_GETADDRINFO = socket.getaddrinfo


def _getaddrinfo(host: object, port: object, *args: object, **kwargs: object):
    family = args[0] if args else kwargs.get("family", 0)
    tmdb = isinstance(host, str) and _tmdb_host(host)
    if tmdb and family in (0, socket.AF_UNSPEC, socket.AF_INET):
        address = quad9_address(host)
        if address:
            socktype = args[1] if len(args) > 1 else kwargs.get("type", 0)
            proto = args[2] if len(args) > 2 else kwargs.get("proto", 0)
            return [
                (
                    socket.AF_INET,
                    socktype or socket.SOCK_STREAM,
                    proto or socket.IPPROTO_TCP,
                    "",
                    (address, _port_number(port)),
                )
            ]
    return _SYSTEM_GETADDRINFO(host, port, *args, **kwargs)


socket.getaddrinfo = _getaddrinfo


def search_movie(
    api_key: str,
    title: str,
    *,
    timeout: float = 20,
    get: Callable[..., requests.Response] | None = None,
) -> MovieInfo:
    """Найти фильм по названию. Язык ответа всегда ru-RU.

    Если с годом пусто, повтор без года. Пустой список — MovieNotFound.
    """
    key = api_key.strip()
    if not key:
        raise TmdbError("Ключ TMDB пустой. Вставьте его в настройках.")
    query, year = movie_query(title)
    if not query:
        raise MovieNotFound("В названии не осталось текста для поиска в TMDB.")
    getter = get or requests.get
    payload = _search(getter, key, query, year=year, timeout=timeout)
    results = _results(payload)
    if not results and year is not None:
        payload = _search(getter, key, query, year=None, timeout=timeout)
        results = _results(payload)
    if not results:
        raise MovieNotFound(f"Фильм «{query}» в TMDB не найден.")
    movie = _movie(results[0])
    movie_id = results[0].get("id")
    runtime, country = _movie_card(getter, key, movie_id, timeout)
    page = _kinopoisk_page(
        getter,
        key,
        movie_id,
        movie.title,
        movie.year,
        timeout,
    )
    return replace(movie, kinopoisk_url=page, runtime_minutes=runtime, country=country)


def _movie_card(
    get: Callable[..., requests.Response],
    api_key: str,
    movie_id: object,
    timeout: float,
) -> tuple[int | None, str | None]:
    """Длительность и страна. В поиске их нет, они есть в карточке фильма."""
    if isinstance(movie_id, bool) or not isinstance(movie_id, int):
        return None, None
    try:
        response = get(
            f"https://api.themoviedb.org/3/movie/{movie_id}",
            params={"api_key": api_key, "language": LANGUAGE},
            timeout=timeout,
        )
    except requests.RequestException:
        logger.info("TMDB не отдал карточку фильма %s.", movie_id)
        return None, None
    payload = _optional_payload(response)
    if payload is None:
        return None, None
    return _minutes(payload.get("runtime")), _countries(payload.get("production_countries")) or ""


_COUNTRY_RU = {
    "US": "США",
    "USA": "США",
    "United States": "США",
    "United States of America": "США",
    "GB": "Великобритания",
    "UK": "Великобритания",
    "United Kingdom": "Великобритания",
    "RU": "Россия",
    "Russia": "Россия",
    "SU": "СССР",
    "Soviet Union": "СССР",
    "FR": "Франция",
    "France": "Франция",
    "DE": "Германия",
    "Germany": "Германия",
    "IT": "Италия",
    "Italy": "Италия",
    "ES": "Испания",
    "Spain": "Испания",
    "CA": "Канада",
    "Canada": "Канада",
    "AU": "Австралия",
    "Australia": "Австралия",
    "JP": "Япония",
    "Japan": "Япония",
    "CN": "Китай",
    "China": "Китай",
    "KR": "Южная Корея",
    "South Korea": "Южная Корея",
    "IN": "Индия",
    "India": "Индия",
    "BR": "Бразилия",
    "Brazil": "Бразилия",
    "MX": "Мексика",
    "Mexico": "Мексика",
    "PL": "Польша",
    "Poland": "Польша",
    "CZ": "Чехия",
    "Czech Republic": "Чехия",
    "Czechia": "Чехия",
    "SE": "Швеция",
    "Sweden": "Швеция",
    "NO": "Норвегия",
    "Norway": "Норвегия",
    "DK": "Дания",
    "Denmark": "Дания",
    "FI": "Финляндия",
    "Finland": "Финляндия",
    "NL": "Нидерланды",
    "Netherlands": "Нидерланды",
    "BE": "Бельгия",
    "Belgium": "Бельгия",
    "CH": "Швейцария",
    "Switzerland": "Швейцария",
    "AT": "Австрия",
    "Austria": "Австрия",
    "IE": "Ирландия",
    "Ireland": "Ирландия",
    "NZ": "Новая Зеландия",
    "New Zealand": "Новая Зеландия",
    "UA": "Украина",
    "Ukraine": "Украина",
    "BY": "Беларусь",
    "Belarus": "Беларусь",
    "KZ": "Казахстан",
    "Kazakhstan": "Казахстан",
    "TR": "Турция",
    "Turkey": "Турция",
    "GR": "Греция",
    "Greece": "Греция",
    "HU": "Венгрия",
    "Hungary": "Венгрия",
    "PT": "Португалия",
    "Portugal": "Португалия",
    "AR": "Аргентина",
    "Argentina": "Аргентина",
    "CL": "Чили",
    "Chile": "Чили",
    "ZA": "ЮАР",
    "South Africa": "ЮАР",
    "IL": "Израиль",
    "Israel": "Израиль",
    "AE": "ОАЭ",
    "United Arab Emirates": "ОАЭ",
    "HK": "Гонконг",
    "Hong Kong": "Гонконг",
    "TW": "Тайвань",
    "Taiwan": "Тайвань",
    "TH": "Таиланд",
    "Thailand": "Таиланд",
    "SG": "Сингапур",
    "Singapore": "Сингапур",
    "ID": "Индонезия",
    "Indonesia": "Индонезия",
    "PH": "Филиппины",
    "Philippines": "Филиппины",
    "VN": "Вьетнам",
    "Vietnam": "Вьетнам",
    "EG": "Египет",
    "Egypt": "Египет",
    "IR": "Иран",
    "Iran": "Иран",
    "RO": "Румыния",
    "Romania": "Румыния",
    "BG": "Болгария",
    "Bulgaria": "Болгария",
    "RS": "Сербия",
    "Serbia": "Сербия",
    "HR": "Хорватия",
    "Croatia": "Хорватия",
    "SK": "Словакия",
    "Slovakia": "Словакия",
    "SI": "Словения",
    "Slovenia": "Словения",
    "LT": "Литва",
    "Lithuania": "Литва",
    "LV": "Латвия",
    "Latvia": "Латвия",
    "EE": "Эстония",
    "Estonia": "Эстония",
    "IS": "Исландия",
    "Iceland": "Исландия",
    "LU": "Люксембург",
    "Luxembourg": "Люксембург",
    "GE": "Грузия",
    "Georgia": "Грузия",
    "AM": "Армения",
    "Armenia": "Армения",
    "AZ": "Азербайджан",
    "Azerbaijan": "Азербайджан",
    "UZ": "Узбекистан",
    "Uzbekistan": "Узбекистан",
    "SA": "Саудовская Аравия",
    "Saudi Arabia": "Саудовская Аравия",
    "CY": "Кипр",
    "Cyprus": "Кипр",
    "MC": "Монако",
    "Monaco": "Монако",
    "Соединённые Штаты Америки": "США",
    "Соединенные Штаты Америки": "США",
    "Соединённые Штаты": "США",
    "Соединенные Штаты": "США",
}

_COUNTRY_RU_FOLDED = {key.casefold(): name for key, name in _COUNTRY_RU.items()}


def country_text(value: str | None) -> str:
    """Страны одной строкой по-русски. Несколько стран через запятую."""
    if value is None:
        return "нет"
    parts = [part.strip() for part in value.replace("/", ",").split(",") if part.strip()]
    if not parts:
        return "нет"
    names: list[str] = []
    for part in parts:
        names.append(_COUNTRY_RU_FOLDED.get(part.casefold(), part))
    return ", ".join(names)


def stored_country(value: str | None) -> str | None:
    """Страна для базы: по-русски. Пустую строку не сохраняем."""
    text = country_text(value)
    if text == "нет":
        return None
    return text


def _countries(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        iso = item.get("iso_3166_1")
        name = item.get("name")
        raw = ""
        if isinstance(iso, str) and iso.strip():
            raw = iso.strip().upper()
        elif isinstance(name, str):
            raw = " ".join(name.split())
        text = country_text(raw)
        if text != "нет" and text not in names:
            names.append(text)
    if not names:
        return None
    return ", ".join(names)


def _minutes(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _kinopoisk_page(
    get: Callable[..., requests.Response],
    api_key: str,
    movie_id: object,
    title: str,
    year: str | None,
    timeout: float,
) -> str:
    """Страница фильма на Кинопоиске. Если id нет — ссылка на поиск по названию."""
    film_id = None
    if isinstance(movie_id, int) and not isinstance(movie_id, bool):
        film_id = _kinopoisk_film_id(get, api_key, movie_id, timeout)
    if film_id:
        return _KINOPOISK_FILM.format(film_id)
    text = title.strip() or "фильм"
    if year:
        text = f"{text} {year}"
    return "https://www.kinopoisk.ru/index.php?kp_query=" + quote_plus(text)


def _kinopoisk_film_id(
    get: Callable[..., requests.Response],
    api_key: str,
    movie_id: int,
    timeout: float,
) -> str | None:
    """Число Кинопоиска из Wikidata: TMDB знает запись Wikidata, там свойство P2603."""
    try:
        response = get(
            f"https://api.themoviedb.org/3/movie/{movie_id}/external_ids",
            params={"api_key": api_key},
            timeout=timeout,
        )
    except requests.RequestException:
        logger.info("TMDB не отдал внешние id фильма %s.", movie_id)
        return None
    payload = _optional_payload(response)
    if payload is None:
        return None
    wiki = payload.get("wikidata_id")
    if not isinstance(wiki, str) or re.fullmatch(r"Q\d+", wiki) is None:
        return None
    time.sleep(1)
    try:
        response = get(
            _WIKIDATA_URL,
            params={
                "action": "wbgetentities",
                "ids": wiki,
                "props": "claims",
                "format": "json",
            },
            timeout=timeout,
            headers={"User-Agent": "Potok/1.0 (local movie catalog)"},
        )
    except requests.RequestException:
        logger.info("Wikidata не ответила для %s.", wiki)
        return None
    if getattr(response, "status_code", 0) == 429:
        time.sleep(3)
        try:
            response = get(
                _WIKIDATA_URL,
                params={
                    "action": "wbgetentities",
                    "ids": wiki,
                    "props": "claims",
                    "format": "json",
                },
                timeout=timeout,
                headers={"User-Agent": "Potok/1.0 (local movie catalog)"},
            )
        except requests.RequestException:
            logger.info("Wikidata не ответила для %s.", wiki)
            return None
    payload = _optional_payload(response)
    if payload is None:
        return None
    return _claim_id(payload, wiki)


def _optional_payload(response: requests.Response) -> dict | None:
    if getattr(response, "status_code", 0) != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _claim_id(payload: dict, entity: str) -> str | None:
    entities = payload.get("entities")
    if not isinstance(entities, dict):
        return None
    record = entities.get(entity)
    if not isinstance(record, dict):
        return None
    claims = record.get("claims")
    if not isinstance(claims, dict):
        return None
    values = claims.get(_KP_PROPERTY)
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    if not isinstance(first, dict):
        return None
    snak = first.get("mainsnak")
    if not isinstance(snak, dict):
        return None
    data = snak.get("datavalue")
    if not isinstance(data, dict):
        return None
    value = data.get("value")
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def format_caption(title: str, player_urls: tuple[str, ...], movie: MovieInfo) -> str:
    """Подпись к обложке: название, год, рейтинг, описание и ссылки VK.

    Описание укорачивается, чтобы ссылки поместились в лимит Telegram (1024).
    """
    links = [url.strip() for url in player_urls if url.strip()]
    year = movie.year or "год неизвестен"
    rating = "нет" if movie.rating is None else f"{movie.rating:.1f}"
    head = f"{movie.title} ({year})\nРейтинг: {rating}"
    tail = "Смотреть во VK:\n" + "\n".join(links) if links else ""
    return _fit(head, movie.overview.strip(), tail, fallback=title)


def _search(
    get: Callable[..., requests.Response],
    api_key: str,
    query: str,
    *,
    year: int | None,
    timeout: float,
) -> dict:
    params = {
        "api_key": api_key,
        "query": query,
        "language": LANGUAGE,
        "include_adult": "false",
    }
    if year is not None:
        params["year"] = str(year)
    try:
        response = get(SEARCH_URL, params=params, timeout=timeout)
    except requests.Timeout as exc:
        raise TmdbError("TMDB не ответил вовремя.") from exc
    except requests.RequestException as exc:
        raise TmdbError("Нет связи с TMDB.") from exc
    return _payload(response)


def _payload(response: requests.Response) -> dict:
    status = getattr(response, "status_code", 0)
    if status == 401:
        raise TmdbError("Ключ TMDB не принят. Нужен API Key (v3 auth) с сайта themoviedb.org.")
    if status != 200:
        raise TmdbError(f"TMDB ответил кодом {status}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TmdbError("TMDB вернул не JSON.") from exc
    if not isinstance(payload, dict):
        raise TmdbError("TMDB вернул неожиданный ответ.")
    return payload


def _results(payload: dict) -> list[dict]:
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _movie(item: dict) -> MovieInfo:
    raw_title = item.get("title") or item.get("original_title") or "Без названия"
    title = str(raw_title).strip() or "Без названия"
    overview = item.get("overview")
    poster = item.get("poster_path")
    poster_url = None
    if isinstance(poster, str) and poster.startswith("/"):
        poster_url = IMAGE_ROOT + poster
    return MovieInfo(
        title=title,
        year=_year(item.get("release_date")),
        rating=_rating(item.get("vote_average")),
        overview=overview.strip() if isinstance(overview, str) else "",
        poster_url=poster_url,
    )


def _year(value: object) -> str | None:
    if not isinstance(value, str) or len(value) < 4:
        return None
    head = value[:4]
    if head.isdigit() and head.startswith(("19", "20")):
        return head
    return None


def _rating(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fit(head: str, overview: str, tail: str, *, fallback: str) -> str:
    glue = "\n\n"
    tail_part = f"{glue}{tail}" if tail else ""
    room = _CAPTION_LIMIT - len(head) - len(tail_part)
    middle = ""
    if overview and room >= 40:
        text = overview
        budget = room - len(glue)
        if len(text) > budget:
            text = text[: max(budget - 1, 0)].rstrip() + "…"
        middle = f"{glue}{text}"
    elif not overview:
        middle = f"{glue}Описания на русском нет."
        if len(head) + len(middle) + len(tail_part) > _CAPTION_LIMIT:
            middle = ""
    result = head + middle + tail_part
    if len(result) <= _CAPTION_LIMIT:
        return result
    if tail:
        return (fallback + glue + tail)[: _CAPTION_LIMIT - 1] + "…"
    return result[: _CAPTION_LIMIT - 1] + "…"
