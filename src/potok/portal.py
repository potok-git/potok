"""Веб-портал. Страницы открывает Flask, старые адреса API остаются для проверок."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from potok.config import AppConfig, ConfigError, StreamingSettings, load_config
from potok.stream import StreamClient, StreamError, playback_links, validate_source_link
from potok.users import DEFAULT_NAME, DEFAULT_PASSWORD, UserError, UserStore

logger = logging.getLogger(__name__)

_MAX_BODY = 64_000
_SOURCE_PREFIX = "/api/stream/sources/"
_USER_PREFIX = "/api/users/"
_COOKIE = "potok_session"


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    content_type: str
    headers: tuple[tuple[str, str], ...] = ()


def main() -> int:
    """Точка входа контейнера portal. Не тянет Telegram и RSS."""
    path = Path(os.environ.get("POTOK_CONFIG", "/etc/potok/config.yaml"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    try:
        config = load_config(path)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1
    serve(config)
    return 0


def serve(config: AppConfig, *, config_path: Path | None = None) -> None:
    """Слушать порт портала, пока процесс не остановят."""
    from potok.qbit import QbitClient
    from potok.secrets import SecretStore
    from potok.web import create_app

    settings = config.streaming
    path = config_path or Path(os.environ.get("POTOK_CONFIG", "/etc/potok/config.yaml"))
    users = UserStore(settings.users_path)
    secrets = SecretStore(config.data_dir())
    secrets.apply_to_environ()
    app = create_app(
        config,
        config_path=path,
        users=users,
        secret_store=secrets,
        client=QbitClient(settings) if settings.enabled else None,
    )
    logger.info(
        "Портал слушает http://%s:%s",
        settings.portal_host,
        settings.portal_port,
    )
    app.run(
        host=settings.portal_host,
        port=settings.portal_port,
        threaded=True,
        use_reloader=False,
    )


def dispatch(
    method: str,
    path: str,
    body: bytes,
    client: StreamClient | None,
    settings: StreamingSettings,
    *,
    actor: str | None = None,
    users: UserStore | None = None,
    session: str | None = None,
) -> HttpResult:
    """Ответить на один запрос. Сеть к TorrServer ходит только через client."""
    route = urlsplit(path).path
    if route == "/health":
        return _json(200, {"ok": True, "streaming": client is not None})
    if route == "/login" and method == "GET":
        return _login_page()
    if route == "/login" and method == "POST":
        return _login(users, body)

    if users is not None and not actor:
        return _need_login(route)

    if route == "/logout" and method == "POST":
        return _logout(users, session)
    if route == "/users" and method == "GET":
        return _users_page(users, actor)
    if route == "/users/add" and method == "POST":
        return _users_action(users, actor, body, "add")
    if route == "/users/password" and method == "POST":
        return _users_action(users, actor, body, "password")
    if route == "/users/delete" and method == "POST":
        return _users_action(users, actor, body, "delete")
    try:
        if route == "/api/users" and method == "GET":
            return _json(200, {"users": list(_require_users(users).list_users())})
        if route == "/api/users" and method == "POST":
            return _api_add_user(users, body)
        if route.startswith(_USER_PREFIX) and method in {"PUT", "DELETE"}:
            return _api_change_user(users, method, route[len(_USER_PREFIX) :], body)
    except UserError as exc:
        return _json(400, {"error": str(exc)})
    if route == "/":
        return _page(client, settings, nav=actor is not None)
    if client is None and route.startswith("/api/stream/"):
        return _json(503, {"error": "Стриминг выключен в config.yaml: streaming.enabled."})

    try:
        if route == "/api/stream/settings" and method == "GET":
            return _json(200, _require_client(client).get_settings())
        if route == "/api/stream/settings" and method == "PUT":
            return _json(200, _require_client(client).update_settings(_read_json(body)))
        if route == "/api/stream/active" and method == "GET":
            items = [_view(item, settings) for item in _require_client(client).active_sources()]
            return _json(200, {"items": items})
        if route == "/api/stream/sources" and method == "GET":
            rows = [_view(item, settings) for item in _require_client(client).list_sources()]
            return _json(200, {"items": rows})
        if route == "/api/stream/sources" and method == "POST":
            return _add(client, settings, body)
        if route.startswith(_SOURCE_PREFIX) and method == "DELETE":
            infohash = route[len(_SOURCE_PREFIX) :].strip("/")
            _require_client(client).remove_source(infohash)
            return _json(200, {"removed": infohash.lower()})
    except StreamError as exc:
        status = 400 if _is_client_error(str(exc)) else 502
        return _json(status, {"error": str(exc)})
    except UserError as exc:
        return _json(400, {"error": str(exc)})

    if route.startswith("/api/"):
        return _json(405, {"error": "Такой метод для этого адреса не подходит."})
    return _json(404, {"error": "Адрес не найден."})


def session_from_cookie(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == _COOKIE and value:
            return value
    return None


def _login(users: UserStore | None, body: bytes) -> HttpResult:
    if users is None:
        return _json(503, {"error": "База пользователей не настроена."})
    try:
        fields = _fields(body)
    except UserError as exc:
        return _login_page(str(exc))
    name = fields.get("username", "")
    password = fields.get("password", "")
    if not users.authenticate(name, password):
        return _login_page("Неверное имя или пароль.")
    token = users.open_session(name.strip())
    return _redirect("/", token)


def _logout(users: UserStore | None, session: str | None) -> HttpResult:
    if users is not None and session:
        users.close_session(session)
    return _redirect("/login", clear=True)


def _users_page(users: UserStore | None, actor: str | None, note: str = "") -> HttpResult:
    store = _require_users(users)
    items = "".join(f"<li>{escape(name)}</li>" for name in store.list_users())
    message = f"<p>{escape(note)}</p>" if note else ""
    current = escape(actor or "")
    text = _layout(
        f"{message}"
        f"<p>Сейчас вы вошли как <strong>{current}</strong>.</p>"
        f"<p>Первый вход, если пароль ещё не меняли: {DEFAULT_NAME}, пароль {DEFAULT_PASSWORD}.</p>"
        f"<h2>Кто может входить</h2><ul>{items}</ul>"
        "<h2>Новый пользователь</h2>"
        '<form method="post" action="/users/add">'
        '<p><label>Имя<br><input name="name" autocomplete="off"></label></p>'
        '<p><label>Пароль<br><input name="password" type="password"></label></p>'
        '<button type="submit">Добавить</button></form>'
        "<h2>Сменить пароль</h2>"
        '<form method="post" action="/users/password">'
        f'<p><label>Имя<br><input name="name" value="{current}"></label></p>'
        '<p><label>Новый пароль<br><input name="password" type="password"></label></p>'
        '<button type="submit">Сохранить</button></form>'
        "<h2>Удалить пользователя</h2>"
        '<form method="post" action="/users/delete">'
        '<p><label>Имя<br><input name="name"></label></p>'
        '<button type="submit">Удалить</button></form>',
        nav=True,
        title="Поток — пользователи",
    )
    return HttpResult(200, text.encode("utf-8"), "text/html; charset=utf-8")


def _users_action(
    users: UserStore | None,
    actor: str | None,
    body: bytes,
    action: str,
) -> HttpResult:
    store = _require_users(users)
    try:
        fields = _fields(body)
        name = fields.get("name", "")
        password = fields.get("password", "")
        if action == "add":
            store.add_user(name, password)
            note = "Пользователь добавлен."
        elif action == "password":
            store.set_password(name, password)
            note = "Пароль сохранён."
        else:
            store.remove_user(name)
            note = "Пользователь удалён."
    except UserError as exc:
        return _users_page(store, actor, str(exc))
    return _users_page(store, actor, note)


def _api_add_user(users: UserStore | None, body: bytes) -> HttpResult:
    store = _require_users(users)
    fields = _fields(body)
    store.add_user(fields.get("name", ""), fields.get("password", ""))
    return _json(201, {"users": list(store.list_users())})


def _api_change_user(
    users: UserStore | None,
    method: str,
    raw_name: str,
    body: bytes,
) -> HttpResult:
    store = _require_users(users)
    name = unquote(raw_name).strip("/")
    if method == "DELETE":
        store.remove_user(name)
    else:
        fields = _fields(body)
        store.set_password(name, fields.get("password", ""))
    return _json(200, {"users": list(store.list_users())})


def _login_page(note: str = "") -> HttpResult:
    message = f"<p>{escape(note)}</p>" if note else ""
    text = _layout(
        f"{message}"
        '<form method="post" action="/login">'
        '<p><label>Имя<br><input name="username" autocomplete="username"></label></p>'
        '<p><label>Пароль<br><input name="password" type="password" '
        'autocomplete="current-password"></label></p>'
        '<button type="submit">Войти</button></form>'
        f"<p>Первый вход: имя {DEFAULT_NAME}, пароль {DEFAULT_PASSWORD}.</p>",
        title="Поток — вход",
    )
    status = 401 if note else 200
    return HttpResult(status, text.encode("utf-8"), "text/html; charset=utf-8")


def _need_login(route: str) -> HttpResult:
    if route.startswith("/api/"):
        return _json(401, {"error": "Нужно войти."})
    return _redirect("/login")


def _add(client: StreamClient | None, settings: StreamingSettings, body: bytes) -> HttpResult:
    stream = _require_client(client)
    payload = _read_json(body)
    if not isinstance(payload, dict):
        raise StreamError("Тело запроса должно быть словарём с полем link.")
    link = payload.get("link")
    title = payload.get("title", "")
    if not isinstance(link, str):
        raise StreamError("Поле link должно быть строкой.")
    if not isinstance(title, str):
        raise StreamError("Поле title должно быть строкой.")
    validate_source_link(link)
    added = stream.add_source(link, title=title)
    infohash = str(added.get("hash") or "")
    view = _view(added, settings) if infohash else {"added": True, "title": title}
    return _json(201, view)


def _page(client: StreamClient | None, settings: StreamingSettings, *, nav: bool) -> HttpResult:
    if client is None:
        text = _layout(
            "<p>Стриминг выключен. В config.yaml поставьте streaming.enabled: true "
            "и перезапустите контейнер portal.</p>",
            nav=nav,
        )
        return HttpResult(200, text.encode("utf-8"), "text/html; charset=utf-8")
    try:
        items = client.list_sources()
    except StreamError as exc:
        text = _layout(f"<p>TorrServer недоступен: {escape(str(exc))}</p>", nav=nav)
        return HttpResult(502, text.encode("utf-8"), "text/html; charset=utf-8")
    rows = []
    for item in items:
        view = _view(item, settings)
        title = escape(str(view["title"]))
        status = escape(str(view["stat_name"]))
        rows.append(
            "<li>"
            f"<strong>{title}</strong> — {status}<br>"
            f'<a href="{escape(str(view["player_url"]))}">Плеер</a> '
            f'<a href="{escape(str(view["hls_url"]))}">HLS</a> '
            f'<a href="{escape(str(view["stream_url"]))}">Поток</a> '
            f'<a href="{escape(str(view["playlist_url"]))}">Плейлист</a>'
            "</li>"
        )
    catalog = "<p>В библиотеке пока пусто.</p>" if not rows else "<ul>" + "".join(rows) + "</ul>"
    ui = escape(settings.public_url)
    text = _layout(
        f'<p><a href="{ui}">Веб-интерфейс TorrServer</a></p>'
        f"{catalog}"
        "<p>HLS — это ссылка на файл m3u8. Она играет, если в образе TorrServer есть GStreamer. "
        "Ссылка «Поток» открывает видео напрямую и работает без перекодирования. "
        "Отдельный MPEG-DASH (файл mpd) TorrServer не отдаёт.</p>",
        nav=nav,
    )
    return HttpResult(200, text.encode("utf-8"), "text/html; charset=utf-8")


def _layout(inner: str, *, nav: bool = False, title: str = "Поток — просмотр") -> str:
    links = ""
    if nav:
        links = (
            '<p><a href="/">Каталог</a> <a href="/users">Пользователи</a></p>'
            '<form method="post" action="/logout"><button type="submit">Выйти</button></form>'
        )
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:sans-serif;max-width:42rem;margin:2rem auto;line-height:1.45}"
        "a{margin-right:0.8rem} form{margin:0.6rem 0}</style></head><body>"
        f"<h1>{escape(title)}</h1>{links}{inner}</body></html>"
    )


def _view(item: dict, settings: StreamingSettings) -> dict:
    return playback_links(settings.public_url, item)


def _read_json(body: bytes) -> object:
    if not body:
        raise StreamError("Тело запроса пустое.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StreamError("Тело запроса должно быть JSON.") from exc


def _fields(body: bytes) -> dict[str, str]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserError("Тело запроса должно быть текстом.") from exc
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UserError("Тело запроса должно быть JSON.") from exc
        if not isinstance(payload, dict):
            raise UserError("Тело запроса должно быть словарём.")
        fields: dict[str, str] = {}
        for key, value in payload.items():
            if not isinstance(value, str):
                raise UserError(f"Поле {key} должно быть строкой.")
            fields[str(key)] = value
        return fields
    parsed = parse_qs(text, keep_blank_values=True, encoding="utf-8")
    return {key: values[0] if values else "" for key, values in parsed.items()}


def _json(status: int, payload: dict) -> HttpResult:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return HttpResult(status, raw, "application/json; charset=utf-8")


def _redirect(location: str, cookie: str | None = None, *, clear: bool = False) -> HttpResult:
    headers = [("Location", location)]
    if cookie:
        headers.append(("Set-Cookie", f"{_COOKIE}={cookie}; HttpOnly; SameSite=Lax; Path=/"))
    if clear:
        headers.append(("Set-Cookie", f"{_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"))
    return HttpResult(303, b"", "text/plain; charset=utf-8", tuple(headers))


def _require_client(client: StreamClient | None) -> StreamClient:
    if client is None:
        raise StreamError("Стриминг выключен в config.yaml: streaming.enabled.")
    return client


def _require_users(users: UserStore | None) -> UserStore:
    if users is None:
        raise UserError("База пользователей не настроена.")
    return users


def _is_client_error(message: str) -> bool:
    markers = (
        "должен",
        "должна",
        "нельзя",
        "Передайте",
        "Поле",
        "Тело",
        "Источник",
        "Hash",
        "пустой",
    )
    return any(marker in message for marker in markers)


def _handler_class(
    client: StreamClient | None,
    settings: StreamingSettings,
    users: UserStore,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._reply("GET")

        def do_PUT(self) -> None:  # noqa: N802
            self._reply("PUT")

        def do_POST(self) -> None:  # noqa: N802
            self._reply("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._reply("DELETE")

        def _reply(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > _MAX_BODY:
                result = _json(400, {"error": "Тело запроса слишком большое."})
            else:
                raw = self.rfile.read(length) if length else b""
                token = session_from_cookie(self.headers.get("Cookie"))
                actor = users.user_by_token(token) if token else None
                result = dispatch(
                    method,
                    self.path,
                    raw,
                    client,
                    settings,
                    actor=actor,
                    users=users,
                    session=token,
                )
            self.send_response(result.status)
            self.send_header("Content-Type", result.content_type)
            self.send_header("Content-Length", str(len(result.body)))
            for key, value in result.headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(result.body)

        def log_message(self, fmt: str, *args: object) -> None:
            logger.info("портал: " + fmt, *args)

    return Handler


if __name__ == "__main__":
    raise SystemExit(main())
