from pathlib import Path

import pytest

from potok.config import ConfigError, combined_vpn_domains, data_dir_for, load_config

VALID = """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
  command_timeout_seconds: 15
routing:
  via_vpn:
    - rss
  via_direct:
    - downloads
logging:
  level: debug
"""


def test_load_config_ok(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID, encoding="utf-8")

    config = load_config(path)

    assert config.vpn.interface == "wg0"
    assert config.vpn.config_path == Path("/etc/potok/wg0.conf")
    assert config.vpn.command_timeout_seconds == 15
    assert config.routing.via_vpn == ("rss",)
    assert config.routing.via_direct == ("downloads",)
    assert config.log_level == "DEBUG"
    assert config.telegram.token_env == "POTOK_TELEGRAM_TOKEN"
    assert config.telegram.chat_id_env == "POTOK_TELEGRAM_CHAT_ID"
    assert config.vk.token_env == "POTOK_VK_TOKEN"
    assert config.vk.privacy_view == "by_link"
    assert config.vk.max_bytes == 15 * 1024**3
    assert config.vk.chunk_bytes == 8 * 1024 * 1024
    assert config.delivery.download_dir == Path("data/downloads")
    assert config.delivery.aria2_timeout_seconds == 0
    assert config.streaming.enabled is False
    assert config.streaming.auto_add is False
    assert config.streaming.base_url == "http://qbittorrent:8080"
    assert config.streaming.portal_port == 8080
    assert config.streaming.users_path == Path("data/portal_users.db")
    assert config.rss.poll_interval_minutes == 60
    assert config.filters.genres == ()
    assert config.jackett.base_url == "http://jackett:9117"
    assert config.jackett.rss_url == ""
    assert config.jackett.api_key == ""
    assert config.jackett.api_key_env == "JACKETT_API_KEY"
    assert config.jackett.min_seeders == 1
    assert config.jackett.page_count == 1
    assert config.tmdb.api_key_env == "TMDB_API_KEY"
    assert config.tmdb.timeout_seconds == 20


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="не найден"):
        load_config(tmp_path / "нет.yaml")


def test_bad_interface(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID.replace("wg0", "wg0; rm -rf"), encoding="utf-8")
    with pytest.raises(ConfigError, match="интерфейса"):
        load_config(path)


def test_rss_filters_and_credentials(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID
        + r"""
rss:
  feeds:
    - name: demo
      url: https://example.com/feed.xml
filters:
  min_rating: 7.5
  max_age_days: 10
  patterns:
    rating: '(\d+(?:[.,]\d+)?)'
database:
  path: data/seen.db
credentials:
  username_env: POTOK_FEED_USER
  password_env: POTOK_FEED_PASSWORD
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.rss.feeds[0].name == "demo"
    assert config.filters.min_rating == 7.5
    assert config.filters.max_age_days == 10
    assert config.filters.patterns.rating is not None
    assert config.filters.patterns.rating.search("8,5").group(1) == "8,5"
    assert config.database_path == Path("data/seen.db")
    assert config.credentials.username_env == "POTOK_FEED_USER"


def test_bad_regex(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID
        + """
filters:
  patterns:
    rating: '('
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="неверное выражение"):
        load_config(path)


def test_streaming_section(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID
        + """
streaming:
  enabled: true
  auto_add: true
  public_url: http://127.0.0.1:8090
  portal_port: 8088
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.streaming.enabled is True
    assert config.streaming.auto_add is True
    assert config.streaming.portal_port == 8088


def test_streaming_rejects_login_in_url(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID
        + """
streaming:
  base_url: http://user:secret@torrserver:8090
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="без логина"):
        load_config(path)


def test_extra_vpn_domain_is_added_to_required(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID
        + """
routing:
  via_vpn: [rss]
  via_direct: [downloads]
  vpn_domains:
    - https://kinozal.tv/browse.php
    - api.telegram.org
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.routing.vpn_domains == ("kinozal.tv",)
    names = combined_vpn_domains(config.routing.vpn_domains)
    assert "api.telegram.org" in names
    assert "rutracker.org" in names
    assert "kinozal.tv" in names


def test_bad_vpn_domain(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID
        + """
routing:
  via_vpn: [rss]
  via_direct: [downloads]
  vpn_domains: ["не домен"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="не похож"):
        load_config(path)


def test_empty_route_list(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID.replace("    - rss\n", ""), encoding="utf-8")
    with pytest.raises(ConfigError, match="via_vpn"):
        load_config(path)


def test_data_dir_stays_outside_db_folder() -> None:
    assert data_dir_for(Path("data/db/seen.db")) == Path("data")
    assert data_dir_for(Path("data/seen.db")) == Path("data")
