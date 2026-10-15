"""Пользователи портала. Пароли лежат в базе как хеш, не открытым текстом."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_NAME = "potok"
DEFAULT_PASSWORD = "potok"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_PBKDF2_ROUNDS = 120_000


class UserError(Exception):
    """Имя или пароль не подходят, либо действие сломает единственный вход."""


class UserStore:
    """Список тех, кто может открыть портал, и их сессии."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    name TEXT PRIMARY KEY,
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                )
                """
            )
            created = self._ensure_default(conn)
        if created:
            logger.info(
                "Создан пользователь %s. Пароль совпадает с именем. Смените его на /users.",
                DEFAULT_NAME,
            )

    def authenticate(self, name: str, password: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT salt, password_hash FROM users WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None:
            return False
        candidate = _hash_password(password, row["salt"])
        return hmac.compare_digest(candidate, row["password_hash"])

    def open_session(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute("INSERT INTO sessions (token, name) VALUES (?, ?)", (token, name))
        return token

    def user_by_token(self, token: str) -> str | None:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT name FROM sessions WHERE token = ?", (token,)).fetchone()
        if row is None:
            return None
        return str(row["name"])

    def close_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def list_users(self) -> tuple[str, ...]:
        with self._connect() as conn:
            rows = conn.execute("SELECT name FROM users ORDER BY name").fetchall()
        return tuple(str(row["name"]) for row in rows)

    def add_user(self, name: str, password: str) -> None:
        clean, secret = _credentials(name, password)
        salt = secrets.token_bytes(16)
        with self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE name = ?", (clean,)).fetchone()
            if exists is not None:
                raise UserError("Такой пользователь уже есть.")
            conn.execute(
                "INSERT INTO users (name, salt, password_hash) VALUES (?, ?, ?)",
                (clean, salt, _hash_password(secret, salt)),
            )

    def set_password(self, name: str, password: str) -> None:
        clean, secret = _credentials(name, password)
        salt = secrets.token_bytes(16)
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE users SET salt = ?, password_hash = ? WHERE name = ?",
                (salt, _hash_password(secret, salt), clean),
            )
            if updated.rowcount == 0:
                raise UserError("Пользователь не найден.")

    def remove_user(self, name: str) -> None:
        clean = _name(name)
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
            if count is None or int(count["total"]) <= 1:
                raise UserError("Нельзя удалить последнего пользователя.")
            deleted = conn.execute("DELETE FROM users WHERE name = ?", (clean,))
            if deleted.rowcount == 0:
                raise UserError("Пользователь не найден.")
            conn.execute("DELETE FROM sessions WHERE name = ?", (clean,))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_default(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
        if row is not None and int(row["total"]) > 0:
            return False
        salt = secrets.token_bytes(16)
        conn.execute(
            "INSERT INTO users (name, salt, password_hash) VALUES (?, ?, ?)",
            (DEFAULT_NAME, salt, _hash_password(DEFAULT_PASSWORD, salt)),
        )
        return True


def _credentials(name: str, password: str) -> tuple[str, str]:
    return _name(name), _password(password)


def _name(name: str) -> str:
    text = name.strip()
    if not _NAME_RE.fullmatch(text):
        raise UserError("Имя: от 1 до 32 знаков, только буквы, цифры, точка, _ и -.")
    return text


def _password(password: str) -> str:
    if not isinstance(password, str) or not 4 <= len(password) <= 128:
        raise UserError("Пароль должен быть от 4 до 128 символов.")
    if any(char in password for char in "\r\n"):
        raise UserError("Пароль должен быть одной строкой.")
    return password


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
