import sqlite3
from datetime import UTC, datetime

from potok.rss import FeedItem
from potok.store import SeenStore


def _item(guid: str = "demo:1") -> FeedItem:
    return FeedItem(
        guid=guid,
        feed_name="demo",
        title="Example",
        link="https://example.com/1",
        published=datetime(2026, 10, 8, tzinfo=UTC),
        torrent_url="https://example.com/a.torrent",
        rating=8.0,
        quality="1080p",
        genre="drama",
    )


def test_genre_and_country_are_saved_clean(tmp_path) -> None:
    path = tmp_path / "seen.db"
    dirty = _item()
    dirty = FeedItem(
        guid=dirty.guid,
        feed_name=dirty.feed_name,
        title=dirty.title,
        link=dirty.link,
        published=dirty.published,
        torrent_url=dirty.torrent_url,
        rating=dirty.rating,
        quality=dirty.quality,
        genre="боевик, США, UHD WEB-DL, Боевик",
        country="United States of America",
    )
    with SeenStore(path) as store:
        row_id = store.add(dirty)
        store.update_details(
            row_id,
            rating=7.0,
            genre="комедия, Австралия",
            overview="текст",
            poster_url=None,
            country="United States of America, Australia",
        )
        item = store.get(row_id)

    assert item is not None
    assert item.genre == "Комедия"
    assert item.country == "США, Австралия"

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE seen SET country = ?, genre = ? WHERE rowid = ?",
            ("United States of America", "триллер, 4K", row_id),
        )

    with SeenStore(path) as store:
        again = store.get(row_id)

    assert again is not None
    assert again.genre == "Триллер"
    assert again.country == "США"


def test_add_and_has(tmp_path) -> None:
    with SeenStore(tmp_path / "seen.db") as store:
        assert store.has("demo:1") is False
        store.add(_item())
        assert store.has("demo:1") is True
        assert store.has("demo:2") is False

    with SeenStore(tmp_path / "seen.db") as again:
        assert again.has("demo:1") is True


def test_decision_and_counts(tmp_path) -> None:
    with SeenStore(tmp_path / "seen.db") as store:
        row_id = store.add(_item())
        assert store.counts() == {"pending": 1, "approved": 0, "rejected": 0}
        pending = store.list_pending()
        assert [item.row_id for item in pending] == [row_id]
        assert pending[0].rating == 8.0

        decided = store.decide(row_id, "rejected")
        assert decided is not None
        assert decided.changed is True
        assert decided.item.status == "rejected"
        again = store.decide(row_id, "approved")
        assert again is not None
        assert again.changed is False
        assert again.item.status == "rejected"
        assert store.decide(999, "approved") is None
        assert store.list_pending() == []


def test_clear_queue_keeps_loaded_and_rejected(tmp_path) -> None:
    with SeenStore(tmp_path / "seen.db") as store:
        pending_id = store.add(_item("wait:1"))
        approved_id = store.add(_item("go:1"))
        loaded_id = store.add(_item("done:1"))
        rejected_id = store.add(_item("no:1"))
        store.decide(approved_id, "approved")
        store.decide(loaded_id, "approved")
        store.set_vk_url(loaded_id, "https://vk.com/video1")
        store.decide(rejected_id, "rejected")

        removed = store.clear_queue()

        assert {item.row_id for item in removed} == {pending_id, approved_id}
        assert store.has("wait:1") is False
        assert store.has("go:1") is False
        assert store.has("done:1") is True
        assert store.has("no:1") is True
        assert store.counts()["pending"] == 0
        assert store.clear_queue() == []


def test_delete_unapproved_keeps_approved(tmp_path) -> None:
    with SeenStore(tmp_path / "seen.db") as store:
        pending_id = store.add(_item("wait:1"))
        approved_id = store.add(_item("go:1"))
        rejected_id = store.add(_item("no:1"))
        store.decide(approved_id, "approved")
        store.decide(rejected_id, "rejected")

        removed = store.delete_unapproved()

        assert removed == 2
        assert store.has("wait:1") is False
        assert store.has("no:1") is False
        assert store.get(approved_id) is not None
        assert store.get(pending_id) is None


def test_reopen_returns_to_pending(tmp_path) -> None:
    with SeenStore(tmp_path / "seen.db") as store:
        row_id = store.add(_item())
        store.decide(row_id, "approved")
        assert store.reopen(row_id) is True
        assert store.reopen(row_id) is False
        rejected_id = store.add(_item("no"))
        store.decide(rejected_id, "rejected")
        assert store.reopen(rejected_id) is False
        assert store.reopen(rejected_id, only="rejected") is True
        restored = store.get(rejected_id)
        assert restored is not None
        assert restored.status == "pending"
        item = store.get(row_id)
        assert item is not None
        assert item.status == "pending"


def test_approved_page_is_newest_first(tmp_path) -> None:
    with SeenStore(tmp_path / "seen.db") as store:
        first = store.add(_item("old"))
        second = store.add(_item("new"))
        store.decide(first, "approved")
        store.decide(second, "approved")
        page = store.list_status_page("approved", limit=1, offset=0)
        assert [item.row_id for item in page] == [second]


def test_old_rows_stay_approved(tmp_path) -> None:
    path = tmp_path / "seen.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE seen (
            guid TEXT PRIMARY KEY,
            feed TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            torrent_url TEXT,
            first_seen TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO seen VALUES (?, ?, ?, ?, ?, ?)",
        ("old:1", "demo", "Old", "https://example.com", None, "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()
    connection.close()

    with SeenStore(path) as store:
        assert store.counts()["approved"] == 1
        assert store.list_pending() == []


def test_same_quality_keeps_the_copy_with_more_seeds(tmp_path) -> None:
    from potok.tmdb import release_key

    first = _item("copy:low")
    second = _item("copy:high")
    key = release_key(first.title, first.quality)
    with SeenStore(tmp_path / "seen.db") as store:
        store.add(first)
        low = _item("copy:low")
        low = FeedItem(
            guid=low.guid,
            feed_name=low.feed_name,
            title=low.title,
            link=low.link,
            published=low.published,
            torrent_url=low.torrent_url,
            rating=low.rating,
            quality=low.quality,
            genre=low.genre,
            seeders=2,
            size_bytes=1000,
        )
        high = FeedItem(
            guid="copy:high",
            feed_name=second.feed_name,
            title=second.title,
            link=second.link,
            published=second.published,
            torrent_url=second.torrent_url,
            rating=second.rating,
            quality=second.quality,
            genre=second.genre,
            seeders=20,
            size_bytes=2000,
            topic_url="https://rutracker.org/forum/viewtopic.php?t=1",
        )
        kept = store.keep_pending_release(key, low)
        assert kept is not None and kept[1] is False
        replaced = store.keep_pending_release(key, high)
        assert replaced is not None and replaced[1] is True
        waiting = store.list_pending()
        assert [item.guid for item in waiting] == ["copy:high"]
        assert waiting[0].seeders == 20
        assert waiting[0].topic_url.endswith("t=1")


def test_lower_quality_with_more_seeds_does_not_replace_higher(tmp_path) -> None:
    from potok.tmdb import release_key

    high = FeedItem(
        guid="copy:hd",
        feed_name="demo",
        title="Восставший / The Uprising [2026]",
        link="https://example.com/hd",
        published=datetime(2026, 10, 8, tzinfo=UTC),
        torrent_url="https://example.com/hd.torrent",
        rating=7.9,
        quality="1080p",
        genre="драма",
        seeders=36,
    )
    low = FeedItem(
        guid="copy:sd",
        feed_name="demo",
        title="Восставший / The Uprising [2026]",
        link="https://example.com/sd",
        published=datetime(2026, 10, 8, tzinfo=UTC),
        torrent_url="https://example.com/sd.torrent",
        rating=7.9,
        quality="720p",
        genre="драма",
        seeders=126,
    )
    key = release_key(high.title)
    with SeenStore(tmp_path / "seen.db") as store:
        store.add(high)
        kept = store.keep_pending_release(key, low, quality_keep="max")
        assert kept is not None and kept[1] is False
        waiting = store.list_pending()
        assert [item.guid for item in waiting] == ["copy:hd"]
        assert store.collapse_pending("max") == 0


def test_runtime_is_stored_and_old_base_gains_the_column(tmp_path) -> None:
    path = tmp_path / "seen.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE seen (
                guid TEXT PRIMARY KEY,
                feed TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                torrent_url TEXT,
                first_seen TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO seen (guid, feed, title, link, first_seen, status)
            VALUES (
                'old:1', 'demo', 'Old', 'https://example.com',
                '2026-01-01T00:00:00+00:00', 'pending'
            )
            """
        )

    with SeenStore(path) as store:
        row_id = store.add(_item())
        store.update_details(
            row_id,
            rating=8.0,
            genre="drama",
            overview="текст",
            poster_url=None,
            runtime_minutes=95,
            country="США",
        )
        item = store.get(row_id)
        old = store.list_pending_without_details()

    assert item is not None
    assert item.runtime_minutes == 95
    assert [row.guid for row in old] == ["old:1"]


def test_approved_torrent_is_not_inserted_again(tmp_path) -> None:
    path = tmp_path / "seen.db"
    with SeenStore(path) as store:
        row_id = store.add(_item("magnet:1"))
        store.decide(row_id, "approved")

    again = FeedItem(
        guid="magnet:1",
        feed_name="demo",
        title="Другой фильм / Other Name",
        link="https://example.com/2",
        published=datetime(2026, 10, 8, tzinfo=UTC),
        torrent_url="https://example.com/a.torrent",
        rating=7.0,
        quality="720p",
        genre="драма",
    )
    with SeenStore(path) as store:
        from potok.tmdb import release_key

        outcome = store.keep_pending_release(release_key(again.title, again.quality), again)
        assert outcome is None
        assert store.counts()["approved"] == 1
        assert store.counts()["pending"] == 0


def test_database_uses_wal_and_indexes(tmp_path) -> None:
    path = tmp_path / "seen.db"
    with SeenStore(path) as store:
        store.add(_item())

    conn = sqlite3.connect(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    names = {row[1] for row in conn.execute("PRAGMA index_list(seen)").fetchall()}
    conn.close()
    assert mode.lower() == "wal"
    assert "idx_seen_status" in names
    assert "idx_seen_release_key" in names
