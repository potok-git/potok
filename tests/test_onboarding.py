from pathlib import Path

from potok.config import load_config
from potok.onboarding import later_path, needs_setup
from potok.secrets import SecretStore
from potok.users import UserStore
from potok.web import create_app


def _yaml(tmp_path: Path) -> Path:
    database = (tmp_path / "seen.db").as_posix()
    text = f"""
vpn:
  interface: wg0
  config_path: "{(tmp_path / "wg0.conf").as_posix()}"
routing:
  via_vpn: [rss]
  via_direct: [downloads]
logging:
  level: INFO
database:
  path: "{database}"
streaming:
  enabled: false
  public_url: http://127.0.0.1:8090
"""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _fresh_app(tmp_path: Path):
    path = _yaml(tmp_path)
    config = load_config(path)
    users = UserStore(tmp_path / "users.db")
    store = SecretStore(tmp_path)
    app = create_app(
        config,
        config_path=path,
        users=users,
        secret_store=store,
        log_path=tmp_path / "worker.log",
    )
    app.config["TESTING"] = True
    return app, users, store, path


def _csrf(page: str) -> str:
    marker = 'name="csrf" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


def _login(client, password: str = "potok"):
    page = client.get("/login")
    token = _csrf(page.data.decode())
    response = client.post(
        "/login",
        data={"csrf": token, "username": "potok", "password": password},
    )
    assert response.status_code == 302
    return response, token


def test_empty_portal_opens_password_step(tmp_path: Path) -> None:
    app, _users, _store, _path = _fresh_app(tmp_path)
    client = app.test_client()
    response, _token = _login(client)
    assert response.headers["Location"].endswith("/setup")
    opened = client.get("/setup")
    text = opened.data.decode()
    assert opened.status_code == 200
    assert "Шаг 1 из 6" in text
    assert "Новый пароль" in text
    home = client.get("/")
    assert home.status_code == 302
    assert home.headers["Location"].endswith("/setup")


def test_jackett_key_skips_that_step(tmp_path: Path, monkeypatch) -> None:
    app, users, store, _path = _fresh_app(tmp_path)
    users.set_password("potok", "secret-pass")
    store.set_many({"JACKETT_API_KEY": "test-key"})
    store.apply_to_environ()
    monkeypatch.setattr(
        "potok.jackett.JackettClient.configured_indexers",
        lambda self: (),
    )
    client = app.test_client()
    response, _token = _login(client, "secret-pass")
    assert response.headers["Location"].endswith("/setup")
    page = client.get("/setup").data.decode()
    assert "Шаг 3 из 6" in page
    assert "Add Indexer" in page
    assert "Новый пароль" not in page
    assert "Справа вверху" not in page


def test_later_stops_login_redirect(tmp_path: Path) -> None:
    app, users, store, path = _fresh_app(tmp_path)
    client = app.test_client()
    _response, token = _login(client)
    later = client.post(
        "/setup",
        data={"csrf": token, "step": "password", "action": "later"},
    )
    assert later.status_code == 302
    assert later.headers["Location"].endswith("/")
    assert later_path(tmp_path).is_file()
    home = client.get("/")
    assert home.status_code == 200
    assert "Настройка не закончена" in home.data.decode()
    client.post("/logout", data={"csrf": token})
    _login(client)
    after = client.get("/")
    assert after.status_code == 200
    assert "Дашборд" in after.data.decode()
    assert not needs_setup(users, load_config(path), store, tmp_path)
