"""Ссылка на разрешение VK и разбор адреса blank.html, который вставил пользователь."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from potok.secrets import SecretStore

_AUTHORIZE = "https://oauth.vk.ru/authorize"
_BLANK = "https://oauth.vk.ru/blank.html"
_VIDEO_GET = "https://api.vk.com/method/video.get"
_TOKEN = re.compile(r"^[A-Za-z0-9._-]{20,512}$")
EXPIRES_NAME = "POTOK_VK_TOKEN_EXPIRES_AT"
REMINDED_NAME = "POTOK_VK_TOKEN_REMINDED_AT"
REMIND_SECONDS = 3600


class VkOAuthError(Exception):
    """VK не выдал токен или не принял его для видео."""


def blank_authorize_url(*, app_id: int, api_version: str) -> str:
    """Страница разрешения. После неё VK открывает blank.html, токен остаётся в адресе."""
    query = urlencode(
        {
            "client_id": str(app_id),
            "display": "page",
            "redirect_uri": _BLANK,
            "scope": "video",
            "response_type": "token",
            "v": api_version,
            "revoke": "1",
        }
    )
    return f"{_AUTHORIZE}?{query}"


def clean_token(value: str) -> str:
    text = value.strip()
    if not _TOKEN.fullmatch(text):
        raise VkOAuthError(
            "В адресе нет токена. Скопируйте строку целиком, "
            "начиная с https://oauth.vk.ru/blank.html."
        )
    return text


def is_token_paste(value: str) -> bool:
    """Сообщение похоже на адрес blank.html или на голый токен."""
    compact = "".join(value.split())
    if "access_token=" in compact:
        return True
    return bool(_TOKEN.fullmatch(value.strip()))


def parse_token_input(value: str) -> tuple[str, int | None]:
    """Адрес с access_token= или голый токен. У голого токена срока нет."""
    compact = "".join(value.split())
    if "access_token=" in compact:
        return parse_blank_address(value)
    stripped = value.strip()
    if _TOKEN.fullmatch(stripped):
        return stripped, None
    raise VkOAuthError("Вставьте адрес целиком, начиная с https://oauth.vk.ru/blank.html.")


def parse_blank_address(value: str) -> tuple[str, int | None]:
    """Достать токен и срок из адреса, который пользователь скопировал с пустой страницы VK."""
    compact = "".join(value.split())
    if "access_token=" not in compact:
        raise VkOAuthError("Вставьте адрес целиком, начиная с https://oauth.vk.ru/blank.html.")
    if "://" not in compact:
        compact = "https://" + compact.lstrip("/")
    parts = urlsplit(compact)
    params = parse_qs(parts.fragment or parts.query, keep_blank_values=False)
    token = clean_token((params.get("access_token") or [""])[0])
    raw_expires = (params.get("expires_in") or [""])[0]
    if not raw_expires.isdigit():
        return token, None
    seconds = int(raw_expires)
    if seconds <= 0 or seconds > 366 * 24 * 3600:
        return token, None
    return token, seconds


def lifetime_note(seconds: int | None) -> str:
    """Короткая фраза для зелёной плашки. Сам токен в текст не попадает."""
    if seconds is None or seconds <= 0:
        return "Токен VK сохранён. В адресе не было срока."
    hours = max(1, round(seconds / 3600))
    return f"Токен VK сохранён. Он действует около {hours} ч."


@dataclass(frozen=True)
class TokenStatus:
    """Состояние токена для дашборда. kind: missing, unknown, ok, expired."""

    kind: str
    message: str


def describe_token(*, has_token: bool, expires_at: str | None, now: float) -> TokenStatus:
    if not has_token:
        return TokenStatus("missing", "Токен VK не задан. Видео загрузить нельзя.")
    if not expires_at or not expires_at.isdigit() or int(expires_at) <= 0:
        return TokenStatus(
            "unknown",
            "Токен VK задан, но срок неизвестен. Если загрузка отвечает кодом 5, обновите токен.",
        )
    left = int(expires_at) - int(now)
    if left <= 0:
        return TokenStatus(
            "expired",
            "Токен VK просрочен. Обновите его, иначе видео не загрузится.",
        )
    hours = max(1, round(left / 3600))
    return TokenStatus("ok", f"Токен VK действует ещё около {hours} ч.")


def check_video_token(
    token: str,
    *,
    api_version: str,
    timeout: float,
    opener=urlopen,
) -> None:
    """Убедиться, что токен вызывает video.get. Иначе в секреты его не пишем."""
    body = urlencode({"v": api_version, "count": "1"}).encode()
    call = Request(
        _VIDEO_GET,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "Potok/0.4",
        },
        method="POST",
    )
    try:
        with opener(call, timeout=timeout) as response:
            raw = response.read(100_000)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise VkOAuthError("VK не ответил. Проверьте связь и вставьте адрес ещё раз.") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VkOAuthError("VK вернул непонятный ответ. Вставьте адрес ещё раз.") from exc
    if not isinstance(payload, dict):
        raise VkOAuthError("VK вернул непонятный ответ. Вставьте адрес ещё раз.")
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("error_code")
        if code == 5:
            raise VkOAuthError(
                "VK не принял токен. Откройте страницу VK ещё раз и вставьте новый адрес."
            )
        if code == 15:
            raise VkOAuthError("У токена нет права на видео. В окне VK разрешите доступ к видео.")
        raise VkOAuthError(f"VK не принял токен, код {code}.")
    if "response" not in payload:
        raise VkOAuthError("VK вернул ответ без данных. Вставьте адрес ещё раз.")


def save_vk_token(
    value: str,
    *,
    token_env: str,
    api_version: str,
    timeout: float,
    store: SecretStore,
    now: float | None = None,
    opener=urlopen,
) -> str:
    """Проверить токен, записать его и срок. Worker не перезапускаем: токен сразу в окружении."""
    token, seconds = parse_token_input(value)
    check_video_token(token, api_version=api_version, timeout=timeout, opener=opener)
    moment = time.time() if now is None else now
    expires_at = "0" if seconds is None else str(int(moment) + seconds)
    store.set_many({token_env: token, EXPIRES_NAME: expires_at})
    os.environ[token_env] = token
    return lifetime_note(seconds)


def should_remind_token(*, expires_at: str | None, reminded_at: str | None, now: float) -> bool:
    """Одно напоминание на срок токена: за час до конца или сразу, если бот включился позже."""
    if not expires_at or not expires_at.isdigit() or int(expires_at) <= 0:
        return False
    if reminded_at == expires_at:
        return False
    return int(expires_at) - int(now) <= REMIND_SECONDS
