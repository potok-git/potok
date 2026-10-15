import json
from dataclasses import replace
from pathlib import Path

import pytest

from potok.config import load_config
from potok.vk_video import (
    MultipartFile,
    VkCatalogItem,
    VkError,
    VkStopped,
    VkVideoClient,
    _post_stream,
    playlist_names,
)

_CONFIG = """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
"""

_PLAYER = "https://vk.com/video_ext.php?oid=5&id=9&hash=abc"


def _settings(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    return load_config(path).vk


class _FakeTransport:
    def __init__(self, file_size: int) -> None:
        self.file_size = file_size
        self.forms: list[tuple[str, dict[str, str]]] = []
        self.headers: list[dict[str, str]] = []
        self.uploads = 0
        self.gets = 0
        self.fail_upload_once = False
        self.processing_once = False
        self.api_error: dict | None = None

    def request_api(self, method: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        self.forms.append((method, form))
        self.headers.append(headers)
        assert "access_token" not in form
        assert headers["Authorization"].startswith("Bearer ")
        if self.api_error is not None:
            error = self.api_error
            self.api_error = None
            return {"error": error}
        if method == "video.save":
            return {
                "response": {
                    "upload_url": "https://ovu.mycdn.me/upload.do?sig=1",
                    "owner_id": 5,
                    "video_id": 9,
                    "access_key": "abc",
                    "title": form["name"],
                }
            }
        if method == "video.edit":
            assert form.get("privacy_view") == "by_link"
            return {"response": {"success": 1}}
        if method == "video.delete":
            return {"response": 1}
        if method == "video.get":
            self.gets += 1
            if self.processing_once and self.gets == 1:
                return {"response": {"count": 1, "items": [{"processing": 1, "player": _PLAYER}]}}
            return {
                "response": {
                    "count": 1,
                    "items": [{"player": _PLAYER, "owner_id": 5, "id": 9}],
                }
            }
        raise AssertionError(method)

    def upload(
        self,
        url: str,
        *,
        content_type: str,
        content_length: int,
        chunks,
        should_stop=None,
    ) -> dict:
        del should_stop
        body = b"".join(chunks)
        assert url.startswith("https://ovu.mycdn.me/")
        assert "video_file" in content_type or "multipart" in content_type
        assert len(body) == content_length
        self.uploads += 1
        if self.fail_upload_once:
            self.fail_upload_once = False
            raise VkError("обрыв связи", retryable=True)
        return {"size": self.file_size, "owner_id": 5, "video_id": 9}


def test_multipart_reads_file_in_chunks(tmp_path: Path) -> None:
    payload = b"0123456789"
    path = tmp_path / "ролик.mp4"
    path.write_bytes(payload)
    stream = MultipartFile(path, chunk_bytes=4)
    seen: list[int] = []
    parts = list(stream.iter_parts(lambda sent, total: seen.append(sent)))
    body = b"".join(parts)
    assert body.startswith(b"--")
    assert b"filename*=UTF-8''" in body
    assert b"".join(parts[1:-1]) == payload
    assert all(len(part) <= 4 for part in parts[1:-1])
    assert len(body) == stream.content_length
    assert seen == [4, 8, 10]


def test_upload_waits_until_player_is_ready(tmp_path: Path) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"video-bytes")
    transport = _FakeTransport(video.stat().st_size)
    transport.processing_once = True
    transport.fail_upload_once = True
    transport.api_error = {"error_code": 6, "error_msg": "too many"}
    sleeps: list[float] = []
    client = VkVideoClient(
        replace(_settings(tmp_path), upload_attempts=2, api_retries=2, poll_interval_seconds=5),
        "secret-token",
        transport=transport,
        sleep=sleeps.append,
    )

    published = client.upload_and_wait(video, title="Фильм", description="https://example.com")

    assert published.player_url == _PLAYER
    assert published.page_url == "https://vk.com/video5_9?access_key=abc"
    assert transport.uploads == 2
    assert transport.gets == 2
    assert "secret-token" not in json.dumps(transport.forms)
    assert sleeps


def test_auth_error_is_not_retried_as_success(tmp_path: Path) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"video-bytes")
    transport = _FakeTransport(video.stat().st_size)
    transport.api_error = {"error_code": 5, "error_msg": "auth"}
    client = VkVideoClient(
        _settings(tmp_path),
        "token",
        transport=transport,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(VkError, match="код 5") as caught:
        client.upload_and_wait(video, title="Фильм")
    assert caught.value.retryable is False
    assert transport.uploads == 0


def test_file_over_limit_does_not_call_vk(tmp_path: Path) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"12345")
    transport = _FakeTransport(5)
    client = VkVideoClient(
        replace(_settings(tmp_path), max_bytes=4),
        "token",
        transport=transport,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(VkError, match="лимита"):
        client.upload_and_wait(video, title="Фильм")
    assert transport.forms == []


def test_poll_timeout(tmp_path: Path) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"1234")
    transport = _FakeTransport(4)
    clock = {"now": 0.0}

    def now() -> float:
        return clock["now"]

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    client = VkVideoClient(
        replace(_settings(tmp_path), poll_interval_seconds=5, poll_timeout_seconds=30),
        "token",
        transport=transport,
        sleep=sleep,
        now=now,
    )
    transport.processing_once = False

    def always_processing(method: str, form: dict[str, str], headers: dict[str, str]) -> dict:
        if method in {"video.save", "video.edit"}:
            return _FakeTransport.request_api(transport, method, form, headers)
        return {"response": {"count": 0, "items": []}}

    transport.request_api = always_processing  # type: ignore[method-assign]
    with pytest.raises(VkError, match="не закончил обработку"):
        client.upload_and_wait(video, title="Фильм")


def test_stop_during_upload_deletes_unfinished_video(tmp_path: Path) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"0123456789")
    transport = _FakeTransport(video.stat().st_size)
    seen = {"chunks": 0}

    def on_progress(_sent: int, _total: int) -> None:
        seen["chunks"] += 1

    client = VkVideoClient(
        replace(_settings(tmp_path), chunk_bytes=4),
        "token",
        transport=transport,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(VkStopped, match="остановлена"):
        client.upload_and_wait(
            video,
            title="Фильм",
            on_progress=on_progress,
            should_stop=lambda: seen["chunks"] > 0,
        )
    methods = [name for name, _form in transport.forms]
    assert methods.count("video.delete") == 1
    assert transport.uploads == 0


def test_magnet_is_not_written_to_vk_description(tmp_path: Path) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(b"video-bytes")
    transport = _FakeTransport(video.stat().st_size)
    client = VkVideoClient(
        _settings(tmp_path),
        "token",
        transport=transport,
        sleep=lambda _seconds: None,
    )
    magnet = "magnet:?xt=urn:btih:" + "ab" * 20 + "&tr=http%3A%2F%2Frutracker.ru%2Fannounce"
    client.upload_and_wait(video, title="Фильм", description=f"О фильме\n{magnet}")
    saved = next(form for name, form in transport.forms if name == "video.save")
    assert saved["description"] == "О фильме"
    assert "magnet:" not in saved["description"]


def test_drop_magnet_descriptions_edits_only_those_videos(tmp_path: Path) -> None:
    magnet = "magnet:?xt=urn:btih:" + "cd" * 20

    class Transport:
        def __init__(self) -> None:
            self.forms: list[tuple[str, dict[str, str]]] = []

        def request_api(self, method: str, form: dict[str, str], headers: dict[str, str]) -> dict:
            del headers
            self.forms.append((method, form))
            if method == "video.get":
                return {
                    "response": {
                        "count": 3,
                        "items": [
                            {
                                "id": 1,
                                "owner_id": -5,
                                "title": "С магнитом",
                                "description": magnet,
                            },
                            {
                                "id": 2,
                                "owner_id": -5,
                                "title": "Без магнита",
                                "description": "обычный текст",
                            },
                            {
                                "id": 3,
                                "owner_id": -5,
                                "title": "С текстом",
                                "description": f"О фильме {magnet}",
                            },
                        ],
                    }
                }
            if method == "video.edit":
                return {"response": 1}
            raise AssertionError(method)

        def upload(self, *_args: object, **_kwargs: object) -> dict:
            raise AssertionError("upload")

    transport = Transport()
    sleeps: list[float] = []
    client = VkVideoClient(
        _settings(tmp_path),
        "token",
        transport=transport,
        sleep=sleeps.append,
    )
    assert client.drop_magnet_descriptions() == 2
    edits = [form for name, form in transport.forms if name == "video.edit"]
    assert edits == [
        {
            "owner_id": "-5",
            "video_id": "1",
            "name": "С магнитом",
            "desc": "",
            "v": "5.131",
        },
        {
            "owner_id": "-5",
            "video_id": "3",
            "name": "С текстом",
            "desc": "О фильме",
            "v": "5.131",
        },
    ]
    assert sleeps == [0.4, 0.4]


def test_list_edit_and_delete(tmp_path: Path) -> None:
    class Transport:
        def __init__(self) -> None:
            self.forms: list[tuple[str, dict[str, str]]] = []

        def request_api(self, method: str, form: dict[str, str], headers: dict[str, str]) -> dict:
            assert headers["Authorization"].startswith("Bearer ")
            self.forms.append((method, form))
            if method == "video.get":
                return {
                    "response": {
                        "count": 1,
                        "items": [
                            {
                                "id": 9,
                                "owner_id": 5,
                                "title": "Фильм",
                                "description": "про фильм",
                                "duration": 65,
                                "player": _PLAYER,
                                "image": [
                                    {"url": "http://evil.example/a.jpg", "width": 2000},
                                    {"url": "https://sun9-1.userapi.com/small.jpg", "width": 130},
                                    {"url": "https://sun9-1.userapi.com/big.jpg", "width": 800},
                                ],
                            }
                        ],
                    }
                }
            if method in {"video.edit", "video.delete"}:
                return {"response": 1}
            raise AssertionError(method)

        def upload(self, *_args: object, **_kwargs: object) -> dict:
            raise AssertionError("upload")

    transport = Transport()
    client = VkVideoClient(
        _settings(tmp_path),
        "token",
        transport=transport,
        sleep=lambda _seconds: None,
    )
    items = client.list_videos()
    assert items == (
        VkCatalogItem(
            owner_id=5,
            video_id=9,
            title="Фильм",
            description="про фильм",
            page_url="https://vk.com/video5_9",
            player_url=_PLAYER,
            duration_seconds=65,
            processing=False,
            image_url="https://sun9-1.userapi.com/big.jpg",
        ),
    )
    client.edit_video(5, 9, title="Новое", description="текст")
    client.delete_video(5, 9)
    assert transport.forms[1] == (
        "video.edit",
        {
            "owner_id": "5",
            "video_id": "9",
            "name": "Новое",
            "desc": "текст",
            "v": "5.131",
        },
    )
    assert transport.forms[2][0] == "video.delete"


def test_post_stream_sends_chunks_even_if_socket_takes_them_in_parts() -> None:
    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return b'{"ok": true}'

        def close(self) -> None:
            return

    class Conn:
        def __init__(self) -> None:
            self.sent = b""
            self.headers: list[tuple[str, str]] = []

        def putrequest(self, method: str, target: str) -> None:
            assert method == "POST"
            assert target == "/upload"

        def putheader(self, key: str, value: str) -> None:
            self.headers.append((key, value))

        def endheaders(self) -> None:
            return

        def send(self, data: bytes) -> int:
            piece = bytes(data[:2])
            self.sent += piece
            return len(piece)

        def getresponse(self) -> Response:
            return Response()

    conn = Conn()
    status, raw = _post_stream(
        conn,  # type: ignore[arg-type]
        "/upload",
        "multipart/form-data; boundary=abc",
        5,
        [b"he", b"llo"],
    )

    assert status == 200
    assert raw == b'{"ok": true}'
    assert conn.sent == b"hello"
    assert ("Content-Length", "5") in conn.headers


def test_playlist_names_are_genre_and_year() -> None:
    assert playlist_names("Комедия, криминал", 2026) == (
        "2026 - Комедия",
        "2026 - криминал",
    )
    assert playlist_names("Драма / мелодрама", 1982) == (
        "1982 - Драма",
        "1982 - мелодрама",
    )
    assert playlist_names("  ", 2026) == ()
    assert playlist_names("Драма", None) == ()


def test_missing_playlist_is_created_and_existing_is_reused(tmp_path: Path) -> None:
    class Transport:
        def __init__(self) -> None:
            self.forms: list[tuple[str, dict[str, str]]] = []

        def request_api(self, method: str, form: dict[str, str], headers: dict[str, str]) -> dict:
            assert headers["Authorization"].startswith("Bearer ")
            self.forms.append((method, form))
            if method == "video.getAlbums":
                return {
                    "response": {
                        "count": 1,
                        "items": [{"id": 3, "title": "2026 - комедия"}],
                    }
                }
            if method == "video.addAlbum":
                assert form["title"] == "2026 - криминал"
                assert "group_id" not in form
                return {"response": 8}
            if method == "video.addToAlbum":
                assert form["access_key"] == "abc"
                assert form["owner_id"] == "5"
                assert form["video_id"] == "9"
                return {"response": 1}
            raise AssertionError(method)

        def upload(self, *_args: object, **_kwargs: object) -> dict:
            raise AssertionError("upload")

    transport = Transport()
    client = VkVideoClient(
        _settings(tmp_path),
        "token",
        transport=transport,
        sleep=lambda _seconds: None,
    )
    placed = client.place_in_playlists(
        5,
        9,
        ("2026 - Комедия", "2026 - криминал"),
        access_key="abc",
    )
    assert placed == ("2026 - Комедия", "2026 - криминал")
    added = [form for method, form in transport.forms if method == "video.addToAlbum"]
    assert [form["album_id"] for form in added] == ["3", "8"]
    assert [method for method, _form in transport.forms].count("video.addAlbum") == 1


def test_group_playlist_is_created_in_the_community(tmp_path: Path) -> None:
    class Transport:
        def __init__(self) -> None:
            self.forms: list[tuple[str, dict[str, str]]] = []

        def request_api(self, method: str, form: dict[str, str], headers: dict[str, str]) -> dict:
            self.forms.append((method, form))
            if method == "video.getAlbums":
                assert form["owner_id"] == "-20"
                return {"response": {"count": 0, "items": []}}
            if method == "video.addAlbum":
                assert form["group_id"] == "20"
                assert form["title"] == "2024 - Драма"
                return {"response": {"album_id": 4}}
            if method == "video.addToAlbum":
                assert form["target_id"] == "-20"
                assert form["album_id"] == "4"
                return {"response": 1}
            raise AssertionError(method)

        def upload(self, *_args: object, **_kwargs: object) -> dict:
            raise AssertionError("upload")

    client = VkVideoClient(
        _settings(tmp_path),
        "token",
        transport=Transport(),
        sleep=lambda _seconds: None,
    )
    placed = client.place_in_playlists(-20, 11, ("2024 - Драма",))
    assert placed == ("2024 - Драма",)
