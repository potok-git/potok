from datetime import UTC, datetime
from pathlib import Path

import pytest

from potok.config import ConfigError, CredentialSettings, load_config
from potok.pipeline import (
    FetchError,
    clear_download_queue,
    deliver_approved,
    redact_url,
    resolve_credentials,
    run_fetch,
    torrent_path,
)
from potok.store import SeenStore
from potok.vpn import VpnManager

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Fresh 1080p rating: 8.5 genre: drama</title>
      <link>https://example.com/item-1</link>
      <guid>item-1</guid>
      <pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate>
      <enclosure url="https://example.com/a.torrent" type="application/x-bittorrent"/>
    </item>
    <item>
      <title>Weak 720p rating: 2.0 genre: noise</title>
      <link>https://example.com/item-2</link>
      <guid>item-2</guid>
      <pubDate>Thu, 08 Oct 2026 09:00:00 GMT</pubDate>
      <enclosure url="https://example.com/b.torrent" type="application/x-bittorrent"/>
    </item>
  </channel>
</rss>
"""


def _config(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss, metadata]
  via_direct: [downloads]
rss:
  metadata_dir: {tmp_path.as_posix()}/metadata
  feeds:
    - name: demo
      url: https://example.com/feed.xml
filters:
  min_rating: 7
  max_age_days: 14
  patterns:
    rating: '(?i)(?:rating|рейтинг)[:\\s]*(\\d+(?:[.,]\\d+)?)'
    quality: '(?i)\\b(2160p|1080p|720p|480p)\\b'
    genre: '(?i)(?:genre|жанр)[:\\s]+([^,\\]\\n]+)'
database:
  path: {tmp_path.as_posix()}/seen.db
""",
        encoding="utf-8",
    )
    return load_config(path)


def test_fetch_waits_for_approval_before_torrent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    events: list[str] = []

    def download(url: str, **kwargs: object) -> bytes:
        events.append(url)
        if url.endswith("feed.xml"):
            return RSS
        return b"d8:announce0:e"

    vpn = VpnManager("wg0", tmp_path / "wg0.conf", timeout_seconds=5)

    notes: list[tuple[str, int]] = []

    def notify(item, row_id: int) -> None:
        notes.append((item.guid, row_id))

    first = run_fetch(config, vpn, now=NOW, download=download, notify=notify)
    second = run_fetch(config, vpn, now=NOW, download=download, notify=notify)

    assert [item.guid for item in first.accepted] == ["demo:item-1"]
    assert first.skipped_filter == 1
    assert first.failed == 0
    assert notes == [("demo:item-1", notes[0][1])]
    assert second.accepted == ()
    assert second.skipped_seen == 1
    assert list((tmp_path / "metadata").glob("*.torrent")) == []
    assert "https://example.com/a.torrent" not in events
    assert "https://example.com/b.torrent" not in events

    with SeenStore(tmp_path / "seen.db") as store:
        waiting = store.get(notes[0][1])
        assert waiting is not None
        assert waiting.status == "pending"
        with pytest.raises(FetchError, match="после одобрения"):
            deliver_approved(config, vpn, waiting, download=download)
        decision = store.decide(waiting.row_id, "approved")
    assert decision is not None and decision.changed

    third = run_fetch(config, vpn, now=NOW, download=download, notify=notify)
    assert third.accepted == ()
    saved = list((tmp_path / "metadata").glob("*.torrent"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == b"d8:announce0:e"
    assert "up" not in events
    assert "down" not in events
    assert events.count("https://example.com/a.torrent") == 1


def test_same_movie_keeps_the_preferred_quality(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
rss:
  metadata_dir: {tmp_path.as_posix()}/metadata
  feeds:
    - name: demo
      url: https://example.com/feed.xml
filters:
  patterns:
    quality: '(?i)\\b(2160p|1080p)\\b'
database:
  path: {tmp_path.as_posix()}/seen.db
""",
        encoding="utf-8",
    )
    config = load_config(path)
    payload = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Восставший / The Uprising [2026, драма, WEB-DL 2160p | One]</title>
        <guid>low</guid>
        <link>https://example.com/low</link>
        <comments>http://rutracker.ru/viewtopic.php?t=1</comments>
        <size>10000000000</size>
        <torznab:attr name="seeders" value="4" />
      </item>
      <item>
        <title>Восставший / The Uprising [2026, драма, WEB-DL 2160p | Two]</title>
        <guid>high</guid>
        <link>https://example.com/high</link>
        <comments>http://rutracker.ru/viewtopic.php?t=2</comments>
        <size>12000000000</size>
        <torznab:attr name="seeders" value="40" />
      </item>
      <item>
        <title>Восставший / The Uprising [2026, драма, WEB-DL 1080p]</title>
        <guid>hd</guid>
        <link>https://example.com/hd</link>
        <comments>http://rutracker.ru/viewtopic.php?t=3</comments>
        <size>5000000000</size>
        <torznab:attr name="seeders" value="7" />
      </item>
    </channel></rss>
    """.encode()
    vpn = VpnManager("wg0", tmp_path / "wg0.conf", timeout_seconds=5)
    vpn.up = (lambda self: None).__get__(vpn, VpnManager)  # type: ignore[method-assign]
    vpn.down = (lambda self: None).__get__(vpn, VpnManager)  # type: ignore[method-assign]

    def download(url: str, **kwargs: object) -> bytes:
        del url, kwargs
        return payload

    report = run_fetch(config, vpn, now=NOW, download=download)
    assert [item.guid for item in report.accepted] == ["demo:high"]
    with SeenStore(tmp_path / "seen.db") as store:
        waiting = store.list_pending()
        assert [item.guid for item in waiting] == ["demo:high"]
        best = next(item for item in waiting if item.guid == "demo:high")
        assert best.seeders == 40
        assert best.topic_url == "http://rutracker.ru/viewtopic.php?t=2"
        assert best.kinopoisk_url is not None
        assert "kinopoisk.ru" in best.kinopoisk_url


def test_same_movie_prefers_1080_over_busier_720(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
rss:
  metadata_dir: {tmp_path.as_posix()}/metadata
  feeds:
    - name: demo
      url: https://example.com/feed.xml
filters:
  quality_keep: max
  patterns:
    quality: '(?i)\\b(2160p|1080p|720p)\\b'
database:
  path: {tmp_path.as_posix()}/seen.db
""",
        encoding="utf-8",
    )
    config = load_config(path)
    payload = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Восставший / The Uprising [2026, драма, WEB-DL 720p]</title>
        <guid>sd</guid>
        <link>https://example.com/sd</link>
        <size>4660000000</size>
        <torznab:attr name="seeders" value="126" />
      </item>
      <item>
        <title>Восставший / The Uprising [2026, драма, WEB-DL 1080p]</title>
        <guid>hd</guid>
        <link>https://example.com/hd</link>
        <size>9440000000</size>
        <torznab:attr name="seeders" value="36" />
      </item>
    </channel></rss>
    """.encode()
    vpn = VpnManager("wg0", tmp_path / "wg0.conf", timeout_seconds=5)
    vpn.up = (lambda self: None).__get__(vpn, VpnManager)  # type: ignore[method-assign]
    vpn.down = (lambda self: None).__get__(vpn, VpnManager)  # type: ignore[method-assign]

    def download(url: str, **kwargs: object) -> bytes:
        del url, kwargs
        return payload

    report = run_fetch(config, vpn, now=NOW, download=download)
    assert [item.quality for item in report.accepted] == ["1080p"]
    with SeenStore(tmp_path / "seen.db") as store:
        waiting = store.list_pending()
        assert [item.quality for item in waiting] == ["1080p"]


def test_pending_copy_is_replaced_only_toward_the_chosen_quality(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
rss:
  metadata_dir: {tmp_path.as_posix()}/metadata
  feeds:
    - name: demo
      url: https://example.com/feed.xml
filters:
  quality_keep: average
  patterns:
    quality: '(?i)\\b(2160p|1080p|720p)\\b'
database:
  path: {tmp_path.as_posix()}/seen.db
""",
        encoding="utf-8",
    )
    config = load_config(path)
    vpn = VpnManager("wg0", tmp_path / "wg0.conf", timeout_seconds=5)
    vpn.up = (lambda self: None).__get__(vpn, VpnManager)  # type: ignore[method-assign]
    vpn.down = (lambda self: None).__get__(vpn, VpnManager)  # type: ignore[method-assign]
    pages = {"body": _movie_rss("720p", "sd", 126)}

    def download(url: str, **kwargs: object) -> bytes:
        del url, kwargs
        return pages["body"]

    first = run_fetch(config, vpn, now=NOW, download=download)
    assert [item.quality for item in first.accepted] == ["720p"]

    pages["body"] = _movie_rss("1080p", "hd", 36)
    second = run_fetch(config, vpn, now=NOW, download=download)
    assert [item.quality for item in second.accepted] == ["1080p"]
    with SeenStore(tmp_path / "seen.db") as store:
        assert [item.quality for item in store.list_pending()] == ["1080p"]

    pages["body"] = _movie_rss("2160p", "uhd", 80)
    third = run_fetch(config, vpn, now=NOW, download=download)
    assert third.accepted == ()
    with SeenStore(tmp_path / "seen.db") as store:
        assert [item.quality for item in store.list_pending()] == ["1080p"]

    pages["body"] = _movie_rss("720p", "sd-again", 200)
    fourth = run_fetch(config, vpn, now=NOW, download=download)
    assert fourth.accepted == ()
    with SeenStore(tmp_path / "seen.db") as store:
        waiting = store.list_pending()
        assert [item.quality for item in waiting] == ["1080p"]


def _movie_rss(quality: str, guid: str, seeders: int) -> bytes:
    return f"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Восставший / The Uprising [2026, драма, WEB-DL {quality}]</title>
        <guid>{guid}</guid>
        <link>https://example.com/{guid}</link>
        <size>5000000000</size>
        <torznab:attr name="seeders" value="{seeders}" />
      </item>
    </channel></rss>
    """.encode()


def test_local_jackett_feed_does_not_enable_vpn(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
jackett:
  jackett_base_url: http://jackett:9117
  jackett_rss_url: ""
database:
  path: {tmp_path.as_posix()}/seen.db
rss:
  metadata_dir: {tmp_path.as_posix()}/metadata
  feeds:
    - name: jackett
      url: http://jackett:9117/feed.xml
""",
        encoding="utf-8",
    )
    config = load_config(path)
    events: list[str] = []
    vpn = VpnManager("wg0", tmp_path / "wg0.conf", timeout_seconds=5)
    vpn.up = (lambda self: events.append("up")).__get__(vpn, VpnManager)  # type: ignore[method-assign]
    vpn.down = (lambda self: events.append("down")).__get__(vpn, VpnManager)  # type: ignore[method-assign]

    def download(url: str, **kwargs: object) -> bytes:
        del kwargs
        events.append(url)
        return RSS

    report = run_fetch(config, vpn, now=NOW, download=download)
    assert report.failed == 0
    assert events == ["http://jackett:9117/feed.xml"]


def test_clear_download_queue_removes_torrent_file(tmp_path: Path) -> None:
    from potok.rss import FeedItem

    config = _config(tmp_path)
    item = FeedItem(
        guid="demo:queued",
        feed_name="demo",
        title="Queued",
        link="https://example.com/q",
        published=NOW,
        torrent_url="https://example.com/a.torrent",
        rating=8.0,
        quality="1080p",
        genre="drama",
    )
    with SeenStore(config.database_path) as store:
        row_id = store.add(item)
        store.decide(row_id, "approved")
    path = torrent_path(config.rss.metadata_dir, item.torrent_url or "")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"torrent")

    assert clear_download_queue(config) == 1
    assert path.is_file() is False
    with SeenStore(config.database_path) as store:
        assert store.has("demo:queued") is False


def test_credentials_require_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_FEED_USER", "reader")
    monkeypatch.delenv("POTOK_FEED_PASSWORD", raising=False)
    settings = CredentialSettings("POTOK_FEED_USER", "POTOK_FEED_PASSWORD")
    with pytest.raises(ConfigError, match="обе переменные"):
        resolve_credentials(settings)


def test_redact_url_hides_jackett_key() -> None:
    hidden = redact_url("http://jackett:9117/api?t=search&apikey=secret-key&cat=1")
    assert "secret-key" not in hidden
    assert "apikey=***" in hidden
    assert "cat=1" in hidden
