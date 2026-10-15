"""Клиент Jackett: фильтр раздач, обрыв связи и чужой ключ."""

from pathlib import Path

import pytest
import requests

from potok.config import JackettSettings, load_config
from potok.jackett import JackettClient, JackettError, feed_sources, resolve_api_key


def _settings(**overrides: object) -> JackettSettings:
    values = {
        "base_url": "http://jackett:9117",
        "rss_url": "",
        "api_key": "",
        "api_key_env": "JACKETT_API_KEY",
        "min_seeders": 2,
        "min_size_bytes": 1000,
        "timeout_seconds": 10.0,
        "page_count": 1,
    }
    values.update(overrides)
    return JackettSettings(
        base_url=str(values["base_url"]),
        rss_url=str(values["rss_url"]),
        api_key=str(values["api_key"]),
        api_key_env=str(values["api_key_env"]),
        min_seeders=int(str(values["min_seeders"])),
        min_size_bytes=int(str(values["min_size_bytes"])),
        timeout_seconds=float(str(values["timeout_seconds"])),
        page_count=int(str(values["page_count"])),
    )


class _Response:
    def __init__(self, status: int, payload: object, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = b"{}" if payload is not None else b""

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, response: _Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
        allow_redirects: bool = True,
    ) -> _Response:
        del timeout, allow_redirects
        self.calls.append((url, params))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_get_torrent_link_picks_the_largest_seed_count() -> None:
    payload = {
        "Results": [
            {
                "Title": "маленькая",
                "Seeders": 1,
                "Size": 5000,
                "Link": "http://jackett/small.torrent",
            },
            {"Title": "короткая", "Seeders": 9, "Size": 100, "Link": "http://jackett/tiny.torrent"},
            {
                "Title": "нужная",
                "Seeders": 4,
                "Size": 2000,
                "Link": "http://jackett:9117/dl/a?apikey=secret&file=movie.torrent",
            },
            {"Title": "без ссылки", "Seeders": 20, "Size": 9000, "Link": ""},
        ]
    }
    session = _Session(_Response(200, payload))
    client = JackettClient(_settings(), "secret", session=session)

    link = client.get_torrent_link("пример")

    assert link == "http://jackett:9117/dl/a?apikey=secret&file=movie.torrent"
    url, params = session.calls[0]
    assert url == "http://jackett:9117/api/v2.0/indexers/all/results"
    assert params["Query"] == "пример"
    assert params["apikey"] == "secret"


def test_connection_error_stops_without_the_key_in_the_message() -> None:
    session = _Session(requests.ConnectionError("down"))
    client = JackettClient(_settings(), "secret-key", session=session)

    with pytest.raises(JackettError, match="недоступен") as caught:
        client.ensure_available()

    assert "secret-key" not in str(caught.value)


def test_bad_api_key() -> None:
    session = _Session(_Response(200, {}, text='<error description="Invalid API Key" />'))
    client = JackettClient(
        _settings(
            rss_url="http://jackett:9117/api/v2.0/indexers/demo/results/torznab/api?t=search"
        ),
        "nope",
        session=session,
    )

    with pytest.raises(JackettError, match="не принял ключ"):
        client.ensure_available()

    url, params = session.calls[0]
    assert url == "http://jackett:9117/api/v2.0/indexers/demo/results/torznab/api"
    assert params["t"] == "caps"
    assert params["apikey"] == "nope"


def test_caps_response_accepts_the_key() -> None:
    session = _Session(_Response(200, {}, text="<?xml version='1.0'?><caps></caps>"))
    client = JackettClient(_settings(), "secret", session=session)

    client.ensure_available()

    url, _params = session.calls[0]
    assert url.endswith("/api/v2.0/indexers/all/results/torznab/api")


def test_resolve_api_key_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JACKETT_API_KEY", "from-env")
    settings = _settings(
        api_key="from-file",
        rss_url="http://jackett:9117/feed?apikey=from-url",
    )

    assert resolve_api_key(settings) == "from-env"


def test_feed_url_comes_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JACKETT_API_KEY", "live-key")
    path = tmp_path / "config.yaml"
    path.write_text(
        """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
jackett:
  jackett_base_url: http://jackett:9117
  jackett_rss_url: http://jackett:9117/api/v2.0/indexers/demo/results/torznab/api?t=search
  jackett_api_key: ""
rss:
  feeds:
    - name: external
      url: https://example.com/feed.xml
""",
        encoding="utf-8",
    )

    config = load_config(path)
    feeds = feed_sources(config)

    assert len(feeds) == 1
    assert feeds[0].name == "jackett"
    assert "apikey=live-key" in feeds[0].url
    assert "example.com" not in feeds[0].url
    assert "genre=" not in feeds[0].url


def test_extra_pages_shift_by_fifty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JACKETT_API_KEY", "live-key")
    path = tmp_path / "config.yaml"
    path.write_text(
        """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
jackett:
  jackett_base_url: http://jackett:9117
  jackett_rss_url: http://jackett:9117/api?t=search&cat=1,2&genre=999&apikey=old
  page_count: 3
""",
        encoding="utf-8",
    )

    feeds = feed_sources(load_config(path))

    assert [item.name for item in feeds] == ["jackett", "jackett", "jackett"]
    assert "genre=" not in feeds[0].url
    assert "genre=50" in feeds[1].url
    assert "genre=100" in feeds[2].url
    assert "apikey=old" not in feeds[1].url
    assert "apikey=live-key" in feeds[2].url


def test_configured_indexers_keeps_only_added() -> None:
    payload = [
        {"id": "rutracker-ru", "configured": True},
        {"id": "other", "configured": False},
        {"name": "broken"},
    ]
    session = _Session(_Response(200, payload))
    client = JackettClient(_settings(), "secret", session=session)

    names = client.configured_indexers()

    assert names == ("rutracker-ru",)
    url, params = session.calls[0]
    assert url == "http://jackett:9117/api/v2.0/indexers"
    assert params["apikey"] == "secret"
