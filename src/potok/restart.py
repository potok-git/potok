"""Перезапуск worker и команды в нём, если из портала виден Docker."""

from __future__ import annotations

import json
import os
from http.client import HTTPConnection
from pathlib import Path

_CONTAINER = "potok-worker"
# Jackett и qBittorrent сидят в сети worker, чтобы их запросы к трекерам
# шли по тем же маршрутам. После перезапуска worker их нужно поднять снова.
_SIDECARS = ("potok-jackett", "potok-qbittorrent")
MANUAL = (
    "Контейнер worker отсюда не перезапустился. "
    "Если бот уже запущен, в папке проекта выполните: docker compose restart worker"
)


def restart_worker(sock: Path | None = None) -> str:
    """Перезапустить potok-worker. Без сокета Docker возвращает команду для ручного запуска."""
    path = sock or Path(os.environ.get("POTOK_DOCKER_SOCK", "/var/run/docker.sock"))
    if not path.exists():
        return MANUAL
    status, body = _request(path, "POST", f"/containers/{_CONTAINER}/restart?t=10")
    if status in {200, 204}:
        for name in _SIDECARS:
            side_status, _side_body = _request(path, "POST", f"/containers/{name}/restart?t=10")
            if side_status not in {200, 204, 404}:
                logger_note = side_status
                return (
                    "Служба ленты перезапущена, но контейнер "
                    f"{name} не поднялся (ответ {logger_note}). "
                    f"Выполните: docker compose restart {name.removeprefix('potok-')}"
                )
        return "Служба ленты перезапущена и сейчас заберёт новые записи."
    if status == 404:
        return f"Контейнер {_CONTAINER} не найден. {MANUAL}"
    detail = " ".join(body.decode("utf-8", errors="replace").split())[:180]
    return f"Docker ответил {status}. {detail} {MANUAL}"


def run_in_worker(
    command: list[str],
    *,
    sock: Path | None = None,
    timeout: float = 90,
) -> tuple[int, str, str]:
    """Выполнить команду в potok-worker. Возвращает код, stdout и stderr."""
    path = sock or Path(os.environ.get("POTOK_DOCKER_SOCK", "/var/run/docker.sock"))
    if not path.exists():
        raise OSError(MANUAL)
    created, raw = _request(
        path,
        "POST",
        f"/containers/{_CONTAINER}/exec",
        json.dumps(
            {"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": command}
        ).encode(),
        timeout=timeout,
    )
    if created == 404:
        raise OSError(f"Контейнер {_CONTAINER} не найден. {MANUAL}")
    if created not in {200, 201}:
        detail = raw.decode("utf-8", errors="replace")[:180]
        raise OSError(f"Docker не запустил команду, ответ {created}. {detail}")
    exec_id = json.loads(raw.decode("utf-8")).get("Id")
    if not isinstance(exec_id, str) or not exec_id:
        raise OSError("Docker не вернул номер команды.")
    started, stream = _request(
        path,
        "POST",
        f"/exec/{exec_id}/start",
        b'{"Detach": false, "Tty": false}',
        timeout=timeout,
    )
    if started not in {200, 101}:
        detail = stream.decode("utf-8", errors="replace")[:180]
        raise OSError(f"Docker не начал команду, ответ {started}. {detail}")
    stdout, stderr = _demux(stream)
    inspected, info = _request(path, "GET", f"/exec/{exec_id}/json", timeout=timeout)
    code = 1
    if inspected == 200:
        exit_code = json.loads(info.decode("utf-8")).get("ExitCode")
        if isinstance(exit_code, int):
            code = exit_code
    return code, stdout, stderr


def _demux(payload: bytes) -> tuple[str, str]:
    """Разобрать поток Docker: 8 байт заголовка, затем кусок stdout или stderr."""
    stdout: list[str] = []
    stderr: list[str] = []
    index = 0
    while index + 8 <= len(payload):
        kind = payload[index]
        size = int.from_bytes(payload[index + 4 : index + 8], "big")
        index += 8
        chunk = payload[index : index + size]
        index += size
        if kind == 1:
            stdout.append(chunk.decode("utf-8", errors="replace"))
        elif kind == 2:
            stderr.append(chunk.decode("utf-8", errors="replace"))
        else:
            return payload.decode("utf-8", errors="replace"), ""
    if stdout or stderr:
        return "".join(stdout), "".join(stderr)
    text = payload.decode("utf-8", errors="replace")
    return text, ""


def _request(
    sock: Path,
    method: str,
    url: str,
    payload: bytes | None = None,
    *,
    timeout: float = 30,
    limit: int | None = None,
) -> tuple[int, bytes]:
    connection = _UnixHTTPConnection(str(sock), timeout=timeout)
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    try:
        connection.request(method, url, body=payload, headers=headers)
        response = connection.getresponse()
        body = response.read() if limit is None else response.read(limit)
        return response.status, body
    except OSError as exc:
        return 0, str(exc).encode()
    finally:
        connection.close()


class _UnixHTTPConnection(HTTPConnection):
    def __init__(self, unix_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix_path = unix_path

    def connect(self) -> None:
        import socket

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._unix_path)
