import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from potok.secrets import SecretStore
from potok.vk_oauth import (
    EXPIRES_NAME,
    VkOAuthError,
    blank_authorize_url,
    check_video_token,
    describe_token,
    is_token_paste,
    lifetime_note,
    parse_blank_address,
    parse_token_input,
    save_vk_token,
    should_remind_token,
)

_SAMPLE = "vk1.a." + ("Ab3_" * 12)
_ADDRESS = "https://oauth.vk.ru/blank.html#access_token=" + _SAMPLE + "&expires_in=86400&user_id=1"


def test_blank_authorize_url_returns_to_blank_page() -> None:
    url = blank_authorize_url(app_id=12345678, api_version="5.131")
    query = parse_qs(urlsplit(url).query)

    assert urlsplit(url).netloc == "oauth.vk.ru"
    assert query["client_id"] == ["12345678"]
    assert query["redirect_uri"] == ["https://oauth.vk.ru/blank.html"]
    assert query["scope"] == ["video"]
    assert query["response_type"] == ["token"]


def test_parse_blank_address_reads_token_and_lifetime() -> None:
    token, seconds = parse_blank_address("  " + _ADDRESS.replace("&", "\n&"))
    assert token == _SAMPLE
    assert seconds == 86400
    try:
        parse_blank_address("просто токен")
    except VkOAuthError as exc:
        assert "адрес целиком" in str(exc)
    else:
        raise AssertionError("без access_token адрес не разбирается")


def test_lifetime_and_dashboard_status() -> None:
    assert "24 ч" in lifetime_note(86400)
    assert describe_token(has_token=False, expires_at=None, now=100).kind == "missing"
    assert describe_token(has_token=True, expires_at="50", now=100).kind == "expired"
    assert describe_token(has_token=True, expires_at="3700", now=100).kind == "ok"
    assert describe_token(has_token=True, expires_at=None, now=100).kind == "unknown"


class _Body:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self, limit: int) -> bytes:
        return json.dumps(self.payload).encode()[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def test_check_video_token_rejects_code_5() -> None:
    def opener(request, timeout):
        del request, timeout
        return _Body({"error": {"error_code": 5, "error_msg": "fail"}})

    try:
        check_video_token(_SAMPLE, api_version="5.131", timeout=5, opener=opener)
    except VkOAuthError as exc:
        assert "не принял" in str(exc)
    else:
        raise AssertionError("код 5 должен остановить запись")


def test_parse_token_input_accepts_bare_token() -> None:
    token, seconds = parse_token_input(_SAMPLE)
    assert token == _SAMPLE
    assert seconds is None
    assert is_token_paste(_ADDRESS) is True
    assert is_token_paste("слово") is False


def test_save_vk_token_writes_secret_without_echoing_it(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    store = SecretStore(tmp_path)

    def opener(request, timeout):
        del request, timeout
        return _Body({"response": {"count": 0, "items": []}})

    note = save_vk_token(
        _ADDRESS,
        token_env="POTOK_VK_TOKEN",
        api_version="5.131",
        timeout=5,
        store=store,
        now=1_000_000,
        opener=opener,
    )
    assert _SAMPLE not in note
    assert "24 ч" in note
    assert store.get("POTOK_VK_TOKEN") == _SAMPLE
    assert store.get(EXPIRES_NAME) == str(1_000_000 + 86400)
    assert os.environ.get("POTOK_VK_TOKEN") == _SAMPLE


def test_save_vk_token_rejects_vk_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("POTOK_VK_TOKEN", raising=False)
    store = SecretStore(tmp_path)

    def opener(request, timeout):
        del request, timeout
        return _Body({"error": {"error_code": 5, "error_msg": "fail"}})

    with pytest.raises(VkOAuthError, match="не принял"):
        save_vk_token(
            _ADDRESS,
            token_env="POTOK_VK_TOKEN",
            api_version="5.131",
            timeout=5,
            store=store,
            opener=opener,
        )
    assert store.get("POTOK_VK_TOKEN") is None
    assert os.environ.get("POTOK_VK_TOKEN") in (None, "")


def test_should_remind_token_once_per_expiry() -> None:
    assert should_remind_token(expires_at=None, reminded_at=None, now=100) is False
    assert should_remind_token(expires_at="0", reminded_at=None, now=100) is False
    assert should_remind_token(expires_at="200", reminded_at=None, now=100) is True
    assert should_remind_token(expires_at="4000", reminded_at=None, now=100) is False
    assert should_remind_token(expires_at="50", reminded_at=None, now=100) is True
    assert should_remind_token(expires_at="200", reminded_at="200", now=100) is False
