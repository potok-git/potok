"""Клиент Jackett: проверка сервиса и ссылка на файл .torrent."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from potok.config import AppConfig, FeedSource, JackettSettings

logger = logging.getLogger(__name__)

_SEARCH_PATH = "/api/v2.0/indexers/all/results"
_CAPS_PATH = "/api/v2.0/indexers/all/results/torznab/api"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# Рутрекер отдаёт 50 строк на страницу. Сдвиг передаётся как genre=,
# описание трекера в config/jackett-definitions подставляет его в start=.
_PAGE_SIZE = 50
_PAGE_QUERY_KEYS = frozenset({"apikey", "genre", "offset", "limit"})


class JackettError(Exception):
    """Jackett не ответил, ключ не подошёл или подходящей раздачи нет."""


class _Http(Protocol):
    """Минимум, который нужен от requests.Session. В тестах подставляется заглушка."""

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
        allow_redirects: bool = True,
    ) -> Any: ...


class JackettClient:
    """Запросы к API Jackett внутри сети Docker.

    Ключ передаётся параметром apikey и в журнал не пишется.
    """

    def __init__(
        self,
        settings: JackettSettings,
        api_key: str,
        *,
        session: _Http | None = None,
    ) -> None:
        self.settings = settings
        self.api_key = api_key.strip()
        self._session = session or requests.Session()

    def ensure_available(self) -> None:
        """Проверить, что Jackett запущен и принимает ключ.

        Вызывается до опроса ленты. Если сервис молчит или ключ неверный,
        цикл загрузки останавливается.
        """
        if not self.api_key:
            raise JackettError(
                "Не задан ключ Jackett. Откройте портал, блок «Учётные данные», "
                "и впишите JACKETT_API_KEY. Его видно в Jackett справа вверху."
            )
        # /api/v2.0/server/config — внутренний адрес окна Jackett.
        # Даже верный ключ он отправляет на страницу входа. Ключ проверяет лента Torznab.
        url, params = _caps_target(self.settings)
        response = self._open(url, params, allow_redirects=False)
        if response.status_code in _REDIRECTS or _rejects_key(response.text):
            raise JackettError(
                "Jackett не принял ключ. Скопируйте API Key из его окна "
                "и запишите в секрет JACKETT_API_KEY."
            )
        if response.status_code >= 400:
            raise JackettError(f"Jackett ответил ошибкой {response.status_code}.")
        if "<caps" not in response.text.lower():
            raise JackettError("Jackett не подтвердил ключ: ответ не похож на ленту.")

    def read_caps(self) -> str:
        """XML со списком категорий трекера из ссылки ленты."""
        if not self.settings.rss_url:
            raise JackettError("Ссылка Torznab Feed пустая.")
        if not self.api_key:
            raise JackettError("Не задан ключ Jackett. Впишите его в поле ключа API.")
        url, params = _caps_target(self.settings)
        response = self._open(url, params, allow_redirects=False)
        if response.status_code in _REDIRECTS or _rejects_key(response.text):
            raise JackettError("Jackett не принял ключ, список категорий не открылся.")
        if response.status_code >= 400 or "<caps" not in response.text.lower():
            raise JackettError("Jackett не отдал список категорий.")
        return response.text

    def get_torrent_link(self, query: str) -> str:
        """Найти раздачу и вернуть ссылку Jackett на файл .torrent.

        В ссылке уже есть ключ доступа, поэтому трекер принимает её без
        отдельного логина. Из ответа берутся только раздачи не меньше
        заданного размера и с достаточным числом сидов. Если таких несколько,
        остаётся та, где сидов больше.
        """
        text = query.strip()
        if not text:
            raise JackettError("Пустой запрос к Jackett.")
        if not self.api_key:
            raise JackettError("Не задан ключ Jackett. Впишите его в секрет JACKETT_API_KEY.")

        payload = self._get(_SEARCH_PATH, params={"Query": text})
        results = payload.get("Results")
        if not isinstance(results, list):
            raise JackettError("Jackett вернул ответ без списка раздач.")

        best_link = ""
        best_seeders = -1
        for item in results:
            link, seeders = _accepted(item, self.settings)
            if link is None or seeders is None:
                continue
            if seeders > best_seeders:
                best_seeders = seeders
                best_link = link

        if not best_link:
            raise JackettError("Jackett не нашёл раздачу с нужным размером и числом сидов.")
        logger.info("Jackett: для запроса %r выбрана раздача, сидов %s.", text, best_seeders)
        return best_link

    def configured_indexers(self) -> tuple[str, ...]:
        """Трекеры, которые уже добавлены в Jackett (галочка configured)."""
        if not self.api_key:
            return ()
        response = self._open(
            f"{self.settings.base_url}/api/v2.0/indexers",
            {},
            allow_redirects=False,
        )
        if response.status_code in _REDIRECTS or _rejects_key(response.text):
            raise JackettError("Jackett не принял ключ, список трекеров не открылся.")
        if response.status_code >= 400:
            raise JackettError(f"Jackett ответил ошибкой {response.status_code}.")
        if not response.content:
            return ()
        try:
            payload = response.json()
        except ValueError as exc:
            raise JackettError("Jackett вернул не JSON.") from exc
        if not isinstance(payload, list):
            raise JackettError("Jackett вернул неожиданный список трекеров.")
        names: list[str] = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("configured"):
                continue
            ident = item.get("id") or item.get("name")
            if isinstance(ident, str) and ident.strip():
                names.append(ident.strip())
        return tuple(names)

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = self._open(f"{self.settings.base_url}{path}", params or {}, allow_redirects=True)
        if response.status_code in {401, 403} or response.status_code in _REDIRECTS:
            raise JackettError(
                "Jackett не принял ключ. Скопируйте API Key из его окна "
                "и запишите в секрет JACKETT_API_KEY."
            )
        if _rejects_key(response.text):
            raise JackettError(
                "Jackett не принял ключ. Скопируйте API Key из его окна "
                "и запишите в секрет JACKETT_API_KEY."
            )
        if response.status_code >= 400:
            raise JackettError(f"Jackett ответил ошибкой {response.status_code}.")
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise JackettError("Jackett вернул не JSON.") from exc
        if not isinstance(payload, dict):
            raise JackettError("Jackett вернул неожиданный ответ.")
        return payload

    def _open(self, url: str, params: dict[str, str], *, allow_redirects: bool) -> Any:
        query = {"apikey": self.api_key}
        query.update(params)
        try:
            return self._session.get(
                url,
                params=query,
                timeout=self.settings.timeout_seconds,
                allow_redirects=allow_redirects,
            )
        except requests.ConnectionError as exc:
            raise JackettError(
                "Jackett недоступен. Проверьте, что контейнер jackett запущен, "
                f"и адрес {self.settings.base_url} открывается."
            ) from exc
        except requests.Timeout as exc:
            raise JackettError("Jackett не ответил вовремя.") from exc
        except requests.RequestException as exc:
            raise JackettError("Запрос к Jackett не выполнился.") from exc


def get_torrent_link(query: str) -> str:
    """Найти раздачу через Jackett и вернуть прямую ссылку на .torrent.

    Настройки и ключ читаются из config.yaml и переменной JACKETT_API_KEY.
    """
    from potok.config import ConfigError, load_config

    try:
        config = load_config(_default_config_path())
    except ConfigError as exc:
        raise JackettError(str(exc)) from exc
    return client_from_config(config).get_torrent_link(query)


def _default_config_path() -> Path:
    env = os.environ.get("POTOK_CONFIG", "").strip()
    if env:
        return Path(env)
    return Path("/etc/potok/config.yaml")


def client_from_config(config: AppConfig, *, session: _Http | None = None) -> JackettClient:
    """Собрать клиент. Ключ из окружения важнее пустого поля в yaml."""
    return JackettClient(config.jackett, resolve_api_key(config.jackett), session=session)


def resolve_api_key(settings: JackettSettings) -> str:
    """Ключ из окружения, иначе из yaml, иначе из параметра apikey в адресе ленты."""
    from_env = os.environ.get(settings.api_key_env, "").strip()
    if from_env:
        return from_env
    if settings.api_key.strip():
        return settings.api_key.strip()
    return _api_key_in_url(settings.rss_url)


def ensure_jackett(config: AppConfig, *, session: _Http | None = None) -> None:
    """Остановить цикл, если лента Jackett задана, а сам сервис не отвечает.

    Пустой jackett_rss_url значит, что Jackett ещё не настроен: старые ленты
    из rss.feeds при этом продолжают работать.
    """
    if not config.jackett.rss_url:
        return
    client_from_config(config, session=session).ensure_available()


def feed_sources(config: AppConfig) -> tuple[FeedSource, ...]:
    """Адрес ленты. Если в настройках есть лента Jackett, внешние сайты не читаются.

    Имя у всех страниц одно и то же: так повтор одной раздачи не попадёт в очередь
    второй раз. Первая страница без сдвига, вторая — genre=50, третья — genre=100.
    """
    url = config.jackett.rss_url
    if not url:
        return config.rss.feeds
    key = resolve_api_key(config.jackett)
    pages = []
    for index in range(config.jackett.page_count):
        pages.append(
            FeedSource(
                name="jackett",
                url=_with_api_key(_page_url(url, start=index * _PAGE_SIZE), key),
            )
        )
    return tuple(pages)


def _page_url(url: str, *, start: int) -> str:
    """Убрать старый сдвиг и, если это не первая страница, записать новый."""
    parts = urlsplit(url)
    pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.lower() not in _PAGE_QUERY_KEYS
    ]
    if start > 0:
        pairs.append(("genre", str(start)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def _with_api_key(url: str, api_key: str) -> str:
    parts = urlsplit(url)
    pairs = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)]
    if api_key:
        pairs = [(name, value) for name, value in pairs if name.lower() != "apikey"]
        pairs.append(("apikey", api_key))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def _api_key_in_url(url: str) -> str:
    if not url:
        return ""
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if name.lower() == "apikey" and value.strip():
            return value.strip()
    return ""


def _caps_target(settings: JackettSettings) -> tuple[str, dict[str, str]]:
    """Адрес короткой проверки ключа. Берётся из ссылки ленты, если она задана."""
    if not settings.rss_url:
        return f"{settings.base_url}{_CAPS_PATH}", {"t": "caps"}
    parts = urlsplit(settings.rss_url)
    pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name.lower() not in {"apikey", "t"}
    ]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return url, {"t": "caps", **dict(pairs)}


def _accepted(item: object, settings: JackettSettings) -> tuple[str | None, int | None]:
    if not isinstance(item, dict):
        return None, None
    seeders = _whole(item.get("Seeders"))
    size = _whole(item.get("Size"))
    if seeders < settings.min_seeders or size < settings.min_size_bytes:
        return None, None
    link = item.get("Link")
    if not isinstance(link, str):
        return None, None
    href = link.strip()
    if not href.startswith(("http://", "https://")):
        return None, None
    return href, seeders


def _whole(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _rejects_key(body: str) -> bool:
    lowered = body.lower()
    return "invalid api" in lowered or "invalid apikey" in lowered
