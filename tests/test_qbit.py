import io
import json
from pathlib import Path
from urllib.error import HTTPError

from potok.config import load_config
from potok.qbit import QbitClient, eta_text
from potok.stream import StreamError

_HASH = "ab" * 20


def _settings(tmp_path: Path):
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
  enabled: true
  base_url: http://qbittorrent:8080
  public_url: http://127.0.0.1:8090
""",
        encoding="utf-8",
    )
    return load_config(path).streaming


class _Body:
    def __init__(self, raw: bytes, *, headers: dict[str, str] | None = None) -> None:
        self.raw = raw
        self.headers = headers or {}

    def read(self, _limit: int) -> bytes:
        return self.raw

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_eta_text_hides_unknown() -> None:
    assert eta_text(90) == "1 мин"
    assert eta_text(8640000) == "неизвестно"


def test_list_pause_and_add(tmp_path: Path) -> None:
    seen: list[str] = []

    def opener(request, timeout: float):
        assert timeout == 20
        seen.append(request.full_url)
        if request.full_url.endswith("/torrents/info"):
            body = json.dumps(
                [
                    {
                        "hash": _HASH,
                        "name": "Фильм",
                        "state": "downloading",
                        "dlspeed": 100,
                        "eta": 30,
                        "progress": 0.5,
                    }
                ]
            ).encode()
            return _Body(body)
        if request.full_url.endswith("/torrents/stop"):
            assert _HASH.encode() in request.data
            return _Body(b"Ok.")
        if request.full_url.endswith("/torrents/add"):
            assert b'filename="film.torrent"' in request.data
            return _Body(b"Ok.")
        return _Body(b"")

    client = QbitClient(_settings(tmp_path), opener=opener)
    rows = client.list_torrents()
    assert rows[0]["name"] == "Фильм"
    client.stop(_HASH)
    client.add_torrent_file("film.torrent", b"d8:announce0:e", title="Фильм")
    assert seen[0].endswith("/api/v2/torrents/info")
    assert any(url.endswith("/torrents/stop") for url in seen)


def test_add_magnet_sends_the_link(tmp_path: Path) -> None:
    link = (
        "magnet:?xt=urn:btih:262aa9e19151461068c542e7c5ae3ac01a379726"
        "&tr=http%3A%2F%2Frutracker.ru%2Fbt%2Fannounce.php%3Fuk%3D56WtM1NE1U"
    )
    sent = b""

    def opener(request, timeout: float):
        nonlocal sent
        assert timeout == 20
        sent = request.data
        return _Body(b"Ok.")

    client = QbitClient(_settings(tmp_path), opener=opener)
    client.add_magnet(link, title="Животные")
    assert b"urls=" in sent
    assert b"262aa9e19151461068c542e7c5ae3ac01a379726" in sent
    assert b"rename=" in sent


def test_add_magnet_already_in_client_is_ok(tmp_path: Path) -> None:
    def opener(request, timeout: float):
        assert timeout == 20
        raise HTTPError(request.full_url, 409, "Conflict", hdrs=None, fp=io.BytesIO(b"Conflict"))

    client = QbitClient(_settings(tmp_path), opener=opener)
    result = client.add_magnet("magnet:?xt=urn:btih:" + "ab" * 20, title="Животные")
    assert result["ok"] is True


def test_move_enables_queue_when_priority_is_missing(tmp_path: Path) -> None:
    seen: list[str] = []

    def opener(request, timeout: float):
        assert timeout == 20
        seen.append(request.full_url)
        if request.full_url.endswith("/torrents/info"):
            body = json.dumps([{"hash": _HASH, "priority": -1, "state": "downloading"}]).encode()
            return _Body(body)
        return _Body(b"Ok.")

    client = QbitClient(_settings(tmp_path), opener=opener)
    assert client.move_in_queue(_HASH, "up") is True
    assert any(url.endswith("/app/setPreferences") for url in seen)
    assert any(url.endswith("/torrents/increasePrio") for url in seen)


def test_bad_hash_is_rejected(tmp_path: Path) -> None:
    client = QbitClient(_settings(tmp_path), opener=lambda *_args, **_kwargs: None)
    try:
        client.remove("не-hash")
    except StreamError as exc:
        assert "40" in str(exc)
    else:
        raise AssertionError("короткий hash должен быть отклонён")


def test_stop_all_sends_hashes_all(tmp_path: Path) -> None:
    seen: list[bytes] = []

    def opener(request, timeout: float):
        del timeout
        if request.full_url.endswith("/torrents/info"):
            body = json.dumps([{"hash": _HASH, "name": "Один"}, {"hash": "cd" * 20}]).encode()
            return _Body(body)
        if request.full_url.endswith("/torrents/stop"):
            seen.append(request.data)
            return _Body(b"Ok.")
        return _Body(b"")

    client = QbitClient(_settings(tmp_path), opener=opener)
    assert client.stop_all() == 2
    assert seen[0] == b"hashes=all"


def test_stop_all_falls_back_to_pause(tmp_path: Path) -> None:
    from io import BytesIO
    from urllib.error import HTTPError

    seen: list[str] = []

    def opener(request, timeout: float):
        del timeout
        seen.append(request.full_url)
        if request.full_url.endswith("/torrents/info"):
            return _Body(b"[]")
        if request.full_url.endswith("/torrents/stop"):
            raise HTTPError(request.full_url, 404, "Not Found", None, BytesIO(b"missing"))
        if request.full_url.endswith("/torrents/pause"):
            assert request.data == b"hashes=all"
            return _Body(b"Ok.")
        return _Body(b"")

    client = QbitClient(_settings(tmp_path), opener=opener)
    assert client.stop_all() == 0
    assert any(url.endswith("/torrents/pause") for url in seen)


def test_torrent_files_asks_qbit_by_hash(tmp_path: Path) -> None:
    seen: list[str] = []

    def opener(request, timeout: float):
        del timeout
        seen.append(request.full_url)
        body = json.dumps([{"name": "film.mkv", "progress": 1}]).encode()
        return _Body(body)

    client = QbitClient(_settings(tmp_path), opener=opener)
    files = client.torrent_files(_HASH)
    assert files[0]["name"] == "film.mkv"
    assert seen[0].endswith(f"/api/v2/torrents/files?hash={_HASH}")
