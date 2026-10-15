import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto

from potok.bot import (
    BOT_COMMANDS,
    _release_telegram,
    accepts_torrent,
    approval_keyboard,
    cmd_token,
    deliver_notice,
    format_card,
    format_clear_result,
    format_dashboard,
    format_pending,
    format_refresh_result,
    format_settings,
    format_status,
    format_vk_token_message,
    genres_keyboard,
    is_allowed,
    loads_menu_keyboard,
    movie_button_label,
    notifier_from_env,
    on_torrent_file,
    on_vk_id_number,
    on_vk_token_paste,
    parse_callback,
    parse_vk_numeric_id,
    pending_keyboard,
    pending_menu_keyboard,
    pending_view,
    process_portal_jobs,
    queue_saved_torrents,
    resolve_telegram,
    root_menu_keyboard,
    set_vk_wait,
    token_keyboard,
    uploaded_view,
    vk_entries,
    vk_reminder,
    vk_section_keyboard,
    vk_token_prompt,
    vk_view,
    vk_wait_field,
)
from potok.config import ConfigError, TelegramSettings, load_config
from potok.jobs import enqueue
from potok.pipeline import FetchReport, torrent_path
from potok.publish import PublishResult
from potok.rss import FeedItem
from potok.secrets import SecretStore
from potok.store import APPROVED, SeenStore, StoredItem
from potok.vk_oauth import EXPIRES_NAME, REMINDED_NAME, VkOAuthError
from potok.vk_video import VkCatalogItem


def _item() -> FeedItem:
    return FeedItem(
        guid="demo:1",
        feed_name="demo",
        title="Example 1080p",
        link="https://example.com/1",
        published=None,
        torrent_url="https://example.com/a.torrent",
        rating=8.5,
        quality="1080p",
        genre="drama",
    )


def test_torrent_is_accepted_from_owner_or_configured_chat() -> None:
    assert accepts_torrent(7, 7, "film.torrent", 7) is True
    assert accepts_torrent(5, -100, "film.torrent", -100) is True
    assert accepts_torrent(5, -100, "photo.jpg", -100) is False
    assert accepts_torrent(5, 5, "film.torrent", 7) is False


def test_torrent_message_is_saved_and_sent_to_vk(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", "vk-token")
    folder = tmp_path / "metadata"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
rss:
  metadata_dir: "{folder.as_posix()}"
database:
  path: "{(tmp_path / "seen.db").as_posix()}"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    started: dict[str, object] = {}

    def fake_deliver(app_config, torrent, *, title, description="", row_id=None):
        del app_config, description, row_id
        started["title"] = title
        started["torrent"] = torrent
        return PublishResult(player_urls=("https://vk.com/video1_2",))

    monkeypatch.setattr("potok.bot.deliver_and_notify", fake_deliver)

    class FakeBot:
        async def download(self, document, destination) -> None:
            del document
            destination.write_bytes(b"d8:announce0:e")

    answers: list[str] = []

    async def answer(text: str, reply_markup=None) -> None:
        del reply_markup
        answers.append(text)

    message = SimpleNamespace(
        document=SimpleNamespace(
            file_name=("7 пьес для черно-белого кино (Отар Иоселиани) [1982, DVDRip].torrent"),
            file_size=20,
        ),
        chat=SimpleNamespace(id=-100),
        from_user=SimpleNamespace(id=5),
        answer=answer,
    )
    asyncio.run(on_torrent_file(message, -100, config, FakeBot()))
    assert started["title"] == "7 пьес для черно-белого кино (1982)"
    assert Path(str(started["torrent"])).is_file()
    assert answers[0].startswith("Принял «7 пьес для черно-белого кино (1982)»")


def test_only_owner_is_allowed() -> None:
    assert is_allowed(7, 7) is True
    assert is_allowed(1, 7) is False
    assert is_allowed(None, 7) is False


def test_parse_callback() -> None:
    assert parse_callback("yes:3") == ("approved", 3)
    assert parse_callback("no:4") == ("rejected", 4)
    assert parse_callback("yes:x") is None
    assert parse_callback("maybe:1") is None
    assert parse_callback("") is None


def test_keyboard_matches_parser() -> None:
    buttons = approval_keyboard(7).inline_keyboard[0]
    assert buttons[0].text == "✅ Одобрить"
    assert buttons[1].text == "❌ Отклонить"
    assert parse_callback(buttons[0].callback_data or "") == ("approved", 7)
    assert parse_callback(buttons[1].callback_data or "") == ("rejected", 7)
    markup = approval_keyboard(
        7,
        topic_url="https://rutracker.org/forum/viewtopic.php?t=1",
        kinopoisk_url="https://www.kinopoisk.ru/film/1/",
    )
    links = markup.inline_keyboard[1]
    assert links[0].text == "Рутрекер"
    assert links[0].url == "http://rutracker.ru/viewtopic.php?t=1"
    assert links[1].text == "Кинопоиск"
    assert links[1].url == "https://www.kinopoisk.ru/film/1/"
    menu = markup.inline_keyboard[-1]
    assert menu[0].text == "Меню"
    assert menu[0].callback_data == "menu"
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "Обновить" not in labels
    assert "Очистить" not in labels


def test_format_helpers() -> None:
    text = format_card(_item())
    assert "Example 1080p" in text
    assert "8.5" in text
    assert "Жанр: Drama" in text
    assert "Страна: нет" in text
    assert "Одобрить" in text
    assert "Сиды: нет" in text
    assert "Размер: нет" in text
    assert "magnet:" not in text
    assert "example.com" not in text

    status = format_status({"pending": 2, "approved": 1, "rejected": 0}, "VPN wg0: выключен.")
    assert "Ожидают решения: 2" in status
    assert "выключен" in status
    assert format_pending([], offset=0, total=0).startswith("Сейчас нет")
    page = format_pending([_stored(1)], offset=10, total=21)
    assert "Ждут решения: 21" in page
    assert "Страница 2 из 3" in page


def _stored(row_id: int, **extra: object) -> StoredItem:
    data: dict[str, object] = {
        "row_id": row_id,
        "guid": f"g:{row_id}",
        "feed_name": "demo",
        "title": f"Фильм {row_id} WEB-DL",
        "link": "https://example.com",
        "torrent_url": None,
        "first_seen": "2026-01-01",
        "status": "pending",
        "rating": 7.9,
        "quality": "1080p",
        "genre": "драма",
    }
    data.update(extra)
    return StoredItem(**data)  # type: ignore[arg-type]


def test_pending_menu_has_ten_films_and_pages() -> None:
    items = [_stored(index, movie_title=f"Фильм {index}") for index in range(1, 11)]
    text = format_pending(items, offset=0, total=25)
    markup = pending_keyboard(items, offset=0, total=25)

    assert "Страница 1 из 3" in text
    buttons = markup.inline_keyboard
    assert buttons[0][0].text == "Фильм 1"
    assert buttons[0][0].callback_data == "open:1"
    assert buttons[9][0].callback_data == "open:10"
    assert buttons[10][0].text == "▶️ Дальше"
    assert buttons[10][0].callback_data == "page:10"
    assert buttons[-1][0].text == "Меню"
    assert buttons[-1][0].callback_data == "menu"

    later = pending_keyboard(items[:5], offset=10, total=25)
    nav = later.inline_keyboard[-2]
    assert nav[0].callback_data == "page:0"
    assert nav[1].callback_data == "page:20"
    assert later.inline_keyboard[-1][0].callback_data == "menu"
    assert len(movie_button_label(_stored(1, movie_title="А" * 80))) == 60
    titled = _stored(
        3,
        movie_title="Танк (2026)",
        genre="боевик, военный",
    )
    assert movie_button_label(titled) == "2026 Танк"
    matchbox = _stored(
        4,
        movie_title="Matchbox: фильм (2026)",
        genre="Боевик, WEB-DL 1080p",
    )
    assert movie_button_label(matchbox) == "2026 Matchbox"
    lego = _stored(5, movie_title="Лего Звёздные войны: Мандалорец (2026)", genre="Мультфильм")
    assert movie_button_label(lego) == "2026 Лего Звёздные войны: Мандалорец"


def test_pending_view_turns_pages(tmp_path: Path) -> None:
    path = tmp_path / "seen.db"
    with SeenStore(path) as store:
        for index in range(12):
            store.add(
                FeedItem(
                    guid=f"g:{index}",
                    feed_name="demo",
                    title=f"Фильм {index}",
                    link="https://example.com",
                    published=None,
                    torrent_url=None,
                    rating=None,
                    quality=None,
                    genre=None,
                )
            )
    first, first_markup = pending_view(path, 0)
    second, second_markup = pending_view(path, 10)
    assert "Ждут решения: 12" in first
    assert "Страница 1 из 2" in first
    assert len(first_markup.inline_keyboard) == 12
    assert "Страница 2 из 2" in second
    assert second_markup.inline_keyboard[-2][0].callback_data == "page:0"
    assert second_markup.inline_keyboard[-1][0].callback_data == "menu"
    assert len(second_markup.inline_keyboard) == 4


def test_pending_view_sorts_by_year(tmp_path: Path) -> None:
    path = tmp_path / "seen.db"
    with SeenStore(path) as store:
        for title, genre in (
            ("Старый [2019, драма]", "драма"),
            ("Новый [2026, боевик]", "боевик"),
            ("Средний [2022, комедия]", "комедия"),
        ):
            store.add(
                FeedItem(
                    guid=title,
                    feed_name="demo",
                    title=title,
                    link="https://example.com",
                    published=None,
                    torrent_url=None,
                    rating=None,
                    quality=None,
                    genre=genre,
                )
            )
    _text, markup = pending_view(path, 0)
    labels = [row[0].text for row in markup.inline_keyboard[:-1]]
    assert labels == [
        "2026 Новый",
        "2022 Средний",
        "2019 Старый",
    ]
    assert markup.inline_keyboard[-1][0].callback_data == "menu"


def test_uploaded_card_uses_poster_fields_and_vk_button() -> None:
    stored = _stored(
        4,
        movie_title="Лего Звёздные войны: Мандалорец",
        title="Лего / Lego [2026, США, фантастика, WEB-DL 1080p]",
        overview="Галактика снова зовёт.",
        country="United States of America",
        genre="фантастика, США, WEB-DL",
        quality="1080p",
        rating=6.7,
        seeders=12,
        size_bytes=5 * 1024**3,
        poster_url="https://image.tmdb.org/t/p/w500/lego.jpg",
        topic_url="https://rutracker.org/forum/viewtopic.php?t=9",
        kinopoisk_url="https://www.kinopoisk.ru/film/9/",
    )
    text, poster, markup = uploaded_view(
        title=stored.title,
        stored=stored,
        urls=("https://vkvideo.ru/video_ext.php?oid=1&id=2",),
    )

    assert poster == "https://image.tmdb.org/t/p/w500/lego.jpg"
    assert "Загружено во VK" in text
    assert "Лего Звёздные войны: Мандалорец" in text
    assert "Рейтинг: 6.7" in text
    assert "Качество: 1080p" in text
    assert "Жанр: Фантастика" in text
    assert "Страна: США" in text
    assert "Сиды: 12" in text
    assert "Галактика снова зовёт." in text
    assert "vkvideo.ru" not in text
    assert markup.inline_keyboard[0][0].text == "Смотреть во VK"
    assert markup.inline_keyboard[0][0].url.startswith("https://vkvideo.ru/")
    assert markup.inline_keyboard[1][0].text == "Рутрекер"
    assert markup.inline_keyboard[1][1].text == "Кинопоиск"
    assert markup.inline_keyboard[-1][0].text == "Меню"


def test_vk_menu_lists_uploaded_films_like_pending() -> None:
    older = _stored(1, movie_title="Старый (2019)", genre="драма", vk_url="https://vk.com/video1_1")
    newer = _stored(2, movie_title="Новый (2026)", genre="боевик", vk_url="https://vk.com/video1_2")
    waiting = _stored(3, movie_title="Ждёт", genre="комедия")
    entries = vk_entries([older, newer, waiting])
    text, markup = vk_view(entries, 0)

    assert "Загружено во VK: 2" in text
    assert "Нажмите фильм" in text
    labels = [row[0].text for row in markup.inline_keyboard[:-1]]
    assert labels == ["2026 Новый", "2019 Старый"]
    assert markup.inline_keyboard[0][0].callback_data == "vkopen:2"
    assert markup.inline_keyboard[-1][0].callback_data == "menu"


def test_vk_menu_uses_vk_catalog_and_turns_pages() -> None:
    rows = [
        _stored(
            index,
            movie_title=f"Фильм {index} ({2000 + index})",
            genre="драма",
            vk_uuid=f"video1_{index}",
        )
        for index in range(1, 13)
    ]
    videos = tuple(
        VkCatalogItem(
            owner_id=1,
            video_id=index,
            title=f"код-{index}",
            description="",
            page_url=f"https://vk.com/video1_{index}",
            player_url=f"https://vk.com/video_ext.php?oid=1&id={index}",
            duration_seconds=100,
            processing=False,
        )
        for index in range(1, 13)
    )
    entries = vk_entries(rows, videos)
    first, first_markup = vk_view(entries, 0)
    second, second_markup = vk_view(entries, 10)

    assert "Загружено во VK: 12" in first
    assert "Страница 1 из 2" in first
    assert len(first_markup.inline_keyboard) == 12
    assert first_markup.inline_keyboard[0][0].text == "2012 Фильм 12"
    assert first_markup.inline_keyboard[0][0].callback_data == "vkvid:1_12"
    assert first_markup.inline_keyboard[-2][0].callback_data == "vkpage:10"
    assert first_markup.inline_keyboard[-1][0].callback_data == "menu"
    assert "Страница 2 из 2" in second
    assert second_markup.inline_keyboard[-2][0].callback_data == "vkpage:0"
    assert second_markup.inline_keyboard[-1][0].callback_data == "menu"


def test_clear_and_refresh_texts() -> None:
    assert "Удалено: 2" in format_clear_result(2)
    assert "Одобренные остались" in format_clear_result(2)
    assert format_clear_result(0) == "Неодобренных записей нет."
    report = FetchReport(accepted=(), skipped_seen=4, skipped_filter=1, failed=0)
    text = format_refresh_result(report)
    assert "Новых в очереди: 0" in text
    assert "Уже были в базе: 4" in text


def test_resolve_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_TELEGRAM_TOKEN", "123:ABC")
    monkeypatch.setenv("POTOK_TELEGRAM_CHAT_ID", "-100")
    settings = TelegramSettings("POTOK_TELEGRAM_TOKEN", "POTOK_TELEGRAM_CHAT_ID")

    token, chat_id = resolve_telegram(settings)

    assert token == "123:ABC"
    assert chat_id == -100


def test_resolve_telegram_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POTOK_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("POTOK_TELEGRAM_CHAT_ID", raising=False)
    settings = TelegramSettings("POTOK_TELEGRAM_TOKEN", "POTOK_TELEGRAM_CHAT_ID")
    with pytest.raises(ConfigError, match="токен"):
        resolve_telegram(settings)


def test_notifier_is_absent_without_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POTOK_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("POTOK_TELEGRAM_CHAT_ID", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
""",
        encoding="utf-8",
    )

    assert notifier_from_env(load_config(path)) is None


def test_format_settings_hides_secrets(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
filters:
  min_rating: 7
  max_age_days: 14
rss:
  feeds:
    - name: demo
      url: https://example.com/feed.xml
database:
  path: data/seen.db
""",
        encoding="utf-8",
    )

    text = format_settings(load_config(path))

    assert "от 7" in text
    assert "14" in text
    assert "demo" in text
    assert "token" not in text.lower()


def test_published_notice_sends_poster(monkeypatch: pytest.MonkeyPatch) -> None:
    photos: list[tuple] = []

    class FakeSession:
        async def close(self) -> None:
            return None

    class FakeBot:
        def __init__(self, token: str, session: object | None = None) -> None:
            self.session = session or FakeSession()

        async def send_photo(
            self,
            chat_id: str,
            photo: str,
            caption: str,
            reply_markup: object | None = None,
        ) -> None:
            photos.append((chat_id, photo, caption, reply_markup))

        async def send_message(
            self,
            chat_id: str,
            caption: str,
            reply_markup: object | None = None,
        ) -> None:
            raise AssertionError(caption)

    monkeypatch.setattr("potok.bot.Bot", FakeBot)
    monkeypatch.setattr("potok.bot.AiohttpSession", lambda timeout: FakeSession())
    deliver_notice("123:ABC", "42", "Матрица", "https://image.tmdb.org/t/p/w500/a.jpg", 10)
    assert photos == [("42", "https://image.tmdb.org/t/p/w500/a.jpg", "Матрица", None)]


def test_bad_poster_falls_back_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []

    class FakeSession:
        async def close(self) -> None:
            return None

    class FakeBot:
        def __init__(self, token: str, session: object | None = None) -> None:
            self.session = session or FakeSession()

        async def send_photo(
            self,
            chat_id: str,
            photo: str,
            caption: str,
            reply_markup: object | None = None,
        ) -> None:
            raise TelegramBadRequest(
                method=SendPhoto(chat_id=chat_id, photo=photo),
                message="wrong type",
            )

        async def send_message(
            self,
            chat_id: str,
            caption: str,
            reply_markup: object | None = None,
        ) -> None:
            messages.append(caption)

    monkeypatch.setattr("potok.bot.Bot", FakeBot)
    monkeypatch.setattr("potok.bot.AiohttpSession", lambda timeout: FakeSession())
    deliver_notice("123:ABC", "42", "ссылка", "https://image.tmdb.org/t/p/w500/a.jpg", 10)
    assert messages == ["ссылка"]


def test_portal_approval_starts_download_without_vk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
database:
  path: "{(tmp_path / "seen.db").as_posix()}"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    torrent = tmp_path / "movie.torrent"
    torrent.write_bytes(b"d8:announce0:e")
    with SeenStore(config.database_path) as store:
        row_id = store.add(_item(), status=APPROVED)
    enqueue(config.database_path.parent, row_id, APPROVED)
    queued: list[str] = []

    def fake_deliver(_config, _vpn, item):
        queued.append(f"meta:{item.title}")
        return torrent

    def fake_queue(_config, path: Path, title: str) -> None:
        queued.append(f"qbit:{path.name}:{title}")

    def fake_publish(*_args, **_kwargs):
        queued.append("vk")

    monkeypatch.setattr("potok.bot.deliver_approved", fake_deliver)
    monkeypatch.setattr("potok.bot.queue_download", fake_queue)
    monkeypatch.setattr("potok.bot._publish", fake_publish)

    handled = asyncio.run(process_portal_jobs(config))

    assert handled == 1
    assert queued == ["meta:Example 1080p", "qbit:movie.torrent:Example 1080p"]


def test_portal_magnet_goes_straight_to_qbittorrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
database:
  path: "{(tmp_path / "seen.db").as_posix()}"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    magnet = (
        "magnet:?xt=urn:btih:262aa9e19151461068c542e7c5ae3ac01a379726"
        "&tr=http%3A%2F%2Frutracker.ru%2Fbt%2Fannounce.php%3Fuk%3D56WtM1NE1U"
    )
    item = FeedItem(
        guid="demo:magnet",
        feed_name="demo",
        title="Животные",
        link="https://rutracker.org/forum/viewtopic.php?t=1",
        published=None,
        torrent_url=magnet,
        rating=7,
        quality="1080p",
        genre="боевик",
    )
    with SeenStore(config.database_path) as store:
        row_id = store.add(item, status=APPROVED)
    enqueue(config.database_path.parent, row_id, APPROVED)
    queued: list[tuple[str, str]] = []

    def fail_deliver(*_args, **_kwargs):
        raise AssertionError("magnet не нужно скачивать по http")

    monkeypatch.setattr("potok.bot.deliver_approved", fail_deliver)
    monkeypatch.setattr(
        "potok.bot.queue_link",
        lambda _config, link, title: queued.append((title, link)),
    )

    handled = asyncio.run(process_portal_jobs(config))

    assert handled == 1
    assert queued == [("Животные", magnet)]


def test_saved_torrent_is_queued_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    meta = tmp_path / "metadata"
    meta.mkdir()
    config_path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
rss:
  metadata_dir: "{meta.as_posix()}"
database:
  path: "{(tmp_path / "seen.db").as_posix()}"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    ready = _item()
    waiting = FeedItem(
        guid="demo:2",
        feed_name="demo",
        title="No file",
        link="https://example.com/2",
        published=None,
        torrent_url="https://example.com/missing.torrent",
        rating=7,
        quality="720p",
        genre="drama",
    )
    with SeenStore(config.database_path) as store:
        store.add(ready, status=APPROVED)
        store.add(waiting, status=APPROVED)
    torrent_path(meta, ready.torrent_url or "").write_bytes(b"d8:announce0:e")
    seen: list[str] = []
    monkeypatch.setattr(
        "potok.bot.queue_download",
        lambda _config, _path, title: seen.append(title),
    )
    done: set[int] = set()

    assert queue_saved_torrents(config, done) == 1
    assert queue_saved_torrents(config, done) == 0
    assert seen == ["Example 1080p"]

    magnet = "magnet:?xt=urn:btih:" + "ab" * 20
    with SeenStore(config.database_path) as store:
        store.add(
            FeedItem(
                guid="demo:magnet",
                feed_name="demo",
                title="Magnet",
                link="https://example.com/m",
                published=None,
                torrent_url=magnet,
                rating=6,
                quality="1080p",
                genre="drama",
            ),
            status=APPROVED,
        )
    links: list[str] = []
    monkeypatch.setattr(
        "potok.bot.queue_link",
        lambda _config, link, title: links.append(title),
    )
    assert queue_saved_torrents(config, done) == 1
    assert links == ["Magnet"]


def test_release_telegram_closes_hung_session() -> None:
    """После смены маршрута WireGuard бот должен бросить старое соединение."""
    closed: list[str] = []

    class _Session:
        async def close(self) -> None:
            closed.append("closed")

    asyncio.run(_release_telegram(SimpleNamespace(session=_Session())))
    asyncio.run(_release_telegram(None))
    assert closed == ["closed"]


_VK_SAMPLE = "vk1.a." + ("Ab3_" * 12)


def _bot_config(tmp_path: Path, extra: str = ""):
    path = tmp_path / "config.yaml"
    database = (tmp_path / "seen.db").as_posix()
    path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
database:
  path: "{database}"
{extra}
""",
        encoding="utf-8",
    )
    return path, load_config(path)


class _Inbox:
    def __init__(self, text: str = "", user_id: int = 7) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.replies: list[str] = []
        self.markups: list[object] = []
        self.deleted = False

    async def answer(self, text: str, reply_markup=None) -> None:
        self.replies.append(text)
        self.markups.append(reply_markup)

    async def delete(self) -> None:
        self.deleted = True


def test_token_button_add_or_update() -> None:
    add = token_keyboard(app_id=12345678, api_version="5.131", has_token=False)
    update = token_keyboard(app_id=12345678, api_version="5.131", has_token=True)
    assert add.inline_keyboard[0][0].text == "Добавить"
    assert update.inline_keyboard[0][0].text == "Обновить"
    assert "oauth.vk.ru" in (add.inline_keyboard[0][0].url or "")
    assert add.inline_keyboard[-1][0].callback_data == "menu"


def test_token_prompt_asks_app_id_without_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    _path, config = _bot_config(tmp_path)
    text, markup, wait = vk_token_prompt(config)
    assert markup is not None
    assert markup.inline_keyboard[-1][0].callback_data == "menu"
    assert wait == "app_id"
    assert "app123456" in text
    assert "Токен VK не задан" in text


def test_format_status_includes_token_line() -> None:
    text = format_status(
        {"pending": 1, "approved": 0, "rejected": 0},
        "VPN wg0: выключен.",
        token_line="Токен VK не задан. Видео загрузить нельзя.",
    )
    assert "Токен VK не задан" in text


def test_cmd_token_without_app_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    set_vk_wait(None)
    _path, config = _bot_config(tmp_path)
    inbox = _Inbox("/token")
    asyncio.run(cmd_token(inbox, 7, config))
    assert vk_wait_field() == "app_id"
    assert "app123456" in inbox.replies[0]
    assert inbox.markups[0] is not None
    assert inbox.markups[0].inline_keyboard[-1][0].callback_data == "menu"


def test_cmd_token_update_button_when_token_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", _VK_SAMPLE)
    _path, config = _bot_config(
        tmp_path,
        """
vk:
  app_id: 12345678
  group_id: 1
""",
    )
    text, markup, wait = format_vk_token_message(
        config,
        has_token=True,
        status_message="Токен VK действует ещё около 2 ч.",
    )
    assert wait is None
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == "Обновить"
    assert markup.inline_keyboard[-1][0].text == "Меню"
    assert _VK_SAMPLE not in text


def test_vk_token_paste_saves_and_deletes_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    _path, config = _bot_config(tmp_path)
    monkeypatch.setattr(
        "potok.bot.save_vk_token",
        lambda *args, **kwargs: "Токен VK сохранён. Он действует около 24 ч.",
    )
    address = "https://oauth.vk.ru/blank.html#access_token=" + _VK_SAMPLE + "&expires_in=86400"
    inbox = _Inbox(address)
    asyncio.run(on_vk_token_paste(inbox, 7, config))
    assert inbox.deleted is True
    assert inbox.replies == ["Токен VK сохранён. Он действует около 24 ч."]
    assert _VK_SAMPLE not in "".join(inbox.replies)


def test_vk_token_paste_keeps_message_on_vk_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, config = _bot_config(tmp_path)

    def boom(*args, **kwargs):
        del args, kwargs
        raise VkOAuthError("VK не принял токен.")

    monkeypatch.setattr("potok.bot.save_vk_token", boom)
    inbox = _Inbox("https://oauth.vk.ru/blank.html#access_token=" + _VK_SAMPLE)
    asyncio.run(on_vk_token_paste(inbox, 7, config))
    assert inbox.deleted is False
    assert "не принял" in inbox.replies[0]
    assert _VK_SAMPLE not in inbox.replies[0]


def test_vk_ids_are_saved_app_then_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    set_vk_wait("app_id")
    path, config = _bot_config(tmp_path)
    dispatcher = SimpleNamespace(workflow_data={"app_config": config})
    inbox = _Inbox("app12345678")
    asyncio.run(on_vk_id_number(inbox, 7, config, dispatcher, path))
    loaded = load_config(path)
    assert loaded.vk.app_id == 12345678
    assert loaded.vk.group_id is None
    assert vk_wait_field() == "group_id"
    assert "ID приложения записан" in inbox.replies[0]
    assert "club123456" in inbox.replies[1]
    assert dispatcher.workflow_data["app_config"].vk.app_id == 12345678

    inbox2 = _Inbox("999001")
    asyncio.run(
        on_vk_id_number(inbox2, 7, dispatcher.workflow_data["app_config"], dispatcher, path)
    )
    loaded = load_config(path)
    assert loaded.vk.group_id == 999001
    assert vk_wait_field() is None
    assert "ID сообщества записан" in inbox2.replies[0]


def test_random_number_is_ignored_until_bot_asks(tmp_path: Path) -> None:
    set_vk_wait(None)
    path, config = _bot_config(tmp_path)
    dispatcher = SimpleNamespace(workflow_data={"app_config": config})
    inbox = _Inbox("123456")
    asyncio.run(on_vk_id_number(inbox, 7, config, dispatcher, path))
    assert load_config(path).vk.app_id is None
    assert inbox.replies == []
    assert parse_vk_numeric_id("club77") == 77
    assert parse_vk_numeric_id("нет") is None


def test_vk_reminder_once_per_expiry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTOK_VK_TOKEN", _VK_SAMPLE)
    _path, config = _bot_config(
        tmp_path,
        """
vk:
  app_id: 12345678
  group_id: 1
""",
    )
    store = SecretStore(tmp_path)
    store.set_many({config.vk.token_env: _VK_SAMPLE, EXPIRES_NAME: "2000"})
    first = vk_reminder(config, store, now=1000)
    assert first is not None
    text, markup, expires = first
    assert "примерно через час" in text
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == "Обновить"
    store.set_many({REMINDED_NAME: expires})
    assert vk_reminder(config, store, now=1000) is None
    store.set_many({EXPIRES_NAME: "0"})
    assert vk_reminder(config, store, now=1000) is None
    store.set_many({EXPIRES_NAME: "1500"})
    later = vk_reminder(config, store, now=2000)
    assert later is not None
    assert "пора продлить" in later[0]
    assert later[1] is not None
    assert later[1].inline_keyboard[-1][0].callback_data == "menu"


def test_bot_commands_hide_settings() -> None:
    names = [item.command for item in BOT_COMMANDS]
    assert "settings" not in names
    assert names == ["status", "pending", "vk", "token", "update", "clear"]


def test_root_and_section_menus_end_with_menu_button() -> None:
    root = root_menu_keyboard().inline_keyboard
    assert [row[0].callback_data for row in root] == [
        "menu:dash",
        "menu:pending",
        "menu:loads",
        "menu:vk",
        "menu",
    ]
    pending = pending_menu_keyboard().inline_keyboard
    assert [row[0].callback_data for row in pending] == [
        "pending:list",
        "genres",
        "pending:all",
        "update",
        "clear",
        "menu",
    ]
    loads = loads_menu_keyboard().inline_keyboard
    assert [row[0].callback_data for row in loads] == ["pause", "asktorrent", "menu"]
    vk_menu = vk_section_keyboard().inline_keyboard
    assert [row[0].callback_data for row in vk_menu] == ["vk:list", "vk:token", "menu"]


def test_dashboard_text_matches_portal_tiles() -> None:
    text = format_dashboard(
        {
            "pending": 3,
            "approved": 2,
            "rejected": 1,
            "loading": 4,
            "exporting": 5,
            "errors": 6,
        },
        "Токен VK действует ещё около 2 ч.",
    )
    assert "Ожидают одобрения: 3" in text
    assert "Одобрено: 2" in text
    assert "Отклонено: 1" in text
    assert "Загружаются: 4" in text
    assert "Выгружаются в ВК: 5" in text
    assert "Ошибки в журнале: 6" in text
    assert "Токен VK действует" in text


def test_pending_genre_filter_and_short_callbacks(tmp_path: Path) -> None:
    path = tmp_path / "seen.db"
    with SeenStore(path) as store:
        for title, genre in (
            ("Первый [2026, драма]", "драма"),
            ("Второй [2026, боевик]", "боевик"),
            ("Третий [2025, драма]", "драма"),
        ):
            store.add(
                FeedItem(
                    guid=title,
                    feed_name="demo",
                    title=title,
                    link="https://example.com",
                    published=None,
                    torrent_url=None,
                    rating=None,
                    quality=None,
                    genre=genre,
                )
            )
    all_text, all_markup = pending_view(path, 0)
    drama, drama_markup = pending_view(path, 0, genre="Драма")
    names = [row[0].text for row in genres_keyboard(["Боевик", "Драма"]).inline_keyboard[:-2]]
    assert names == ["Боевик", "Драма"]
    assert genres_keyboard(["Боевик", "Драма"]).inline_keyboard[1][0].callback_data == "genre:1"
    assert "Ждут решения: 3" in all_text
    assert all_markup.inline_keyboard[-1][0].callback_data == "menu"
    assert "Жанр: Драма" in drama
    assert "Ждут решения: 2" in drama
    labels = [row[0].text for row in drama_markup.inline_keyboard[:-1]]
    assert len(labels) == 2
    assert "Первый" in labels[0]
    assert "Третий" in labels[1]
    assert "Второй" not in "".join(labels)
