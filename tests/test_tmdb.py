import pytest
import requests

from potok.tmdb import (
    _CACHE,
    MovieInfo,
    MovieNotFound,
    TmdbError,
    _getaddrinfo,
    _parse_a,
    country_text,
    format_caption,
    movie_query,
    search_movie,
)


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _movie_payload(**extra: object) -> dict:
    item = {
        "title": "Матрица",
        "overview": "Хакер узнаёт правду.",
        "poster_path": "/poster.jpg",
        "release_date": "1999-03-31",
        "vote_average": 8.2,
    }
    item.update(extra)
    return {"results": [item]}


def test_query_drops_release_noise() -> None:
    query, year = movie_query("Fun with Dick and Jane (2005) 1080p WEB-DL by TCG.mkv")
    assert query == "Fun with Dick and Jane"
    assert year == 2005

    query, year = movie_query(
        "7 пьес для черно-белого кино (Отар Иоселиани) [1982, Франция, DVDRip]"
    )
    assert query == "7 пьес для черно-белого кино"
    assert year == 1982

    query, year = movie_query(
        "Лего Звёздные войны: Мандалорец / Lego Star Wars: The Mandalorian "
        "(Крис Бакли) [2026, Мультфильм, WEB-DL 1080p] Dub (Даблин)"
    )
    assert query == "Лего Звёздные войны: Мандалорец"
    assert year == 2026


def test_search_uses_russian_and_builds_poster() -> None:
    calls: list[dict] = []

    def get(url: str, *, params: dict, timeout: float) -> _Response:
        calls.append({"url": url, "params": params, "timeout": timeout})
        return _Response(200, _movie_payload())

    movie = search_movie("secret-key", "Матрица (1999) 1080p", timeout=12, get=get)

    assert calls[0]["params"]["language"] == "ru-RU"
    assert calls[0]["params"]["api_key"] == "secret-key"
    assert calls[0]["params"]["query"] == "Матрица"
    assert calls[0]["params"]["year"] == "1999"
    assert calls[0]["timeout"] == 12
    assert movie.title == "Матрица"
    assert movie.year == "1999"
    assert movie.rating == 8.2
    assert movie.overview == "Хакер узнаёт правду."
    assert movie.poster_url == "https://image.tmdb.org/t/p/w500/poster.jpg"
    assert movie.kinopoisk_url is not None
    assert "kp_query=" in movie.kinopoisk_url
    assert movie.runtime_minutes is None


def test_search_reads_runtime_from_movie_card() -> None:
    def get(url: str, *, params: dict, timeout: float) -> _Response:
        del params, timeout
        if url.endswith("/603"):
            return _Response(
                200,
                {
                    "runtime": 136,
                    "production_countries": [
                        {"iso_3166_1": "US", "name": "United States of America"},
                        {"name": " Австралия "},
                    ],
                },
            )
        return _Response(200, _movie_payload(id=603))

    movie = search_movie("key", "Матрица", get=get)

    assert movie.runtime_minutes == 136
    assert movie.country == "США, Австралия"

    def no_country(url: str, *, params: dict, timeout: float) -> _Response:
        del params, timeout
        if url.endswith("/603"):
            return _Response(200, {"runtime": 100, "production_countries": []})
        return _Response(200, _movie_payload(id=603))

    empty = search_movie("key", "Матрица", get=no_country)
    assert empty.country == ""


def test_country_text_translates_english_names() -> None:
    assert country_text(None) == "нет"
    assert country_text("  ") == "нет"
    assert country_text("США") == "США"
    assert country_text("United States of America, Australia") == "США, Австралия"
    assert country_text("united states of america") == "США"
    assert country_text("Соединённые Штаты Америки") == "США"
    assert country_text("US") == "США"
    assert country_text("Uzbekistan, Saudi Arabia, Cyprus, Monaco") == (
        "Узбекистан, Саудовская Аравия, Кипр, Монако"
    )


def test_empty_year_search_retries_without_year() -> None:
    years: list[str | None] = []

    def get(_url: str, *, params: dict, timeout: float) -> _Response:
        del timeout
        years.append(params.get("year"))
        if params.get("year"):
            return _Response(200, {"results": []})
        return _Response(200, _movie_payload(title="Матрица"))

    movie = search_movie("key", "Матрица (1999)", get=get)

    assert years == ["1999", None]
    assert movie.title == "Матрица"


def test_kinopoisk_page_uses_wikidata_id() -> None:
    def get(url: str, *, params: dict, timeout: float, headers: dict | None = None) -> _Response:
        del params, timeout, headers
        if url.endswith("/external_ids"):
            return _Response(200, {"wikidata_id": "Q135103999"})
        if "wikidata.org" in url:
            payload = {
                "entities": {
                    "Q135103999": {
                        "claims": {
                            "P2603": [{"mainsnak": {"datavalue": {"value": "8005532"}}}]
                        }
                    }
                }
            }
            return _Response(200, payload)
        return _Response(200, _movie_payload(id=977942))

    movie = search_movie("key", "Восставший [2026]", get=get)
    assert movie.kinopoisk_url == "https://www.kinopoisk.ru/film/8005532/"
    def empty(_url: str, *, params: dict, timeout: float) -> _Response:
        del params, timeout
        return _Response(200, {"results": []})

    with pytest.raises(MovieNotFound, match="не найден"):
        search_movie("key", "Нет такого фильма", get=empty)

    def denied(_url: str, *, params: dict, timeout: float) -> _Response:
        del params, timeout
        return _Response(401, {})

    with pytest.raises(TmdbError, match="не принят"):
        search_movie("bad", "Матрица", get=denied)

    def offline(_url: str, *, params: dict, timeout: float) -> _Response:
        del params, timeout
        raise requests.Timeout("slow")

    with pytest.raises(TmdbError, match="вовремя"):
        search_movie("key", "Матрица", get=offline)


def test_quad9_answer_is_used_for_tmdb_only(monkeypatch: pytest.MonkeyPatch) -> None:
    name = b"\x03api\x0athemoviedb\x03org\x00"
    packet = (
        b"\x00\x01\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
        + name
        + b"\x00\x01\x00\x01"
        + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x01\x02\x03\x04"
    )
    assert _parse_a(packet) == ("1.2.3.4", 60)

    _CACHE.clear()
    monkeypatch.setattr("potok.tmdb._query_quad9", lambda host: ("1.2.3.4", 60))
    found = _getaddrinfo("api.themoviedb.org", 443)
    assert found[0][4][0] == "1.2.3.4"

    def fail(host: str) -> tuple[str | None, int]:
        raise AssertionError(host)

    monkeypatch.setattr("potok.tmdb._query_quad9", fail)
    ordinary = _getaddrinfo("localhost", 80)
    assert ordinary


def test_caption_keeps_vk_link_when_overview_is_long() -> None:
    movie = MovieInfo(
        title="Матрица",
        year="1999",
        rating=8.2,
        overview="слово " * 800,
        poster_url="https://image.tmdb.org/t/p/w500/poster.jpg",
    )
    url = "https://vk.com/video_ext.php?oid=1&id=2"
    text = format_caption("Матрица 1080p", (url,), movie)

    assert text.startswith("Матрица (1999)")
    assert "Рейтинг: 8.2" in text
    assert url in text
    assert len(text) <= 1024
