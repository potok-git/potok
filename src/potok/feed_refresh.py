"""Фоновое чтение ленты по страницам. Страница сайта забирает уже готовые фильмы."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from potok.config import AppConfig

logger = logging.getLogger(__name__)

ProgressHook = Callable[[int, int, int], None]
Work = Callable[[AppConfig, ProgressHook], None]


@dataclass(frozen=True)
class FeedProgress:
    running: bool = False
    page: int = 0
    pages: int = 0
    added: int = 0
    message: str = ""
    error: str = ""


_lock = threading.Lock()
_state = FeedProgress()
_worker: threading.Thread | None = None


def snapshot() -> FeedProgress:
    with _lock:
        return replace(_state)


def reset() -> None:
    """Сбросить состояние. Нужно тестам, чтобы прошлый прогон не мешал."""
    global _state, _worker
    with _lock:
        if _state.running:
            return
        _state = FeedProgress()
        _worker = None


def begin(config: AppConfig, *, work: Work | None = None) -> bool:
    """Начать чтение. False, если предыдущее ещё не закончилось."""
    global _worker
    with _lock:
        if _state.running:
            return False
        _set_locked(
            running=True,
            page=0,
            pages=0,
            added=0,
            error="",
            message="Читаю ленту.",
        )
    thread = threading.Thread(
        target=_execute,
        args=(config, work or _fetch),
        name="feed-refresh",
        daemon=True,
    )
    with _lock:
        _worker = thread
    thread.start()
    return True


def wait(timeout: float = 5) -> None:
    thread = _worker
    if thread is not None:
        thread.join(timeout)


def note(page: int, pages: int, added: int) -> None:
    with _lock:
        if not _state.running:
            return
        _set_locked(
            page=page,
            pages=pages,
            added=added,
            message=f"Страница {page} из {pages}. Новых фильмов: {added}.",
        )


def _execute(config: AppConfig, work: Work) -> None:
    try:
        work(config, note)
    except Exception as exc:
        logger.error("Обновление ленты: %s", exc)
        with _lock:
            _set_locked(
                running=False,
                error=str(exc),
                message=f"Не удалось обновить ленту. {exc}",
            )
        return
    with _lock:
        _set_locked(running=False, message=f"Готово. Новых фильмов: {_state.added}.")


def _fetch(config: AppConfig, on_page: ProgressHook) -> None:
    from potok.pipeline import run_fetch
    from potok.secrets import SecretStore
    from potok.vpn import VpnManager

    SecretStore(config.data_dir()).apply_to_environ()
    vpn = VpnManager(
        interface=config.vpn.interface,
        config_path=config.vpn.config_path,
        timeout_seconds=config.vpn.command_timeout_seconds,
    )
    run_fetch(config, vpn, on_page=on_page)


def _set_locked(**changes: object) -> None:
    global _state
    _state = replace(_state, **changes)
