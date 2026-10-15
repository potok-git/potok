"""Чтение config.yaml."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

logger = logging.getLogger(__name__)

_INTERFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,14}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# Эти имена всегда идут через VPN. Из списка в настройках их убрать нельзя.
REQUIRED_VPN_DOMAINS = (
    "api.telegram.org",
    "rutracker.org",
    "rutracker.net",
    "rutracker.nl",
    "rutracker.ru",
    "static.rutracker.cc",
    "bt.rutracker.cc",
    "bt2.rutracker.cc",
    "bt3.rutracker.cc",
    "bt4.rutracker.cc",
)
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HTTP_SCHEMES = {"http", "https"}


class ConfigError(Exception):
    """Файл настроек отсутствует или заполнен неверно."""


@dataclass(frozen=True)
class VpnSettings:
    interface: str
    config_path: Path
    command_timeout_seconds: float


@dataclass(frozen=True)
class RoutingSettings:
    via_vpn: tuple[str, ...]
    via_direct: tuple[str, ...]
    vpn_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str


@dataclass(frozen=True)
class RssSettings:
    feeds: tuple[FeedSource, ...]
    timeout_seconds: float
    user_agent: str
    metadata_dir: Path
    poll_interval_minutes: int = 60


@dataclass(frozen=True)
class MetadataPatterns:
    """Регулярные выражения для полей в заголовке и описании записи."""

    rating: re.Pattern[str] | None
    quality: re.Pattern[str] | None
    genre: re.Pattern[str] | None


@dataclass(frozen=True)
class FilterSettings:
    min_rating: float | None
    max_age_days: int | None
    patterns: MetadataPatterns
    genres: tuple[str, ...] = ()
    quality_keep: str = "max"


@dataclass(frozen=True)
class CredentialSettings:
    """Имена переменных окружения. Сами пароль и логин в файл не пишутся."""

    username_env: str | None
    password_env: str | None


@dataclass(frozen=True)
class TelegramSettings:
    """Имена переменных с токеном бота и id чата. Сами значения в файл не пишутся."""

    token_env: str
    chat_id_env: str
    timeout_seconds: float = 30


@dataclass(frozen=True)
class VkSettings:
    """Загрузка в VK Video. Токен только в окружении."""

    api_version: str
    token_env: str
    group_id: int | None
    wallpost: bool
    is_private: bool
    privacy_view: str
    chunk_bytes: int
    max_bytes: int
    upload_idle_timeout_seconds: float
    upload_attempts: int
    poll_interval_seconds: float
    poll_timeout_seconds: float
    api_timeout_seconds: float
    api_retries: int
    app_id: int | None = None


@dataclass(frozen=True)
class DeliverySettings:
    """Куда aria2c кладёт видео после одобрения."""

    download_dir: Path
    aria2_bin: str
    aria2_timeout_seconds: float


@dataclass(frozen=True)
class StreamingSettings:
    """qBittorrent. Пароль только в окружении, не в файле."""

    enabled: bool
    base_url: str
    public_url: str
    timeout_seconds: float
    auto_add: bool
    server_torrent_dir: str
    portal_host: str
    portal_port: int
    users_path: Path
    username_env: str
    password_env: str


@dataclass(frozen=True)
class JackettSettings:
    """Адреса Jackett. Сам ключ лежит в переменной окружения, не в файле."""

    base_url: str
    rss_url: str
    api_key: str
    api_key_env: str
    min_seeders: int
    min_size_bytes: int
    timeout_seconds: float
    page_count: int


@dataclass(frozen=True)
class TmdbSettings:
    """Поиск обложки и описания. Сам ключ лежит в переменной окружения, не в файле."""

    api_key_env: str
    timeout_seconds: float


def data_dir_for(database_path: Path) -> Path:
    """Папка данных: секреты и очередь остаются рядом с data/, не в data/db/."""
    parent = database_path.parent
    if parent.name == "db":
        return parent.parent
    return parent


@dataclass(frozen=True)
class AppConfig:
    vpn: VpnSettings
    routing: RoutingSettings
    log_level: str
    rss: RssSettings
    filters: FilterSettings
    database_path: Path
    credentials: CredentialSettings
    telegram: TelegramSettings
    vk: VkSettings
    delivery: DeliverySettings
    streaming: StreamingSettings
    jackett: JackettSettings
    tmdb: TmdbSettings

    def data_dir(self) -> Path:
        return data_dir_for(self.database_path)


def load_config(path: Path) -> AppConfig:
    """Прочитать YAML и проверить поля, без которых переключение VPN небезопасно."""
    if not path.is_file():
        raise ConfigError(f"Файл настроек не найден: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Не удалось разобрать {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} должен содержать словарь настроек.")

    vpn_raw = _section(raw, "vpn", path)
    routing_raw = _section(raw, "routing", path)
    logging_raw = _optional_dict(raw, "logging", path)
    rss_raw = _optional_dict(raw, "rss", path)
    filters_raw = _optional_dict(raw, "filters", path)
    database_raw = _optional_dict(raw, "database", path)
    credentials_raw = _optional_dict(raw, "credentials", path)
    telegram_raw = _optional_dict(raw, "telegram", path)
    vk_raw = _optional_dict(raw, "vk", path)
    delivery_raw = _optional_dict(raw, "delivery", path)
    streaming_raw = _optional_dict(raw, "streaming", path)
    jackett_raw = _optional_dict(raw, "jackett", path)
    tmdb_raw = _optional_dict(raw, "tmdb", path)

    interface = _required_str(vpn_raw, "interface", path)
    if not _INTERFACE_RE.fullmatch(interface):
        raise ConfigError(f"Имя интерфейса {interface!r} недопустимо. Пример: wg0.")

    config_path = Path(_required_str(vpn_raw, "config_path", path))
    timeout = vpn_raw.get("command_timeout_seconds", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("vpn.command_timeout_seconds должен быть числом больше нуля.")

    log_level = str(logging_raw.get("level", "INFO")).upper()
    if log_level not in _LOG_LEVELS:
        raise ConfigError(f"logging.level должен быть одним из: {', '.join(sorted(_LOG_LEVELS))}.")

    config = AppConfig(
        vpn=VpnSettings(
            interface=interface,
            config_path=config_path,
            command_timeout_seconds=float(timeout),
        ),
        routing=RoutingSettings(
            via_vpn=_name_list(routing_raw, "via_vpn", path),
            via_direct=_name_list(routing_raw, "via_direct", path),
            vpn_domains=_domain_list(routing_raw, path),
        ),
        log_level=log_level,
        rss=_rss_settings(rss_raw, path),
        filters=_filter_settings(filters_raw, path),
        database_path=Path(_optional_str(database_raw, "path", "data/db/seen.db")),
        credentials=_credentials(credentials_raw, path),
        telegram=_telegram(telegram_raw, path),
        vk=_vk_settings(vk_raw, path),
        delivery=_delivery_settings(delivery_raw, path),
        streaming=_streaming_settings(streaming_raw, path),
        jackett=_jackett_settings(jackett_raw, path),
        tmdb=_tmdb_settings(tmdb_raw, path),
    )
    logger.debug("Настройки прочитаны из %s", path)
    return config


def _rss_settings(section: dict, path: Path) -> RssSettings:
    timeout = section.get("timeout_seconds", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("rss.timeout_seconds должен быть числом больше нуля.")

    user_agent = section.get("user_agent", "Potok/0.2")
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise ConfigError("rss.user_agent должен быть непустой строкой.")

    metadata_dir = section.get("metadata_dir", "data/metadata")
    if not isinstance(metadata_dir, str) or not metadata_dir.strip():
        raise ConfigError("rss.metadata_dir должен быть непустой строкой.")

    feeds_raw = section.get("feeds", [])
    if feeds_raw is None:
        feeds_raw = []
    if not isinstance(feeds_raw, list):
        raise ConfigError(f"В {path} rss.feeds должен быть списком.")

    feeds: list[FeedSource] = []
    for index, item in enumerate(feeds_raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"В {path} лента #{index} должна быть словарём с name и url.")
        name = item.get("name")
        url = item.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"В {path} у ленты #{index} поле name должно быть строкой.")
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(f"В {path} у ленты #{index} поле url должно быть строкой.")
        parsed = urlparse(url.strip())
        if parsed.scheme not in _HTTP_SCHEMES or not parsed.netloc:
            raise ConfigError(
                f"Адрес ленты {name.strip()!r} должен начинаться с http:// или https://."
            )
        feeds.append(FeedSource(name=name.strip(), url=url.strip()))

    return RssSettings(
        feeds=tuple(feeds),
        timeout_seconds=float(timeout),
        user_agent=user_agent.strip(),
        metadata_dir=Path(metadata_dir.strip()),
        poll_interval_minutes=_bounded_int(
            section.get("poll_interval_minutes", 60),
            "rss.poll_interval_minutes",
            low=1,
            high=1440,
        ),
    )


_QUALITY_KEEP = frozenset({"max", "average", "min"})


def _quality_keep(section: dict) -> str:
    raw = section.get("quality_keep", "max")
    if raw is None or raw == "":
        return "max"
    if not isinstance(raw, str) or raw.strip() not in _QUALITY_KEEP:
        raise ConfigError(
            "filters.quality_keep: укажите max (максимальное), "
            "average (среднее) или min (минимальное)."
        )
    return raw.strip()


def _filter_settings(section: dict, path: Path) -> FilterSettings:
    min_rating = section.get("min_rating")
    if min_rating is not None:
        if isinstance(min_rating, bool) or not isinstance(min_rating, (int, float)):
            raise ConfigError("filters.min_rating должен быть числом или пустым.")
        if min_rating < 0:
            raise ConfigError("filters.min_rating не может быть отрицательным.")
        min_rating = float(min_rating)

    max_age = section.get("max_age_days")
    if max_age is not None:
        if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 1:
            raise ConfigError("filters.max_age_days должен быть целым числом от 1 или пустым.")

    patterns_raw = section.get("patterns", {})
    if patterns_raw is None:
        patterns_raw = {}
    if not isinstance(patterns_raw, dict):
        raise ConfigError(f"В {path} filters.patterns должен быть словарём.")

    return FilterSettings(
        min_rating=min_rating,
        max_age_days=max_age,
        patterns=MetadataPatterns(
            rating=_compile_pattern(patterns_raw.get("rating"), "rating", path),
            quality=_compile_pattern(patterns_raw.get("quality"), "quality", path),
            genre=_compile_pattern(patterns_raw.get("genre"), "genre", path),
        ),
        genres=_genres(section),
        quality_keep=_quality_keep(section),
    )


def _telegram(section: dict, path: Path) -> TelegramSettings:
    token_env = _env_name(
        section.get("token_env", "POTOK_TELEGRAM_TOKEN"),
        "token_env",
        path,
        section="telegram",
    )
    chat_id_env = _env_name(
        section.get("chat_id_env", "POTOK_TELEGRAM_CHAT_ID"),
        "chat_id_env",
        path,
        section="telegram",
    )
    return TelegramSettings(
        token_env=token_env or "POTOK_TELEGRAM_TOKEN",
        chat_id_env=chat_id_env or "POTOK_TELEGRAM_CHAT_ID",
        timeout_seconds=_bounded_number(
            section.get("timeout_seconds", 30),
            "telegram.timeout_seconds",
            low=5,
            high=120,
        ),
    )


def _vk_settings(section: dict, path: Path) -> VkSettings:
    token_env = _env_name(
        section.get("token_env", "POTOK_VK_TOKEN"),
        "token_env",
        path,
        section="vk",
    )
    return VkSettings(
        api_version=_api_version(section.get("api_version", "5.131")),
        token_env=token_env or "POTOK_VK_TOKEN",
        group_id=_group_id(section.get("group_id")),
        wallpost=_bool_value(section.get("wallpost", False), "vk.wallpost"),
        is_private=_bool_value(section.get("is_private", False), "vk.is_private"),
        privacy_view=_privacy_view(section.get("privacy_view", "by_link")),
        chunk_bytes=_bounded_int(
            section.get("chunk_bytes", 8 * 1024 * 1024),
            "vk.chunk_bytes",
            low=256 * 1024,
            high=32 * 1024 * 1024,
        ),
        max_bytes=_bounded_int(
            section.get("max_bytes", 15 * 1024**3),
            "vk.max_bytes",
            low=1,
            high=256 * 1024**3,
        ),
        upload_idle_timeout_seconds=_bounded_number(
            section.get("upload_idle_timeout_seconds", 300),
            "vk.upload_idle_timeout_seconds",
            low=30,
            high=3600,
        ),
        upload_attempts=_bounded_int(
            section.get("upload_attempts", 3),
            "vk.upload_attempts",
            low=1,
            high=5,
        ),
        poll_interval_seconds=_bounded_number(
            section.get("poll_interval_seconds", 15),
            "vk.poll_interval_seconds",
            low=5,
            high=600,
        ),
        poll_timeout_seconds=_bounded_number(
            section.get("poll_timeout_seconds", 6 * 60 * 60),
            "vk.poll_timeout_seconds",
            low=30,
            high=48 * 60 * 60,
        ),
        api_timeout_seconds=_bounded_number(
            section.get("api_timeout_seconds", 30),
            "vk.api_timeout_seconds",
            low=5,
            high=120,
        ),
        api_retries=_bounded_int(
            section.get("api_retries", 4),
            "vk.api_retries",
            low=1,
            high=8,
        ),
        app_id=_app_id(section.get("app_id")),
    )


def _delivery_settings(section: dict, path: Path) -> DeliverySettings:
    del path
    download_dir = section.get("download_dir", "data/downloads")
    if not isinstance(download_dir, str) or not download_dir.strip():
        raise ConfigError("delivery.download_dir должен быть непустой строкой.")
    binary = section.get("aria2_bin", "aria2c")
    if not isinstance(binary, str) or not binary.strip():
        raise ConfigError("delivery.aria2_bin должен быть непустой строкой.")
    timeout = section.get("aria2_timeout_seconds", 0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
        raise ConfigError("delivery.aria2_timeout_seconds должен быть числом от нуля.")
    return DeliverySettings(
        download_dir=Path(download_dir.strip()),
        aria2_bin=binary.strip(),
        aria2_timeout_seconds=float(timeout),
    )


def _streaming_settings(section: dict, path: Path) -> StreamingSettings:
    return StreamingSettings(
        enabled=_bool_value(section.get("enabled", False), "streaming.enabled"),
        base_url=_http_url(
            section.get("base_url", "http://qbittorrent:8080"),
            "streaming.base_url",
        ),
        public_url=_http_url(
            section.get("public_url", "http://127.0.0.1:8090"),
            "streaming.public_url",
        ),
        timeout_seconds=_bounded_number(
            section.get("timeout_seconds", 20),
            "streaming.timeout_seconds",
            low=5,
            high=120,
        ),
        auto_add=_bool_value(section.get("auto_add", False), "streaming.auto_add"),
        server_torrent_dir=_container_dir(
            section.get("server_torrent_dir", "/opt/ts/inbox"),
            "streaming.server_torrent_dir",
        ),
        portal_host=_host(section.get("portal_host", "0.0.0.0")),
        portal_port=_bounded_int(
            section.get("portal_port", 8080),
            "streaming.portal_port",
            low=1,
            high=65535,
        ),
        users_path=Path(_optional_str(section, "users_path", "data/portal_users.db")),
        username_env=_env_name(
            section.get("username_env", "POTOK_STREAM_USER"),
            "username_env",
            path,
            section="streaming",
        )
        or "POTOK_STREAM_USER",
        password_env=_env_name(
            section.get("password_env", "POTOK_STREAM_PASSWORD"),
            "password_env",
            path,
            section="streaming",
        )
        or "POTOK_STREAM_PASSWORD",
    )


_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def _http_url(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} должен быть адресом http:// или https://.")
    text = value.strip().rstrip("/")
    parsed = urlparse(text)
    if (
        parsed.scheme not in _HTTP_SCHEMES
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ConfigError(f"{key} должен быть адресом http:// или https:// без логина в ссылке.")
    return text


def _container_dir(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} должен быть путём внутри контейнера, например /opt/ts/inbox.")
    text = value.strip().rstrip("/")
    if not text.startswith("/opt/ts/") or ".." in text.split("/"):
        raise ConfigError(f"{key} должен начинаться с /opt/ts/ и не содержать '..'.")
    return text


def _host(value: object) -> str:
    if not isinstance(value, str) or not _HOST_RE.fullmatch(value.strip()):
        raise ConfigError("streaming.portal_host должен быть адресом вроде 0.0.0.0 или 127.0.0.1.")
    return value.strip()


def _jackett_settings(section: dict, path: Path) -> JackettSettings:
    base_url = section.get("jackett_base_url", section.get("base_url", "http://jackett:9117"))
    rss_raw = section.get("jackett_rss_url", section.get("rss_url", ""))
    if rss_raw is None:
        rss_raw = ""
    if not isinstance(rss_raw, str):
        raise ConfigError(f"В {path} jackett.jackett_rss_url должен быть строкой.")
    rss_url = rss_raw.strip()
    if rss_url:
        rss_url = _http_url(rss_url, "jackett.jackett_rss_url")
    api_key = section.get("jackett_api_key", section.get("api_key", ""))
    if api_key is None:
        api_key = ""
    if not isinstance(api_key, str):
        raise ConfigError(f"В {path} jackett.jackett_api_key должен быть строкой.")
    api_key_env = _env_name(
        section.get("api_key_env", "JACKETT_API_KEY"),
        "api_key_env",
        path,
        section="jackett",
    )
    return JackettSettings(
        base_url=_http_url(base_url, "jackett.jackett_base_url"),
        rss_url=rss_url,
        api_key=api_key.strip(),
        api_key_env=api_key_env or "JACKETT_API_KEY",
        min_seeders=_bounded_int(
            section.get("min_seeders", 1),
            "jackett.min_seeders",
            low=0,
            high=100_000,
        ),
        min_size_bytes=_bounded_int(
            section.get("min_size_bytes", 0),
            "jackett.min_size_bytes",
            low=0,
            high=10 * 1024**4,
        ),
        timeout_seconds=_bounded_number(
            section.get("timeout_seconds", 30),
            "jackett.timeout_seconds",
            low=5,
            high=120,
        ),
        page_count=_bounded_int(
            section.get("page_count", 1),
            "jackett.page_count",
            low=1,
            high=20,
        ),
    )


def _tmdb_settings(section: dict, path: Path) -> TmdbSettings:
    api_key_env = _env_name(
        section.get("api_key_env", "TMDB_API_KEY"),
        "api_key_env",
        path,
        section="tmdb",
    )
    return TmdbSettings(
        api_key_env=api_key_env or "TMDB_API_KEY",
        timeout_seconds=_bounded_number(
            section.get("timeout_seconds", 20),
            "tmdb.timeout_seconds",
            low=5,
            high=60,
        ),
    )


def _credentials(section: dict, path: Path) -> CredentialSettings:
    return CredentialSettings(
        username_env=_env_name(section.get("username_env"), "username_env", path),
        password_env=_env_name(section.get("password_env"), "password_env", path),
    )


def _api_version(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, bool):
        text = ""
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        text = ""
    if not re.fullmatch(r"5\.\d+", text):
        raise ConfigError('vk.api_version должен быть версией API, например "5.131".')
    return text


_PRIVACY_VIEW = frozenset({"all", "friends", "friends_of_friends", "only_me", "nobody", "by_link"})


def _privacy_view(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            "vk.privacy_view должен быть строкой. Для «у кого есть ссылка» укажите by_link."
        )
    text = value.strip()
    if text not in _PRIVACY_VIEW:
        allowed = ", ".join(sorted(_PRIVACY_VIEW))
        raise ConfigError(f"vk.privacy_view должен быть одним из: {allowed}.")
    return text


def _group_id(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError("vk.group_id должен быть положительным числом сообщества или пустым.")
    return value


def _app_id(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2_000_000_000:
        raise ConfigError("vk.app_id должен быть положительным числом приложения или пустым.")
    return value


def _bool_value(value: object, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} должен быть true или false.")
    return value


def _bounded_int(value: object, key: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} должен быть целым числом.")
    if not low <= value <= high:
        raise ConfigError(f"{key} должен быть от {low} до {high}.")
    return value


def _bounded_number(value: object, key: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} должен быть числом.")
    number = float(value)
    if not low <= number <= high:
        raise ConfigError(f"{key} должен быть от {low:g} до {high:g}.")
    return number


def _genres(section: dict) -> tuple[str, ...]:
    raw = section.get("genres", [])
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        raise ConfigError("filters.genres должен быть списком названий.")
    if len(raw) > 40:
        raise ConfigError("filters.genres: не больше 40 жанров.")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("filters.genres должен содержать непустые строки.")
        name = item.strip()
        if len(name) > 40:
            raise ConfigError("Название жанра длиннее 40 символов.")
        names.append(name)
    return tuple(names)


def _compile_pattern(value: object, key: str, path: Path) -> re.Pattern[str] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"В {path} filters.patterns.{key} должен быть строкой.")
    text = value.strip()
    if not text:
        return None
    try:
        return re.compile(text)
    except re.error as exc:
        raise ConfigError(f"В {path} filters.patterns.{key} — неверное выражение: {exc}") from exc


def _env_name(value: object, key: str, path: Path, *, section: str = "credentials") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"В {path} {section}.{key} должен быть строкой.")
    text = value.strip()
    if not text:
        return None
    if not _ENV_NAME_RE.fullmatch(text):
        raise ConfigError(
            f"{section}.{key} должен быть именем переменной окружения, например POTOK_FEED_USER."
        )
    return text


def _section(raw: dict, name: str, path: Path) -> dict:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"В {path} нет раздела {name}.")
    return section


def _optional_dict(raw: dict, name: str, path: Path) -> dict:
    section = raw.get(name, {})
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"В {path} раздел {name} должен быть словарём.")
    return section


def _optional_str(section: dict, key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Поле {key} должно быть непустой строкой.")
    return value.strip()


def _required_str(section: dict, key: str, path: Path) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"В {path} поле {key} должно быть непустой строкой.")
    return value.strip()


def normalize_domain(value: str) -> str:
    """Имя сайта латиницей, без схемы и пути. Пример: kinozal.tv."""
    text = value.strip().lower()
    if "://" in text:
        text = urlparse(text).hostname or ""
    text = text.split("/", 1)[0].split(":", 1)[0].strip(".")
    if not text or not _DOMAIN_RE.fullmatch(text):
        raise ConfigError(f"Домен «{value.strip()}» не похож на имя сайта. Пример: kinozal.tv.")
    return text


def combined_vpn_domains(extra: tuple[str, ...]) -> tuple[str, ...]:
    """Обязательные домены плюс то, что дописали в настройках. Повторы убираются."""
    seen: list[str] = []
    for name in (*REQUIRED_VPN_DOMAINS, *extra):
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def _domain_list(section: dict, path: Path) -> tuple[str, ...]:
    """Дополнительные домены. Пустой список допустим: обязательные всё равно остаются."""
    if "vpn_domains" not in section or section.get("vpn_domains") in (None, []):
        return ()
    value = section["vpn_domains"]
    if isinstance(value, str):
        raw_items = [part.strip() for part in value.replace(";", ",").split(",")]
    elif isinstance(value, list):
        raw_items = value
    else:
        raise ConfigError(f"В {path} поле vpn_domains должно быть списком.")
    names: list[str] = []
    for item in raw_items:
        if isinstance(item, str) and not item.strip():
            continue
        if not isinstance(item, str):
            raise ConfigError(f"В {path} элементы vpn_domains должны быть строками.")
        name = normalize_domain(item)
        if name in REQUIRED_VPN_DOMAINS or name in names:
            continue
        names.append(name)
    return tuple(names)


def _name_list(section: dict, key: str, path: Path) -> tuple[str, ...]:
    value = section.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"В {path} список {key} не должен быть пустым.")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"В {path} элементы {key} должны быть строками.")
        names.append(item.strip())
    return tuple(names)
