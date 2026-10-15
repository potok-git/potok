"""Общие предохранители для pytest.

Прогон тестов не должен писать в живой чат. Токен бота лежит в .env,
а команда demo внутри тестов этот файл подхватывает. Без заглушки
следующие тесты отправляют карточки с фальшивыми ссылками VK.
"""

import pytest


@pytest.fixture(autouse=True)
def _keep_tests_off_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POTOK_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("POTOK_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)

    def _drop(token: str, chat_id: str, text: str, timeout: float) -> None:
        del token, chat_id, text, timeout

    monkeypatch.setattr("potok.publish.send_telegram", _drop)
