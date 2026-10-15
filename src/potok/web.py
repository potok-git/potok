"""Веб-портал на Flask: статистика, настройки, одобрение, журнал и загрузки."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml
from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from potok.config import REQUIRED_VPN_DOMAINS, AppConfig, ConfigError, data_dir_for, load_config
from potok.feed_refresh import begin, snapshot
from potok.jackett_cats import load_category_groups, selected_category_ids
from potok.jobs import enqueue
from potok.logs import count_errors, iter_events, worker_log_path
from potok.onboarding import (
    JACKETT_UI,
    STEPS,
    TITLES,
    inspect,
    later_chosen,
    mark_finished,
    mark_later,
    needs_setup,
    previous_step,
    step_number,
    tracker_names,
    wizard_finished,
)
from potok.publish import (
    NotifyError,
    already_sent_to_vk,
    bind_uploaded_codes,
    discard_queued_files,
    publish_file,
    row_for_filename,
    seed_remaining_seconds,
    send_vk_card,
    torrent_info_hash,
    waiting_vk_hashes,
)
from potok.qbit import QbitClient, eta_text
from potok.restart import restart_worker, run_in_worker
from potok.rss import genre_parts, genre_text, genres_from_title
from potok.secrets import SecretError, SecretStore
from potok.settings_io import (
    SettingsError,
    apply_settings,
    portal_hint,
    read_wireguard,
    resolve_wg_path,
    save_config,
    save_wireguard,
)
from potok.store import APPROVED, PENDING, REJECTED, SeenStore, StoredItem
from potok.stream import StreamError, torrent_basename
from potok.titles import (
    has_cyrillic,
    matching_manual_torrent,
    quality_from_name,
    release_title,
    russian_title,
)
from potok.tmdb import TmdbError, country_text, movie_query, search_movie
from potok.users import DEFAULT_NAME, DEFAULT_PASSWORD, UserError, UserStore
from potok.vk_oauth import (
    EXPIRES_NAME,
    VkOAuthError,
    blank_authorize_url,
    describe_token,
    save_vk_token,
)
from potok.vk_uploads import UploadBoard, UploadOrder, board_path, order_path
from potok.vk_video import VkCatalogItem, VkError, VkVideoClient
from potok.vpn import VpnError, VpnManager, redact_secrets

logger = logging.getLogger(__name__)

_FIXED_TITLES: dict[tuple[int, int], str] = {}

_OPEN = {"/login", "/health", "/favicon.ico", "/about"}
_VIDEO_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".wmv",
    ".ts",
    ".m2ts",
}
_PRIVACY = (
    ("all", "Все"),
    ("friends", "Только друзья"),
    ("friends_of_friends", "Друзья и друзья друзей"),
    ("only_me", "Только я"),
    ("nobody", "Никто"),
    ("by_link", "Только у кого есть ссылка"),
)
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_PAGE_SIZES = (10, 20, 50, 100)


@dataclass(frozen=True)
class ManualCard:
    """Карточка ручной загрузки: название из файла .torrent, без постера TMDB."""

    filename: str
    title: str
    quality: str | None
    size_bytes: int | None
    genre: str | None
    movie_title: str | None = None
    row_id: int = 0
    rating: float | None = None
    overview: str | None = None
    poster_url: str | None = None
    seeders: int | None = None
    runtime_minutes: int | None = None
    country: str | None = None


def _queue_params() -> tuple[int, int]:
    """Сколько фильмов на странице и номер страницы. Размер запоминается."""
    raw_size = request.args.get("size", type=int)
    if raw_size in _PAGE_SIZES:
        session["pending_page_size"] = raw_size
    size = session.get("pending_page_size", 20)
    if size not in _PAGE_SIZES:
        size = 20
    page = request.args.get("page", default=1, type=int) or 1
    if page < 1:
        page = 1
    return size, page


def _page_numbers(page: int, pages: int) -> list[int | None]:
    """Номера для пагинатора. None — пропуск, если страниц много."""
    if pages <= 7:
        return list(range(1, pages + 1))
    picked = sorted(
        number for number in {1, pages, page - 1, page, page + 1} if 1 <= number <= pages
    )
    numbers: list[int | None] = []
    previous = 0
    for number in picked:
        if previous and number > previous + 1:
            numbers.append(None)
        numbers.append(number)
        previous = number
    return numbers


def _pending_here() -> str:
    """Вернуться на ту же страницу очереди, а не на чужой сайт."""
    return _back_to("pending")


def _back_to(endpoint: str) -> str:
    """Остаться на той же странице портала, включая номер и размер страницы."""
    target = request.referrer or ""
    parts = urlsplit(target)
    here = url_for(endpoint)
    if parts.scheme not in {"http", "https"} or parts.netloc != request.host:
        return here
    if parts.path.rstrip("/") != here.rstrip("/"):
        return here
    return target


def create_app(
    config: AppConfig,
    *,
    config_path: Path,
    users: UserStore,
    secret_store: SecretStore,
    client: QbitClient | None = None,
    log_path: Path | None = None,
) -> Flask:
    """Собрать портал. client нужен в тестах; без него страница загрузок ходит в qBittorrent."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent / "static"),
    )
    app.secret_key = secret_store.session_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=2_500_000,
        POTOK_CONFIG_PATH=config_path,
        POTOK_FALLBACK=config,
        POTOK_USERS=users,
        POTOK_SECRETS=secret_store,
        POTOK_LOG=log_path or worker_log_path(config.database_path),
        POTOK_STREAM=client,
    )

    @app.before_request
    def _prepare() -> Response | None:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        g.config = _current_config(app)
        g.config_error = getattr(g, "config_error", "")
        if request.method == "POST":
            _check_csrf()
        if request.path in _OPEN or request.path.startswith("/static/"):
            return None
        if not session.get("user"):
            return redirect(url_for("login"))
        if request.path in {"/logout", "/setup"}:
            return None
        if needs_setup(
            _users(app),
            g.config,
            app.config["POTOK_SECRETS"],
            g.config.data_dir(),
        ):
            return redirect(url_for("setup"))
        return None

    @app.context_processor
    def _nav() -> dict[str, object]:
        banner = False
        if session.get("user") and request.path != "/setup":
            folder = g.config.data_dir()
            banner = later_chosen(folder) and not wizard_finished(folder)
        return {
            "current_user": session.get("user", ""),
            "csrf": session.get("csrf", ""),
            "country_text": country_text,
            "genre_text": genre_text,
            "onboarding_banner": banner,
        }

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return send_from_directory(app.static_folder, "favicon.png", mimetype="image/png")

    @app.get("/health")
    def health() -> Response:
        body = '{"ok": true}'
        return Response(body, mimetype="application/json")

    @app.get("/about")
    def about_page() -> str:
        return render_template("about.html")

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Response | str:
        note = ""
        if request.method == "POST":
            name = request.form.get("username", "")
            password = request.form.get("password", "")
            if _users(app).authenticate(name, password):
                session["user"] = name.strip()
                config = _current_config(app)
                if needs_setup(
                    _users(app),
                    config,
                    app.config["POTOK_SECRETS"],
                    config.data_dir(),
                ):
                    return redirect(url_for("setup"))
                return redirect(url_for("dashboard"))
            note = "Неверное имя или пароль."
        status = 401 if note else 200
        return render_template("login.html", note=note, default_name=DEFAULT_NAME), status

    @app.post("/logout")
    def logout() -> Response:
        session.pop("user", None)
        return redirect(url_for("login"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup() -> Response | str:
        config = _require_config()
        store: SecretStore = app.config["POTOK_SECRETS"]
        path: Path = app.config["POTOK_CONFIG_PATH"]
        users = _users(app)
        data_dir = config.data_dir()
        if request.method == "POST":
            return _setup_post(app, config, store, path, users, data_dir)
        state = inspect(
            users=users,
            config=config,
            secrets=store,
            config_path=path,
            data_dir=data_dir,
        )
        asked = request.args.get("step") or session.get("setup_step") or ""
        if asked in STEPS or asked == "done":
            step = str(asked)
        else:
            step = state.first_open()
        if step == "done":
            if state.required_done():
                mark_finished(data_dir)
            else:
                step = state.first_open()
        wg_path = resolve_wg_path(path, config.vpn.config_path)
        try:
            wg_text = read_wireguard(wg_path, store)
        except SettingsError:
            wg_text = ""
        names = tracker_names(config, store) if step == "tracker" else ()
        return render_template(
            "onboarding.html",
            step=step,
            title=TITLES.get(step, ""),
            number=step_number(step),
            total=len(STEPS),
            jackett_ui=JACKETT_UI,
            tracker_names=names,
            feed_url=config.jackett.rss_url,
            app_id="" if config.vk.app_id is None else config.vk.app_id,
            wg_text=wg_text,
        )

    @app.get("/")
    def dashboard() -> str:
        config = _require_config()
        with SeenStore(config.database_path) as store:
            counts = store.counts()
            loading = len(store.list_approved_without_vk())
            seen = store.list_seen()
        log_path = app.config["POTOK_LOG"]
        return render_template(
            "dashboard.html",
            pending_count=counts.get("pending", 0),
            approved=counts.get(APPROVED, 0),
            rejected=counts.get(REJECTED, 0),
            loading=loading,
            exporting=_export_count(config, seen),
            errors=count_errors(log_path),
            config_error=getattr(g, "config_error", ""),
            vk_status=_vk_token_status(config, app.config["POTOK_SECRETS"]),
        )

    @app.get("/pending")
    def pending() -> str:
        config = _require_config()
        size, page = _queue_params()
        with SeenStore(config.database_path) as store:
            total = store.counts().get(PENDING, 0)
            pages = max(1, (total + size - 1) // size) if total else 1
            if page > pages:
                page = pages
            items = store.list_pending(limit=size, offset=(page - 1) * size) if total else []
        genres, qualities = _pending_choices(items)
        return render_template(
            "pending.html",
            items=items,
            total=total,
            size=size,
            page=page,
            pages=pages,
            page_numbers=_page_numbers(page, pages),
            page_sizes=_PAGE_SIZES,
            genres=genres,
            qualities=qualities,
            movie_year=_movie_year,
            runtime_text=_runtime_text,
            release_title=release_title,
            feed=snapshot(),
        )

    @app.get("/pending/progress")
    def pending_progress() -> Response:
        _require_config()
        current = snapshot()
        body = json.dumps(
            {
                "running": current.running,
                "page": current.page,
                "pages": current.pages,
                "added": current.added,
                "message": current.message,
                "error": current.error,
            },
            ensure_ascii=False,
        )
        return Response(body, mimetype="application/json")

    @app.get("/pending/cards")
    def pending_cards() -> str:
        config = _require_config()
        size, page = _queue_params()
        with SeenStore(config.database_path) as store:
            total = store.counts().get(PENDING, 0)
            pages = max(1, (total + size - 1) // size) if total else 1
            if page > pages:
                page = pages
            items = store.list_pending(limit=size, offset=(page - 1) * size) if total else []
        return render_template(
            "pending_cards.html",
            items=items,
            movie_year=_movie_year,
            runtime_text=_runtime_text,
            release_title=release_title,
        )

    @app.get("/poster/<int:row_id>")
    def poster(row_id: int) -> Response:
        config = _require_config()
        with SeenStore(config.database_path) as store:
            item = store.get(row_id)
        if item is None or not _tmdb_image(item.poster_url):
            abort(404)
        try:
            body, kind = _read_poster(item.poster_url or "")
        except (HTTPError, URLError, TimeoutError, OSError):
            abort(404)
        return Response(
            body,
            mimetype=kind,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.post("/pending/<int:row_id>/<action>")
    def pending_decide(row_id: int, action: str) -> Response:
        status = {"approve": APPROVED, "reject": REJECTED}.get(action)
        if status is None:
            abort(404)
        config = _require_config()
        with SeenStore(config.database_path) as store:
            decision = store.decide(row_id, status)
        if decision is None:
            flash("Запись не найдена.", "warning")
        elif not decision.changed:
            flash("По этой записи решение уже есть. Бот видит ту же базу.", "warning")
        else:
            enqueue(config.data_dir(), row_id, status)
            if status == APPROVED:
                flash(
                    "Одобрено. Загрузка в qBittorrent начнётся сразу, без ожидания VK.",
                    "success",
                )
            else:
                flash("Отклонено. В Telegram эта запись тоже исчезнет из очереди.", "success")
        return redirect(_pending_here())

    @app.get("/approved")
    def approved_page() -> str:
        return _decision_page(APPROVED, "approved.html")

    @app.get("/rejected")
    def rejected_page() -> str:
        return _decision_page(REJECTED, "rejected.html")

    @app.post("/approved/<int:row_id>/revoke")
    def approved_revoke(row_id: int) -> Response:
        config = _require_config()
        with SeenStore(config.database_path) as store:
            item = store.get(row_id)
            changed = store.reopen(row_id) if item is not None else False
        if item is None:
            flash("Запись не найдена.", "warning")
        elif not changed:
            flash("Эта запись уже не одобрена.", "warning")
        else:
            flash(_revoke_note(config, item), "success")
        return redirect(_back_to("approved_page"))

    @app.post("/rejected/<int:row_id>/restore")
    def rejected_restore(row_id: int) -> Response:
        with SeenStore(_require_config().database_path) as store:
            item = store.get(row_id)
            changed = store.reopen(row_id, only=REJECTED) if item is not None else False
        if item is None:
            flash("Запись не найдена.", "warning")
        elif not changed:
            flash("Эта запись уже не отклонена.", "warning")
        else:
            flash("Отклонение снято. Фильм снова в очереди «Ожидают».", "success")
        return redirect(_back_to("rejected_page"))

    @app.post("/pending/clear")
    def pending_clear() -> Response:
        config = _require_config()
        with SeenStore(config.database_path) as store:
            removed = store.delete_unapproved()
        if removed:
            flash(f"Из очереди убраны не одобренные. Удалено: {removed}.", "success")
        else:
            flash("Неодобренных записей нет.", "secondary")
        return redirect(_pending_here())

    @app.post("/pending/update")
    def pending_update() -> Response:
        config = _require_config()
        starter = current_app.config.get("POTOK_FEED_RUNNER", begin)
        if starter(config):
            flash("Читаю ленту в фоне. Новые фильмы появятся по мере готовности.", "success")
        else:
            flash("Лента уже читается. Новые фильмы появляются по мере готовности.", "secondary")
        return redirect(_pending_here())

    @app.post("/downloads/<infohash>/<action>")
    def download_control(infohash: str, action: str) -> Response:
        if action not in {"pause", "resume", "delete", "up", "down"}:
            abort(404)
        config = _require_config()
        try:
            client = _stream_client(config)
            if action == "pause":
                client.stop(infohash)
                flash("Загрузка на паузе.", "success")
            elif action == "resume":
                client.start(infohash)
                flash("Загрузка продолжена.", "success")
            elif action == "delete":
                client.remove(infohash)
                flash("Раздача убрана из списка. Файл на диске остаётся.", "success")
            else:
                enabled = client.move_in_queue(infohash, action)
                note = "Поднял в очереди загрузки."
                if action == "down":
                    note = "Опустил в очереди загрузки."
                if enabled:
                    note += " Включил очередь qBittorrent, чтобы верхние качались раньше."
                flash(note, "success")
        except StreamError as exc:
            flash(str(exc), "danger")
        return redirect(url_for("downloads"))

    @app.route("/downloads", methods=["GET", "POST"])
    def downloads() -> str:
        config = _require_config()
        note = ""
        if request.method == "POST" and request.form.get("action") == "vk":
            note = _send_videos_to_vk(config)
        elif request.method == "POST":
            note = _add_torrent(config)
        sources, stream_error = _sources(config)
        films = _manual_cards(config)
        size, page = _queue_params()
        total = len(films)
        pages = max(1, (total + size - 1) // size) if total else 1
        if page > pages:
            page = pages
        items = films[(page - 1) * size : page * size] if total else []
        genres, qualities = _pending_choices(items)
        return render_template(
            "downloads.html",
            sources=sources,
            stream_error=stream_error,
            note=note,
            items=items,
            total=total,
            size=size,
            page=page,
            pages=pages,
            page_numbers=_page_numbers(page, pages),
            page_sizes=_PAGE_SIZES,
            genres=genres,
            qualities=qualities,
            movie_year=_movie_year,
            runtime_text=_runtime_text,
            release_title=release_title,
            public_url=config.streaming.public_url,
        )

    @app.get("/vk")
    def vk_page() -> str:
        return render_template("vk.html")

    @app.get("/vk/queue")
    def vk_queue_page() -> str:
        config = _require_config()
        rows, error = _vk_queue(config)
        return render_template("vk_queue.html", rows=rows, stream_error=error)

    @app.post("/vk/queue/<infohash>/<action>")
    def vk_queue_move(infohash: str, action: str) -> Response:
        if action not in {"up", "down"}:
            abort(404)
        config = _require_config()
        try:
            hashes, _rows = _vk_waiting(config)
            UploadOrder(order_path(config.database_path)).move(hashes, infohash, action)
            flash(
                "Поднял в очереди VK." if action == "up" else "Опустил в очереди VK.",
                "success",
            )
        except ValueError as exc:
            flash(str(exc), "danger")
        except StreamError as exc:
            flash(str(exc), "danger")
        return redirect(url_for("vk_queue_page"))

    @app.post("/vk/queue/<infohash>/delete")
    def vk_queue_delete(infohash: str) -> Response:
        config = _require_config()
        try:
            flash(_drop_vk_queue_item(config, infohash), "success")
        except (ValueError, StreamError) as exc:
            flash(str(exc), "danger")
        return redirect(url_for("vk_queue_page"))

    @app.get("/api/vk/uploads")
    def api_vk_uploads() -> Response:
        config = _require_config()
        board = UploadBoard(board_path(config.database_path))
        board.settle_stops()
        return _json_ok({"uploads": board.jobs()})

    @app.get("/api/vk")
    def api_vk() -> Response:
        config = _require_config()
        board = UploadBoard(board_path(config.database_path))
        board.settle_stops()
        uploads = board.jobs()
        videos, error = _vk_catalog(config, uploads)
        return _json_ok({"videos": videos, "uploads": uploads, "error": error})

    @app.post("/api/vk/edit")
    def api_vk_edit() -> Response:
        config = _require_config()
        try:
            payload = _api_body()
            owner_id, video_id = _vk_ids(payload)
            title = payload.get("title")
            description = payload.get("description")
            if not isinstance(title, str) or not isinstance(description, str):
                raise VkError("Нужны текстовые поля title и description.")
            _vk_client(config).edit_video(
                owner_id,
                video_id,
                title=title,
                description=description,
            )
        except VkError as exc:
            return _json_error(400, str(exc))
        _forget_vk_catalog(config)
        return _json_ok({"ok": True})

    @app.post("/api/vk/delete")
    def api_vk_delete() -> Response:
        config = _require_config()
        try:
            owner_id, video_id = _vk_ids(_api_body())
            _vk_client(config).delete_video(owner_id, video_id)
        except VkError as exc:
            return _json_error(400, str(exc))
        _forget_vk_catalog(config)
        return _json_ok({"ok": True})

    @app.post("/api/vk/stop")
    def api_vk_stop() -> Response:
        config = _require_config()
        try:
            payload = _api_body()
        except VkError as exc:
            return _json_error(400, str(exc))
        job_id = payload.get("id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{12}", job_id):
            return _json_error(400, "Неизвестный номер отправки.")
        board = UploadBoard(board_path(config.database_path))
        current = board.job(job_id)
        if current is None:
            return _json_error(404, "Отправка не найдена.")
        board.request_stop(job_id)
        warning = ""
        if current.get("status") in {"uploading", "processing"}:
            warning = _delete_unfinished(config, current)
        return _json_ok({"ok": True, "warning": warning})

    @app.post("/api/vk/retry")
    def api_vk_retry() -> Response:
        config = _require_config()
        try:
            payload = _api_body()
        except VkError as exc:
            return _json_error(400, str(exc))
        job_id = payload.get("id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{12}", job_id):
            return _json_error(400, "Неизвестный номер отправки.")
        board = UploadBoard(board_path(config.database_path))
        current = board.job(job_id)
        if current is None:
            return _json_error(404, "Отправка не найдена.")
        if current.get("status") not in {"stopped", "error"}:
            return _json_error(
                400, "Повторить можно только остановленную или сорвавшуюся отправку."
            )
        name = str(current.get("name") or "")
        try:
            message = _retry_upload(config, name, job_id)
        except VkError as exc:
            return _json_error(400, str(exc))
        return _json_ok({"ok": True, "message": message})

    @app.post("/api/vk/dismiss")
    def api_vk_dismiss() -> Response:
        config = _require_config()
        try:
            payload = _api_body()
        except VkError as exc:
            return _json_error(400, str(exc))
        job_id = payload.get("id")
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{12}", job_id):
            return _json_error(400, "Неизвестный номер отправки.")
        board = UploadBoard(board_path(config.database_path))
        removed = board.drop(job_id)
        if removed is None:
            return _json_error(404, "Эту отправку нельзя убрать, пока она идёт.")
        warning = ""
        if removed.get("status") in {"stopped", "error"} or removed.get("stop"):
            warning = _delete_unfinished(config, removed)
        return _json_ok({"ok": True, "warning": warning})

    @app.post("/api/vk/bot")
    def api_vk_bot() -> Response:
        config = _require_config()
        try:
            owner_id, video_id = _vk_ids(_api_body())
            item = _video_by_id(config, owner_id, video_id)
            send_vk_card(
                config,
                title=item.title,
                description=item.description,
                page_url=item.page_url,
            )
        except (VkError, ConfigError, NotifyError) as exc:
            return _json_error(400, str(exc))
        return _json_ok({"ok": True})

    @app.get("/logs")
    def logs_page() -> str:
        return render_template("logs.html", log_path=str(app.config["POTOK_LOG"]))

    @app.get("/logs/stream")
    def logs_stream() -> Response:
        follow = request.args.get("once") != "1"
        stream = iter_events(app.config["POTOK_LOG"], follow=follow)
        return Response(
            stream,
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/settings", methods=["GET", "POST"])
    def settings() -> str | Response:
        config = _require_config()
        store: SecretStore = app.config["POTOK_SECRETS"]
        path: Path = app.config["POTOK_CONFIG_PATH"]
        wg_path = resolve_wg_path(path, config.vpn.config_path)
        if request.method == "POST":
            _save(path, store, wg_path)
            return redirect(url_for("settings"))
        try:
            wg_text = read_wireguard(wg_path, store)
        except SettingsError as exc:
            wg_text = ""
            flash(str(exc), "danger")
        categories, categories_note = load_category_groups(config, store)
        chosen = selected_category_ids(config.jackett.rss_url)
        return render_template(
            "settings.html",
            draft=_draft(config, wg_text, store),
            privacy_choices=_PRIVACY,
            log_levels=_LEVELS,
            wg_path=str(wg_path),
            config_path=str(path),
            vk_authorize=_vk_authorize_link(config),
            jackett_categories=categories,
            jackett_categories_note=categories_note,
            jackett_cat_base=",".join(sorted(chosen, key=int)),
        )

    @app.post("/vk/token")
    def vk_token_paste() -> Response:
        config = _require_config()
        store: SecretStore = app.config["POTOK_SECRETS"]
        try:
            note = save_vk_token(
                request.form.get("vk_address", ""),
                token_env=config.vk.token_env,
                api_version=config.vk.api_version,
                timeout=config.vk.api_timeout_seconds,
                store=store,
            )
        except (VkOAuthError, SecretError) as exc:
            flash(str(exc), "danger")
            return redirect(url_for("settings") + "#vk")
        logger.info("Токен VK сохранён из адресной строки.")
        flash(note + " " + restart_worker(), "success")
        return redirect(url_for("dashboard"))

    @app.post("/settings/telegram-check")
    def telegram_check() -> Response:
        config = _require_config()
        store: SecretStore = app.config["POTOK_SECRETS"]
        category, text = _check_telegram(config, store, request.form.get("telegram_token", ""))
        flash(text, category)
        return redirect(url_for("settings"))

    @app.post("/settings/tmdb-check")
    def tmdb_check() -> Response:
        config = _require_config()
        store: SecretStore = app.config["POTOK_SECRETS"]
        category, text = _check_tmdb(config, store, request.form.get("tmdb_api_key", ""))
        flash(text, category)
        return redirect(url_for("settings"))

    @app.post("/settings/wireguard-check")
    def wireguard_check() -> Response:
        config = _require_config()
        store: SecretStore = app.config["POTOK_SECRETS"]
        path: Path = app.config["POTOK_CONFIG_PATH"]
        wg_path = resolve_wg_path(path, config.vpn.config_path)
        try:
            save_wireguard(wg_path, request.form.get("wg_conf", ""), store)
            flash(probe_wireguard(config), "success")
        except (SettingsError, SecretError, VpnError, OSError) as exc:
            flash(str(exc), "danger")
        return redirect(url_for("settings"))

    @app.route("/users", methods=["GET", "POST"])
    def users_page() -> str:
        store = _users(app)
        note = ""
        note_ok = False
        if request.method == "POST":
            note, note_ok = _users_action(store)
        return render_template(
            "users.html",
            names=store.list_users(),
            note=note,
            note_ok=note_ok,
        )

    @app.errorhandler(413)
    def too_big(_exc: object) -> tuple[str, int]:
        return "Файл больше 2 МБ.", 413

    return app


def _setup_post(
    app: Flask,
    config: AppConfig,
    store: SecretStore,
    path: Path,
    users: UserStore,
    data_dir: Path,
) -> Response:
    action = request.form.get("action", "")
    step = request.form.get("step", "")
    if action == "later":
        mark_later(data_dir)
        session["setup_step"] = ""
        return redirect(url_for("dashboard"))
    if action == "back":
        previous = previous_step(step)
        session["setup_step"] = previous
        return redirect(url_for("setup", step=previous))
    if action == "skip":
        if step == "notify":
            session["setup_step"] = "vpn"
            return redirect(url_for("setup", step="vpn"))
        if step == "vpn":
            mark_finished(data_dir)
            session["setup_step"] = "done"
            return redirect(url_for("setup", step="done"))
        flash("Этот шаг нельзя пропустить.", "danger")
        return redirect(url_for("setup", step=step or None))
    if action != "next":
        flash("Не понял действие.", "danger")
        return redirect(url_for("setup"))

    error = _setup_save_step(app, config, store, path, users, step)
    if error:
        flash(error, "danger")
        session["setup_step"] = step
        return redirect(url_for("setup", step=step))
    session["setup_step"] = ""
    if step == "vpn":
        mark_finished(data_dir)
        session["setup_step"] = "done"
        return redirect(url_for("setup", step="done"))
    return redirect(url_for("setup"))


def _setup_save_step(
    app: Flask,
    config: AppConfig,
    store: SecretStore,
    path: Path,
    users: UserStore,
    step: str,
) -> str:
    if step == "password":
        password = request.form.get("password", "")
        repeat = request.form.get("password_repeat", "")
        if password != repeat:
            return "Пароли не совпадают."
        if password == DEFAULT_PASSWORD:
            return "Придумайте другой пароль, не potok."
        try:
            users.set_password(session.get("user") or DEFAULT_NAME, password)
        except UserError as exc:
            return str(exc)
        if users.authenticate(DEFAULT_NAME, DEFAULT_PASSWORD):
            return "Старый пароль potok всё ещё подходит. Смените пароль пользователя potok."
        return ""
    if step == "jackett":
        key = request.form.get("jackett_api_key", "").strip()
        if not key:
            return "Вставьте ключ API из окна Jackett."
        return _save_onboarding(path, store, {"jackett_api_key": key})
    if step == "tracker":
        names = tracker_names(config, store)
        if not names:
            return "Сначала добавьте трекер в Jackett, затем нажмите «Дальше» ещё раз."
        return ""
    if step == "feed":
        url = request.form.get("jackett_rss_url", "").strip()
        if not url:
            return "Вставьте ссылку Torznab Feed."
        return _save_onboarding(
            path,
            store,
            {
                "jackett_rss_url": url,
                "jackett_base_url": config.jackett.base_url,
            },
        )
    if step == "notify":
        fields: dict[str, str | list[str]] = {}
        for name in (
            "telegram_token",
            "telegram_chat_id",
            "app_id",
            "vk_token",
            "tmdb_api_key",
        ):
            value = request.form.get(name, "").strip()
            if value:
                fields[name] = value
        if not fields:
            return ""
        return _save_onboarding(path, store, fields)
    if step == "vpn":
        text = request.form.get("wg_conf", "")
        if not text.strip() or "REPLACE_WITH" in text:
            return "Вставьте свой текст WireGuard или нажмите «Позже»."
        wg_path = resolve_wg_path(path, config.vpn.config_path)
        return _save_onboarding(path, store, {"wg_conf": text}, wg_path=wg_path)
    return "Неизвестный шаг."


def _save_onboarding(
    path: Path,
    store: SecretStore,
    form: dict[str, str | list[str]],
    wg_path: Path | None = None,
) -> str:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return f"Не удалось прочитать config.yaml: {exc}"
    if not isinstance(raw, dict):
        return "config.yaml должен содержать словарь настроек."
    try:
        updated, secret_values, wireguard = apply_settings(raw, form)
        save_config(path, updated)
        if secret_values:
            store.set_many(secret_values)
            store.apply_to_environ()
        if wireguard is not None and wg_path is not None:
            save_wireguard(wg_path, wireguard, store)
    except (SettingsError, SecretError) as exc:
        return str(exc)
    restart_worker()
    return ""


def _save(path: Path, store: SecretStore, wg_path: Path) -> None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        flash(f"Не удалось прочитать config.yaml: {exc}", "danger")
        return
    if not isinstance(raw, dict):
        flash("config.yaml должен содержать словарь настроек.", "danger")
        return
    try:
        updated, secret_values, wireguard = apply_settings(raw, _posted())
        save_config(path, updated)
        if secret_values:
            store.set_many(secret_values)
            store.apply_to_environ()
        if wireguard is not None:
            save_wireguard(wg_path, wireguard, store)
    except (SettingsError, SecretError) as exc:
        flash(str(exc), "danger")
        return
    hint = portal_hint(raw, updated)
    flash("Настройки сохранены. " + restart_worker() + hint, "success")


def _posted() -> dict[str, str | list[str]]:
    fields: dict[str, str | list[str]] = {}
    for key in request.form:
        if key in {"feed_name", "feed_url", "jackett_cat"}:
            fields[key] = request.form.getlist(key)
        else:
            fields[key] = request.form.get(key, "")
    return fields


def _add_torrent(config: AppConfig) -> str:
    upload = request.files.get("torrent")
    if upload is None or not upload.filename:
        return "Выберите файл .torrent."
    name = torrent_basename(upload.filename)
    if name is None:
        return "Нужен файл .torrent без папки в имени. Скобки и запятые в названии можно."
    content = upload.read(2_100_000)
    if not content.startswith(b"d") or len(content) > 2_000_000:
        return "Это не похоже на файл .torrent или он больше 2 МБ."
    directory = config.rss.metadata_dir
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(content)
    title = request.form.get("title", "").strip() or Path(name).stem
    if not config.streaming.enabled:
        return f"Файл сохранён: {name}. qBittorrent в настройках выключен, раздача не добавлена."
    try:
        _stream_client(config).add_torrent_file(name, content, title=title)
    except StreamError as exc:
        return f"Файл сохранён: {name}. qBittorrent не принял его: {exc}"
    return f"Файл {name} добавлен в загрузки."


_PAUSED = {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
_DOWNLOADING = {
    "downloading",
    "forcedDL",
    "metaDL",
    "stalledDL",
    "allocating",
    "queuedDL",
    "checkingDL",
    "pausedDL",
    "stoppedDL",
}
_STATE_RU = {
    "downloading": "качается",
    "forcedDL": "качается",
    "metaDL": "метаданные",
    "stalledDL": "мало пиров",
    "allocating": "место на диске",
    "uploading": "раздаётся",
    "forcedUP": "раздаётся",
    "stalledUP": "раздаётся",
    "pausedDL": "пауза",
    "pausedUP": "пауза",
    "stoppedDL": "пауза",
    "stoppedUP": "пауза",
    "queuedDL": "в очереди",
    "queuedUP": "в очереди",
    "checkingDL": "проверка",
    "checkingUP": "проверка",
    "checkingResumeData": "проверка",
    "error": "ошибка",
    "missingFiles": "нет файлов",
}


def _seed_state(item: dict, state: str) -> str:
    left = seed_remaining_seconds(item, time.time())
    if left is None:
        return _STATE_RU.get(state, state or "неизвестно")
    if left <= 0:
        return "скоро пауза и VK"
    minutes = max(1, (left + 59) // 60)
    return f"раздаётся ещё {minutes} мин"


def _sources(config: AppConfig) -> tuple[list[dict], str]:
    if not config.streaming.enabled:
        return [], "qBittorrent выключен. Включите его в настройках, чтобы видеть загрузки."
    try:
        items = _stream_client(config).list_torrents()
    except StreamError as exc:
        return [], str(exc)
    rows = []
    for item in items:
        state = str(item.get("state") or "")
        progress = item.get("progress")
        percent = ""
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            percent = f"{round(float(progress) * 100)}%"
        priority = item.get("priority")
        place = priority if isinstance(priority, int) and not isinstance(priority, bool) else None
        rows.append(
            {
                "hash": str(item.get("hash") or "").lower(),
                "title": str(item.get("name") or item.get("title") or ""),
                "state_ru": _seed_state(item, state),
                "paused": state in _PAUSED,
                "can_move": state in _DOWNLOADING,
                "can_up": False,
                "can_down": False,
                "place": place if place is not None and place >= 0 else None,
                "speed": _size(int(item.get("dlspeed") or 0)) + "/с",
                "eta": eta_text(item.get("eta")),
                "progress": percent,
            }
        )
    rows.sort(
        key=lambda row: (
            (0, row["place"] if row["place"] is not None else 10**9) if row["can_move"] else (1, 0)
        )
    )
    movable = [row for row in rows if row["can_move"]]
    for index, row in enumerate(movable):
        row["place"] = index + 1
        row["can_up"] = index > 0
        row["can_down"] = index < len(movable) - 1
    return rows, ""


def _export_count(config: AppConfig, seen: list[StoredItem]) -> int:
    if not config.streaming.enabled:
        return 0
    try:
        torrents = _stream_client(config).list_torrents()
    except StreamError:
        return 0
    waiting = waiting_vk_hashes(torrents, seen)
    return len(UploadOrder(order_path(config.database_path)).arranged(waiting))


def _vk_waiting(config: AppConfig) -> tuple[list[str], list[dict]]:
    if not config.streaming.enabled:
        raise StreamError("qBittorrent выключен. Включите его в настройках.")
    torrents = _stream_client(config).list_torrents()
    with SeenStore(config.database_path) as store:
        seen = store.list_seen()
    return waiting_vk_hashes(torrents, seen), torrents


def _vk_queue(config: AppConfig) -> tuple[list[dict], str]:
    if not config.streaming.enabled:
        return [], "qBittorrent выключен. Включите его в настройках, чтобы видеть очередь VK."
    try:
        hashes, torrents = _vk_waiting(config)
    except StreamError as exc:
        return [], str(exc)
    order = UploadOrder(order_path(config.database_path)).apply(hashes)
    by_hash = {str(item.get("hash") or "").lower(): item for item in torrents}
    active = _active_upload_titles(config)
    rows = []
    for index, infohash in enumerate(order):
        torrent = by_hash.get(infohash)
        if torrent is None:
            continue
        title = str(torrent.get("name") or infohash)
        rows.append(
            {
                "hash": infohash,
                "title": title,
                "place": index + 1,
                "status": _export_status(torrent, title, active),
                "can_up": index > 0,
                "can_down": index < len(order) - 1,
            }
        )
    return rows, ""


def _active_upload_titles(config: AppConfig) -> set[str]:
    titles: set[str] = set()
    for job in UploadBoard(board_path(config.database_path)).jobs():
        if job.get("status") not in {"uploading", "processing", "stopping"}:
            continue
        title = str(job.get("title") or "")
        name = str(job.get("name") or "")
        if title:
            titles.add(title)
        if name:
            titles.add(name)
    return titles


def _drop_vk_queue_item(config: AppConfig, infohash: str) -> str:
    """Убрать фильм из очереди VK, стереть видео и файл .torrent."""
    digest = infohash.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{40}", digest):
        raise ValueError("Неизвестная раздача.")
    hashes, torrents = _vk_waiting(config)
    if digest not in hashes:
        raise ValueError("Этой раздачи уже нет в очереди.")
    torrent = next(
        (item for item in torrents if str(item.get("hash") or "").lower() == digest),
        {},
    )
    title = str(torrent.get("name") or digest)
    client = _stream_client(config)
    getter = getattr(client, "torrent_files", None)
    if not callable(getter):
        raise StreamError("qBittorrent не отдал список файлов раздачи.")
    files = getter(digest)
    if not isinstance(files, list):
        raise StreamError("qBittorrent не отдал список файлов раздачи.")
    if _queue_upload_busy(config, title, files):
        raise ValueError("Этот файл сейчас отправляется в VK. Сначала остановите отправку.")
    content = torrent.get("content_path")
    videos, torrent_files = discard_queued_files(
        config,
        digest,
        [item for item in files if isinstance(item, dict)],
        content_path=content if isinstance(content, str) else "",
    )
    client.remove(digest, delete_files=True)
    UploadOrder(order_path(config.database_path)).forget(digest)
    note = "Убрал из очереди и из qBittorrent."
    if videos:
        note += f" Удалил видео: {videos}."
    else:
        note += " Видеофайла в папке загрузок не было."
    note += f" Файлов .torrent удалено: {torrent_files}."
    return note


def _queue_upload_busy(config: AppConfig, title: str, files: list[dict]) -> bool:
    names = {Path(str(item.get("name") or "")).name for item in files}
    names.discard("")
    names.add(title)
    for job in UploadBoard(board_path(config.database_path)).jobs():
        if job.get("status") not in {"uploading", "processing", "stopping"}:
            continue
        if str(job.get("name") or "") in names or str(job.get("title") or "") in names:
            return True
    return False


def _export_status(torrent: dict, title: str, active: set[str]) -> str:
    if title in active:
        return "отправляется"
    text = _seed_state(torrent, str(torrent.get("state") or ""))
    if "раздаётся" in text or text == "скоро пауза и VK":
        return text
    return "ждёт отправки"


def _send_videos_to_vk(config: AppConfig) -> str:
    names = request.form.getlist("video")
    if not names:
        return "Не выбран файл."
    if not os.environ.get(config.vk.token_env, "").strip():
        return f"Сначала задайте токен VK в настройках, поле для {config.vk.token_env}."
    allowed = {item.filename for item in _manual_cards(config)}
    directory = config.delivery.download_dir
    paths: list[Path] = []
    for name in names:
        path = _video_in_folder(directory, name)
        if path is None:
            return "Можно отправить только видео из папки загрузок."
        if path.name not in allowed:
            return "Можно отправить только вручную скачанный фильм, который ещё не в VK."
        paths.append(path)
    threading.Thread(target=_upload_videos, args=(config, tuple(paths)), daemon=True).start()
    titles = ", ".join(path.name for path in paths)
    return f"Отправляю в VK: {titles}. Процент, остановка и ссылка — в разделе ВК."


def _upload_videos(config: AppConfig, paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            title = russian_title(config.rss.metadata_dir, path.name) or path.stem
            result = publish_file(config, path, title=title)
        except (VkError, ConfigError) as exc:
            logger.error("VK не принял %s: %s", path.name, exc)
            continue
        logger.info("VK принял %s: %s", path.name, " ".join(result.player_urls))


def _retry_upload(config: AppConfig, name: str, job_id: str) -> str:
    """Снова отправить остановленный файл. Старая строка очереди при этом убирается."""
    path = _video_by_name(config.delivery.download_dir, name)
    if path is None:
        raise VkError("Файл уже не лежит в папке загрузок.")
    if name in _RETRYING:
        return "Повтор уже запущен."
    busy = _upload_busy(config)
    _RETRYING.add(name)

    def run() -> None:
        try:
            while _upload_busy(config):
                time.sleep(5)
            UploadBoard(board_path(config.database_path)).drop(job_id)
            row = row_for_filename(config, path.name)
            title = row.title if row is not None else path.stem
            description = row.link if row is not None else ""
            row_id = row.row_id if row is not None else None
            publish_file(
                config,
                path,
                title=title,
                description=description,
                row_id=row_id,
            )
        except (VkError, ConfigError) as exc:
            logger.error("Повтор «%s» не удался: %s", path.name, exc)
        finally:
            _RETRYING.discard(name)

    threading.Thread(target=run, daemon=True).start()
    if busy:
        return "Сейчас уходит другое видео. Это отправится следом."
    return "Отправляю снова."


def _upload_busy(config: AppConfig) -> bool:
    jobs = UploadBoard(board_path(config.database_path)).jobs()
    return any(job.get("status") in {"uploading", "processing", "stopping"} for job in jobs)


def _video_by_name(directory: Path, name: str) -> Path | None:
    if not name or name != Path(name).name or Path(name).suffix.lower() not in _VIDEO_SUFFIXES:
        return None
    direct = directory / name
    if direct.is_file():
        return direct
    if not directory.is_dir():
        return None
    for path in directory.rglob(name):
        if path.is_file() and path.name == name:
            return path
    return None


def _video_in_folder(directory: Path, name: str) -> Path | None:
    if not name or name != Path(name).name or Path(name).suffix.lower() not in _VIDEO_SUFFIXES:
        return None
    root = directory.resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        return None
    return path


def _manual_cards(config: AppConfig) -> list[ManualCard]:
    """Докачанные вручную фильмы, которые ещё не ушли в VK."""
    directory = config.delivery.download_dir
    if not directory.is_dir():
        return []
    downloading = _downloading_state(config)
    cards: list[tuple[float, ManualCard]] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        if not path.is_file() or path.suffix.lower() not in _VIDEO_SUFFIXES:
            continue
        torrent = matching_manual_torrent(config.rss.metadata_dir, path.name)
        if torrent is None or already_sent_to_vk(config, path):
            continue
        if _still_downloading_video(path, torrent, downloading):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        source = torrent.stem
        title = release_title(source) or source
        cards.append(
            (
                stat.st_mtime,
                ManualCard(
                    filename=path.name,
                    title=source,
                    movie_title=title,
                    quality=quality_from_name(source) or quality_from_name(path.name),
                    size_bytes=stat.st_size,
                    genre=genres_from_title(source),
                ),
            )
        )
    cards.sort(key=lambda item: item[0], reverse=True)
    return [card for _, card in cards]


def _downloading_state(config: AppConfig) -> tuple[set[str], set[str]] | None:
    """Имена файлов и хеши раздач, которые ещё качаются. None — qBittorrent молчит."""
    if not config.streaming.enabled:
        return set(), set()
    try:
        client = _stream_client(config)
        torrents = client.list_torrents()
    except StreamError:
        return None
    names: set[str] = set()
    hashes: set[str] = set()
    getter = getattr(client, "torrent_files", None)
    for item in torrents:
        if not _still_downloading(item):
            continue
        digest = str(item.get("hash") or "").lower()
        if digest:
            hashes.add(digest)
        names.add(Path(str(item.get("name") or "")).name)
        if not callable(getter) or not digest:
            continue
        try:
            files = getter(digest)
        except (StreamError, TypeError, AttributeError):
            continue
        for file in files or []:
            names.add(Path(str(file.get("name") or "")).name)
    return names, hashes


def _still_downloading(item: dict) -> bool:
    state = str(item.get("state") or "")
    if state in _DOWNLOADING:
        return True
    progress = item.get("progress")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        return float(progress) < 0.999
    return False


def _still_downloading_video(
    path: Path,
    torrent: Path,
    downloading: tuple[set[str], set[str]] | None,
) -> bool:
    if downloading is None:
        return False
    names, hashes = downloading
    if path.name in names or torrent.stem in names or torrent.name in names:
        return True
    try:
        blob = torrent.read_bytes()
    except OSError:
        return False
    digest = torrent_info_hash(blob)
    return bool(digest and digest in hashes)


def _users_action(store: UserStore) -> tuple[str, bool]:
    action = request.form.get("action", "")
    name = request.form.get("name", "")
    password = request.form.get("password", "")
    try:
        if action == "add":
            store.add_user(name, password)
            return "Пользователь добавлен.", True
        if action == "password":
            store.set_password(name, password)
            return "Пароль сохранён.", True
        if action == "delete":
            store.remove_user(name)
            return "Пользователь удалён.", True
    except UserError as exc:
        return str(exc), False
    return "Не понял действие.", False


def _draft(config: AppConfig, wg_text: str, store: SecretStore) -> dict[str, object]:
    patterns = config.filters.patterns
    known = store.names()
    return {
        "poll_interval_minutes": config.rss.poll_interval_minutes,
        "rss_timeout": _trim(config.rss.timeout_seconds),
        "user_agent": config.rss.user_agent,
        "metadata_dir": config.rss.metadata_dir.as_posix(),
        "feeds": config.rss.feeds,
        "min_rating": "" if config.filters.min_rating is None else _trim(config.filters.min_rating),
        "max_age_days": "" if config.filters.max_age_days is None else config.filters.max_age_days,
        "genres": ", ".join(config.filters.genres),
        "quality_keep": config.filters.quality_keep,
        "pattern_rating": "" if patterns.rating is None else patterns.rating.pattern,
        "pattern_quality": "" if patterns.quality is None else patterns.quality.pattern,
        "pattern_genre": "" if patterns.genre is None else patterns.genre.pattern,
        "log_level": config.log_level,
        "database_path": config.database_path.as_posix(),
        "via_vpn": ", ".join(config.routing.via_vpn),
        "via_direct": ", ".join(config.routing.via_direct),
        "vpn_domains": ", ".join(config.routing.vpn_domains),
        "required_vpn_domains": ", ".join(REQUIRED_VPN_DOMAINS),
        "interface": config.vpn.interface,
        "vpn_timeout": _trim(config.vpn.command_timeout_seconds),
        "config_path_vpn": str(config.vpn.config_path),
        "telegram_timeout": _trim(config.telegram.timeout_seconds),
        "telegram_token_set": config.telegram.token_env in known,
        "telegram_chat_set": config.telegram.chat_id_env in known,
        "feed_user_set": (config.credentials.username_env or "POTOK_FEED_USER") in known,
        "feed_password_set": (config.credentials.password_env or "POTOK_FEED_PASSWORD") in known,
        "vk_token_set": config.vk.token_env in known,
        "stream_user_set": config.streaming.username_env in known,
        "stream_password_set": config.streaming.password_env in known,
        "jackett_key_set": config.jackett.api_key_env in known,
        "tmdb_key_set": config.tmdb.api_key_env in known,
        "telegram_token": _stored_secret(store, config.telegram.token_env),
        "telegram_chat_id": _stored_secret(store, config.telegram.chat_id_env),
        "feed_username": _stored_secret(
            store, config.credentials.username_env or "POTOK_FEED_USER"
        ),
        "feed_password": _stored_secret(
            store, config.credentials.password_env or "POTOK_FEED_PASSWORD"
        ),
        "vk_token": _stored_secret(store, config.vk.token_env),
        "stream_username": _stored_secret(store, config.streaming.username_env),
        "stream_password": _stored_secret(store, config.streaming.password_env),
        "jackett_api_key": _stored_secret(store, config.jackett.api_key_env),
        "tmdb_api_key": _stored_secret(store, config.tmdb.api_key_env),
        "jackett_base_url": config.jackett.base_url,
        "jackett_rss_url": config.jackett.rss_url,
        "jackett_min_seeders": config.jackett.min_seeders,
        "jackett_min_size_bytes": config.jackett.min_size_bytes,
        "jackett_page_count": config.jackett.page_count,
        "api_version": config.vk.api_version,
        "app_id": "" if config.vk.app_id is None else config.vk.app_id,
        "group_id": "" if config.vk.group_id is None else config.vk.group_id,
        "privacy_view": config.vk.privacy_view,
        "wallpost": config.vk.wallpost,
        "is_private": config.vk.is_private,
        "chunk_bytes": config.vk.chunk_bytes,
        "max_bytes": config.vk.max_bytes,
        "upload_attempts": config.vk.upload_attempts,
        "upload_idle_timeout_seconds": _trim(config.vk.upload_idle_timeout_seconds),
        "poll_interval_seconds": _trim(config.vk.poll_interval_seconds),
        "poll_timeout_seconds": _trim(config.vk.poll_timeout_seconds),
        "api_timeout_seconds": _trim(config.vk.api_timeout_seconds),
        "api_retries": config.vk.api_retries,
        "download_dir": config.delivery.download_dir.as_posix(),
        "aria2_bin": config.delivery.aria2_bin,
        "aria2_timeout": _trim(config.delivery.aria2_timeout_seconds),
        "streaming_enabled": config.streaming.enabled,
        "auto_add": config.streaming.auto_add,
        "base_url": config.streaming.base_url,
        "public_url": config.streaming.public_url,
        "stream_timeout": _trim(config.streaming.timeout_seconds),
        "server_torrent_dir": config.streaming.server_torrent_dir,
        "portal_host": config.streaming.portal_host,
        "portal_port": config.streaming.portal_port,
        "wg_conf": wg_text,
    }


def _runtime_text(minutes: int | None) -> str:
    """Длительность для карточки: «1 ч 42 мин». Ноль значит, что TMDB её не знает."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        return "нет"
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def _decision_page(status: str, template: str) -> str:
    config = _require_config()
    size, page = _queue_params()
    with SeenStore(config.database_path) as store:
        total = store.counts().get(status, 0)
        pages = max(1, (total + size - 1) // size) if total else 1
        if page > pages:
            page = pages
        items = (
            store.list_status_page(status, limit=size, offset=(page - 1) * size) if total else []
        )
    genres, qualities = _pending_choices(items)
    return render_template(
        template,
        items=items,
        total=total,
        size=size,
        page=page,
        pages=pages,
        page_numbers=_page_numbers(page, pages),
        page_sizes=_PAGE_SIZES,
        genres=genres,
        qualities=qualities,
        movie_year=_movie_year,
        runtime_text=_runtime_text,
        release_title=release_title,
    )


def _revoke_note(config: AppConfig, item: StoredItem) -> str:
    """Текст после снятия одобрения. Раздачу убираем из qBittorrent, файл на диске оставляем."""
    note = "Одобрение снято. Фильм снова в очереди «Ожидают»."
    dropped = _drop_approved_download(config, item)
    if dropped == "removed":
        note += " Раздача убрана из qBittorrent, файл на диске остаётся."
    elif dropped == "error":
        note += " Загрузку в qBittorrent убрать не удалось."
    if item.vk_url:
        note += " Ролик во ВК при этом не удаляется."
    return note


def _drop_approved_download(config: AppConfig, item: StoredItem) -> str:
    if not config.streaming.enabled:
        return ""
    try:
        client = _stream_client(config)
        hashes = _download_hashes(item, client.list_torrents())
        for infohash in hashes:
            client.remove(infohash)
    except StreamError:
        return "error"
    return "removed" if hashes else ""


def _download_hashes(item: StoredItem, torrents: list[dict]) -> list[str]:
    digest = _infohash_of(item.torrent_url)
    title = " ".join(item.title.split())
    found: list[str] = []
    for torrent in torrents:
        infohash = str(torrent.get("hash") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{40}", infohash):
            continue
        name = " ".join(str(torrent.get("name") or "").split())
        if (digest and infohash == digest) or (title and name == title):
            found.append(infohash)
    return found


def _infohash_of(url: str | None) -> str | None:
    if not url:
        return None
    magnet = re.search(r"btih:([a-fA-F0-9]{40})", url, re.IGNORECASE)
    if magnet:
        return magnet.group(1).lower()
    plain = re.search(r"(?<![a-fA-F0-9])([a-fA-F0-9]{40})(?![a-fA-F0-9])", url)
    if plain:
        return plain.group(1).lower()
    return None


def _movie_year(title: str) -> int | None:
    """Год из названия раздачи: «Фильм (1999)» или «Фильм [1999»."""
    _, year = movie_query(title)
    return year


def _pending_choices(items: list[StoredItem]) -> tuple[list[str], list[str]]:
    """Уникальные жанры и качества для списков фильтра."""
    genres: set[str] = set()
    qualities: set[str] = set()
    for item in items:
        if item.quality and item.quality.strip():
            qualities.add(item.quality.strip())
        for text in genre_parts(item.genre, pretty=True):
            genres.add(text)
    return (
        sorted(genres, key=str.casefold),
        sorted(qualities, key=str.casefold),
    )


def _tmdb_image(url: str | None) -> bool:
    """Постер берём только с image.tmdb.org, не с произвольного адреса."""
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host == "image.tmdb.org" and parts.path.startswith("/t/p/")


def _read_poster(url: str) -> tuple[bytes, str]:
    """Скачать постер на сервере. В браузере image.tmdb.org часто не открывается."""
    import potok.tmdb  # noqa: F401  подмена DNS для доменов tmdb.org

    request = Request(url, headers={"User-Agent": "Potok/0.2"})
    with urlopen(request, timeout=20) as response:
        kind = str(response.headers.get("Content-Type", "image/jpeg")).split(";", 1)[0]
        body = response.read(2_000_000)
    if not body:
        raise URLError("Пустой постер")
    if not kind.startswith("image/"):
        kind = "image/jpeg"
    return body, kind.strip()


def _current_config(app: Flask) -> AppConfig:
    path: Path = app.config["POTOK_CONFIG_PATH"]
    try:
        g.config_error = ""
        return load_config(path)
    except ConfigError as exc:
        g.config_error = str(exc)
        return app.config["POTOK_FALLBACK"]


def _require_config() -> AppConfig:
    config = g.config
    if not isinstance(config, AppConfig):
        abort(500)
    return config


def _stream_client(config: AppConfig) -> QbitClient:
    injected = current_app.config.get("POTOK_STREAM")
    if injected is not None:
        return injected
    return QbitClient(config.streaming)


def _users(app: Flask) -> UserStore:
    return app.config["POTOK_USERS"]


def _vk_authorize_link(config: AppConfig) -> str:
    if config.vk.app_id is None:
        return ""
    return blank_authorize_url(app_id=config.vk.app_id, api_version=config.vk.api_version)


def _vk_token_status(config: AppConfig, store: SecretStore):
    stored = store.get(config.vk.token_env) or ""
    has_token = bool(stored.strip() or os.environ.get(config.vk.token_env, "").strip())
    return describe_token(
        has_token=has_token,
        expires_at=store.get(EXPIRES_NAME),
        now=time.time(),
    )


def _check_csrf() -> None:
    expected = session.get("csrf", "")
    posted = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or len(expected) != len(posted) or not hmac.compare_digest(expected, posted):
        abort(400, "Форма устарела. Обновите страницу и отправьте её ещё раз.")


_VK_CATALOG_SECONDS = 15.0
_vk_catalog_lock = threading.Lock()
_vk_catalog_memory: dict[str, tuple[float, list[dict[str, object]], str]] = {}
_vk_catalog_busy: set[str] = set()
_vk_catalog_events: dict[str, threading.Event] = {}
_vk_catalog_generation: dict[str, int] = {}


def _catalog_key(config: AppConfig) -> str:
    return str(config.database_path.resolve())


def _vk_catalog_path(config: AppConfig) -> Path:
    return data_dir_for(config.database_path) / "vk_catalog.json"


def _read_vk_catalog(config: AppConfig) -> list[dict[str, object]] | None:
    path = _vk_catalog_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    videos = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(videos, list):
        return None
    return [item for item in videos if isinstance(item, dict)]


def _write_vk_catalog(config: AppConfig, videos: list[dict[str, object]]) -> None:
    path = _vk_catalog_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"videos": videos}, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _forget_vk_catalog(config: AppConfig) -> None:
    """После правки или удаления следующий заход берёт список у ВК заново."""
    key = _catalog_key(config)
    with _vk_catalog_lock:
        _vk_catalog_generation[key] = _vk_catalog_generation.get(key, 0) + 1
        _vk_catalog_memory.pop(key, None)
    try:
        _vk_catalog_path(config).unlink()
    except OSError:
        pass


def _remember_vk_catalog(
    key: str,
    config: AppConfig,
    videos: list[dict[str, object]],
    error: str,
    generation: int,
    fallback: tuple[float, list[dict[str, object]], str] | None,
) -> None:
    with _vk_catalog_lock:
        if _vk_catalog_generation.get(key, 0) != generation:
            return
        if error and fallback is not None:
            _vk_catalog_memory[key] = (time.time(), fallback[1], "")
            return
        _vk_catalog_memory[key] = (time.time(), videos, error)
    if error:
        return
    try:
        _write_vk_catalog(config, videos)
    except OSError as exc:
        logger.warning("Не записал список ВК на диск: %s", exc)


def _vk_catalog(config: AppConfig, uploads: list[dict]) -> tuple[list[dict[str, object]], str]:
    """Список роликов. Пока он свежий, страницу не заставляем ждать api.vk.com."""
    key = _catalog_key(config)
    now = time.time()
    with _vk_catalog_lock:
        cached = _vk_catalog_memory.get(key)
        if cached is not None and now - cached[0] < _VK_CATALOG_SECONDS:
            return cached[1], cached[2]
        event = _vk_catalog_events.get(key)
        if key in _vk_catalog_busy and event is not None:
            leader = False
        else:
            leader = True
            _vk_catalog_busy.add(key)
            event = threading.Event()
            _vk_catalog_events[key] = event
            if cached is None:
                disk = _read_vk_catalog(config)
                if disk is not None:
                    cached = (now - _VK_CATALOG_SECONDS - 1, disk, "")
                    _vk_catalog_memory[key] = cached
        stale = cached
        generation = _vk_catalog_generation.get(key, 0)
    if event is None:
        return [], "Список ВК ещё загружается. Обновите страницу."
    if not leader:
        if stale is not None:
            return stale[1], stale[2]
        event.wait(90)
        with _vk_catalog_lock:
            cached = _vk_catalog_memory.get(key)
        if cached is not None:
            return cached[1], cached[2]
        return [], "Список ВК ещё загружается. Обновите страницу."
    if stale is not None and not current_app.config.get("TESTING"):
        app = current_app._get_current_object()

        def refresh() -> None:
            try:
                with app.app_context(), app.test_request_context():
                    videos, error = _build_vk_catalog(config, uploads)
                _remember_vk_catalog(key, config, videos, error, generation, stale)
            except Exception:
                logger.exception("Не обновился список ВК")
            finally:
                with _vk_catalog_lock:
                    _vk_catalog_busy.discard(key)
                event.set()

        threading.Thread(target=refresh, name="vk-catalog", daemon=True).start()
        return stale[1], stale[2]
    if stale is not None:
        with _vk_catalog_lock:
            _vk_catalog_busy.discard(key)
        event.set()
        return stale[1], stale[2]
    try:
        videos, error = _build_vk_catalog(config, uploads)
        _remember_vk_catalog(key, config, videos, error, generation, None)
        return videos, error
    finally:
        with _vk_catalog_lock:
            _vk_catalog_busy.discard(key)
        event.set()


def _build_vk_catalog(
    config: AppConfig, uploads: list[dict]
) -> tuple[list[dict[str, object]], str]:
    started = time.perf_counter()
    try:
        with SeenStore(config.database_path) as store:
            known = store.list_seen()
    except sqlite3.OperationalError as exc:
        logger.warning("База не открылась, карточки ВК будут без обложек: %s", exc)
        known = []
    try:
        _matched_files(config, known)
    except sqlite3.OperationalError as exc:
        logger.warning("База не открылась, карточки ВК будут без обложек: %s", exc)
    try:
        listed = _with_russian_titles(config, _vk_client(config).list_videos())
        catalog = _keep_uuid_names(config, listed, uploads, known)
        videos = [
            _video_row(
                item,
                _with_cover(
                    _stored_for_video(known, item) or _stored_from_upload(uploads, item),
                    known,
                ),
            )
            for item in catalog
        ]
        error = ""
    except VkError as exc:
        videos = []
        error = str(exc)
    logger.info(
        "Список ВК собран за %.1f с, роликов: %s",
        time.perf_counter() - started,
        len(videos),
    )
    return videos, error


def _with_russian_titles(
    config: AppConfig,
    items: tuple[VkCatalogItem, ...],
) -> tuple[VkCatalogItem, ...]:
    """Латинское имя файла заменить русским из .torrent и записать его во VK."""
    renamed: list[VkCatalogItem] = []
    client: VkVideoClient | None = None
    for item in items:
        key = (item.owner_id, item.video_id)
        cached = _FIXED_TITLES.get(key)
        if cached:
            renamed.append(replace(item, title=cached))
            continue
        if has_cyrillic(item.title) or _is_uuid(item.title):
            renamed.append(item)
            continue
        title = russian_title(config.rss.metadata_dir, item.title)
        if title is None or title == item.title:
            renamed.append(item)
            continue
        try:
            if client is None:
                client = _vk_client(config)
            client.edit_video(
                item.owner_id,
                item.video_id,
                title=title,
                description=item.description,
            )
        except VkError as exc:
            logger.warning("Не удалось записать русское имя %s_%s: %s", *key, exc)
            renamed.append(item)
            continue
        _FIXED_TITLES[key] = title
        renamed.append(replace(item, title=title))
    return tuple(renamed)


def _video_by_id(config: AppConfig, owner_id: int, video_id: int) -> VkCatalogItem:
    items = _with_russian_titles(config, _vk_client(config).list_videos())
    for item in items:
        if item.owner_id == owner_id and item.video_id == video_id:
            return item
    raise VkError("Видео не найдено во ВКонтакте.")


def _vk_client(config: AppConfig) -> VkVideoClient:
    token = os.environ.get(config.vk.token_env, "").strip()
    if not token:
        raise VkError(f"Сначала задайте токен VK в настройках, поле для {config.vk.token_env}.")
    return VkVideoClient(config.vk, token)


def _api_body() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise VkError("Нужен JSON-объект.")
    return payload


def _vk_ids(payload: dict) -> tuple[int, int]:
    owner_id = payload.get("owner_id")
    video_id = payload.get("video_id")
    if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id == 0:
        raise VkError("Укажите owner_id видео.")
    if isinstance(video_id, bool) or not isinstance(video_id, int) or video_id < 1:
        raise VkError("Укажите video_id видео.")
    return owner_id, video_id


def _delete_unfinished(config: AppConfig, job: dict) -> str:
    owner_id = job.get("owner_id")
    video_id = job.get("video_id")
    if isinstance(owner_id, bool) or not isinstance(owner_id, int):
        return ""
    if isinstance(video_id, bool) or not isinstance(video_id, int):
        return ""
    try:
        _vk_client(config).delete_video(owner_id, video_id)
    except VkError as exc:
        return str(exc)
    _forget_vk_catalog(config)
    return ""


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MATCH_AT = 0.0
_MATCH_ROWS: dict[str, StoredItem] = {}
_RETRYING: set[str] = set()


def _is_uuid(text: str) -> bool:
    return bool(_UUID_RE.match(text.strip()))


_UUID_KEPT: set[tuple[int, int]] = set()


def _keep_uuid_names(
    config: AppConfig,
    items: tuple[VkCatalogItem, ...],
    uploads: list[dict],
    rows: list[StoredItem],
) -> tuple[VkCatalogItem, ...]:
    """Во VK имя ролика — код загрузки. Если ВК подставил своё, вернуть код."""
    client: VkVideoClient | None = None
    renamed: list[VkCatalogItem] = []
    for item in items:
        key = (item.owner_id, item.video_id)
        if key in _UUID_KEPT:
            renamed.append(item)
            continue
        code = _code_for_video(item, uploads, rows)
        if not code or item.title.strip() == code:
            if code:
                _UUID_KEPT.add(key)
            renamed.append(item)
            continue
        try:
            if client is None:
                client = _vk_client(config)
            client.edit_video(
                item.owner_id,
                item.video_id,
                title=code,
                description=item.description,
            )
        except VkError as exc:
            logger.warning("Не вернул код имени %s_%s: %s", *key, exc)
            renamed.append(item)
            continue
        _UUID_KEPT.add(key)
        renamed.append(replace(item, title=code))
    return tuple(renamed)


def _code_for_video(item: VkCatalogItem, uploads: list[dict], rows: list[StoredItem]) -> str:
    for job in uploads:
        if job.get("owner_id") == item.owner_id and job.get("video_id") == item.video_id:
            code = str(job.get("vk_uuid") or "").strip()
            if code:
                return code
    marker = f"video{item.owner_id}_{item.video_id}"
    for row in rows:
        for line in (row.vk_uuid or "").splitlines():
            if marker not in line:
                continue
            for part in line.split():
                if _is_uuid(part):
                    return part
    title = item.title.strip()
    if _is_uuid(title):
        return title
    return ""


def _matched_files(
    config: AppConfig, rows: list[StoredItem] | None = None
) -> dict[str, StoredItem]:
    """Не спрашивать qBittorrent на каждой перерисовке страницы."""
    global _MATCH_AT, _MATCH_ROWS
    now = time.time()
    if now - _MATCH_AT < 30:
        return _MATCH_ROWS
    found = bind_uploaded_codes(config, rows)
    _MATCH_AT = now
    _MATCH_ROWS = found
    return found


def _stored_from_upload(uploads: list[dict], item: VkCatalogItem) -> StoredItem | None:
    for job in uploads:
        same_video = job.get("owner_id") == item.owner_id and job.get("video_id") == item.video_id
        same_code = bool(job.get("vk_uuid")) and job.get("vk_uuid") == item.title.strip()
        if not same_video and not same_code:
            continue
        row = _MATCH_ROWS.get(str(job.get("name") or ""))
        if row is not None:
            return row
    return None


def _with_cover(stored: StoredItem | None, rows: list[StoredItem]) -> StoredItem | None:
    """Если у этой раздачи нет обложки, взять её у той же картины с другой раздачи."""
    if stored is None:
        return None
    if stored.poster_url and stored.overview and stored.genre:
        return stored
    short = release_title(stored.title)
    if not short:
        return stored
    donor: StoredItem | None = None
    for row in rows:
        if row.row_id == stored.row_id or release_title(row.title) != short:
            continue
        if not (row.poster_url or row.overview):
            continue
        if donor is None or (row.poster_url and not donor.poster_url):
            donor = row
    if donor is None:
        return stored
    poster_id = stored.row_id if stored.poster_url else donor.row_id
    return replace(
        stored,
        row_id=poster_id,
        poster_url=stored.poster_url or donor.poster_url,
        overview=stored.overview or donor.overview,
        rating=stored.rating if stored.rating is not None else donor.rating,
        quality=stored.quality or donor.quality,
        genre=stored.genre or donor.genre,
        country=stored.country or donor.country,
        seeders=stored.seeders if stored.seeders is not None else donor.seeders,
        size_bytes=stored.size_bytes if stored.size_bytes is not None else donor.size_bytes,
        runtime_minutes=stored.runtime_minutes or donor.runtime_minutes,
        movie_title=stored.movie_title or donor.movie_title,
    )


def _stored_for_video(rows: list[StoredItem], item: VkCatalogItem) -> StoredItem | None:
    """Найти фильм в базе: сначала UUID ролика, потом ссылка, потом название."""
    title = item.title.strip()
    marker = f"video{item.owner_id}_{item.video_id}"
    for row in rows:
        blob = f"{row.vk_uuid or ''}\n{row.vk_url or ''}"
        if marker in blob:
            return row
    if title:
        for row in rows:
            uuids = [part.strip() for part in (row.vk_uuid or "").splitlines() if part.strip()]
            if title in uuids:
                return row
    if marker:
        for row in rows:
            if marker in (row.vk_url or ""):
                return row
    if not title:
        return None
    short = release_title(title)
    for row in rows:
        if title == (row.title or "").strip():
            return row
        if short and short == release_title(row.title):
            return row
        if title == (row.movie_title or "").strip():
            return row
    return None


def _card_title(stored: StoredItem | None, fallback: str) -> str:
    """На портале имя фильма из базы, как в очереди ожидания. Во VK остаётся код."""
    if stored is None:
        return fallback
    movie = (stored.movie_title or "").strip()
    if movie:
        return movie
    short = release_title(stored.title)
    if short:
        return short
    text = (stored.title or "").strip()
    return text or fallback


def _size_text(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "нет"
    return f"{size_bytes / 1073741824:.2f} ГБ"


def _video_row(item: VkCatalogItem, stored: StoredItem | None = None) -> dict[str, object]:
    clock = _clock(item.duration_seconds)
    runtime = _runtime_text(None if stored is None else stored.runtime_minutes)
    if runtime == "нет" and clock:
        runtime = clock
    year = ""
    if stored is not None:
        found = _movie_year(stored.title) or _movie_year(_card_title(stored, stored.title))
        year = "" if found is None else str(found)
    image = item.image_url
    if stored is not None and _tmdb_image(stored.poster_url):
        image = url_for("poster", row_id=stored.row_id)
    shown = _card_title(stored, item.title)
    return {
        "owner_id": item.owner_id,
        "video_id": item.video_id,
        "title": shown,
        "vk_title": item.title,
        "description": item.description,
        "vk_description": item.description,
        "overview": "" if stored is None else (stored.overview or ""),
        "page_url": item.page_url,
        "player_url": item.player_url,
        "duration": runtime,
        "rating": "нет" if stored is None or stored.rating is None else f"{stored.rating:g}",
        "quality": "нет" if stored is None or not stored.quality else stored.quality,
        "genre": genre_text(None if stored is None else stored.genre) or "нет",
        "country": country_text(None if stored is None else stored.country),
        "year": year or "нет",
        "seeds": "нет" if stored is None or stored.seeders is None else str(stored.seeders),
        "size": _size_text(None if stored is None else stored.size_bytes),
        "processing": item.processing,
        "image_url": image,
    }


def _clock(seconds: int) -> str:
    if seconds <= 0:
        return ""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _json_ok(payload: dict) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False), mimetype="application/json")


def _json_error(status: int, message: str) -> Response:
    body = json.dumps({"ok": False, "error": message}, ensure_ascii=False)
    return Response(body, status=status, mimetype="application/json")


def probe_wireguard(config: AppConfig) -> str:
    """Проверить туннель в worker, если виден Docker. Иначе — в этом же процессе."""
    sock = Path(os.environ.get("POTOK_DOCKER_SOCK", "/var/run/docker.sock"))
    if sock.exists():
        timeout = max(config.vpn.command_timeout_seconds * 3, 45)
        code, stdout, stderr = run_in_worker(
            ["python", "-m", "potok", "probe"],
            sock=sock,
            timeout=timeout,
        )
        text = redact_secrets(stdout if code == 0 else stderr or stdout).strip()
        if code != 0:
            raise VpnError(text or f"Проверка VPN завершилась с кодом {code}.")
        return text or f"Туннель {config.vpn.interface} включён."
    vpn = VpnManager(
        interface=config.vpn.interface,
        config_path=config.vpn.config_path,
        timeout_seconds=config.vpn.command_timeout_seconds,
    )
    return vpn.probe()


def _check_telegram(config: AppConfig, store: SecretStore, posted: str) -> tuple[str, str]:
    """Спросить у Telegram имя бота. Новый токен пишется в secrets.enc, не в yaml."""
    typed = posted.strip()
    if any(char in typed for char in "\r\n\x00 "):
        return "danger", "Токен бота должен быть одной строкой без пробелов."
    env_name = config.telegram.token_env
    token = typed or (store.get(env_name) or os.environ.get(env_name, "")).strip()
    if not token:
        return "danger", "Сначала вставьте токен бота. Его присылает @BotFather."
    username = _telegram_username(token, config.telegram.timeout_seconds)
    if username.startswith("Ошибка:"):
        return "danger", username.removeprefix("Ошибка: ")
    previous = store.get(env_name)
    note = ""
    if typed and typed != previous:
        try:
            store.set_many({env_name: typed})
        except SecretError as exc:
            return "danger", str(exc)
        os.environ[env_name] = typed
        note = " Токен записан в secrets.enc. " + restart_worker()
    return "success", f"Токен подходит. Бот @{username}.{note}"


def _telegram_username(token: str, timeout: float) -> str:
    request = Request(f"https://api.telegram.org/bot{token}/getMe")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(100_000)
    except HTTPError as exc:
        if exc.code == 401:
            return "Ошибка: Токен не принят. Скопируйте его у @BotFather целиком."
        return f"Ошибка: Telegram ответил {exc.code}."
    except (URLError, TimeoutError, OSError) as exc:
        return f"Ошибка: Нет связи с Telegram: {exc}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return "Ошибка: Telegram вернул не JSON."
    if not isinstance(payload, dict) or not payload.get("ok"):
        return "Ошибка: Telegram не принял токен."
    result = payload.get("result")
    if not isinstance(result, dict):
        return "Ошибка: Telegram не прислал имя бота."
    username = result.get("username")
    if not isinstance(username, str) or not username:
        return "Ошибка: У бота нет имени пользователя."
    return username


def _check_tmdb(config: AppConfig, store: SecretStore, posted: str) -> tuple[str, str]:
    """Проверить ключ пробным поиском. В файл настроек ключ не пишется."""
    typed = posted.strip()
    if any(char in typed for char in "\r\n\x00"):
        return "danger", "Ключ TMDB должен быть одной строкой."
    env_name = config.tmdb.api_key_env
    key = typed or (store.get(env_name) or os.environ.get(env_name, "")).strip()
    if not key:
        return "danger", "Сначала вставьте ключ TMDB."
    try:
        movie = search_movie(key, "Матрица", timeout=config.tmdb.timeout_seconds)
    except TmdbError as exc:
        return "danger", str(exc)
    previous = store.get(env_name)
    note = ""
    if typed and typed != previous:
        try:
            store.set_many({env_name: typed})
        except SecretError as exc:
            return "danger", str(exc)
        os.environ[env_name] = typed
        note = " Ключ записан в secrets.enc. " + restart_worker()
    year = movie.year or "год неизвестен"
    rating = "нет" if movie.rating is None else f"{movie.rating:.1f}"
    return (
        "success",
        f"Ключ TMDB подходит. Для проверки найден фильм «{movie.title}» "
        f"({year}), рейтинг {rating}.{note}",
    )


def _stored_secret(store: SecretStore, name: str | None) -> str:
    """Значение из secrets.enc. Пустое имя или пустой файл — пустое поле."""
    if not name:
        return ""
    return store.get(name) or ""


def _trim(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _size(value: int) -> str:
    if value < 1024:
        return f"{value} Б"
    if value < 1024**2:
        return f"{value / 1024:.1f} КБ"
    if value < 1024**3:
        return f"{value / 1024**2:.1f} МБ"
    return f"{value / 1024**3:.2f} ГБ"
