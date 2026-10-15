"""Фоновое чтение ленты сообщает номер страницы, не блокируя сайт."""

from pathlib import Path

from potok.config import load_config
from potok.feed_refresh import begin, reset, snapshot, wait
from potok.onboarding import mark_later
from potok.secrets import SecretStore
from potok.users import UserStore
from potok.web import create_app


def _config(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
""",
        encoding="utf-8",
    )
    return load_config(path)


def test_second_start_waits_for_the_first(tmp_path: Path) -> None:
    reset()
    config = _config(tmp_path)

    def work(_config, on_page) -> None:
        assert begin(config, work=work) is False
        on_page(1, 3, 2)

    assert begin(config, work=work) is True
    wait()
    current = snapshot()
    assert current.running is False
    assert current.page == 1
    assert current.pages == 3
    assert current.added == 2
    assert current.message.startswith("Готово")
    reset()


def test_pending_update_returns_immediately(tmp_path: Path) -> None:
    database = (tmp_path / "seen.db").as_posix()
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
vpn:
  interface: wg0
  config_path: /etc/potok/wg0.conf
routing:
  via_vpn: [rss]
  via_direct: [downloads]
database:
  path: "{database}"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    app = create_app(
        config,
        config_path=path,
        users=UserStore(tmp_path / "users.db"),
        secret_store=SecretStore(tmp_path),
        log_path=tmp_path / "worker.log",
    )
    app.config["TESTING"] = True
    mark_later(tmp_path)
    started: list[int] = []

    def starter(received) -> bool:
        started.append(received.jackett.page_count)
        return True

    app.config["POTOK_FEED_RUNNER"] = starter
    client = app.test_client()
    page = client.get("/login")
    token = _csrf(page.data.decode())
    client.post("/login", data={"csrf": token, "username": "potok", "password": "potok"})
    sent = client.post("/pending/update", data={"csrf": token})
    assert sent.status_code == 302
    assert sent.headers["Location"].endswith("/pending")
    assert started == [1]
    opened = client.get("/pending")
    assert "Читаю ленту в фоне" in opened.data.decode()
    progress = client.get("/pending/progress")
    assert progress.status_code == 200
    assert progress.get_json()["running"] is False


def _csrf(page: str) -> str:
    marker = 'name="csrf" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]
