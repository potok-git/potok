import json
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from potok.config import load_config
from potok.publish import (
    DownloadError,
    PublishResult,
    _playlist_names,
    bind_uploaded_codes,
    build_aria2_command,
    deliver_and_notify,
    download_with_aria2,
    message_text,
    publish_file,
    publish_finished,
    publish_pending,
    publish_torrent,
    queue_download,
    send_vk_card,
)
from potok.rss import FeedItem
from potok.store import SeenStore
from potok.stream import StreamError
from potok.tmdb import MovieInfo, MovieNotFound
from potok.vk_video import PublishedVideo, VkError

_CONFIG = """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
database:
  path: {db}
delivery:
  download_dir: {downloads}
  aria2_bin: aria2c
  aria2_timeout_seconds: 0
"""


def _config(tmp_path: Path):
    path = tmp_path / "config.yaml"
    db = (tmp_path / "seen.db").as_posix()
    downloads = (tmp_path / "downloads").as_posix()
    path.write_text(_CONFIG.format(db=db, downloads=downloads), encoding="utf-8")
    return load_config(path)


def _published(player: str = "https://vk.com/video_ext.php?oid=1&id=2&hash=z") -> PublishedVideo:
    return PublishedVideo(
        owner_id=1,
        video_id=2,
        access_key="z",
        player_url=player,
        page_url="https://vk.com/video1_2",
    )


def test_aria2_then_vk_then_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg-token")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "42")
    config = _config(tmp_path)
    torrent = tmp_path / "movie.torrent"
    torrent.write_bytes(b"d8:announce0:e")
    events: list[str] = []

    def run_aria2(command: list[str], timeout: float | None) -> None:
        assert command[0] == "aria2c"
        assert timeout is None
        folder = tmp_path / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "clip.mp4").write_bytes(b"mp4")
        (folder / "note.txt").write_bytes(b"skip")
        events.append("aria")

    def upload(path: Path, *, title: str, description: str) -> PublishedVideo:
        assert path.name == "clip.mp4"
        assert title == "Фильм"
        assert description == "https://example.com/item"
        events.append("vk")
        return _published()

    def notify(text: str) -> None:
        events.append(text)

    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:1",
                feed_name="demo",
                title="Фильм",
                link="https://example.com/item",
                published=datetime(2026, 10, 8, tzinfo=UTC),
                torrent_url="https://example.com/a.torrent",
                rating=8,
                quality="1080p",
                genre="drama",
            ),
            status="approved",
        )

    result = deliver_and_notify(
        config,
        torrent,
        title="Фильм",
        description="https://example.com/item",
        row_id=row_id,
        run_aria2=run_aria2,
        upload=upload,
        notify=notify,
    )

    assert events[0] == "aria"
    assert events[1] == "vk"
    assert "Название: Фильм" in events[2]
    assert "1080p" in events[2]
    assert "Drama" in events[2]
    assert "https://vk.com/video_ext.php" not in events[2]
    assert result.player_urls == (_published().player_url,)
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
        assert item is not None
        assert item.vk_url == _published().player_url
        assert store.list_approved_without_vk() == []


def test_second_publish_does_not_upload_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg-token")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "42")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    config = _config(tmp_path)
    video = tmp_path / "ready.mp4"
    video.write_bytes(b"mp4")
    calls: list[str] = []

    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:1",
                feed_name="demo",
                title="Фильм",
                link="https://example.com",
                published=None,
                torrent_url="https://example.com/a.torrent",
                rating=None,
                quality=None,
                genre=None,
            ),
            status="approved",
        )
        store.set_vk_url(row_id, "https://vk.com/video_ext.php?oid=1&id=2")

    result = deliver_and_notify(
        config,
        video,
        title="Фильм",
        row_id=row_id,
        upload=lambda *_args, **_kwargs: calls.append("vk"),
        notify=lambda text: calls.append(text),
    )

    assert "Название: Фильм" in calls[0]
    assert result.player_urls[0] not in calls[0]
    assert result == PublishResult(player_urls=("https://vk.com/video_ext.php?oid=1&id=2",))


def test_queue_download_starts_without_vk_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    config = _config(tmp_path)
    torrent = tmp_path / "movie.torrent"
    torrent.write_bytes(b"d8:announce0:e")
    seen: list[str] = []

    def remember(_config, path: Path, title: str) -> dict:
        seen.append(f"{path.name}:{title}")
        return {"ok": True}

    monkeypatch.setattr("potok.publish.register_downloaded", remember)
    queue_download(config, torrent, "Фильм")
    assert seen == ["movie.torrent:Фильм"]


def test_missing_vk_token_stops_before_aria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "1")
    config = _config(tmp_path)
    torrent = tmp_path / "a.torrent"
    torrent.write_bytes(b"torrent")
    called = []

    def run_aria2(_command: list[str], _timeout: float | None) -> None:
        called.append("aria")

    with pytest.raises(Exception, match="POTOK_VK_TOKEN"):
        download_dir = config.delivery.download_dir
        download_dir.mkdir(parents=True, exist_ok=True)
        deliver_and_notify(
            config,
            torrent,
            title="Фильм",
            run_aria2=run_aria2,
            notify=lambda _text: called.append("tg"),
        )
    assert called == []


def test_download_picks_video_not_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "a.torrent"
    source.write_bytes(b"t")
    directory = tmp_path / "out"

    def run_aria2(_command: list[str], _timeout: float | None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "movie.mkv").write_bytes(b"v" * 20)
        (directory / "movie.srt").write_bytes(b"s")
        (directory / "movie.mkv.aria2").write_bytes(b"ctl")

    files = download_with_aria2(
        source,
        directory,
        binary="aria2c",
        timeout=None,
        run_aria2=run_aria2,
    )
    assert [path.name for path in files] == ["movie.mkv"]


def test_aria2_failure_code() -> None:
    from potok.publish import _run_aria2

    with pytest.raises(DownloadError, match="кодом 3"):
        _run_aria2([sys.executable, "-c", "import sys; print('aria-test'); sys.exit(3)"], 10)


def test_publish_pending_without_torrent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk")
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "1")
    config = _config(tmp_path)
    with SeenStore(config.database_path) as store:
        store.add(
            FeedItem(
                guid="demo:1",
                feed_name="demo",
                title="Фильм",
                link="https://example.com",
                published=None,
                torrent_url="https://example.com/missing.torrent",
                rating=None,
                quality=None,
                genre=None,
            ),
            status="approved",
        )
    assert publish_pending(config) == 1


def test_vk_name_is_uuid_not_the_movie_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _config(tmp_path)
    video = tmp_path / "film.mp4"
    video.write_bytes(b"mp4")
    seen: dict[str, str] = {}

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def upload_and_wait(self, _path: Path, *, title: str, description: str, **_kwargs: object):
            del description
            seen["name"] = title
            return _published()

        def place_in_playlists(self, *_args: object, **_kwargs: object) -> tuple[str, ...]:
            return ()

    monkeypatch.setattr("potok.publish.VkVideoClient", FakeClient)
    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:uuid",
                feed_name="demo",
                title="Секретный фильм",
                link="https://example.com",
                published=None,
                torrent_url="https://example.com/a.torrent",
                rating=None,
                quality=None,
                genre=None,
            ),
            status="approved",
        )
    publish_file(config, video, title="Секретный фильм", row_id=row_id)
    parsed = uuid.UUID(seen["name"])
    assert str(parsed) == seen["name"]
    assert "Секретный" not in seen["name"]
    with SeenStore(config.database_path) as store:
        item = store.get(row_id)
        assert item is not None
        assert item.vk_uuid == seen["name"]


def test_playlist_uses_stored_genre_and_year(tmp_path: Path) -> None:
    config = _config(tmp_path)
    title = "Фильм [2026, боевик, WEB-DL 1080p]"
    with SeenStore(config.database_path) as store:
        row_id = store.add(
            FeedItem(
                guid="demo:genre",
                feed_name="demo",
                title=title,
                link="https://example.com/item",
                published=datetime(2026, 10, 8, tzinfo=UTC),
                torrent_url="https://example.com/a.torrent",
                rating=None,
                quality="1080p",
                genre="боевик, фантастика",
            )
        )
    assert _playlist_names(config, title=title, filename="clip.mkv", row_id=row_id) == (
        "2026 - Боевик",
        "2026 - Фантастика",
    )


def test_playlist_from_torrent_name_when_file_has_no_genre(tmp_path: Path) -> None:
    config = _config(tmp_path)
    folder = tmp_path / "metadata"
    folder.mkdir()
    config = replace(config, rss=replace(config.rss, metadata_dir=folder))
    (folder / "Звери [2026, Драма, WEB-DL 1080p].torrent").write_bytes(b"Animals.2026.1080p.mkv")
    assert _playlist_names(
        config,
        title="Animals.2026.1080p",
        filename="Animals.2026.1080p.mkv",
        row_id=None,
    ) == ("2026 - Драма",)


def test_message_and_command() -> None:
    text = message_text("Название", ("https://vk.com/video_ext.php?oid=1&id=2",))
    assert text.startswith("Название\nhttps://")
    command = build_aria2_command("aria2c", "movie.torrent", Path("data/downloads"))
    assert command[0] == "aria2c"
    assert "--seed-time=0" in command
    assert command[-1] == "movie.torrent"


def test_upload_error_is_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk")
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "1")
    config = _config(tmp_path)
    video = tmp_path / "a.mp4"
    video.write_bytes(b"data")

    def upload(_path: Path, *, title: str, description: str) -> PublishedVideo:
        del title, description
        raise VkError("сервер отклонил файл")

    with pytest.raises(VkError, match="отклонил"):
        deliver_and_notify(config, video, title="Фильм", upload=upload, notify=lambda _text: None)


def test_torrent_is_registered_for_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _config(tmp_path)
    torrent = tmp_path / "movie.torrent"
    torrent.write_bytes(b"d8:announce0:e")
    seen: list[str] = []

    def run_aria2(_command: list[str], _timeout: float | None) -> None:
        folder = tmp_path / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "clip.mp4").write_bytes(b"mp4")

    def remember(_config, path: Path, title: str) -> dict:
        seen.append(f"{path.name}:{title}")
        return {"hash": "abc"}

    monkeypatch.setattr("potok.publish.register_downloaded", remember)
    publish_torrent(
        config,
        torrent,
        title="Фильм",
        run_aria2=run_aria2,
        upload=lambda path, *, title, description: _published(),
    )
    assert seen == ["movie.torrent:Фильм"]


def test_streaming_error_does_not_stop_vk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _config(tmp_path)
    torrent = tmp_path / "movie.torrent"
    torrent.write_bytes(b"d8:announce0:e")

    def run_aria2(_command: list[str], _timeout: float | None) -> None:
        folder = tmp_path / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "clip.mp4").write_bytes(b"mp4")

    def remember(_config, _path: Path, _title: str) -> None:
        raise StreamError("нет связи")

    monkeypatch.setattr("potok.publish.register_downloaded", remember)
    result = publish_torrent(
        config,
        torrent,
        title="Фильм",
        run_aria2=run_aria2,
        upload=lambda path, *, title, description: _published(),
    )
    assert result.player_urls == (_published().player_url,)


def test_tmdb_is_asked_only_after_vk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg-token")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-key")
    config = _config(tmp_path)
    video = tmp_path / "ready.mp4"
    video.write_bytes(b"mp4")
    order: list[str] = []

    def upload(path: Path, *, title: str, description: str) -> PublishedVideo:
        del path, title, description
        order.append("vk")
        return _published()

    def search(api_key: str, title: str, *, timeout: float, get=None) -> MovieInfo:
        del timeout, get
        order.append("tmdb")
        assert api_key == "tmdb-key"
        assert "Dick" in title
        return MovieInfo(
            title="Аферисты Дик и Джейн",
            year="2005",
            rating=6.2,
            overview="Пара изображает ограбления.",
            poster_url="https://image.tmdb.org/t/p/w500/dick.jpg",
        )

    monkeypatch.setattr("potok.publish.search_movie", search)
    notes: list[str] = []
    deliver_and_notify(
        config,
        video,
        title="Fun with Dick and Jane (2005) 1080p",
        upload=upload,
        notify=notes.append,
    )

    assert order == ["vk", "tmdb"]
    assert "Аферисты Дик и Джейн (2005)" in notes[0]
    assert "Рейтинг: 6.2" in notes[0]
    assert "ограбления" in notes[0]
    assert "Качество: 1080p" in notes[0]
    assert _published().player_url not in notes[0]


def test_poster_is_sent_with_send_photo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg-token")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-key")
    config = _config(tmp_path)
    video = tmp_path / "ready.mp4"
    video.write_bytes(b"mp4")
    sent: dict[str, str | None] = {}

    def search(_api_key: str, _title: str, *, timeout: float, get=None) -> MovieInfo:
        del timeout, get
        return MovieInfo(
            title="Матрица",
            year="1999",
            rating=8.2,
            overview="Хакер.",
            poster_url="https://image.tmdb.org/t/p/w500/poster.jpg",
        )

    def notice(
        token: str,
        chat_id: str,
        caption: str,
        poster: str | None,
        timeout: float,
        reply_markup: object | None = None,
    ) -> None:
        del token, timeout
        sent["chat"] = chat_id
        sent["caption"] = caption
        sent["poster"] = poster
        sent["markup"] = reply_markup

    monkeypatch.setattr("potok.publish.search_movie", search)
    monkeypatch.setattr("potok.bot.deliver_notice", notice)
    deliver_and_notify(
        config,
        video,
        title="Матрица",
        upload=lambda path, *, title, description: _published(),
    )
    assert sent["chat"] == "42"
    assert sent["poster"] == "https://image.tmdb.org/t/p/w500/poster.jpg"
    assert "Хакер." in str(sent["caption"])
    assert "Название: Матрица (1999)" in str(sent["caption"])
    assert _published().player_url not in str(sent["caption"])
    button = sent["markup"].inline_keyboard[0][0]
    assert button.text == "Смотреть во VK"
    assert button.url == _published().player_url


def test_tmdb_miss_still_sends_vk_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg-token")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TMDB_API_KEY", "tmdb-key")
    config = _config(tmp_path)
    video = tmp_path / "ready.mp4"
    video.write_bytes(b"mp4")

    def search(_api_key: str, _title: str, *, timeout: float, get=None) -> MovieInfo:
        del timeout, get
        raise MovieNotFound("Фильм «Фильм» в TMDB не найден.")

    sent: dict[str, object] = {}

    def notice(
        token: str,
        chat_id: str,
        caption: str,
        poster: str | None,
        timeout: float,
        reply_markup: object | None = None,
    ) -> None:
        del token, chat_id, poster, timeout
        sent["caption"] = caption
        sent["markup"] = reply_markup

    monkeypatch.setattr("potok.publish.search_movie", search)
    monkeypatch.setattr("potok.bot.deliver_notice", notice)
    result = deliver_and_notify(
        config,
        video,
        title="Фильм",
        upload=lambda path, *, title, description: _published(),
    )
    assert "Название: Фильм" in str(sent["caption"])
    assert result.player_urls[0] not in str(sent["caption"])
    assert sent["markup"].inline_keyboard[0][0].url == result.player_urls[0]


def test_vk_card_sends_title_description_and_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "tg-token")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "42")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    config = _config(tmp_path)
    sent: dict[str, object] = {}

    def notice(
        token: str,
        chat_id: str,
        caption: str,
        poster: str | None,
        timeout: float,
        reply_markup: object | None = None,
    ) -> None:
        del poster, timeout
        sent["token"] = token
        sent["chat"] = chat_id
        sent["text"] = caption
        sent["markup"] = reply_markup

    monkeypatch.setattr("potok.bot.deliver_notice", notice)
    send_vk_card(
        config,
        title="7 пьес для черно-белого кино (1982)",
        description="короткий метр",
        page_url="https://vk.com/video5_9",
    )
    assert sent["chat"] == "42"
    assert "7 пьес для черно-белого кино (1982)" in str(sent["text"])
    assert "короткий метр" in str(sent["text"])
    assert "https://vk.com/video5_9" not in str(sent["text"])
    assert "Качество:" in str(sent["text"])
    assert sent["markup"].inline_keyboard[0][0].url == "https://vk.com/video5_9"
    assert sent["token"] == "tg-token"


_HASH = "ab" * 20


def _finished_config(tmp_path: Path):
    config = _config(tmp_path)
    return replace(config, streaming=replace(config.streaming, enabled=True))


class _Torrents:
    def __init__(self, rows: list[dict], files: dict[str, list[dict]]) -> None:
        self.rows = rows
        self.files = files
        self.asked: list[str] = []

    def list_torrents(self) -> list[dict]:
        return self.rows

    def torrent_files(self, infohash: str) -> list[dict]:
        self.asked.append(infohash)
        return self.files.get(infohash, [])


def test_finished_torrent_is_sent_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    video = folder / "film.mkv"
    video.write_bytes(b"video")
    sent: list[Path] = []

    def upload(path: Path, **kwargs: object) -> PublishedVideo:
        del kwargs
        sent.append(path)
        return _published()

    client = _Torrents(
        [{"hash": _HASH, "name": "Фильм", "state": "stoppedUP", "progress": 1}],
        {_HASH: [{"name": "film.mkv", "progress": 1}, {"name": "note.txt", "progress": 1}]},
    )
    assert publish_finished(config, client=client, upload=upload) == 1
    assert sent == [video]
    assert publish_finished(config, client=client, upload=upload) == 0
    assert sent == [video]


def test_manual_torrent_is_not_auto_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    base = _finished_config(tmp_path)
    config = replace(base, rss=replace(base.rss, metadata_dir=tmp_path / "metadata"))
    folder = tmp_path / "downloads"
    folder.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (folder / "film.mkv").write_bytes(b"video")
    (metadata / "Ручной фильм.torrent").write_bytes(b"film.mkv")
    sent: list[Path] = []
    client = _Torrents(
        [{"hash": _HASH, "name": "Фильм", "state": "stoppedUP", "progress": 1}],
        {_HASH: [{"name": "film.mkv", "progress": 1}]},
    )
    def upload(path: Path, **kwargs: object) -> None:
        del kwargs
        sent.append(path)

    assert publish_finished(config, client=client, upload=upload) == 0
    assert sent == []


def test_auto_feed_torrent_still_goes_to_vk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    base = _finished_config(tmp_path)
    config = replace(base, rss=replace(base.rss, metadata_dir=tmp_path / "metadata"))
    folder = tmp_path / "downloads"
    folder.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    video = folder / "film.mkv"
    video.write_bytes(b"video")
    (metadata / "aabbccddeeff0011.torrent").write_bytes(b"film.mkv")
    sent: list[Path] = []

    def upload(path: Path, **kwargs: object) -> PublishedVideo:
        del kwargs
        sent.append(path)
        return _published()

    client = _Torrents(
        [{"hash": _HASH, "name": "Фильм", "state": "stoppedUP", "progress": 1}],
        {_HASH: [{"name": "film.mkv", "progress": 1}]},
    )
    assert publish_finished(config, client=client, upload=upload) == 1
    assert sent == [video]


def test_vk_queue_sends_the_upper_film_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "a.mkv").write_bytes(b"a")
    (folder / "b.mkv").write_bytes(b"b")
    later = "cd" * 20
    from potok.vk_uploads import UploadOrder, order_path

    UploadOrder(order_path(config.database_path)).apply([later, _HASH])
    sent: list[str] = []

    def upload(path: Path, **kwargs: object) -> PublishedVideo:
        del kwargs
        sent.append(path.name)
        return _published()

    client = _Torrents(
        [
            {"hash": _HASH, "name": "Первый", "state": "stoppedUP", "progress": 1},
            {"hash": later, "name": "Второй", "state": "stoppedUP", "progress": 1},
        ],
        {
            _HASH: [{"name": "a.mkv", "progress": 1}],
            later: [{"name": "b.mkv", "progress": 1}],
        },
    )
    assert publish_finished(config, client=client, upload=upload) == 1
    assert sent == ["b.mkv"]


def test_unfinished_torrent_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "part.mkv").write_bytes(b"video")
    sent: list[Path] = []
    client = _Torrents(
        [{"hash": _HASH, "name": "Фильм", "state": "downloading", "progress": 0.4}],
        {_HASH: [{"name": "part.mkv", "progress": 0.4}]},
    )
    assert (
        publish_finished(config, client=client, upload=lambda path, **kwargs: sent.append(path))
        == 0
    )
    assert sent == []
    assert client.asked == []


def test_vk_error_does_not_retry_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "film.mkv").write_bytes(b"video")
    calls = {"n": 0}

    def upload(path: Path, **kwargs: object) -> PublishedVideo:
        del path, kwargs
        calls["n"] += 1
        raise VkError("сеть")

    client = _Torrents(
        [{"hash": _HASH, "name": "Фильм", "state": "stoppedUP", "progress": 1}],
        {_HASH: [{"name": "film.mkv", "progress": 1}]},
    )
    assert publish_finished(config, client=client, upload=upload) == 0
    assert publish_finished(config, client=client, upload=upload) == 0
    assert calls["n"] == 1


def test_busy_upload_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    from potok.vk_uploads import UploadBoard, board_path

    UploadBoard(board_path(config.database_path)).begin(name="other.mkv", title="Другой")
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "film.mkv").write_bytes(b"video")
    sent: list[Path] = []
    client = _Torrents(
        [{"hash": _HASH, "name": "Фильм", "state": "stoppedUP", "progress": 1}],
        {_HASH: [{"name": "film.mkv", "progress": 1}]},
    )
    assert (
        publish_finished(config, client=client, upload=lambda path, **kwargs: sent.append(path))
        == 0
    )
    assert sent == []


def test_stopped_upload_can_be_sent_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    from potok.vk_uploads import UploadBoard, board_path

    board = UploadBoard(board_path(config.database_path))
    job_id = board.begin(name="film.mkv", title="Фильм")
    board.set_status(job_id, "stopped", message="Остановлено")
    folder = tmp_path / "downloads"
    folder.mkdir()
    video = folder / "film.mkv"
    video.write_bytes(b"video")
    sent: list[Path] = []

    def upload(path: Path, **kwargs: object) -> PublishedVideo:
        del kwargs
        sent.append(path)
        return _published()

    client = _Torrents(
        [{"hash": _HASH, "name": "Фильм", "state": "stoppedUP", "progress": 1}],
        {_HASH: [{"name": "film.mkv", "progress": 1}]},
    )
    assert publish_finished(config, client=client, upload=upload) == 1
    assert sent == [video]


def test_series_episodes_share_one_playlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    first = folder / "Show" / "S01E01.mkv"
    second = folder / "Show" / "S01E02.mkv"
    first.parent.mkdir()
    first.write_bytes(b"a")
    second.write_bytes(b"bbb")
    placed: list[tuple[str, ...]] = []
    uploaded: list[str] = []

    class FakeClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def upload_and_wait(self, path: Path, *, title: str, description: str, **_kwargs: object):
            del title, description
            uploaded.append(path.name)
            return _published(player=f"https://vk.com/video_ext.php?oid=1&id={len(uploaded)}")

        def place_in_playlists(self, *_args: object, **_kwargs: object) -> tuple[str, ...]:
            names = _args[2]
            assert isinstance(names, tuple)
            placed.append(names)
            return names

    monkeypatch.setattr("potok.publish.VkVideoClient", FakeClient)
    with SeenStore(config.database_path) as store:
        store.add(
            FeedItem(
                guid="demo:series",
                feed_name="demo",
                title="Сериал [2024, Драма, WEB-DL]",
                link="https://example.com/series",
                published=None,
                torrent_url=f"magnet:?xt=urn:btih:{_HASH}",
                rating=None,
                quality=None,
                genre="Драма",
            ),
            status="approved",
        )
    files = [
        {"name": "Show/S01E01.mkv", "progress": 1},
        {"name": "Show/S01E02.mkv", "progress": 1},
        {"name": "Show/note.txt", "progress": 1},
    ]
    client = _Torrents(
        [{"hash": _HASH, "name": "Сериал", "state": "stoppedUP", "progress": 1}],
        {_HASH: files},
    )
    assert publish_finished(config, client=client) == 2
    assert uploaded == ["S01E01.mkv", "S01E02.mkv"]
    assert len(placed) == 2
    code = placed[0][0]
    assert uuid.UUID(code)
    assert placed[0] == placed[1] == (code, "2024 - Драма")
    saved = json.loads((tmp_path / "vk_sent.json").read_text(encoding="utf-8"))
    assert saved["playlists"][_HASH] == code

    third = folder / "Show" / "S01E03.mkv"
    third.write_bytes(b"c")
    files.append({"name": "Show/S01E03.mkv", "progress": 1})
    assert publish_finished(config, client=client) == 1
    assert uploaded[-1] == "S01E03.mkv"
    assert placed[-1][0] == code


class _Lifecycle(_Torrents):
    def __init__(self, rows: list[dict], files: dict[str, list[dict]]) -> None:
        super().__init__(rows, files)
        self.stopped: list[str] = []
        self.removed: list[str] = []

    def stop(self, infohash: str) -> None:
        self.stopped.append(infohash)

    def remove(self, infohash: str) -> None:
        self.removed.append(infohash)


def _mini_torrent(name: str) -> bytes:
    info = (
        b"d6:lengthi5e4:name"
        + str(len(name.encode())).encode()
        + b":"
        + name.encode()
        + b"12:piece lengthi16384e6:pieces20:"
        + (b"a" * 20)
        + b"e"
    )
    return b"d8:announce9:udp://x.i4:info" + info + b"e"


def test_fresh_download_keeps_seeding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "film.mkv").write_bytes(b"video")
    now = 1_700_000_000.0
    monkeypatch.setattr("potok.publish.time.time", lambda: now)
    sent: list[Path] = []
    client = _Lifecycle(
        [
            {
                "hash": _HASH,
                "name": "Фильм",
                "state": "uploading",
                "progress": 1,
                "completion_on": int(now) - 60,
            }
        ],
        {_HASH: [{"name": "film.mkv", "progress": 1}]},
    )
    assert (
        publish_finished(config, client=client, upload=lambda path, **kwargs: sent.append(path))
        == 0
    )
    assert sent == []
    assert client.stopped == []
    assert client.asked == []


def test_after_thirty_minutes_torrent_is_paused_and_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    video = folder / "film.mkv"
    video.write_bytes(b"video")
    now = 1_700_000_000.0
    monkeypatch.setattr("potok.publish.time.time", lambda: now)
    sent: list[Path] = []

    def upload(path: Path, **kwargs: object) -> PublishedVideo:
        del kwargs
        sent.append(path)
        return _published()

    client = _Lifecycle(
        [
            {
                "hash": _HASH,
                "name": "Фильм",
                "state": "uploading",
                "progress": 1,
                "completion_on": int(now) - 31 * 60,
            }
        ],
        {_HASH: [{"name": "film.mkv", "progress": 1}]},
    )
    assert publish_finished(config, client=client, upload=upload) == 1
    assert client.stopped == [_HASH]
    assert sent == [video]


def test_missing_completion_time_still_waits_thirty_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    (folder / "film.mkv").write_bytes(b"video")
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr("potok.publish.time.time", lambda: clock["now"])
    sent: list[Path] = []

    def upload(path: Path, **kwargs: object) -> PublishedVideo:
        del kwargs
        sent.append(path)
        return _published()

    client = _Lifecycle(
        [{"hash": _HASH, "name": "Фильм", "state": "stalledUP", "progress": 1}],
        {_HASH: [{"name": "film.mkv", "progress": 1}]},
    )
    assert publish_finished(config, client=client, upload=upload) == 0
    assert sent == []
    assert client.stopped == []
    saved = json.loads((tmp_path / "vk_sent.json").read_text(encoding="utf-8"))
    assert saved["completed"][_HASH] == clock["now"]
    clock["now"] += 31 * 60
    assert publish_finished(config, client=client, upload=upload) == 1
    assert client.stopped == [_HASH]
    assert len(sent) == 1


def test_old_vk_download_deletes_video_and_torrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = replace(
        _finished_config(tmp_path),
        rss=replace(_finished_config(tmp_path).rss, metadata_dir=tmp_path / "meta"),
    )
    folder = tmp_path / "downloads"
    folder.mkdir()
    video = folder / "film.mkv"
    video.write_bytes(b"video")
    meta = tmp_path / "meta"
    meta.mkdir()
    blob = _mini_torrent("film")
    from potok.publish import _torrent_info_hash

    digest = _torrent_info_hash(blob)
    assert digest is not None
    torrent = meta / "film.torrent"
    torrent.write_bytes(blob)
    (tmp_path / "vk_sent.json").write_text(
        json.dumps({"files": ["film.mkv"], "hold": {}, "playlists": {}}),
        encoding="utf-8",
    )
    sent: list[Path] = []
    client = _Lifecycle(
        [
            {
                "hash": digest,
                "name": "Фильм",
                "state": "stoppedUP",
                "progress": 1,
                "added_on": int(time.time()) - 11 * 24 * 60 * 60,
            }
        ],
        {digest: [{"name": "film.mkv", "progress": 1}]},
    )
    assert (
        publish_finished(config, client=client, upload=lambda path, **kwargs: sent.append(path))
        == 0
    )
    assert sent == []
    assert not video.exists()
    assert not torrent.exists()
    assert client.removed == [digest]


def test_recent_or_not_uploaded_files_stay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    config = _finished_config(tmp_path)
    folder = tmp_path / "downloads"
    folder.mkdir()
    recent = folder / "fresh.mkv"
    failed = folder / "failed.mkv"
    recent.write_bytes(b"video")
    failed.write_bytes(b"video")
    (tmp_path / "vk_sent.json").write_text(
        json.dumps({"files": ["fresh.mkv"], "hold": {}, "playlists": {}}),
        encoding="utf-8",
    )
    now = int(time.time())
    fresh_hash = "ab" * 20
    old_hash = "cd" * 20

    def upload(path: Path, **kwargs: object) -> PublishedVideo:
        del path, kwargs
        raise VkError("сеть")

    client = _Lifecycle(
        [
            {
                "hash": fresh_hash,
                "name": "Свежий",
                "state": "stoppedUP",
                "progress": 1,
                "added_on": now - 24 * 60 * 60,
            },
            {
                "hash": old_hash,
                "name": "Старый",
                "state": "stoppedUP",
                "progress": 1,
                "added_on": now - 11 * 24 * 60 * 60,
            },
        ],
        {
            fresh_hash: [{"name": "fresh.mkv", "progress": 1}],
            old_hash: [{"name": "failed.mkv", "progress": 1}],
        },
    )
    assert publish_finished(config, client=client, upload=upload) == 0
    assert recent.exists()
    assert failed.exists()
    assert client.removed == []


def test_bind_uploaded_codes_reads_table_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _finished_config(tmp_path)
    other = "cd" * 20
    with SeenStore(config.database_path) as store:
        store.add(
            FeedItem(
                guid="demo:1",
                feed_name="demo",
                title="Фильм",
                link="https://example.com/1",
                published=None,
                torrent_url=f"magnet:?xt=urn:btih:{_HASH}",
                rating=None,
                quality="1080p",
                genre="драма",
            ),
            status="approved",
        )
    from potok.vk_uploads import UploadBoard, board_path

    UploadBoard(board_path(config.database_path)).begin(name="film.mkv", title="Фильм")
    client = _Torrents(
        [
            {"hash": _HASH, "name": "A", "state": "stoppedUP", "progress": 1},
            {"hash": other, "name": "B", "state": "stoppedUP", "progress": 1},
        ],
        {
            _HASH: [{"name": "film.mkv", "progress": 1}],
            other: [{"name": "other.mkv", "progress": 1}],
        },
    )
    monkeypatch.setattr("potok.qbit.QbitClient", lambda settings, **kwargs: client)
    opens = {"n": 0}
    real = SeenStore.list_seen

    def counting(self) -> list:
        opens["n"] += 1
        return real(self)

    monkeypatch.setattr(SeenStore, "list_seen", counting)
    found = bind_uploaded_codes(config)
    assert opens["n"] == 1
    assert found["film.mkv"].title == "Фильм"
