"""Запись config.yaml и секретов из формы портала."""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from potok.config import REQUIRED_VPN_DOMAINS, ConfigError, load_config, normalize_domain
from potok.jackett_cats import apply_categories, category_id_set, selected_category_ids
from potok.secrets import WG_SECRET, SecretStore

_HEADER = "# Настройки Потока. Секреты в этот файл не записываются.\n"
_SECRET_FIELDS = {
    "feed_username": ("credentials", "username_env", "POTOK_FEED_USER"),
    "feed_password": ("credentials", "password_env", "POTOK_FEED_PASSWORD"),
    "telegram_token": ("telegram", "token_env", "POTOK_TELEGRAM_TOKEN"),
    "telegram_chat_id": ("telegram", "chat_id_env", "POTOK_TELEGRAM_CHAT_ID"),
    "vk_token": ("vk", "token_env", "POTOK_VK_TOKEN"),
    "stream_username": ("streaming", "username_env", "POTOK_STREAM_USER"),
    "stream_password": ("streaming", "password_env", "POTOK_STREAM_PASSWORD"),
    "jackett_api_key": ("jackett", "api_key_env", "JACKETT_API_KEY"),
    "tmdb_api_key": ("tmdb", "api_key_env", "TMDB_API_KEY"),
}


class SettingsError(Exception):
    """Форма заполнена так, что настройки нельзя сохранить."""


def resolve_wg_path(config_path: Path, configured: Path) -> Path:
    """Куда писать wg0.conf. В контейнере это /etc/potok, на Windows — файл рядом с yaml."""
    if configured.parent.is_dir():
        return configured
    return config_path.parent / configured.name


def read_wireguard(path: Path, secrets: SecretStore) -> str:
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SettingsError(f"Не удалось прочитать {path.name}: {exc}") from exc
    return secrets.get(WG_SECRET) or ""


def apply_settings(
    raw: dict, form: Mapping[str, str | list[str]]
) -> tuple[dict, dict[str, str], str | None]:
    """Собрать новый словарь настроек, секреты и текст WireGuard. Файл ещё не трогаем."""
    updated = copy.deepcopy(raw)
    if not isinstance(updated, dict):
        raise SettingsError("config.yaml должен содержать словарь настроек.")
    _apply_text(updated, form)
    secrets = _secrets(updated, form)
    wireguard = _wireguard(form)
    return updated, secrets, wireguard


def save_config(path: Path, raw: dict) -> None:
    """Записать yaml и сразу проверить его. При ошибке вернуть прежний текст."""
    text = _HEADER + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    _check_text(path, text)
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    _write_in_place(path, text)
    try:
        load_config(path)
    except ConfigError as exc:
        if previous is not None:
            _write_in_place(path, previous)
        raise SettingsError(str(exc)) from exc


def save_vk_id(path: Path, field: str, value: int) -> None:
    """Записать vk.app_id или vk.group_id в config.yaml и сразу перечитать файл."""
    if field not in {"app_id", "group_id"}:
        raise SettingsError("Можно записать только ID приложения или сообщества.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"Не удалось прочитать config.yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise SettingsError("config.yaml должен содержать словарь настроек.")
    vk = raw.get("vk")
    if not isinstance(vk, dict):
        vk = {}
        raw["vk"] = vk
    vk[field] = value
    save_config(path, raw)


def save_wireguard(path: Path, text: str, secrets: SecretStore) -> None:
    cleaned = validate_wg(text)
    secrets.set_many({WG_SECRET: cleaned})
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_in_place(path, cleaned)
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def validate_wg(text: str) -> str:
    if "\x00" in text:
        raise SettingsError("В конфиге WireGuard есть недопустимые символы.")
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        raise SettingsError("Конфиг WireGuard пустой.")
    if len(cleaned) > 16_000:
        raise SettingsError("Конфиг WireGuard слишком большой.")
    if "[Interface]" not in cleaned or "PrivateKey" not in cleaned:
        raise SettingsError("В конфиге WireGuard нужны блок [Interface] и строка PrivateKey.")
    return cleaned + "\n"


def _apply_text(raw: dict, form: Mapping[str, str | list[str]]) -> None:
    if "poll_interval_minutes" in form:
        _section(raw, "rss")["poll_interval_minutes"] = _whole(
            _text(form, "poll_interval_minutes"), "Интервал опроса"
        )
    if "rss_timeout" in form:
        _section(raw, "rss")["timeout_seconds"] = _number(
            _text(form, "rss_timeout"), "Таймаут ленты"
        )
    if "user_agent" in form:
        agent = _text(form, "user_agent").strip()
        if not agent:
            raise SettingsError("Имя программы для ленты не должно быть пустым.")
        _section(raw, "rss")["user_agent"] = agent
    if "metadata_dir" in form:
        _section(raw, "rss")["metadata_dir"] = _project_path(
            _text(form, "metadata_dir"), "Папка метаданных"
        )
    if "feed_name" in form:
        _section(raw, "rss")["feeds"] = _feeds(form)

    if any(
        key in form
        for key in (
            "jackett_base_url",
            "jackett_rss_url",
            "jackett_min_seeders",
            "jackett_min_size_bytes",
            "jackett_page_count",
        )
    ):
        jackett = _section(raw, "jackett")
        jackett.setdefault("api_key_env", "JACKETT_API_KEY")
        jackett["jackett_api_key"] = ""
        if "jackett_base_url" in form:
            jackett["jackett_base_url"] = _text(form, "jackett_base_url").strip()
        if "jackett_rss_url" in form:
            url = _text(form, "jackett_rss_url").strip()
            if url and _optional(form, "jackett_cats_shown") == "1":
                base = category_id_set(_optional(form, "jackett_cat_base"))
                if selected_category_ids(url) == base:
                    url = apply_categories(url, _many(form, "jackett_cat"))
            jackett["jackett_rss_url"] = url
        if "jackett_min_seeders" in form:
            jackett["min_seeders"] = _whole(
                _text(form, "jackett_min_seeders"), "Минимум сидов Jackett"
            )
        if "jackett_min_size_bytes" in form:
            jackett["min_size_bytes"] = _whole(
                _text(form, "jackett_min_size_bytes"), "Минимальный размер Jackett"
            )
        if "jackett_page_count" in form:
            pages = _whole(_text(form, "jackett_page_count"), "Сколько страниц")
            if not 1 <= pages <= 20:
                raise SettingsError("Сколько страниц: укажите число от 1 до 20.")
            jackett["page_count"] = pages

    filters = _section(raw, "filters")
    if "min_rating" in form:
        filters["min_rating"] = _optional_number(_text(form, "min_rating"), "Минимальный рейтинг")
    if "max_age_days" in form:
        filters["max_age_days"] = _optional_whole(
            _text(form, "max_age_days"), "Максимальный возраст"
        )
    if "genres" in form:
        filters["genres"] = _split_list(_text(form, "genres"))
    if "quality_keep" in form:
        quality_keep = _text(form, "quality_keep").strip()
        if quality_keep not in ("max", "average", "min"):
            raise SettingsError(
                "Какое качество оставить: выберите максимальное, среднее или минимальное."
            )
        filters["quality_keep"] = quality_keep
    if any(key in form for key in ("pattern_rating", "pattern_quality", "pattern_genre")):
        patterns = filters.get("patterns")
        if not isinstance(patterns, dict):
            patterns = {}
            filters["patterns"] = patterns
        for key, field in (
            ("rating", "pattern_rating"),
            ("quality", "pattern_quality"),
            ("genre", "pattern_genre"),
        ):
            if field in form:
                patterns[key] = _text(form, field).strip() or None

    if "log_level" in form:
        _section(raw, "logging")["level"] = _text(form, "log_level").strip() or "INFO"
    if "database_path" in form:
        _section(raw, "database")["path"] = _project_path(_text(form, "database_path"), "Файл базы")
    if "via_vpn" in form:
        _section(raw, "routing")["via_vpn"] = _required_list(_text(form, "via_vpn"), "с VPN")
    if "via_direct" in form:
        _section(raw, "routing")["via_direct"] = _required_list(
            _text(form, "via_direct"), "без VPN"
        )
    if "vpn_domains" in form:
        _section(raw, "routing")["vpn_domains"] = _domain_names(_text(form, "vpn_domains"))

    if "interface" in form:
        _section(raw, "vpn")["interface"] = _text(form, "interface").strip()
    if "vpn_timeout" in form:
        _section(raw, "vpn")["command_timeout_seconds"] = _number(
            _text(form, "vpn_timeout"), "Таймаут VPN"
        )

    if "telegram_timeout" in form:
        _section(raw, "telegram")["timeout_seconds"] = _number(
            _text(form, "telegram_timeout"), "Таймаут Telegram"
        )

    if "settings_form" in form or "tmdb_api_key" in form:
        tmdb = _section(raw, "tmdb")
        tmdb["api_key_env"] = "TMDB_API_KEY"
        tmdb.setdefault("timeout_seconds", 20)
        tmdb.pop("api_key", None)

    vk = _section(raw, "vk")
    _vk_numbers(vk, form)
    if "api_version" in form:
        vk["api_version"] = _text(form, "api_version").strip()
    if "group_id" in form:
        text = _text(form, "group_id").strip()
        vk["group_id"] = None if not text else _whole(text, "ID сообщества VK")
    if "app_id" in form:
        text = _text(form, "app_id").strip()
        vk["app_id"] = None if not text else _whole(text, "ID приложения VK")
    if "privacy_view" in form:
        vk["privacy_view"] = _text(form, "privacy_view").strip()
    if "settings_form" in form:
        vk["wallpost"] = "wallpost" in form
        vk["is_private"] = "is_private" in form

    if "download_dir" in form:
        _section(raw, "delivery")["download_dir"] = _project_path(
            _text(form, "download_dir"), "Папка загрузок"
        )
    if "aria2_bin" in form:
        binary = _text(form, "aria2_bin").strip()
        if not binary:
            raise SettingsError("Имя программы aria2c не должно быть пустым.")
        _section(raw, "delivery")["aria2_bin"] = binary
    if "aria2_timeout" in form:
        _section(raw, "delivery")["aria2_timeout_seconds"] = _number(
            _text(form, "aria2_timeout"), "Таймаут aria2c"
        )

    streaming = _section(raw, "streaming")
    if "settings_form" in form:
        streaming["enabled"] = "streaming_enabled" in form
        streaming["auto_add"] = "auto_add" in form
    for field, key in (
        ("base_url", "base_url"),
        ("public_url", "public_url"),
        ("server_torrent_dir", "server_torrent_dir"),
        ("portal_host", "portal_host"),
    ):
        if field in form:
            streaming[key] = _text(form, field).strip()
    if "stream_timeout" in form:
        streaming["timeout_seconds"] = _number(_text(form, "stream_timeout"), "Таймаут qBittorrent")
    if "portal_port" in form:
        streaming["portal_port"] = _whole(_text(form, "portal_port"), "Порт портала")


def _vk_numbers(vk: dict, form: Mapping[str, str | list[str]]) -> None:
    whole = {
        "chunk_bytes": "Размер куска VK",
        "max_bytes": "Максимальный размер VK",
        "upload_attempts": "Попытки загрузки VK",
        "api_retries": "Повторы API VK",
    }
    floats = {
        "upload_idle_timeout_seconds": "Пауза загрузки VK",
        "poll_interval_seconds": "Интервал проверки VK",
        "poll_timeout_seconds": "Срок проверки VK",
        "api_timeout_seconds": "Таймаут API VK",
    }
    for field, label in whole.items():
        if field in form:
            vk[field] = _whole(_text(form, field), label)
    for field, label in floats.items():
        if field in form:
            vk[field] = _number(_text(form, field), label)


def _secrets(raw: dict, form: Mapping[str, str | list[str]]) -> dict[str, str]:
    updates: dict[str, str] = {}
    for field, (section, key, default) in _SECRET_FIELDS.items():
        if field not in form:
            continue
        value = _secret_text(_text(form, field), field)
        if not value:
            continue
        if field == "telegram_chat_id" and not value.lstrip("-").isdigit():
            raise SettingsError("Id чата Telegram должен быть числом.")
        updates[_env_name(raw, section, key, default)] = value
    return updates


def _wireguard(form: Mapping[str, str | list[str]]) -> str | None:
    if "wg_conf" not in form:
        return None
    text = _text(form, "wg_conf")
    if not text.strip():
        return None
    return validate_wg(text)


def _feeds(form: Mapping[str, str | list[str]]) -> list[dict[str, str]]:
    names = _many(form, "feed_name")
    urls = _many(form, "feed_url")
    if len(names) != len(urls):
        raise SettingsError("У каждой ленты должны быть имя и адрес.")
    feeds: list[dict[str, str]] = []
    for index, (name, url) in enumerate(zip(names, urls, strict=True), start=1):
        name = name.strip()
        url = url.strip()
        if not name and not url:
            continue
        if not name or not url:
            raise SettingsError(f"У ленты #{index} заполните и имя, и адрес.")
        feeds.append({"name": name, "url": url})
    if len(feeds) > 30:
        raise SettingsError("Лент не больше 30.")
    return feeds


def _optional(form: Mapping[str, str | list[str]], key: str) -> str:
    if key not in form:
        return ""
    return _text(form, key).strip()


def _text(form: Mapping[str, str | list[str]], key: str) -> str:
    value = form[key]
    if isinstance(value, list):
        return value[0] if value else ""
    return value


def _many(form: Mapping[str, str | list[str]], key: str) -> list[str]:
    value = form.get(key, "")
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _env_name(raw: dict, section: str, key: str, default: str) -> str:
    block = raw.get(section)
    if isinstance(block, dict):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _section(raw: dict, name: str) -> dict:
    block = raw.get(name)
    if not isinstance(block, dict):
        block = {}
        raw[name] = block
    return block


def _secret_text(value: str, label: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise SettingsError(f"Поле {label} должно быть одной строкой.")
    text = value.strip()
    if len(text) > 500:
        raise SettingsError(f"Поле {label} слишком длинное.")
    return text


def _project_path(value: str, label: str) -> str:
    text = value.strip().replace("\\", "/")
    parts = Path(text).parts
    if not text or text.startswith("/") or ".." in parts or ":" in text:
        raise SettingsError(f"{label}: укажите путь внутри проекта, например data/папка.")
    return text


def _domain_names(value: str) -> list[str]:
    """Дополнительные сайты. Telegram и Rutracker сюда не записываются: они и так обязательны."""
    names: list[str] = []
    for part in value.replace(";", ",").replace("\n", ",").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            name = normalize_domain(text)
        except ConfigError as exc:
            raise SettingsError(str(exc)) from exc
        if name in REQUIRED_VPN_DOMAINS or name in names:
            continue
        names.append(name)
    return names


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _required_list(value: str, label: str) -> list[str]:
    items = _split_list(value)
    if not items:
        raise SettingsError(f"Список «{label}» не должен быть пустым.")
    return items


def _optional_number(value: str, label: str) -> float | None:
    if not value.strip():
        return None
    return _number(value, label)


def _optional_whole(value: str, label: str) -> int | None:
    if not value.strip():
        return None
    return _whole(value, label)


def _whole(value: str, label: str) -> int:
    number = _number(value, label)
    if not number.is_integer():
        raise SettingsError(f"{label} должен быть целым числом.")
    return int(number)


def _number(value: str, label: str) -> float:
    try:
        return float(value.strip().replace(",", "."))
    except ValueError as exc:
        raise SettingsError(f"{label} должен быть числом.") from exc


def _check_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".checking")
    temporary.write_text(text, encoding="utf-8")
    try:
        load_config(temporary)
    except ConfigError as exc:
        raise SettingsError(str(exc)) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _write_in_place(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                return
    except OSError as exc:
        raise SettingsError(f"Не удалось записать {path.name}: {exc}") from exc


def portal_hint(before: dict, after: dict) -> str:
    """Подсказка, если сменился адрес самого портала."""
    old = before.get("streaming") if isinstance(before.get("streaming"), dict) else {}
    new = after.get("streaming") if isinstance(after.get("streaming"), dict) else {}
    if old.get("portal_port") != new.get("portal_port") or old.get("portal_host") != new.get(
        "portal_host"
    ):
        return " Новый адрес портала заработает после перезапуска контейнера portal."
    return ""
