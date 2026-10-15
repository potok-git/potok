"""SQLite: какие записи ленты уже обработаны и какое по ним решение."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from potok.filters import choose_copy
from potok.rss import FeedItem, genre_text, rutracker_topic
from potok.tmdb import release_key, stored_country

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
_DECISIONS = frozenset({APPROVED, REJECTED})
_KNOWN = frozenset({PENDING, APPROVED, REJECTED})


@dataclass(frozen=True)
class StoredItem:
    """Запись из базы вместе со статусом согласования."""

    row_id: int
    guid: str
    feed_name: str
    title: str
    link: str
    torrent_url: str | None
    first_seen: str
    status: str
    rating: float | None
    quality: str | None
    genre: str | None
    vk_url: str | None = None
    vk_uuid: str | None = None
    overview: str | None = None
    poster_url: str | None = None
    seeders: int | None = None
    size_bytes: int | None = None
    topic_url: str | None = None
    kinopoisk_url: str | None = None
    release_key: str = ""
    runtime_minutes: int | None = None
    movie_title: str | None = None
    country: str | None = None


@dataclass(frozen=True)
class Decision:
    """Результат нажатия кнопки. changed=False, если решение уже было."""

    item: StoredItem
    changed: bool


class SeenStore:
    """Запоминает guid, чтобы одна и та же запись не обрабатывалась дважды.

    Новая запись получает статус pending и ждёт кнопки в Telegram.
    Старые базы без колонки status считаются уже обработанными: статус approved.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _open_database(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                guid TEXT PRIMARY KEY,
                feed TEXT NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                torrent_url TEXT,
                first_seen TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                rating REAL,
                quality TEXT,
                genre TEXT,
                vk_url TEXT,
                vk_uuid TEXT,
                overview TEXT,
                poster_url TEXT,
                seeders INTEGER,
                size_bytes INTEGER,
                topic_url TEXT,
                kinopoisk_url TEXT,
                release_key TEXT,
                runtime_minutes INTEGER,
                movie_title TEXT,
                country TEXT
            )
            """
        )
        self._migrate()
        self._normalize_genres()
        self._normalize_countries()
        self._conn.commit()

    def _normalize_genres(self) -> None:
        """Убрать из уже сохранённых жанров страну, качество и повторы."""
        rows = self._conn.execute(
            "SELECT guid, genre FROM seen WHERE genre IS NOT NULL AND genre != ''"
        ).fetchall()
        for row in rows:
            cleaned = genre_text(str(row["genre"]))
            if cleaned == str(row["genre"]).strip():
                continue
            self._conn.execute(
                "UPDATE seen SET genre = ? WHERE guid = ?",
                (cleaned or None, row["guid"]),
            )

    def _normalize_countries(self) -> None:
        """Уже сохранённые страны перевести на русские названия."""
        rows = self._conn.execute(
            "SELECT guid, country FROM seen WHERE country IS NOT NULL AND country != ''"
        ).fetchall()
        for row in rows:
            cleaned = stored_country(str(row["country"]))
            if cleaned == str(row["country"]).strip():
                continue
            self._conn.execute(
                "UPDATE seen SET country = ? WHERE guid = ?",
                (cleaned, row["guid"]),
            )

    def has(self, guid: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM seen WHERE guid = ?", (guid,)).fetchone()
        return row is not None

    def add(self, item: FeedItem, *, status: str = PENDING) -> int:
        if status not in _KNOWN:
            raise ValueError(f"Неизвестный статус: {status}")
        seen_at = datetime.now(UTC).isoformat(timespec="seconds")
        key = release_key(item.title, item.quality)
        cursor = self._conn.execute(
            """
            INSERT INTO seen (
                guid, feed, title, link, torrent_url, first_seen,
                status, rating, quality, genre, overview, poster_url,
                seeders, size_bytes, topic_url, kinopoisk_url, release_key,
                runtime_minutes, movie_title, country
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.guid,
                item.feed_name,
                item.title,
                item.link,
                item.torrent_url,
                seen_at,
                status,
                item.rating,
                item.quality,
                _stored_genre(item.genre),
                item.overview,
                item.poster_url,
                item.seeders,
                item.size_bytes,
                item.topic_url,
                item.kinopoisk_url,
                key,
                item.runtime_minutes,
                item.movie_title,
                stored_country(item.country),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def genre_for_title(self, title: str) -> str | None:
        """Жанр последней записи с таким названием. Для ручной отправки файла в VK."""
        row = self._conn.execute(
            """
            SELECT genre
            FROM seen
            WHERE title = ? AND genre IS NOT NULL AND genre != ''
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (title,),
        ).fetchone()
        if row is None or row["genre"] is None:
            return None
        text = str(row["genre"]).strip()
        return text or None

    def get(self, row_id: int) -> StoredItem | None:
        row = self._conn.execute(
            f"SELECT {_SELECT} FROM seen WHERE rowid = ?",
            (row_id,),
        ).fetchone()
        if row is None:
            return None
        return _to_item(row)

    def decide(self, row_id: int, status: str) -> Decision | None:
        """Поставить approved или rejected, только если запись ещё pending."""
        if status not in _DECISIONS:
            raise ValueError("Решение может быть только approved или rejected.")
        current = self.get(row_id)
        if current is None:
            return None
        if current.status != PENDING:
            return Decision(item=current, changed=False)
        cursor = self._conn.execute(
            "UPDATE seen SET status = ? WHERE rowid = ? AND status = ?",
            (status, row_id, PENDING),
        )
        self._conn.commit()
        updated = self.get(row_id)
        if updated is None:
            return None
        return Decision(item=updated, changed=cursor.rowcount == 1)

    def reopen(self, row_id: int, *, only: str = APPROVED) -> bool:
        """Вернуть одобренную или отклонённую запись в ожидание."""
        if only not in {APPROVED, REJECTED}:
            raise ValueError("Вернуть в очередь можно только одобренную или отклонённую запись.")
        cursor = self._conn.execute(
            "UPDATE seen SET status = ? WHERE rowid = ? AND status = ?",
            (PENDING, row_id, only),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def counts(self) -> dict[str, int]:
        totals = {PENDING: 0, APPROVED: 0, REJECTED: 0}
        for row in self._conn.execute("SELECT status, COUNT(*) AS n FROM seen GROUP BY status"):
            totals[str(row["status"])] = int(row["n"])
        return totals

    def list_status(self, status: str) -> list[StoredItem]:
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT}
            FROM seen
            WHERE status = ?
            ORDER BY first_seen, rowid
            """,
            (status,),
        ).fetchall()
        return [_to_item(row) for row in rows]

    def set_vk_url(self, row_id: int, url: str) -> None:
        text = url.strip()
        if not text:
            raise ValueError("Ссылка VK пустая.")
        self._conn.execute("UPDATE seen SET vk_url = ? WHERE rowid = ?", (text, row_id))
        self._conn.commit()

    def set_vk_uuid(self, row_id: int, video_uuid: str) -> None:
        """UUID, которым ролик назван во VK. Несколько файлов — с новой строки."""
        text = video_uuid.strip()
        if not text:
            raise ValueError("UUID видео пустой.")
        self._conn.execute("UPDATE seen SET vk_uuid = ? WHERE rowid = ?", (text, row_id))
        self._conn.commit()

    def list_approved_without_vk(self) -> list[StoredItem]:
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT}
            FROM seen
            WHERE status = ?
              AND torrent_url IS NOT NULL
              AND torrent_url != ''
              AND (vk_url IS NULL OR vk_url = '')
            ORDER BY first_seen, rowid
            """,
            (APPROVED,),
        ).fetchall()
        return [_to_item(row) for row in rows]

    def delete_unapproved(self) -> int:
        """Удалить из очереди всё, что ещё не одобрено. Одобренные остаются."""
        cursor = self._conn.execute(
            "DELETE FROM seen WHERE status != ?",
            (APPROVED,),
        )
        self._conn.commit()
        return int(cursor.rowcount)

    def update_details(
        self,
        row_id: int,
        *,
        rating: float | None,
        genre: str | None,
        overview: str | None,
        poster_url: str | None,
        kinopoisk_url: str | None = None,
        runtime_minutes: int | None = None,
        movie_title: str | None = None,
        country: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE seen
            SET rating = ?, genre = ?, overview = ?, poster_url = ?,
                kinopoisk_url = COALESCE(?, kinopoisk_url),
                runtime_minutes = COALESCE(?, runtime_minutes),
                movie_title = COALESCE(?, movie_title),
                country = COALESCE(?, country)
            WHERE rowid = ?
            """,
            (
                rating,
                _stored_genre(genre),
                overview,
                poster_url,
                kinopoisk_url,
                runtime_minutes,
                movie_title,
                stored_country(country),
                row_id,
            ),
        )
        self._conn.commit()

    def clear_queue(self) -> list[StoredItem]:
        """Удалить очередь загрузки.

        Уходят записи, которые ещё ждут решения, и одобренные без ссылки VK.
        Отклонённые и уже загруженные в VK остаются в базе.
        """
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT}
            FROM seen
            WHERE status = ?
               OR (status = ? AND (vk_url IS NULL OR vk_url = ''))
            ORDER BY rowid
            """,
            (PENDING, APPROVED),
        ).fetchall()
        items = [_to_item(row) for row in rows]
        if not items:
            return []
        row_ids = [item.row_id for item in items]
        marks = ",".join("?" for _ in row_ids)
        self._conn.execute(f"DELETE FROM seen WHERE rowid IN ({marks})", row_ids)
        self._conn.commit()
        return items

    def list_seen(self) -> list[StoredItem]:
        """Все записи. Нужны, чтобы к ролику VK подставить карточку фильма."""
        rows = self._conn.execute(f"SELECT {_SELECT} FROM seen").fetchall()
        return [_to_item(row) for row in rows]

    def list_status_page(self, status: str, *, limit: int, offset: int) -> list[StoredItem]:
        """Страница записей одного статуса. Сверху самые новые."""
        if status not in _KNOWN:
            raise ValueError(f"Неизвестный статус: {status}")
        if offset < 0:
            offset = 0
        if limit < 1:
            limit = 1
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT}
            FROM seen
            WHERE status = ?
            ORDER BY first_seen DESC, rowid DESC
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        ).fetchall()
        return [_to_item(row) for row in rows]

    def list_pending(self, limit: int = 20, offset: int = 0) -> list[StoredItem]:
        if offset < 0:
            offset = 0
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT}
            FROM seen
            WHERE status = ?
            ORDER BY first_seen, rowid
            LIMIT ? OFFSET ?
            """,
            (PENDING, limit, offset),
        ).fetchall()
        return [_to_item(row) for row in rows]

    def list_pending_without_details(self, limit: int = 100) -> list[StoredItem]:
        """Записи очереди без описания, длительности или названия с обложки."""
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT}
            FROM seen
            WHERE status = ? AND (
                overview IS NULL OR runtime_minutes IS NULL
                OR (
                    poster_url IS NOT NULL AND poster_url != ''
                    AND (movie_title IS NULL OR movie_title = '')
                )
                OR country IS NULL
            )
            ORDER BY first_seen, rowid
            LIMIT ?
            """,
            (PENDING, limit),
        ).fetchall()
        return [_to_item(row) for row in rows]

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(seen)")}
        if "status" not in columns:
            self._conn.execute(
                "ALTER TABLE seen ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'"
            )
        if "rating" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN rating REAL")
        if "quality" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN quality TEXT")
        if "genre" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN genre TEXT")
        if "vk_url" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN vk_url TEXT")
        if "vk_uuid" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN vk_uuid TEXT")
        if "overview" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN overview TEXT")
        if "poster_url" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN poster_url TEXT")
        if "seeders" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN seeders INTEGER")
        if "size_bytes" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN size_bytes INTEGER")
        if "topic_url" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN topic_url TEXT")
        if "kinopoisk_url" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN kinopoisk_url TEXT")
        if "release_key" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN release_key TEXT")
        if "runtime_minutes" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN runtime_minutes INTEGER")
        if "movie_title" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN movie_title TEXT")
        if "country" not in columns:
            self._conn.execute("ALTER TABLE seen ADD COLUMN country TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_status ON seen(status)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_release_key ON seen(release_key)")
        self._fill_release_keys()

    def _fill_release_keys(self) -> None:
        rows = self._conn.execute("SELECT rowid, title, quality, release_key FROM seen").fetchall()
        for row in rows:
            key = release_key(str(row["title"]), row["quality"])
            if str(row["release_key"] or "") == key:
                continue
            self._conn.execute(
                "UPDATE seen SET release_key = ? WHERE rowid = ?",
                (key, row["rowid"]),
            )

    def collapse_pending(self, quality_keep: str = "max") -> int:
        """В очереди оставить одну копию каждого фильма."""
        rows = self._conn.execute(
            f"SELECT {_SELECT} FROM seen WHERE status = ?",
            (PENDING,),
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            key = release_key(str(row["title"]))
            groups.setdefault(key, []).append(row)
        removed = 0
        for key, group in groups.items():
            if len(group) < 2:
                continue
            best = choose_copy([_row_as_feed(row) for row in group], quality_keep)
            keep = next(row for row in group if str(row["guid"]) == best.guid)
            removed += len(group) - 1
            self._delete_pending_except(key, int(keep["rowid"]))
        return removed

    def pending_feeds(self, key: str) -> list[FeedItem]:
        """Ожидающие копии этого фильма, чтобы сравнить их с новой лентой."""
        return [_row_as_feed(row) for row in self._pending_rows(key)]

    def has_decided_release(self, key: str) -> bool:
        """Этот фильм уже одобрен или отклонён."""
        row = self._conn.execute(
            """
            SELECT 1 FROM seen
            WHERE release_key = ? AND status != ?
            LIMIT 1
            """,
            (key, PENDING),
        ).fetchone()
        return row is not None

    def pending_release_ready(self, key: str, item: FeedItem) -> bool:
        """В очереди уже одна эта раздача, и на карточке есть сиды, размер и ссылки."""
        rows = self._pending_rows(key)
        if len(rows) != 1:
            return False
        row = rows[0]
        if str(row["guid"]) != item.guid:
            return False
        if row["seeders"] is None and item.seeders is not None:
            return False
        if row["size_bytes"] is None and item.size_bytes is not None:
            return False
        if not row["topic_url"] and item.topic_url:
            return False
        if "/film/" not in str(row["kinopoisk_url"] or ""):
            return False
        stored = -1 if row["seeders"] is None else int(row["seeders"])
        incoming = -1 if item.seeders is None else item.seeders
        return incoming <= stored

    def drop_pending_release(self, key: str) -> None:
        self._conn.execute(
            "DELETE FROM seen WHERE release_key = ? AND status = ?",
            (key, PENDING),
        )
        self._conn.commit()

    def keep_pending_release(
        self,
        key: str,
        item: FeedItem,
        *,
        quality_keep: str = "max",
    ) -> tuple[int, bool] | None:
        """Оставить одну ожидающую копию фильма.

        Одобренная или отклонённая запись закрывает фильм: новые не добавляются.
        Качество выбирается по настройке. Среди копий одного качества остаётся
        раздача с большим числом сидов.
        Возвращает (row_id, создана_ли_новая). None — решение уже было.
        """
        known = self._guid_row(item.guid)
        if known is not None and str(known["status"]) != PENDING:
            return None
        if self.has_decided_release(key):
            self.drop_pending_release(key)
            return None
        pending = self._pending_rows(key)
        if not pending:
            if known is not None:
                self._conn.execute(
                    "UPDATE seen SET release_key = ? WHERE rowid = ?",
                    (key, int(known["rowid"])),
                )
                self._refresh_pending(int(known["rowid"]), item)
                return int(known["rowid"]), False
            return self.add(item), True
        by_guid = {str(row["guid"]): _row_as_feed(row) for row in pending}
        by_guid[item.guid] = item
        best = choose_copy(list(by_guid.values()), quality_keep)
        best_row = next((row for row in pending if str(row["guid"]) == best.guid), None)
        if best_row is None:
            self.drop_pending_release(key)
            again = self._guid_row(item.guid)
            if again is not None and str(again["status"]) != PENDING:
                return None
            if again is not None:
                self._conn.execute(
                    "UPDATE seen SET release_key = ? WHERE rowid = ?",
                    (key, int(again["rowid"])),
                )
                self._refresh_pending(int(again["rowid"]), item)
                return int(again["rowid"]), False
            return self.add(item), True
        self._delete_pending_except(key, int(best_row["rowid"]))
        if best.guid == item.guid:
            self._refresh_pending(int(best_row["rowid"]), item)
            return int(best_row["rowid"]), False
        if item.kinopoisk_url and not best_row["kinopoisk_url"]:
            self._conn.execute(
                "UPDATE seen SET kinopoisk_url = ? WHERE rowid = ?",
                (item.kinopoisk_url, int(best_row["rowid"])),
            )
            self._conn.commit()
        return int(best_row["rowid"]), False

    def _guid_row(self, guid: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT rowid, status, release_key FROM seen WHERE guid = ?",
            (guid,),
        ).fetchone()

    def _pending_rows(self, key: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            f"""
            SELECT {_SELECT}
            FROM seen
            WHERE release_key = ? AND status = ?
            ORDER BY rowid
            """,
            (key, PENDING),
        ).fetchall()

    def _delete_pending_except(self, key: str, keep_id: int) -> None:
        self._conn.execute(
            """
            DELETE FROM seen
            WHERE release_key = ? AND status = ? AND rowid != ?
            """,
            (key, PENDING, keep_id),
        )
        self._conn.commit()

    def _refresh_pending(self, row_id: int, item: FeedItem) -> None:
        current = self.get(row_id)
        if current is None:
            return
        seeders = item.seeders if item.seeders is not None else current.seeders
        size_bytes = item.size_bytes if item.size_bytes is not None else current.size_bytes
        country = item.country if item.country not in (None, "") else current.country
        self._conn.execute(
            """
            UPDATE seen
            SET seeders = ?, size_bytes = ?, topic_url = ?, kinopoisk_url = ?,
                rating = ?, genre = ?, overview = ?, poster_url = ?,
                runtime_minutes = COALESCE(?, runtime_minutes),
                movie_title = COALESCE(?, movie_title),
                country = COALESCE(?, country)
            WHERE rowid = ?
            """,
            (
                seeders,
                size_bytes,
                item.topic_url or current.topic_url,
                item.kinopoisk_url or current.kinopoisk_url,
                item.rating if item.rating is not None else current.rating,
                _stored_genre(item.genre or current.genre),
                item.overview or current.overview,
                item.poster_url or current.poster_url,
                item.runtime_minutes,
                item.movie_title,
                stored_country(country),
                row_id,
            ),
        )
        self._conn.commit()

    def __enter__(self) -> SeenStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_SELECT = (
    "rowid, guid, feed, title, link, torrent_url, first_seen, "
    "status, rating, quality, genre, vk_url, vk_uuid, overview, poster_url, "
    "seeders, size_bytes, topic_url, kinopoisk_url, release_key, "
    "runtime_minutes, movie_title, country"
)


def _stored_genre(value: str | None) -> str | None:
    """Жанр для базы: без страны, качества и повторов."""
    text = genre_text(value)
    return text or None


def _topic_url(value: object) -> str | None:
    """Старые карточки хранят адрес rutracker.org. Для кнопки нужен рабочий .ru."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return rutracker_topic(text) or text


def _open_database(path: Path) -> sqlite3.Connection:
    """Открыть базу. Короткий сбой диска не должен ронять цикл портала сразу."""
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(3):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(path, timeout=30)
            # WAL на диске Docker безопасен: сайт и worker пишут в одну базу.
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except sqlite3.OperationalError as exc:
            last_error = exc
            if conn is not None:
                conn.close()
            if attempt == 2:
                raise
            time.sleep(0.4)
    if last_error is not None:
        raise last_error
    raise sqlite3.OperationalError(f"Не удалось открыть базу: {path}")


def _row_as_feed(row: sqlite3.Row) -> FeedItem:
    return FeedItem(
        guid=str(row["guid"]),
        feed_name=str(row["feed"]),
        title=str(row["title"]),
        link=str(row["link"]),
        published=None,
        torrent_url=None if row["torrent_url"] is None else str(row["torrent_url"]),
        rating=None if row["rating"] is None else float(row["rating"]),
        quality=None if row["quality"] is None else str(row["quality"]),
        genre=None if row["genre"] is None else str(row["genre"]),
        seeders=None if row["seeders"] is None else int(row["seeders"]),
        size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
        topic_url=_topic_url(row["topic_url"]),
        kinopoisk_url=None if row["kinopoisk_url"] is None else str(row["kinopoisk_url"]),
    )


def _to_item(row: sqlite3.Row) -> StoredItem:
    rating = row["rating"]
    return StoredItem(
        row_id=int(row["rowid"]),
        guid=str(row["guid"]),
        feed_name=str(row["feed"]),
        title=str(row["title"]),
        link=str(row["link"]),
        torrent_url=None if row["torrent_url"] is None else str(row["torrent_url"]),
        first_seen=str(row["first_seen"]),
        status=str(row["status"]),
        rating=None if rating is None else float(rating),
        quality=None if row["quality"] is None else str(row["quality"]),
        genre=None if row["genre"] is None else str(row["genre"]),
        vk_url=None if row["vk_url"] is None else str(row["vk_url"]),
        vk_uuid=None if row["vk_uuid"] is None else str(row["vk_uuid"]),
        overview=None if row["overview"] is None else str(row["overview"]),
        poster_url=None if row["poster_url"] is None else str(row["poster_url"]),
        seeders=None if row["seeders"] is None else int(row["seeders"]),
        size_bytes=None if row["size_bytes"] is None else int(row["size_bytes"]),
        topic_url=_topic_url(row["topic_url"]),
        kinopoisk_url=None if row["kinopoisk_url"] is None else str(row["kinopoisk_url"]),
        release_key="" if row["release_key"] is None else str(row["release_key"]),
        runtime_minutes=None if row["runtime_minutes"] is None else int(row["runtime_minutes"]),
        movie_title=None if row["movie_title"] is None else str(row["movie_title"]),
        country=None if row["country"] is None else str(row["country"]),
    )
