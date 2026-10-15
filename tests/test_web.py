import io
import json
import os
from pathlib import Path
from urllib.error import HTTPError

import yaml

from potok.config import load_config
from potok.jobs import drain_jobs, enqueue, jobs_path
from potok.logs import count_errors, iter_events
from potok.onboarding import mark_later
from potok.pipeline import torrent_path
from potok.publish import PublishResult
from potok.restart import restart_worker
from potok.rss import FeedItem
from potok.secrets import SecretStore
from potok.settings_io import resolve_wg_path
from potok.store import SeenStore
from potok.stream import torrent_basename
from potok.users import UserStore
from potok.vk_oauth import check_video_token
from potok.vk_uploads import UploadBoard
from potok.vk_video import VkCatalogItem
from potok.web import create_app

_TOKEN = "SUPERSECRETTOKEN"
_PRIVATE = "SUPERSECRETPRIVATEKEYVALUE"


def _yaml(tmp_path: Path, *, streaming: bool = False) -> Path:
    database = (tmp_path / "seen.db").as_posix()
    wireguard = (tmp_path / "wg0.conf").as_posix()
    metadata = (tmp_path / "metadata").as_posix()
    downloads = (tmp_path / "downloads").as_posix()
    enabled = "true" if streaming else "false"
    text = f"""
vpn:
  interface: wg0
  config_path: "{wireguard}"
routing:
  via_vpn: [rss]
  via_direct: [downloads]
logging:
  level: INFO
database:
  path: "{database}"
rss:
  metadata_dir: "{metadata}"
delivery:
  download_dir: "{downloads}"
streaming:
  enabled: {enabled}
  public_url: http://127.0.0.1:8090
"""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _app(tmp_path: Path, *, streaming: bool = False, client: object | None = None):
    path = _yaml(tmp_path, streaming=streaming)
    config = load_config(path)
    users = UserStore(tmp_path / "users.db")
    store = SecretStore(tmp_path)
    app = create_app(
        config,
        config_path=path,
        users=users,
        secret_store=store,
        client=client,
        log_path=tmp_path / "worker.log",
    )
    app.config["TESTING"] = True
    # Обычные тесты портала не проходят мастер: флаг как у кнопки «Настроить позже».
    mark_later(tmp_path)
    return app


def _csrf(page: str) -> str:
    marker = 'name="csrf" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


def _login(client) -> str:
    page = client.get("/login")
    token = _csrf(page.data.decode())
    response = client.post(
        "/login",
        data={"csrf": token, "username": "potok", "password": "potok"},
    )
    assert response.status_code == 302
    return token


def test_logo_files_are_public(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    page = client.get("/login").data.decode()
    assert "logo.svg" in page
    assert "favicon.png" in page
    assert "logo.png" in page
    for name in ("logo.svg", "logo.png", "favicon.png"):
        response = client.get(f"/static/{name}")
        assert response.status_code == 200
        assert len(response.data) > 100
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.mimetype == "image/png"


def test_about_page_is_open_and_plain(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    page = client.get("/about")
    assert page.status_code == 200
    text = page.data.decode()
    assert "О проекте" in text
    assert "Частые вопросы" in text
    assert 'href="#faq"' in text
    assert 'id="faq"' in text
    assert "Jackett не отдаёт ленту" in text
    assert "about/login.png" in text
    assert "пароль potok" not in text
    assert "Сколько это заняло" not in text
    assert "Что дальше" not in text
    for name in (
        "login.png",
        "dashboard.png",
        "pending.png",
        "downloads.png",
        "vk.png",
        "settings.png",
        "users.png",
    ):
        image = client.get(f"/static/about/{name}")
        assert image.status_code == 200
        assert image.mimetype == "image/png"
    _login(client)
    assert "О проекте" in client.get("/").data.decode()


def test_dashboard_requires_login_and_shows_counts(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    locked = client.get("/")
    assert locked.status_code == 302
    assert locked.headers["Location"].endswith("/login")

    log = tmp_path / "worker.log"
    log.write_text("12:00:00 ERROR potok: лента недоступна\n", encoding="utf-8")
    _login(client)
    page = client.get("/")
    text = page.data.decode()
    assert page.status_code == 200
    assert "Ожидают одобрения" in text
    assert "Одобрено" in text
    assert "Загружаются" in text
    assert "Выгружаются в ВК" in text
    assert "Ближайшие в очереди" not in text
    assert "Последние ошибки" not in text
    assert "лента недоступна" not in text
    assert count_errors(log) == 1


def test_dashboard_counts_approved_still_loading(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    config = load_config(tmp_path / "config.yaml")

    def film(guid: str, title: str) -> FeedItem:
        return FeedItem(
            guid=guid,
            feed_name="demo",
            title=title,
            link=f"https://example.com/{guid}",
            published=None,
            torrent_url=f"https://example.com/{guid}.torrent",
            rating=7,
            quality="1080p",
            genre="драма",
        )

    with SeenStore(config.database_path) as store:
        done_id = store.add(film("done", "Уже в VK"), status="approved")
        store.set_vk_url(done_id, "https://vk.com/video1")
        store.add(film("load", "Ещё качается"), status="approved")
        store.add(film("nope", "Не надо"), status="rejected")
    text = client.get("/").data.decode()
    assert 'Одобрено</span>\n    <span class="dash-tile-value">2</span>' in text
    assert 'Загружаются</span>\n    <span class="dash-tile-value">1</span>' in text
    assert 'Ожидают одобрения</span>\n    <span class="dash-tile-value">0</span>' in text
    assert 'Отклонено</span>\n    <span class="dash-tile-value">1</span>' in text
    assert 'href="/rejected"' in text
    assert "Одобренные" not in text
    assert "Отклонённые" not in text
    assert "Открыть список" not in text


def test_approved_list_can_revoke(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:approved",
                feed_name="demo",
                title="Тихий дон",
                link="https://example.com/quiet",
                published=None,
                torrent_url="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                rating=8,
                quality="1080p",
                genre="драма",
            ),
            status="approved",
        )
        store.set_vk_url(row_id, "https://vk.com/video1")
    home = client.get("/")
    assert 'href="/approved"' in home.data.decode()
    page = client.get("/approved")
    text = page.data.decode()
    assert page.status_code == 200
    assert "Тихий дон" in text
    assert "Отменить одобрение" in text
    assert "Уже во ВК" in text
    revoked = client.post(
        f"/approved/{row_id}/revoke",
        data={"csrf": token},
        follow_redirects=True,
    )
    body = revoked.data.decode()
    assert "Одобрение снято" in body
    assert "Ролик во ВК при этом не удаляется." in body
    assert "Тихий дон" not in body
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
        assert item is not None
        assert item.status == "pending"
    waiting = client.get("/pending")
    assert "Тихий дон" in waiting.data.decode()


def test_revoke_removes_the_torrent_from_qbittorrent(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path, streaming=True)
    client = app.test_client()
    token = _login(client)
    config = load_config(tmp_path / "config.yaml")
    digest = "a" * 40
    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:hash",
                feed_name="demo",
                title="Тихий дон",
                link="https://example.com/quiet",
                published=None,
                torrent_url=f"magnet:?xt=urn:btih:{digest}",
                rating=8,
                quality="1080p",
                genre="драма",
            ),
            status="approved",
        )
    removed: list[str] = []

    class _Qbit:
        def list_torrents(self) -> list[dict[str, str]]:
            return [{"hash": digest, "name": "другое имя"}]

        def remove(self, infohash: str) -> None:
            removed.append(infohash)

    monkeypatch.setattr("potok.web._stream_client", lambda _config: _Qbit())
    revoked = client.post(f"/approved/{row_id}/revoke", data={"csrf": token}, follow_redirects=True)
    assert "Раздача убрана из qBittorrent" in revoked.data.decode()
    assert removed == [digest]


def test_rejected_list_can_restore(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:rejected",
                feed_name="demo",
                title="Тихий дон",
                link="https://example.com/quiet",
                published=None,
                torrent_url="https://example.com/quiet.torrent",
                rating=8,
                quality="1080p",
                genre="драма",
            ),
            status="rejected",
        )
    page = client.get("/rejected")
    text = page.data.decode()
    assert page.status_code == 200
    assert "Тихий дон" in text
    assert "Отменить отклонение" in text
    restored = client.post(
        f"/rejected/{row_id}/restore",
        data={"csrf": token},
        follow_redirects=True,
    )
    body = restored.data.decode()
    assert "Отклонение снято" in body
    assert "Тихий дон" not in body
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
        assert item is not None
        assert item.status == "pending"
    waiting = client.get("/pending")
    assert "Тихий дон" in waiting.data.decode()


def test_approve_is_visible_to_the_bot_queue(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:1",
                feed_name="demo",
                title="Фильм <b>",
                link="https://example.com/1",
                published=None,
                torrent_url="https://example.com/a.torrent",
                rating=8,
                quality="1080p",
                genre="драма",
            )
        )
    page = client.get("/pending")
    assert "Фильм &lt;b&gt;" in page.data.decode()
    decided = client.post(
        f"/pending/{row_id}/approve",
        data={"csrf": token},
        follow_redirects=True,
    )
    text = decided.data.decode()
    assert "Одобрено. Загрузка в qBittorrent начнётся сразу, без ожидания VK." in text
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
    assert item is not None
    assert item.status == "approved"
    jobs = drain_jobs(jobs_path(config.database_path.parent))
    assert jobs == [{"row_id": row_id, "status": "approved"}]


def test_pending_page_shows_year_and_filter(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        store.add(
            FeedItem(
                guid="demo:year",
                feed_name="demo",
                title="Матрица (1999)",
                link="https://example.com/matrix",
                published=None,
                torrent_url="https://example.com/matrix.torrent",
                rating=8.7,
                quality="1080p",
                genre="фантастика, боевик",
                seeders=40,
                size_bytes=2 * 1024**3,
                runtime_minutes=136,
                poster_url="https://image.tmdb.org/t/p/w500/matrix.jpg",
                movie_title="Матрица",
            )
        )
    text = client.get("/pending").data.decode()
    assert '<h2 class="h5"><span>Матрица</span></h2>' in text
    assert "📅 1999" in text
    assert "⏱️ 2 ч 16 мин" in text
    assert 'id="flt-runtime-min"' in text
    assert 'id="flt-runtime-max"' in text
    assert 'data-runtime="136"' in text
    assert 'id="flt-toggle"' in text
    assert 'id="flt-panel"' in text
    assert 'id="flt-sort"' in text
    assert "Фантастика" in text
    assert "Боевик" in text
    assert "1080p" in text
    assert 'data-year="1999"' in text
    assert 'data-seeds="40"' in text


def test_poster_is_loaded_by_the_site(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:poster",
                feed_name="demo",
                title="Фильм",
                link="magnet:?xt=urn:btih:abc",
                published=None,
                torrent_url=None,
                rating=7.1,
                quality="1080p",
                genre="драма",
                poster_url="https://image.tmdb.org/t/p/w500/abc.jpg",
            )
        )
    monkeypatch.setattr("potok.web._read_poster", lambda _url: (b"\xff\xd8\xff", "image/jpeg"))

    page = client.get("/pending")
    assert f"/poster/{row_id}" in page.data.decode()
    image = client.get(f"/poster/{row_id}")
    assert image.status_code == 200
    assert image.mimetype == "image/jpeg"
    assert image.data == b"\xff\xd8\xff"
    assert client.get("/poster/999").status_code == 404


def test_settings_keep_secrets_encrypted(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    saved = client.post(
        "/settings",
        data={
            "csrf": token,
            "settings_form": "1",
            "poll_interval_minutes": "15",
            "min_rating": "8",
            "genres": "драма, комедия",
            "feed_name": "demo",
            "feed_url": "https://example.com/feed.xml",
            "telegram_token": _TOKEN,
            "wg_conf": f"[Interface]\nPrivateKey = {_PRIVATE}\n",
        },
        follow_redirects=True,
    )
    assert "Настройки сохранены" in saved.data.decode()
    raw = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert _TOKEN not in raw
    assert _PRIVATE not in raw
    encrypted = (tmp_path / "secrets.enc").read_bytes()
    assert _TOKEN.encode() not in encrypted
    assert _PRIVATE.encode() not in encrypted
    assert SecretStore(tmp_path).get("POTOK_TELEGRAM_TOKEN") == _TOKEN
    config = load_config(tmp_path / "config.yaml")
    assert config.rss.poll_interval_minutes == 15
    assert config.filters.min_rating == 8
    assert config.filters.genres == ("драма", "комедия")
    assert config.rss.feeds[0].url == "https://example.com/feed.xml"
    assert _PRIVATE in (tmp_path / "wg0.conf").read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw)
    assert "telegram_token" not in json.dumps(loaded)


def test_rutracker_torrent_name_is_saved(tmp_path: Path) -> None:
    name = (
        "7 пьес для черно-белого кино (Отар Иоселиани) "
        "[1982, Франция, арт-хаус, короткий метр, DVDRip] [rutracker-5550768].torrent"
    )
    assert torrent_basename("D:\\down\\YA\\" + name) == name
    picked = "D:/down/YA/" + name

    class FakeStream:
        def __init__(self) -> None:
            self.files: list[str] = []

        def list_torrents(self) -> list[dict]:
            return []

        def add_torrent_file(self, filename: str, content: bytes, *, title: str = "") -> dict:
            self.files.append(filename)
            return {"hash": "ab" * 20, "title": title}

    fake = FakeStream()
    app = _app(tmp_path, streaming=True, client=fake)
    client = app.test_client()
    token = _login(client)
    uploaded = client.post(
        "/downloads",
        data={
            "csrf": token,
            "title": "",
            "torrent": (io.BytesIO(b"d8:announce0:"), picked),
        },
        content_type="multipart/form-data",
    )
    text = uploaded.data.decode()
    assert "добавлен в загрузки" in text
    assert fake.files == [name]
    assert (tmp_path / "metadata" / name).is_file()

    rejected = client.post(
        "/downloads",
        data={
            "csrf": token,
            "torrent": (io.BytesIO(b"d8:announce0:"), "notes.txt"),
        },
        content_type="multipart/form-data",
    )
    assert "Нужен файл .torrent" in rejected.data.decode()


def test_download_and_vk_queue_can_be_reordered(tmp_path: Path) -> None:
    first = "ab" * 20
    second = "cd" * 20

    class FakeStream:
        def __init__(self) -> None:
            self.rows = [
                {
                    "hash": first,
                    "name": "Первый",
                    "state": "downloading",
                    "priority": 0,
                    "progress": 0.2,
                    "dlspeed": 1,
                    "eta": 10,
                },
                {
                    "hash": second,
                    "name": "Второй",
                    "state": "stoppedUP",
                    "priority": -1,
                    "progress": 1,
                    "dlspeed": 0,
                    "eta": 0,
                },
                {
                    "hash": "ef" * 20,
                    "name": "Третий",
                    "state": "stoppedUP",
                    "priority": -1,
                    "progress": 1,
                    "dlspeed": 0,
                    "eta": 0,
                },
                {
                    "hash": "12" * 20,
                    "name": "Четвёртый",
                    "state": "downloading",
                    "priority": 1,
                    "progress": 0.1,
                    "dlspeed": 1,
                    "eta": 20,
                },
            ]
            self.moved: list[tuple[str, str]] = []

        def list_torrents(self) -> list[dict]:
            return self.rows

        def move_in_queue(self, infohash: str, direction: str) -> bool:
            self.moved.append((infohash, direction))
            return False

    fake = FakeStream()
    app = _app(tmp_path, streaming=True, client=fake)
    client = app.test_client()
    token = _login(client)
    page = client.get("/downloads").data.decode()
    assert page.index("Первый") < page.index("Второй")
    assert "Выше" in page
    assert "Ниже" in page
    moved = client.post(f"/downloads/{first}/down", data={"csrf": token}, follow_redirects=True)
    assert "Опустил в очереди загрузки." in moved.data.decode()
    assert fake.moved == [(first, "down")]

    queue = client.get("/vk/queue").data.decode()
    assert queue.index("Второй") < queue.index("Третий")
    assert "Первый" not in queue
    assert "Удалить" in queue
    client.post(f"/vk/queue/{second}/down", data={"csrf": token}, follow_redirects=True)
    shifted = client.get("/vk/queue").data.decode()
    assert shifted.index("Третий") < shifted.index("Второй")
    again = client.get("/").data.decode()
    assert ">Выгружаются в ВК</span>" in again
    assert '<span class="dash-tile-value">2</span>' in again


def test_vk_queue_delete_removes_video_and_torrent(tmp_path: Path) -> None:
    digest = "ab" * 20
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    video = downloads / "film.mkv"
    video.write_bytes(b"video")
    folder = downloads / "pack"
    folder.mkdir()
    extra = folder / "extra.mkv"
    extra.write_bytes(b"extra")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    url = f"https://example.com/{digest}.torrent"
    torrent = torrent_path(metadata, url)
    torrent.write_bytes(b"torrent-bytes")

    class FakeStream:
        def __init__(self) -> None:
            self.rows = [
                {
                    "hash": digest,
                    "name": "Фильм",
                    "state": "stoppedUP",
                    "progress": 1,
                    "content_path": str(video),
                }
            ]
            self.removed: list[tuple[str, bool]] = []

        def list_torrents(self) -> list[dict]:
            return self.rows

        def torrent_files(self, infohash: str) -> list[dict]:
            assert infohash == digest
            return [
                {"name": "film.mkv", "progress": 1},
                {"name": "pack/extra.mkv", "progress": 1},
            ]

        def remove(self, infohash: str, *, delete_files: bool = False) -> None:
            self.removed.append((infohash, delete_files))
            self.rows = []

    fake = FakeStream()
    app = _app(tmp_path, streaming=True, client=fake)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        store.add(
            FeedItem(
                guid="demo:film",
                feed_name="demo",
                title="Фильм",
                link="https://example.com/film",
                published=None,
                torrent_url=url,
                rating=8,
                quality="1080p",
                genre="драма",
            ),
            status="approved",
        )
    client = app.test_client()
    token = _login(client)
    deleted = client.post(
        f"/vk/queue/{digest}/delete",
        data={"csrf": token},
        follow_redirects=True,
    )
    text = deleted.data.decode()
    assert "Убрал из очереди и из qBittorrent." in text
    assert "Удалил видео: 2." in text
    assert "Файлов .torrent удалено: 1." in text
    assert fake.removed == [(digest, True)]
    assert not video.exists()
    assert not extra.exists()
    assert not folder.exists()
    assert not torrent.exists()
    assert "Фильм" not in client.get("/vk/queue").data.decode()


def test_vk_queue_delete_keeps_a_file_that_is_uploading(tmp_path: Path) -> None:
    digest = "cd" * 20
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    video = downloads / "film.mkv"
    video.write_bytes(b"video")

    class FakeStream:
        def list_torrents(self) -> list[dict]:
            return [
                {
                    "hash": digest,
                    "name": "Фильм",
                    "state": "stoppedUP",
                    "progress": 1,
                }
            ]

        def torrent_files(self, infohash: str) -> list[dict]:
            return [{"name": "film.mkv", "progress": 1}]

        def remove(self, infohash: str, *, delete_files: bool = False) -> None:
            raise AssertionError("нельзя удалять во время отправки")

    app = _app(tmp_path, streaming=True, client=FakeStream())
    board = UploadBoard(tmp_path / "vk_uploads.json")
    board.begin(name="film.mkv", title="Фильм")
    client = app.test_client()
    token = _login(client)
    deleted = client.post(
        f"/vk/queue/{digest}/delete",
        data={"csrf": token},
        follow_redirects=True,
    )
    assert "сейчас отправляется" in deleted.data.decode()
    assert video.is_file()
    uploads = client.get("/api/vk/uploads").get_json()
    assert uploads["uploads"][0]["name"] == "film.mkv"


def test_upload_torrent_and_live_log_snapshot(tmp_path: Path) -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.files: list[str] = []
            self.sources = [
                {
                    "hash": "ab" * 20,
                    "name": "Ролик",
                    "state": "downloading",
                    "progress": 0.4,
                    "dlspeed": 2048,
                    "eta": 90,
                }
            ]

        def list_torrents(self) -> list[dict]:
            return self.sources

        def add_torrent_file(self, filename: str, content: bytes, *, title: str = "") -> dict:
            self.files.append(filename)
            return {"hash": "ab" * 20, "title": title}

    fake = FakeStream()
    app = _app(tmp_path, streaming=True, client=fake)
    client = app.test_client()
    token = _login(client)
    page = client.get("/downloads")
    assert "Ролик" in page.data.decode()
    uploaded = client.post(
        "/downloads",
        data={
            "csrf": token,
            "title": "Новый",
            "torrent": (io.BytesIO(b"d8:announce0:"), "film.torrent"),
        },
        content_type="multipart/form-data",
    )
    assert "film.torrent" in uploaded.data.decode()
    assert fake.files == ["film.torrent"]
    assert (tmp_path / "metadata" / "film.torrent").is_file()

    log = tmp_path / "worker.log"
    log.write_text("строка журнала\n", encoding="utf-8")
    stream = client.get("/logs/stream?once=1")
    assert stream.mimetype == "text/event-stream"
    assert "строка журнала" in stream.data.decode()
    assert "строка журнала" in "".join(iter_events(log, follow=False))


_FILE_SECRETS = {
    "JACKETT_API_KEY": "key-from-file-jackett",
    "POTOK_FEED_USER": "user-from-file",
    "POTOK_FEED_PASSWORD": "pass-from-file",
    "POTOK_TELEGRAM_TOKEN": "token-from-file",
    "POTOK_TELEGRAM_CHAT_ID": "424242",
    "POTOK_VK_TOKEN": "vk-from-file",
    "POTOK_STREAM_USER": "stream-user-file",
    "POTOK_STREAM_PASSWORD": "stream-pass-file",
    "TMDB_API_KEY": "tmdb-from-file",
}
_SECRET_FIELDS = {
    "JACKETT_API_KEY": "jackett_api_key",
    "POTOK_FEED_USER": "feed_username",
    "POTOK_FEED_PASSWORD": "feed_password",
    "POTOK_TELEGRAM_TOKEN": "telegram_token",
    "POTOK_TELEGRAM_CHAT_ID": "telegram_chat_id",
    "POTOK_VK_TOKEN": "vk_token",
    "POTOK_STREAM_USER": "stream_username",
    "POTOK_STREAM_PASSWORD": "stream_password",
    "TMDB_API_KEY": "tmdb_api_key",
}


def _full_settings_form(token: str) -> dict[str, object]:
    """Все поля формы. Пути остаются как в тестовом yaml: их в этот словарь не кладём."""
    return {
        "csrf": token,
        "settings_form": "1",
        "poll_interval_minutes": "15",
        "rss_timeout": "25",
        "user_agent": "Potok/test",
        "feed_name": ["demo", ""],
        "feed_url": ["https://example.com/feed.xml", ""],
        "min_rating": "8",
        "max_age_days": "10",
        "genres": "драма, комедия",
        "pattern_rating": "rating[:\\s]*(\\d+)",
        "pattern_quality": "1080p",
        "quality_keep": "average",
        "pattern_genre": "genre[:\\s]+(\\w+)",
        "jackett_base_url": "http://jackett:9117",
        "jackett_rss_url": "https://example.com/torznab",
        "jackett_min_seeders": "2",
        "jackett_min_size_bytes": "1000",
        "jackett_page_count": "4",
        "telegram_timeout": "20",
        "api_version": "5.131",
        "group_id": "77",
        "privacy_view": "by_link",
        "wallpost": "1",
        "chunk_bytes": "1048576",
        "max_bytes": "1073741824",
        "upload_attempts": "2",
        "upload_idle_timeout_seconds": "120",
        "poll_interval_seconds": "20",
        "poll_timeout_seconds": "600",
        "api_timeout_seconds": "15",
        "api_retries": "3",
        "aria2_bin": "aria2c",
        "aria2_timeout": "0",
        "streaming_enabled": "1",
        "auto_add": "1",
        "base_url": "http://qbittorrent:8080",
        "public_url": "http://127.0.0.1:8090",
        "stream_timeout": "15",
        "server_torrent_dir": "/opt/ts/inbox",
        "portal_host": "127.0.0.1",
        "portal_port": "8080",
        "log_level": "WARNING",
        "via_vpn": "rss, metadata",
        "via_direct": "downloads",
        "vpn_domains": "kinozal.tv, api.telegram.org",
        "interface": "wg0",
        "vpn_timeout": "40",
        "wg_conf": "[Interface]\nPrivateKey = fileprivatekeyvalue\n",
        **_FILE_SECRETS_AS_FORM,
    }


_FILE_SECRETS_AS_FORM = {
    field: _FILE_SECRETS[env_name] for env_name, field in _SECRET_FIELDS.items()
}


def test_settings_form_fills_secrets_from_file(tmp_path: Path) -> None:
    app = _app(tmp_path)
    SecretStore(tmp_path).set_many(
        {**_FILE_SECRETS, "wireguard.conf": "[Interface]\nPrivateKey = fileprivatekeyvalue\n"}
    )
    client = app.test_client()
    _login(client)
    page = client.get("/settings")
    text = page.data.decode()
    assert page.status_code == 200
    assert "Сколько страниц" in text
    for env_name, field in _SECRET_FIELDS.items():
        assert f'name="{field}"' in text
        assert f'value="{_FILE_SECRETS[env_name]}"' in text
    assert "fileprivatekeyvalue" in text
    raw = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    for value in _FILE_SECRETS.values():
        assert value not in raw


def test_portal_pages_and_full_settings_save(tmp_path: Path) -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.sources = [
                {
                    "hash": "ab" * 20,
                    "name": "Ролик",
                    "state": "downloading",
                    "progress": 0.4,
                    "dlspeed": 2048,
                    "eta": 90,
                }
            ]

        def list_torrents(self) -> list[dict]:
            return self.sources

        def add_torrent_file(self, filename: str, content: bytes, *, title: str = "") -> dict:
            return {"hash": "ab" * 20, "title": title}

    app = _app(tmp_path, streaming=True, client=FakeStream())
    client = app.test_client()
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["ok"] is True

    login_page = client.get("/login").data.decode()
    bad = client.post(
        "/login",
        data={"csrf": _csrf(login_page), "username": "potok", "password": "нет"},
    )
    assert bad.status_code == 401

    token = _login(client)
    saved = client.post("/settings", data=_full_settings_form(token), follow_redirects=True)
    body = saved.data.decode()
    assert "Настройки сохранены" in body
    assert 'value="average" selected' in body
    for value in _FILE_SECRETS.values():
        assert f'value="{value}"' in body
    raw = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    for value in _FILE_SECRETS.values():
        assert value not in raw
    store = SecretStore(tmp_path)
    for env_name, value in _FILE_SECRETS.items():
        assert store.get(env_name) == value

    kept = dict(_full_settings_form(token))
    kept["telegram_token"] = ""
    again = client.post("/settings", data=kept, follow_redirects=True)
    assert "Настройки сохранены" in again.data.decode()
    assert store.get("POTOK_TELEGRAM_TOKEN") == _FILE_SECRETS["POTOK_TELEGRAM_TOKEN"]

    config = load_config(tmp_path / "config.yaml")
    assert config.rss.poll_interval_minutes == 15
    assert config.filters.genres == ("драма", "комедия")
    assert config.filters.quality_keep == "average"
    assert config.vk.group_id == 77
    assert config.vk.wallpost is True
    assert config.streaming.enabled is True
    assert config.log_level == "WARNING"
    assert config.jackett.min_seeders == 2
    assert config.jackett.page_count == 4
    assert config.routing.vpn_domains == ("kinozal.tv",)

    with SeenStore(config.database_path) as seen:
        approved_id = seen.add(
            FeedItem(
                guid="demo:ok",
                feed_name="demo",
                title="К одобрению",
                link="https://example.com/ok",
                published=None,
                torrent_url="https://example.com/ok.torrent",
                rating=8,
                quality="1080p",
                genre="драма",
            )
        )
        rejected_id = seen.add(
            FeedItem(
                guid="demo:no",
                feed_name="demo",
                title="К отклонению",
                link="https://example.com/no",
                published=None,
                torrent_url="https://example.com/no.torrent",
                rating=6,
                quality="720p",
                genre="комедия",
            )
        )
    home = client.get("/")
    assert home.status_code == 200
    assert "Ожидают одобрения" in home.data.decode()
    pending = client.get("/pending")
    assert "К одобрению" in pending.data.decode()
    approved = client.post(
        f"/pending/{approved_id}/approve", data={"csrf": token}, follow_redirects=True
    )
    assert "Одобрено" in approved.data.decode()
    rejected = client.post(
        f"/pending/{rejected_id}/reject", data={"csrf": token}, follow_redirects=True
    )
    assert "Отклонено" in rejected.data.decode()

    downloads = client.get("/downloads")
    assert "Ролик" in downloads.data.decode()
    uploaded = client.post(
        "/downloads",
        data={
            "csrf": token,
            "title": "Новый",
            "torrent": (io.BytesIO(b"d8:announce0:"), "film.torrent"),
        },
        content_type="multipart/form-data",
    )
    assert "film.torrent" in uploaded.data.decode()

    log = tmp_path / "worker.log"
    log.write_text("строка полного прогона\n", encoding="utf-8")
    logs = client.get("/logs")
    assert logs.status_code == 200
    stream = client.get("/logs/stream?once=1")
    assert "строка полного прогона" in stream.data.decode()

    users = client.post(
        "/users",
        data={"csrf": token, "action": "add", "name": "anna", "password": "secret-pass"},
    )
    assert "Пользователь добавлен." in users.data.decode()
    changed = client.post(
        "/users",
        data={"csrf": token, "action": "password", "name": "anna", "password": "new-secret-pass"},
    )
    assert "Пароль сохранён." in changed.data.decode()
    removed = client.post(
        "/users",
        data={"csrf": token, "action": "delete", "name": "anna"},
    )
    assert "Пользователь удалён." in removed.data.decode()
    last = client.post(
        "/users",
        data={"csrf": token, "action": "delete", "name": "potok"},
    )
    assert "последнего" in last.data.decode()

    left = client.post("/logout", data={"csrf": token})
    assert left.status_code == 302
    assert left.headers["Location"].endswith("/login")
    locked = client.get("/pending")
    assert locked.status_code == 302


def test_selected_videos_are_sent_to_vk(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "downloads"
    folder.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (folder / "film.mkv").write_bytes(b"video")
    (folder / "notes.txt").write_bytes(b"text")
    (folder / "done.mkv").write_bytes(b"old")
    (folder / "auto.mkv").write_bytes(b"rss")
    (metadata / "Фильм [2020, драма, WEB-DL 1080p].torrent").write_bytes(b"film.mkv")
    (metadata / "aabbccddeeff0011.torrent").write_bytes(b"auto.mkv")
    (tmp_path / "vk_sent.json").write_text(
        json.dumps({"files": ["done.mkv", "name:done.mkv"]}),
        encoding="utf-8",
    )
    sent: list[str] = []

    def fake_publish(_config, path: Path, **_kwargs) -> PublishResult:
        sent.append(path.name)
        return PublishResult(player_urls=("https://vk.com/video1",))

    class _Now:
        def __init__(self, target, args=(), kwargs=None, daemon=None) -> None:
            self._target = target
            self._args = args

        def start(self) -> None:
            self._target(*self._args)

    monkeypatch.setattr("potok.web.publish_file", fake_publish)
    monkeypatch.setattr("potok.web.threading.Thread", _Now)
    monkeypatch.setenv("POTOK_VK_TOKEN", "token")
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    page = client.get("/downloads")
    text = page.data.decode()
    assert "Файлы .torrent" not in text
    assert "Отправить выбранные в VK" not in text
    assert "Отправить в VK" in text
    assert 'name="video" value="film.mkv"' in text
    assert "Фильм (2020)" in text
    assert "1080p" in text
    assert "notes.txt" not in text
    assert "done.mkv" not in text
    assert "auto.mkv" not in text

    empty = client.post("/downloads", data={"csrf": token, "action": "vk"})
    assert "Не выбран файл" in empty.data.decode()

    sent_page = client.post(
        "/downloads",
        data={"csrf": token, "action": "vk", "video": "film.mkv"},
    )
    assert "Отправляю в VK: film.mkv" in sent_page.data.decode()
    assert sent == ["film.mkv"]

    blocked = client.post(
        "/downloads",
        data={"csrf": token, "action": "vk", "video": "../film.mkv"},
    )
    assert "только видео из папки" in blocked.data.decode()
    assert sent == ["film.mkv"]

    skipped = client.post(
        "/downloads",
        data={"csrf": token, "action": "vk", "video": "auto.mkv"},
    )
    assert "вручную скачанный" in skipped.data.decode()
    assert sent == ["film.mkv"]


def test_tmdb_settings_check_saves_key_outside_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "")
    from potok.tmdb import MovieInfo, TmdbError

    def search(api_key: str, title: str, *, timeout: float, get=None) -> MovieInfo:
        del timeout, get
        assert title == "Матрица"
        if api_key == "bad-key":
            raise TmdbError("Ключ TMDB не принят. Нужен API Key (v3 auth) с сайта themoviedb.org.")
        assert api_key == "new-tmdb-key"
        return MovieInfo(
            title="Матрица",
            year="1999",
            rating=8.2,
            overview="Хакер.",
            poster_url="https://image.tmdb.org/t/p/w500/x.jpg",
        )

    monkeypatch.setattr("potok.web.search_movie", search)
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    page = client.get("/settings").data.decode()
    assert "themoviedb.org" in page
    assert 'name="tmdb_api_key"' in page
    assert "Проверить ключ" in page

    denied = client.post(
        "/settings/tmdb-check",
        data={"csrf": token, "tmdb_api_key": "bad-key"},
        follow_redirects=True,
    )
    assert "не принят" in denied.data.decode()
    assert SecretStore(tmp_path).get("TMDB_API_KEY") is None

    checked = client.post(
        "/settings/tmdb-check",
        data={"csrf": token, "tmdb_api_key": "new-tmdb-key"},
        follow_redirects=True,
    )
    assert "Ключ TMDB подходит" in checked.data.decode()
    assert "Матрица" in checked.data.decode()
    assert SecretStore(tmp_path).get("TMDB_API_KEY") == "new-tmdb-key"
    raw = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "new-tmdb-key" not in raw


def test_wireguard_check_reports_status_and_saves_file(tmp_path: Path, monkeypatch) -> None:
    calls: list[object] = []

    def probe(config: object) -> str:
        calls.append(config)
        return "Туннель wg0 поднялся и опустился. VPN отвечает."

    monkeypatch.setattr("potok.web.probe_wireguard", probe)
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    page = client.get("/settings").data.decode()
    assert 'formaction="/settings/wireguard-check"' in page

    broken = client.post(
        "/settings/wireguard-check",
        data={"csrf": token, "wg_conf": "пусто"},
        follow_redirects=True,
    )
    assert "PrivateKey" in broken.data.decode()
    assert calls == []

    conf = "[Interface]\nPrivateKey = not-a-real-key\n"
    checked = client.post(
        "/settings/wireguard-check",
        data={"csrf": token, "wg_conf": conf},
        follow_redirects=True,
    )
    text = checked.data.decode()
    assert "поднялся и опустился" in text
    assert calls
    stored = (tmp_path / "wg0.conf").read_text(encoding="utf-8")
    assert "PrivateKey = not-a-real-key" in stored


def test_telegram_check_saves_token_outside_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "")

    class Response:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def read(self, _limit: int) -> bytes:
            return self.raw

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    def fake_open(request, timeout):
        del timeout
        address = request.full_url
        if "/botbad-token/" in address:
            raise HTTPError(address, 401, "Unauthorized", hdrs=None, fp=None)
        assert "/bot123456:ABC_def-token/" in address
        body = b'{"ok": true, "result": {"username": "potok_bot"}}'
        return Response(body)

    monkeypatch.setattr("potok.web.urlopen", fake_open)
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    page = client.get("/settings").data.decode()
    assert "@userinfobot" in page
    assert "@getmyid_bot" in page
    assert 'formaction="/settings/telegram-check"' in page

    denied = client.post(
        "/settings/telegram-check",
        data={"csrf": token, "telegram_token": "bad-token"},
        follow_redirects=True,
    )
    assert "не принят" in denied.data.decode()
    assert SecretStore(tmp_path).get("POTOK_TELEGRAM_TOKEN") is None

    checked = client.post(
        "/settings/telegram-check",
        data={"csrf": token, "telegram_token": "123456:ABC_def-token"},
        follow_redirects=True,
    )
    text = checked.data.decode()
    assert "Токен подходит" in text
    assert "@potok_bot" in text
    assert SecretStore(tmp_path).get("POTOK_TELEGRAM_TOKEN") == "123456:ABC_def-token"
    raw = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "ABC_def-token" not in raw


def test_retry_removes_the_stopped_row(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "film.mkv").write_bytes(b"video")
    sent: list[str] = []

    def fake_publish(_config: object, path: Path, **_kwargs: object) -> PublishResult:
        sent.append(path.name)
        return PublishResult(player_urls=("https://vk.com/video1",))

    class _Now:
        def __init__(self, target, args=(), kwargs=None, daemon=None) -> None:
            del kwargs, daemon
            self._target = target
            self._args = args

        def start(self) -> None:
            self._target(*self._args)

    monkeypatch.setattr("potok.web.publish_file", fake_publish)
    monkeypatch.setattr("potok.web.threading.Thread", _Now)
    monkeypatch.setenv("POTOK_VK_TOKEN", "token")
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    board = UploadBoard(tmp_path / "vk_uploads.json")
    job_id = board.begin(name="film.mkv", title="Фильм")
    board.set_status(job_id, "stopped", message="Остановлено")

    response = client.post(
        "/api/vk/retry",
        json={"id": job_id},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert sent == ["film.mkv"]
    assert board.jobs() == []


def test_vk_page_lists_edits_deletes_and_stops(tmp_path: Path, monkeypatch) -> None:
    stored: dict[str, object] = {"title": "Фильм", "description": "про фильм"}

    class Fake:
        def list_videos(self, *, limit: int = 200) -> tuple[VkCatalogItem, ...]:
            del limit
            return (
                VkCatalogItem(
                    owner_id=5,
                    video_id=9,
                    title=str(stored["title"]),
                    description=str(stored["description"]),
                    page_url="https://vk.com/video5_9",
                    player_url="",
                    duration_seconds=65,
                    processing=False,
                ),
            )

        def edit_video(self, owner_id: int, video_id: int, *, title: str, description: str) -> None:
            assert (owner_id, video_id) == (5, 9)
            stored["title"] = title
            stored["description"] = description

        def delete_video(self, owner_id: int, video_id: int) -> None:
            stored["deleted"] = (owner_id, video_id)

    monkeypatch.setenv("POTOK_VK_TOKEN", "token")
    monkeypatch.setattr("potok.web._vk_client", lambda _config: Fake())
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    page = client.get("/vk")
    text = page.data.decode()
    assert "Видео в ВК" in text
    assert 'id="pending-rail"' in text
    assert 'id="flt-panel"' in text
    assert "pending-card:hover" in text
    assert "POST /api/vk/edit" in text
    assert "Сейчас отправляется" not in text
    queue = client.get("/vk/queue").data.decode()
    assert "Сейчас отправляется" in queue
    assert "МБ/с" in queue
    assert "Повторить" in queue
    assert "Убрать" in queue

    listing = client.get("/api/vk")
    payload = listing.get_json()
    assert payload["videos"][0]["title"] == "Фильм"
    assert payload["videos"][0]["description"] == "про фильм"
    assert payload["videos"][0]["page_url"] == "https://vk.com/video5_9"
    assert payload["videos"][0]["duration"] == "1:05"
    assert payload["videos"][0]["vk_title"] == "Фильм"
    assert payload["error"] == ""

    edited = client.post(
        "/api/vk/edit",
        json={"owner_id": 5, "video_id": 9, "title": "Новое", "description": "текст"},
        headers={"X-CSRF-Token": token},
    )
    assert edited.status_code == 200
    assert stored["title"] == "Новое"

    removed = client.post(
        "/api/vk/delete",
        json={"owner_id": 5, "video_id": 9},
        headers={"X-CSRF-Token": token},
    )
    assert removed.status_code == 200
    assert stored["deleted"] == (5, 9)

    board = UploadBoard(tmp_path / "vk_uploads.json")
    job_id = board.begin(name="film.mkv", title="Фильм")
    board.tick(job_id, 40, 100)
    stopped = client.post(
        "/api/vk/stop",
        json={"id": job_id},
        headers={"X-CSRF-Token": token},
    )
    assert stopped.status_code == 200
    assert board.is_stop_requested(job_id)
    again = client.get("/api/vk").get_json()
    assert again["uploads"][0]["percent"] == 40
    assert again["uploads"][0]["status"] == "stopping"

    cleared = client.post(
        "/api/vk/dismiss",
        json={"id": job_id},
        headers={"X-CSRF-Token": token},
    )
    assert cleared.status_code == 200
    assert client.get("/api/vk").get_json()["uploads"] == []

    locked = client.post("/api/vk/delete", json={"owner_id": 5, "video_id": 9})
    assert locked.status_code == 400


def test_vk_list_is_not_requested_on_every_refresh(tmp_path: Path, monkeypatch) -> None:
    calls = {"count": 0}

    class Fake:
        def list_videos(self, *, limit: int = 200) -> tuple[VkCatalogItem, ...]:
            del limit
            calls["count"] += 1
            return (
                VkCatalogItem(
                    owner_id=5,
                    video_id=9,
                    title="Фильм",
                    description="",
                    page_url="https://vk.com/video5_9",
                    player_url="",
                    duration_seconds=10,
                    processing=False,
                ),
            )

    monkeypatch.setenv("POTOK_VK_TOKEN", "token")
    monkeypatch.setattr("potok.web._vk_client", lambda _config: Fake())
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    assert client.get("/api/vk").get_json()["videos"][0]["title"] == "Фильм"
    assert client.get("/api/vk").get_json()["videos"][0]["title"] == "Фильм"
    assert calls["count"] == 1


def test_vk_page_opens_saved_list_without_calling_vk(tmp_path: Path, monkeypatch) -> None:
    class Fake:
        def list_videos(self, *, limit: int = 200) -> tuple[VkCatalogItem, ...]:
            del limit
            raise AssertionError("сохранённый список не должен ждать ВК")

    monkeypatch.setenv("POTOK_VK_TOKEN", "token")
    monkeypatch.setattr("potok.web._vk_client", lambda _config: Fake())
    (tmp_path / "vk_catalog.json").write_text(
        json.dumps(
            {"videos": [{"title": "Уже было", "owner_id": 5, "video_id": 9}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    payload = client.get("/api/vk").get_json()
    assert payload["videos"][0]["title"] == "Уже было"
    assert payload["error"] == ""


def test_vk_card_uses_cover_title_and_pending_fields(tmp_path: Path, monkeypatch) -> None:
    class Fake:
        def list_videos(self, *, limit: int = 200) -> tuple[VkCatalogItem, ...]:
            del limit
            return (
                VkCatalogItem(
                    owner_id=5,
                    video_id=9,
                    title="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    description="имя из вк",
                    page_url="https://vk.com/video5_9",
                    player_url="",
                    duration_seconds=8700,
                    processing=False,
                ),
            )

    monkeypatch.setenv("POTOK_VK_TOKEN", "token")
    monkeypatch.setattr("potok.web._vk_client", lambda _config: Fake())
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:cover",
                feed_name="demo",
                title="Матрица (1999) 1080p WEB-DL",
                link="https://example.com/matrix",
                published=None,
                torrent_url=None,
                rating=8.7,
                quality="1080p",
                genre="фантастика",
                overview="Хакер узнаёт правду.",
                poster_url="https://image.tmdb.org/t/p/w500/matrix.jpg",
                seeders=12,
                size_bytes=2 * 1024**3,
                runtime_minutes=136,
                movie_title="Матрица",
            )
        )
        store.set_vk_uuid(row_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    video = client.get("/api/vk").get_json()["videos"][0]
    assert video["title"] == "Матрица"
    assert video["vk_title"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert video["rating"] == "8.7"
    assert video["quality"] == "1080p"
    assert video["genre"] == "Фантастика"
    assert video["year"] == "1999"
    assert video["duration"] == "2 ч 16 мин"
    assert video["seeds"] == "12"
    assert video["size"] == "2.00 ГБ"
    assert video["overview"] == "Хакер узнаёт правду."
    assert video["image_url"] == f"/poster/{row_id}"


def test_vk_card_survives_when_vk_forgets_the_code(tmp_path: Path, monkeypatch) -> None:
    renamed: list[str] = []

    class Fake:
        def list_videos(self, *, limit: int = 200) -> tuple[VkCatalogItem, ...]:
            del limit
            return (
                VkCatalogItem(
                    owner_id=-5,
                    video_id=20,
                    title="Видео от Поток",
                    description="старое",
                    page_url="https://vk.com/video-5_20",
                    player_url="",
                    duration_seconds=8700,
                    processing=False,
                ),
            )

        def edit_video(self, owner_id: int, video_id: int, *, title: str, description: str) -> None:
            del owner_id, video_id, description
            renamed.append(title)

    monkeypatch.setenv("POTOK_VK_TOKEN", "token")
    monkeypatch.setattr("potok.web._vk_client", lambda _config: Fake())
    monkeypatch.setattr("potok.web._UUID_KEPT", set())
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    config = load_config(tmp_path / "config.yaml")
    code = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with SeenStore(config.database_path) as store:
        plain_id = store.add(
            FeedItem(
                guid="demo:plain-stop",
                feed_name="demo",
                title="Матрица (1999) WEB-DL",
                link="https://example.com/matrix-plain",
                published=None,
                torrent_url=None,
                rating=None,
                quality=None,
                genre=None,
                overview=None,
                poster_url=None,
            )
        )
        rich_id = store.add(
            FeedItem(
                guid="demo:rich-stop",
                feed_name="demo",
                title="Матрица (1999) 1080p WEB-DL",
                link="https://example.com/matrix",
                published=None,
                torrent_url=None,
                rating=8.7,
                quality="1080p",
                genre="фантастика",
                overview="Хакер узнаёт правду.",
                poster_url="https://image.tmdb.org/t/p/w500/matrix.jpg",
                seeders=4,
                size_bytes=2 * 1024**3,
                runtime_minutes=136,
            )
        )
        store.set_vk_uuid(plain_id, f"{code}\nvideo-5_20 {code}")

    video = client.get("/api/vk").get_json()["videos"][0]
    assert "Матрица" in video["title"]
    assert video["vk_title"] == code
    assert video["overview"] == "Хакер узнаёт правду."
    assert video["genre"] == "Фантастика"
    assert video["image_url"] == f"/poster/{rich_id}"
    assert renamed == [code]


def test_vk_card_without_movie_title_uses_release_name(tmp_path: Path, monkeypatch) -> None:
    class Fake:
        def list_videos(self, *, limit: int = 200) -> tuple[VkCatalogItem, ...]:
            del limit
            return (
                VkCatalogItem(
                    owner_id=5,
                    video_id=9,
                    title="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    description="",
                    page_url="https://vk.com/video5_9",
                    player_url="",
                    duration_seconds=0,
                    processing=False,
                ),
            )

    monkeypatch.setenv("POTOK_VK_TOKEN", "token")
    monkeypatch.setattr("potok.web._vk_client", lambda _config: Fake())
    monkeypatch.setattr("potok.web._MATCH_AT", 0.0)
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        plain_id = store.add(
            FeedItem(
                guid="demo:plain",
                feed_name="demo",
                title="Матрица (1999) WEB-DL",
                link="https://example.com/matrix-plain",
                published=None,
                torrent_url=None,
                rating=None,
                quality=None,
                genre=None,
                overview=None,
                poster_url=None,
            )
        )
        rich_id = store.add(
            FeedItem(
                guid="demo:rich",
                feed_name="demo",
                title="Матрица (1999) 1080p WEB-DL",
                link="https://example.com/matrix",
                published=None,
                torrent_url=None,
                rating=8.7,
                quality="1080p",
                genre="фантастика",
                overview="Хакер узнаёт правду.",
                poster_url="https://image.tmdb.org/t/p/w500/matrix.jpg",
                seeders=12,
                size_bytes=2 * 1024**3,
                runtime_minutes=136,
            )
        )
        store.set_vk_uuid(plain_id, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    video = client.get("/api/vk").get_json()["videos"][0]
    assert "Матрица" in video["title"]
    assert video["title"] != "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert video["overview"] == "Хакер узнаёт правду."
    assert video["image_url"] == f"/poster/{rich_id}"
    assert video["genre"] == "Фантастика"
    assert video["vk_title"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_latin_vk_title_becomes_russian_and_bot_gets_the_card(tmp_path: Path, monkeypatch) -> None:
    from potok.web import _FIXED_TITLES

    _FIXED_TITLES.clear()
    folder = tmp_path / "metadata"
    folder.mkdir()
    torrent = (
        "7 пьес для черно-белого кино (Отар Иоселиани) "
        "[1982, Франция, DVDRip] [rutracker-1].torrent"
    )
    (folder / torrent).write_bytes(b"inside 7_pjes_dlya_kino.avi")
    renamed: dict[str, str] = {}
    sent: dict[str, str] = {}

    class Fake:
        def list_videos(self, *, limit: int = 200) -> tuple[VkCatalogItem, ...]:
            del limit
            return (
                VkCatalogItem(
                    owner_id=5,
                    video_id=9,
                    title=renamed.get("title", "7_pjes_dlya_kino"),
                    description="короткий метр",
                    page_url="https://vk.com/video5_9",
                    player_url="",
                    duration_seconds=1200,
                    processing=False,
                ),
            )

        def edit_video(self, owner_id: int, video_id: int, *, title: str, description: str) -> None:
            assert (owner_id, video_id) == (5, 9)
            assert description == "короткий метр"
            renamed["title"] = title

    def fake_card(_config, *, title: str, description: str, page_url: str) -> None:
        sent["title"] = title
        sent["description"] = description
        sent["page_url"] = page_url

    monkeypatch.setattr("potok.web._vk_client", lambda _config: Fake())
    monkeypatch.setattr("potok.web.send_vk_card", fake_card)
    app = _app(tmp_path)
    client = app.test_client()
    token = _login(client)
    assert "Отправить в бот" in client.get("/vk").data.decode()

    listing = client.get("/api/vk").get_json()
    assert listing["videos"][0]["title"] == "7 пьес для черно-белого кино (1982)"
    assert renamed["title"] == "7 пьес для черно-белого кино (1982)"

    sent_page = client.post(
        "/api/vk/bot",
        json={"owner_id": 5, "video_id": 9},
        headers={"X-CSRF-Token": token},
    )
    assert sent_page.status_code == 200
    assert sent == {
        "title": "7 пьес для черно-белого кино (1982)",
        "description": "короткий метр",
        "page_url": "https://vk.com/video5_9",
    }


_VK_TOKEN = "vk1.a." + ("Ab3_" * 12)


class _VkBody:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self, limit: int) -> bytes:
        return json.dumps(self.payload).encode()[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def test_settings_shows_jackett_category_ticks(tmp_path: Path, monkeypatch) -> None:
    from potok.jackett_cats import CategoryGroup, FeedCategory

    group = CategoryGroup(
        "Кинематограф",
        (FeedCategory("101730", "Зарубежные фильмы", True),),
    )
    monkeypatch.setattr(
        "potok.web.load_category_groups",
        lambda config, store: ((group,), ""),
    )
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    page = client.get("/settings").data.decode()
    assert "Зарубежные фильмы" in page
    assert 'name="jackett_cat" value="101730"' in page
    assert "jackett_cats_shown" in page


def test_vk_settings_button_saves_token(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    csrf = _login(client)
    page = client.get("/settings").data.decode()
    assert "Открыть страницу VK" in page
    assert "https://oauth.vk.ru/blank.html" in page
    assert 'formaction="/vk/token"' in page
    rss, rest = page.split('id="filters"', 1)
    secrets, after_secrets = rest.split('id="secrets"', 1)[1].split('id="tmdb"', 1)
    vk = after_secrets.split('id="vk"', 1)[1]
    assert "Лента с паролем" in rss
    assert "Лента с паролем" not in secrets
    assert 'name="vk_address"' in vk
    assert 'name="vk_token"' in vk
    assert 'value="by_link" selected' in vk
    assert "Только у кого есть ссылка" in vk
    assert ">by_link<" not in vk

    empty = client.post("/vk/token", data={"csrf": csrf, "vk_address": ""}, follow_redirects=True)
    assert "адрес целиком" in empty.data.decode()
    assert SecretStore(tmp_path).get("POTOK_VK_TOKEN") is None

    def vk_opener(request, timeout):
        del request, timeout
        return _VkBody({"response": {"count": 0, "items": []}})

    def fake_check(token, *, api_version, timeout, opener=None):
        del opener
        return check_video_token(token, api_version=api_version, timeout=timeout, opener=vk_opener)

    monkeypatch.setattr("potok.vk_oauth.check_video_token", fake_check)
    address = (
        "https://oauth.vk.ru/blank.html#access_token=" + _VK_TOKEN + "&expires_in=86400&user_id=1"
    )
    saved = client.post(
        "/vk/token",
        data={"csrf": csrf, "vk_address": address},
        follow_redirects=True,
    )
    text = saved.data.decode()
    assert "Токен VK сохранён" in text
    assert "24 ч" in text
    assert "действует ещё" in text
    assert _VK_TOKEN not in text
    assert SecretStore(tmp_path).get("POTOK_VK_TOKEN") == _VK_TOKEN
    expires = SecretStore(tmp_path).get("POTOK_VK_TOKEN_EXPIRES_AT")
    assert expires is not None and int(expires) > 1_000_000_000

    SecretStore(tmp_path).set_many({"POTOK_VK_TOKEN_EXPIRES_AT": "1"})
    board = client.get("/").data.decode()
    assert "просрочен" in board
    assert "Обновить токен" in board
    assert _VK_TOKEN not in board
    os.environ.pop("POTOK_VK_TOKEN", None)


def test_jobs_round_trip_and_restart_hint(tmp_path: Path, monkeypatch) -> None:
    enqueue(tmp_path, 4, "rejected")
    assert drain_jobs(jobs_path(tmp_path)) == [{"row_id": 4, "status": "rejected"}]
    assert "docker compose restart worker" in restart_worker(tmp_path / "missing.sock")
    sock = tmp_path / "docker.sock"
    sock.write_text("", encoding="utf-8")
    monkeypatch.setattr("potok.restart._request", lambda *_args: (204, ""))
    assert "перезапущена" in restart_worker(sock)
    assert resolve_wg_path(tmp_path / "config.yaml", Path("/no/such/potok/wg0.conf")) == (
        tmp_path / "wg0.conf"
    )


def test_pending_list_is_split_into_pages(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    _login(client)
    config = load_config(tmp_path / "config.yaml")
    with SeenStore(config.database_path) as store:
        for index in range(25):
            store.add(
                FeedItem(
                    guid=f"demo:{index}",
                    feed_name="demo",
                    title=f"Фильм {index}",
                    link="https://example.com/1",
                    published=None,
                    torrent_url="https://example.com/a.torrent",
                    rating=8,
                    quality="1080p",
                    genre="драма",
                )
            )

    first = client.get("/pending").data.decode()
    assert first.count('class="col-12 col-md-6 col-xl-3 pending-card"') == 20
    assert "aspect-ratio: 2 / 3" in first
    assert "width: 240px;" in first
    assert 'id="pending-size"' in first
    assert 'id="to-top"' in first
    assert "page=2" in first
    second = client.get("/pending?page=2").data.decode()
    assert second.count('class="col-12 col-md-6 col-xl-3 pending-card"') == 5
    assert "Фильм 24" in second
    small = client.get("/pending?size=10&page=1").data.decode()
    assert small.count('class="col-12 col-md-6 col-xl-3 pending-card"') == 10
    remembered = client.get("/pending").data.decode()
    assert remembered.count('class="col-12 col-md-6 col-xl-3 pending-card"') == 10
