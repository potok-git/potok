import json
from pathlib import Path

from potok.config import StreamingSettings
from potok.portal import dispatch, session_from_cookie
from potok.stream import StreamError
from potok.users import UserStore

_HASH = "ab" * 20


def _settings() -> StreamingSettings:
    return StreamingSettings(
        enabled=True,
        base_url="http://torrserver:8090",
        public_url="http://127.0.0.1:8090",
        timeout_seconds=20,
        auto_add=True,
        server_torrent_dir="/opt/ts/inbox",
        portal_host="127.0.0.1",
        portal_port=8080,
        users_path=Path("data/portal_users.db"),
        username_env="POTOK_STREAM_USER",
        password_env="POTOK_STREAM_PASSWORD",
    )


class FakeStream:
    def __init__(self) -> None:
        self.sources = [
            {
                "hash": _HASH,
                "title": "Ролик <b>",
                "stat": 3,
                "file_stats": [{"id": 1}],
            }
        ]
        self.settings = {"cacheSize": 64 * 1024 * 1024, "friendlyName": "Potok"}
        self.removed: list[str] = []

    def list_sources(self) -> list[dict]:
        return self.sources

    def active_sources(self) -> list[dict]:
        return self.sources

    def get_settings(self) -> dict:
        return self.settings

    def update_settings(self, patch: dict) -> dict:
        self.settings.update(patch)
        return self.settings

    def add_source(self, link: str, *, title: str = "") -> dict:
        if link.startswith("/"):
            raise StreamError("Источник должен быть magnet-ссылкой, адресом http(s) или infohash.")
        item = {"hash": _HASH, "title": title or link, "stat": 0}
        self.sources.append(item)
        return item

    def remove_source(self, infohash: str) -> None:
        self.removed.append(infohash)


def test_catalog_lists_player_and_hls() -> None:
    result = dispatch("GET", "/", b"", FakeStream(), _settings())
    page = result.body.decode()
    assert result.status == 200
    assert "Ролик &lt;b&gt;" in page
    assert f"/play/{_HASH}/1" in page
    assert f"/gst/{_HASH}/master.m3u8" in page
    assert "MPEG-DASH" in page


def test_sources_and_active() -> None:
    client = FakeStream()
    listed = dispatch("GET", "/api/stream/sources", b"", client, _settings())
    active = dispatch("GET", "/api/stream/active", b"", client, _settings())
    assert listed.status == 200
    assert json.loads(active.body.decode())["items"][0]["stat_name"] == "working"


def test_add_and_remove_source() -> None:
    client = FakeStream()
    added = dispatch(
        "POST",
        "/api/stream/sources",
        json.dumps({"link": "https://example.com/a.torrent", "title": "Новый"}).encode(),
        client,
        _settings(),
    )
    assert added.status == 201
    assert json.loads(added.body.decode())["title"] == "Новый"
    removed = dispatch("DELETE", f"/api/stream/sources/{_HASH}", b"", client, _settings())
    assert removed.status == 200
    assert client.removed == [_HASH]


def test_bad_source_is_400() -> None:
    result = dispatch(
        "POST",
        "/api/stream/sources",
        json.dumps({"link": "C:/video.torrent"}).encode(),
        FakeStream(),
        _settings(),
    )
    assert result.status == 400


def test_settings_round_trip() -> None:
    client = FakeStream()
    updated = dispatch(
        "PUT",
        "/api/stream/settings",
        json.dumps({"useDisk": True}).encode(),
        client,
        _settings(),
    )
    assert updated.status == 200
    assert json.loads(updated.body.decode())["useDisk"] is True
    assert client.settings["friendlyName"] == "Potok"


def test_disabled_streaming_returns_503() -> None:
    result = dispatch("GET", "/api/stream/sources", b"", None, _settings())
    assert result.status == 503
    health = dispatch("GET", "/health", b"", None, _settings())
    assert json.loads(health.body.decode())["streaming"] is False


def test_server_down_is_502() -> None:
    class Down(FakeStream):
        def active_sources(self) -> list[dict]:
            raise StreamError("Нет связи с TorrServer: timeout")

    result = dispatch("GET", "/api/stream/active", b"", Down(), _settings())
    assert result.status == 502


def test_default_user_opens_users_page(tmp_path: Path) -> None:
    users = UserStore(tmp_path / "users.db")
    locked = dispatch("GET", "/users", b"", FakeStream(), _settings(), users=users)
    assert locked.status == 303
    assert locked.headers[0] == ("Location", "/login")

    login = dispatch(
        "POST",
        "/login",
        b"username=potok&password=potok",
        None,
        _settings(),
        users=users,
    )
    assert login.status == 303
    cookie = dict(login.headers)["Set-Cookie"]
    token = session_from_cookie(cookie)
    assert token is not None
    assert users.user_by_token(token) == "potok"

    page = dispatch(
        "GET",
        "/users",
        b"",
        FakeStream(),
        _settings(),
        actor="potok",
        users=users,
    )
    text = page.body.decode()
    assert page.status == 200
    assert "Новый пользователь" in text
    assert "Сменить пароль" in text
    assert ">potok</li>" in text


def test_users_page_adds_and_refuses_last_delete(tmp_path: Path) -> None:
    users = UserStore(tmp_path / "users.db")
    added = dispatch(
        "POST",
        "/users/add",
        b"name=anna&password=secret",
        None,
        _settings(),
        actor="potok",
        users=users,
    )
    assert added.status == 200
    assert ">anna</li>" in added.body.decode()

    removed = dispatch(
        "POST",
        "/users/delete",
        b"name=anna",
        None,
        _settings(),
        actor="potok",
        users=users,
    )
    assert "удалён" in removed.body.decode()
    last = dispatch(
        "POST",
        "/users/delete",
        b"name=potok",
        None,
        _settings(),
        actor="potok",
        users=users,
    )
    assert "последнего" in last.body.decode()
    assert users.list_users() == ("potok",)


def test_api_without_login_is_401(tmp_path: Path) -> None:
    users = UserStore(tmp_path / "users.db")
    result = dispatch("GET", "/api/users", b"", None, _settings(), users=users)
    assert result.status == 401
