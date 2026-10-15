import json
from pathlib import Path

import pytest

from potok.config import load_config
from potok.stream import (
    StreamClient,
    StreamError,
    playback_links,
    register_downloaded,
    validate_settings_patch,
    validate_source_link,
)

_CONFIG = """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
streaming:
  enabled: true
  auto_add: true
  base_url: http://torrserver:8090
  public_url: http://127.0.0.1:8090
"""


def _settings(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    return load_config(path).streaming


class _Body:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def read(self, _limit: int) -> bytes:
        return self.raw

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_link_must_be_magnet_url_or_hash() -> None:
    assert validate_source_link("a" * 40) == "a" * 40
    magnet = "magnet:?xt=urn:btih:" + "ab" * 20
    assert validate_source_link(magnet) == magnet
    assert validate_source_link("https://example.com/a.torrent")
    with pytest.raises(StreamError, match="Источник"):
        validate_source_link("/etc/passwd")


def test_settings_patch_keeps_only_known_fields() -> None:
    cleaned = validate_settings_patch({"cacheSize": 64 * 1024 * 1024, "useDisk": True})
    assert cleaned == {"cacheSize": 64 * 1024 * 1024, "useDisk": True}
    with pytest.raises(StreamError, match="нельзя"):
        validate_settings_patch({"enableRutorSearch": True})


def test_playback_links_point_at_hls_and_stream() -> None:
    item = {
        "hash": "AB" * 20,
        "title": "Фильм",
        "stat": 3,
        "file_stats": [{"id": 2, "path": "a.mkv"}],
        "download_speed": 10,
        "torrent_size": 5,
    }
    links = playback_links("http://127.0.0.1:8090", item)
    assert links["stat_name"] == "working"
    assert links["hls_url"] == "http://127.0.0.1:8090/gst/" + "ab" * 20 + "/master.m3u8?index=2"
    assert "link=" + "ab" * 20 in links["stream_url"]
    assert links["player_url"].endswith("/play/" + "ab" * 20 + "/2")


def test_client_adds_and_lists(tmp_path: Path) -> None:
    seen: list[tuple[str, bytes]] = []

    def opener(request, timeout: float):
        assert timeout == 20
        seen.append((request.full_url, request.data))
        if request.full_url.endswith("/torrents"):
            body = json.loads(request.data.decode())
            if body["action"] == "list":
                return _Body(json.dumps([{"hash": "ab" * 20, "title": "A", "stat": 3}]).encode())
            return _Body(json.dumps({"hash": "ab" * 20, "title": body["title"]}).encode())
        return _Body(b"")

    client = StreamClient(_settings(tmp_path), opener=opener)
    added = client.add_source("https://example.com/a.torrent", title="Ролик")
    active = client.active_sources()
    assert added["title"] == "Ролик"
    assert active[0]["title"] == "A"
    assert json.loads(seen[0][1].decode())["save_to_db"] is True


def test_update_settings_merges_current(tmp_path: Path) -> None:
    def opener(request, timeout: float):
        del timeout
        body = json.loads(request.data.decode())
        if body["action"] == "get":
            return _Body(json.dumps({"cacheSize": 100, "friendlyName": "Potok"}).encode())
        assert body["sets"]["friendlyName"] == "Potok"
        assert body["sets"]["useDisk"] is True
        return _Body(b"")

    client = StreamClient(_settings(tmp_path), opener=opener)
    saved = client.update_settings({"useDisk": True})
    assert saved["cacheSize"] == 100
    assert saved["useDisk"] is True


def test_register_skips_when_disabled(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
streaming:
  enabled: false
  auto_add: true
""",
        encoding="utf-8",
    )
    torrent = tmp_path / "a.torrent"
    torrent.write_bytes(b"d8:announce0:e")
    assert register_downloaded(load_config(path), torrent, "Фильм") is None


def test_register_uploads_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    def opener(request, timeout: float):
        del timeout
        captured.append(request)
        return _Body(json.dumps([{"hash": "cd" * 20}]).encode())

    monkeypatch.setattr("potok.qbit.urlopen", opener)
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    torrent = tmp_path / "abcd.torrent"
    torrent.write_bytes(b"d8:announce0:e")
    added = register_downloaded(load_config(path), torrent, "Фильм")
    assert added == {"ok": True, "title": "Фильм"}
    sent = captured[0]
    assert sent.full_url.endswith("/api/v2/torrents/add")
    assert b'filename="abcd.torrent"' in sent.data
    assert b"d8:announce0:e" in sent.data
