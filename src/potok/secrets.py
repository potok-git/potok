"""Секреты портала: токены и пароли лежат в файле только в зашифрованном виде."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

WG_SECRET = "wireguard.conf"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretError(Exception):
    """Файл секретов не читается или ключ к нему не подходит."""


class SecretStore:
    """Пара файлов: secrets.key и secrets.enc в папке данных.

    В config.yaml значения не пишутся. Уже заданная переменная окружения
    важнее файла: так .env и явный запуск не затираются.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.key_path = directory / "secrets.key"
        self.data_path = directory / "secrets.enc"

    def session_key(self) -> bytes:
        """Постоянный ключ подписи cookie. Создаётся один раз."""
        return self._key()

    def names(self) -> set[str]:
        if not self.data_path.is_file():
            return set()
        return {key for key, value in self._read().items() if value}

    def get(self, name: str) -> str | None:
        if not self.data_path.is_file():
            return None
        value = self._read().get(name, "").strip()
        return value or None

    def set_many(self, updates: dict[str, str]) -> None:
        """Записать только непустые значения. Пустая строка секрет не стирает."""
        if not updates:
            return
        current = self._read() if self.data_path.is_file() else {}
        for key, value in updates.items():
            text = value.strip()
            if text:
                current[key] = text
        self._write(current)

    def apply_to_environ(self) -> None:
        """Подставить секреты в окружение, если переменная ещё пустая."""
        if not self.data_path.is_file():
            return
        for name, value in self._read().items():
            if name == WG_SECRET or not value or not _ENV_NAME.fullmatch(name):
                continue
            if os.environ.get(name, "").strip():
                continue
            os.environ[name] = value

    def _read(self) -> dict[str, str]:
        try:
            token = self.data_path.read_bytes()
            raw = Fernet(self._key()).decrypt(token)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, InvalidToken, json.JSONDecodeError, ValueError) as exc:
            raise SecretError(
                "Не удалось расшифровать data/secrets.enc. "
                "Ключ secrets.key должен быть от этого же файла."
            ) from exc
        if not isinstance(payload, dict):
            raise SecretError("Файл секретов повреждён.")
        result: dict[str, str] = {}
        for key, value in payload.items():
            if isinstance(key, str) and isinstance(value, str):
                result[key] = value
        return result

    def _write(self, payload: dict[str, str]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        encrypted = Fernet(self._key()).encrypt(raw)
        temporary = self.data_path.with_suffix(".enc.tmp")
        temporary.write_bytes(encrypted)
        _chmod(temporary)
        temporary.replace(self.data_path)
        _chmod(self.data_path)

    def _key(self) -> bytes:
        if not self.key_path.is_file():
            self.directory.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(Fernet.generate_key())
            _chmod(self.key_path)
            logger.info("Создан ключ шифрования секретов: %s", self.key_path.name)
        key = self.key_path.read_bytes().strip()
        if not key:
            raise SecretError("Файл secrets.key пустой.")
        return key


def _chmod(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        return
