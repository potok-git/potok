"""Постоянный WireGuard: через туннель идут только заданные домены."""

from __future__ import annotations

import ipaddress
import logging
import os
import random
import socket
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

from potok.config import combined_vpn_domains

logger = logging.getLogger(__name__)

_SECRET_MARKERS = ("privatekey", "presharedkey")
_DEFAULT_DNS = "9.9.9.9"
_PROXY_HOST = "127.0.0.54"
_DOCKER_DNS = "127.0.0.11"


class VpnError(Exception):
    """Команда wg/wg-quick не выполнилась или конфиг недоступен."""


class VpnManager:
    """Держит один интерфейс WireGuard постоянно.

    Маршрут по умолчанию не трогается. В туннель ставятся только адреса
    Telegram, Rutracker и доменов из настройки.
    """

    def __init__(
        self,
        interface: str,
        config_path: Path,
        timeout_seconds: float,
        domains: tuple[str, ...] = (),
    ) -> None:
        self.interface = interface
        self.config_path = config_path
        self.timeout_seconds = timeout_seconds
        self.domains = domains

    def route_names(self) -> tuple[str, ...]:
        """Имена, которые должны идти через VPN, включая обязательные."""
        return combined_vpn_domains(self.domains)

    def is_up(self) -> bool:
        """True, если интерфейс уже есть в `wg show`."""
        completed = self._run(["wg", "show", self.interface])
        return completed.returncode == 0

    def up(self) -> None:
        """Поднять туннель без смены маршрута по умолчанию."""
        self._ensure_config()
        source = self.config_path.read_text(encoding="utf-8")
        runtime = self._runtime_file()
        runtime.write_text(runtime_wireguard_text(source), encoding="utf-8")
        try:
            os.chmod(runtime, 0o600)
        except OSError:
            pass
        self._active_file().write_text(str(runtime), encoding="utf-8")
        if self.is_up():
            logger.info("VPN %s уже включён", self.interface)
            return

        logger.info("Включаю VPN %s. Маршрут по умолчанию не меняю.", self.interface)
        completed = self._run(["wg-quick", "up", str(runtime)])
        if completed.returncode != 0:
            raise VpnError(self._failure("up", completed))
        if not self.is_up():
            raise VpnError(f"wg-quick up завершился, но интерфейс {self.interface} не поднялся.")
        logger.info("VPN %s включён. Через него пойдут только выбранные домены.", self.interface)

    def down(self) -> None:
        """Опустить туннель. Если он уже выключен, только пишем это в лог."""
        if not self.is_up():
            logger.info("VPN %s уже выключен. Трафик идёт напрямую.", self.interface)
            return

        target = self._active_config()
        logger.info("Выключаю VPN %s. Дальнейший трафик пойдёт напрямую.", self.interface)
        completed = self._run(["wg-quick", "down", str(target)])
        if completed.returncode != 0:
            raise VpnError(self._failure("down", completed))
        if self.is_up():
            raise VpnError(
                f"wg-quick down завершился, но интерфейс {self.interface} всё ещё активен."
            )
        logger.info("VPN %s выключен", self.interface)

    def ensure(self) -> str:
        """Включить туннель и обновить маршруты к доменам. Выключать его не нужно."""
        self._ensure_config()
        source = self.config_path.read_text(encoding="utf-8")
        if self.is_up() and self._captures_all_traffic():
            logger.info(
                "Туннель %s забирает весь трафик. Переключаю на маршруты по доменам.",
                self.interface,
            )
            self.down()
        self.up()
        names = self.route_names()
        blocked = _blocked_addresses(source)
        dns_servers = [item for item in dns_servers_from(source) if item not in blocked]
        if not dns_servers:
            raise VpnError("В wg0.conf не осталось DNS-сервера, который можно пустить через VPN.")
        for server in dns_servers:
            self._route_replace(server)

        resolved: set[str] = set()
        failed: list[str] = []
        for name in names:
            try:
                found = resolve_ipv4(name, dns_servers[0], timeout=min(self.timeout_seconds, 5))
            except VpnError as exc:
                failed.append(name)
                logger.warning("Домен %s: %s", name, exc)
                continue
            for item in found:
                if item in blocked or not _public_ipv4(item):
                    continue
                resolved.add(item)
        if failed and not resolved:
            raise VpnError(
                "Не удалось узнать адреса доменов через VPN. "
                "Проверьте, что туннель отвечает. Старые маршруты не стираю."
            )
        current = self._known_routes()
        # Часть доменов не открылась. Старые адреса оставляем: иначе снимется
        # маршрут к Telegram или Рутрекеру, и следующий круг снова упадёт.
        desired = merged_routes(
            current,
            set(resolved) | set(dns_servers),
            keep_current=bool(failed),
        )
        for command in route_edits(current, desired, self.interface):
            completed = self._run(command)
            if completed.returncode != 0:
                detail = redact_secrets(completed.stderr or completed.stdout).strip()
                if route_is_already_gone(command, detail):
                    logger.info("Маршрут уже был снят: %s", " ".join(command))
                    continue
                raise VpnError(f"Не удалось обновить маршрут: {detail or command[2]}")
        self._routes_file().write_text("\n".join(sorted(desired)) + "\n", encoding="utf-8")
        if failed:
            logger.warning("Часть доменов не открылась: %s", ", ".join(failed))
        _proxy.configure(
            domains=names,
            server=dns_servers[0],
            interface=self.interface,
            timeout=min(self.timeout_seconds, 5),
        )
        self._run(["ip", "addr", "add", "127.0.0.54/32", "dev", "lo"])
        _proxy.start()
        point_resolv_at_proxy()
        return (
            f"Туннель {self.interface} включён и остаётся включённым. "
            f"Через VPN только: {', '.join(names)}. Адресов в маршруте: {len(resolved)}."
        )

    def probe(self) -> str:
        """Проверить, что туннель поднимается и маршруты к доменам ставятся. Не выключать."""
        text = self.ensure()
        if not self.is_up():
            raise VpnError(f"Туннель {self.interface} не поднялся.")
        return text

    def _captures_all_traffic(self) -> bool:
        completed = self._run(["ip", "route", "show", "default"])
        if completed.returncode != 0:
            return False
        return captures_all_traffic(completed.stdout, self.interface)

    def _known_routes(self) -> set[str]:
        file = self._routes_file()
        if not file.is_file():
            return set()
        return parse_device_routes(file.read_text(encoding="utf-8"))

    def _route_replace(self, address: str) -> None:
        completed = self._run(["ip", "route", "replace", f"{address}/32", "dev", self.interface])
        if completed.returncode != 0:
            detail = redact_secrets(completed.stderr or completed.stdout).strip()
            raise VpnError(f"Не удалось добавить маршрут к {address}: {detail or 'ошибка ip'}")

    def _runtime_file(self) -> Path:
        # wg-quick называет интерфейс по имени файла. Файл должен называться wg0.conf, не potok-wg0.conf.
        folder = Path(tempfile.gettempdir()) / "potok-wg"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self.interface}.conf"

    def _active_file(self) -> Path:
        return Path(tempfile.gettempdir()) / f"potok-{self.interface}.path"

    def _routes_file(self) -> Path:
        return Path(tempfile.gettempdir()) / f"potok-{self.interface}.routes"

    def _active_config(self) -> Path:
        marker = self._active_file()
        if marker.is_file():
            recorded = Path(marker.read_text(encoding="utf-8").strip())
            if recorded.is_file():
                return recorded
        return self.config_path

    def _ensure_config(self) -> None:
        if self.config_path.is_file():
            return
        example = self.config_path.with_name(f"{self.config_path.name}.example")
        hint = ""
        if example.is_file() or self.config_path.name == "wg0.conf":
            hint = " Скопируйте config/wg0.conf.example в config/wg0.conf и впишите свои ключи."
        raise VpnError(f"Файл конфигурации VPN не найден: {self.config_path}.{hint}")

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            program = args[0]
            raise VpnError(
                f"Команда {program} не найдена. Запустите скрипт в контейнере worker: "
                "docker compose exec worker python -m potok demo"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VpnError(
                f"Команда {' '.join(args)} не завершилась за {self.timeout_seconds:.0f} с."
            ) from exc

    def _failure(self, action: str, completed: subprocess.CompletedProcess[str]) -> str:
        detail = redact_secrets(completed.stderr or completed.stdout).strip()
        if len(detail) > 500:
            detail = detail[:500] + "…"
        logger.error("wg-quick %s завершился с кодом %s: %s", action, completed.returncode, detail)
        if detail:
            return f"Не удалось выполнить wg-quick {action} для {self.interface}: {detail}"
        return (
            f"Не удалось выполнить wg-quick {action} для {self.interface}: "
            f"код {completed.returncode}."
        )


def runtime_wireguard_text(source: str) -> str:
    """Конфиг для wg-quick: туннель есть, а маршрут по умолчанию остаётся прямым.

    DNS из файла убирается, иначе пропадут имена контейнеров вроде worker.
    AllowedIPs остаётся 0.0.0.0/0, чтобы туннель принимал пакеты,
    но в него их отправляют только отдельные маршруты.
    """
    lines = source.splitlines()
    section = ""
    output: list[str] = []
    interface_seen = False
    table_set = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "interface" and not table_set:
                output.append("Table = off")
                table_set = True
            section = stripped[1:-1].strip().lower()
            output.append(stripped)
            if section == "interface":
                interface_seen = True
            continue
        key = stripped.split("=", 1)[0].strip().lower()
        if section == "interface" and key == "dns":
            continue
        if section == "interface" and key == "table":
            output.append("Table = off")
            table_set = True
            continue
        if section == "peer" and key == "allowedips":
            output.append("AllowedIPs = 0.0.0.0/0")
            continue
        output.append(line)
    if section == "interface" and not table_set:
        output.append("Table = off")
    if not interface_seen:
        raise VpnError("В wg0.conf нет раздела [Interface].")
    return "\n".join(output).rstrip() + "\n"


def dns_servers_from(source: str) -> tuple[str, ...]:
    """IPv4 из строки DNS. Если её нет, берётся Quad9: через него узнаём адреса доменов."""
    found: list[str] = []
    section = ""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section != "interface" or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip().lower() != "dns":
            continue
        for part in value.replace(",", " ").split():
            if _public_ipv4(part) and part not in found:
                found.append(part)
    if not found:
        return (_DEFAULT_DNS,)
    return tuple(found)


def endpoint_host(source: str) -> str | None:
    """Адрес сервера VPN. Его нельзя отправлять в сам туннель, иначе связь оборвётся."""
    section = ""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section != "peer" or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip().lower() != "endpoint":
            continue
        host = value.strip()
        if host.startswith("[") and "]" in host:
            return host[1 : host.index("]")]
        if host.count(":") == 1:
            return host.rsplit(":", 1)[0]
        return host or None
    return None


def interface_addresses(source: str) -> set[str]:
    found: set[str] = set()
    section = ""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            continue
        if section != "interface" or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip().lower() != "address":
            continue
        for part in value.replace(",", " ").split():
            found.add(part.split("/", 1)[0])
    return found


def captures_all_traffic(text: str, interface: str) -> bool:
    for line in text.splitlines():
        if line.strip().startswith("default") and f"dev {interface}" in line:
            return True
    return False


def parse_device_routes(text: str) -> set[str]:
    found: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] == "default":
            continue
        token = parts[0].split("/", 1)[0]
        try:
            ipaddress.ip_address(token)
        except ValueError:
            continue
        found.add(token)
    return found


def route_edits(current: set[str], desired: set[str], interface: str) -> list[list[str]]:
    """Команды ip: добавить новые адреса и убрать те, которых больше нет."""
    commands: list[list[str]] = []
    for address in sorted(desired - current):
        commands.append(["ip", "route", "replace", f"{address}/32", "dev", interface])
    for address in sorted(current - desired):
        commands.append(["ip", "route", "del", f"{address}/32", "dev", interface])
    return commands


def merged_routes(current: set[str], desired: set[str], *, keep_current: bool) -> set[str]:
    """Если DNS ответил не по всем доменам, уже стоящие маршруты не стираем."""
    if keep_current:
        return set(desired) | set(current)
    return set(desired)


def route_is_already_gone(command: list[str], detail: str) -> bool:
    """Удаление маршрута, которого в системе уже нет. Это не сбой туннеля."""
    if len(command) < 3 or command[2] != "del":
        return False
    text = detail.lower()
    return "no such process" in text or "cannot find device" in text or "no such device" in text


def name_uses_vpn(host: str, domains: tuple[str, ...]) -> bool:
    """Точное имя или его поддомен. api.telegram.org не захватывает весь telegram.org."""
    name = host.rstrip(".").lower()
    return any(name == item or name.endswith("." + item) for item in domains)


def question_name(payload: bytes) -> str:
    if len(payload) < 12:
        return ""
    labels: list[str] = []
    offset = 12
    seen = 0
    while offset < len(payload) and seen < 128:
        length = payload[offset]
        if length == 0:
            break
        if length & 0xC0 == 0xC0:
            break
        offset += 1
        labels.append(payload[offset : offset + length].decode("ascii", errors="ignore"))
        offset += length
        seen += 1
    return ".".join(labels)


def question_type(payload: bytes) -> int:
    if len(payload) < 12:
        return 0
    offset = 12
    seen = 0
    while offset < len(payload) and seen < 128:
        length = payload[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            offset += 2
            break
        offset += 1 + length
        seen += 1
    if offset + 2 > len(payload):
        return 0
    return struct.unpack(">H", payload[offset : offset + 2])[0]


def build_a_response(query: bytes, addresses: list[str]) -> bytes:
    """Ответ с теми же вопросом и номером, что в запросе."""
    if len(query) < 12:
        return query
    name = question_name(query)
    query_id = struct.unpack(">H", query[:2])[0]
    header = struct.pack(">HHHHHH", query_id, 0x8180, 1, len(addresses), 0, 0)
    question = encode_dns_query(name or "invalid", 0)[12:]
    answers = b"".join(_a_record(address) for address in addresses)
    return header + question + answers


def point_resolv_at_proxy() -> None:
    """Смотреть имена через локальный DNS. Он уже слушает в общей сети контейнеров."""
    path = Path("/etc/resolv.conf")
    if not path.exists():
        return
    desired = f"nameserver {_PROXY_HOST}\noptions ndots:0\n"
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        return
    if current == desired:
        return
    try:
        path.write_text(desired, encoding="utf-8")
    except OSError as exc:
        logger.warning("Не удалось переключить DNS контейнера: %s", exc)


class _DnsProxy:
    """Локальный DNS: выбранные домены спрашивает через VPN, остальные — как раньше."""

    def __init__(self) -> None:
        self.domains: tuple[str, ...] = ()
        self.server = _DEFAULT_DNS
        self.interface = "wg0"
        self.timeout = 5.0
        self._lock = threading.Lock()
        self._started = False

    def configure(
        self,
        *,
        domains: tuple[str, ...],
        server: str,
        interface: str,
        timeout: float,
    ) -> None:
        with self._lock:
            self.domains = domains
            self.server = server
            self.interface = interface
            self.timeout = timeout

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._bind()
            self._started = True

    def _bind(self) -> None:
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind((_PROXY_HOST, 53))
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp.bind((_PROXY_HOST, 53))
            tcp.listen(16)
        except OSError as exc:
            raise VpnError(
                f"Локальный DNS {_PROXY_HOST} не запустился: {exc}. "
                "Он нужен, чтобы контейнеры видели те же адреса, что и маршруты VPN."
            ) from exc
        threading.Thread(
            target=self._udp_loop, args=(udp,), name="potok-dns-udp", daemon=True
        ).start()
        threading.Thread(
            target=self._tcp_loop, args=(tcp,), name="potok-dns-tcp", daemon=True
        ).start()
        logger.info("Локальный DNS слушает %s. Выбранные домены спрашивает через VPN.", _PROXY_HOST)

    def answer(self, payload: bytes) -> bytes:
        host = question_name(payload)
        with self._lock:
            domains = self.domains
            server = self.server
            interface = self.interface
            timeout = self.timeout
        if host and name_uses_vpn(host, domains):
            if question_type(payload) != 1:
                return build_a_response(payload, [])
            try:
                found = resolve_ipv4(host, server, timeout)
            except VpnError as exc:
                logger.warning("DNS для %s: %s", host, exc)
                return _servfail(payload)
            public = [item for item in found if _public_ipv4(item)]
            for address in public:
                _install_route(address, interface)
            return build_a_response(payload, public)
        try:
            return _forward_dns(payload, _DOCKER_DNS, timeout)
        except VpnError:
            return _servfail(payload)

    def _udp_loop(self, sock: socket.socket) -> None:
        while True:
            try:
                payload, address = sock.recvfrom(2048)
                sock.sendto(self.answer(payload), address)
            except OSError:
                logger.exception("Локальный DNS (UDP) остановился")
                return

    def _tcp_loop(self, sock: socket.socket) -> None:
        while True:
            try:
                connection, _address = sock.accept()
            except OSError:
                logger.exception("Локальный DNS (TCP) остановился")
                return
            threading.Thread(
                target=self._tcp_client,
                args=(connection,),
                name="potok-dns-client",
                daemon=True,
            ).start()

    def _tcp_client(self, connection: socket.socket) -> None:
        with connection:
            try:
                header = _recv_exact(connection, 2)
                if header is None:
                    return
                length = struct.unpack(">H", header)[0]
                payload = _recv_exact(connection, length)
                if payload is None:
                    return
                reply = self.answer(payload)
                connection.sendall(struct.pack(">H", len(reply)) + reply)
            except OSError:
                return


_proxy = _DnsProxy()


def _install_route(address: str, interface: str) -> None:
    try:
        subprocess.run(
            ["ip", "route", "replace", f"{address}/32", "dev", interface],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Маршрут к %s не добавился: %s", address, exc)


def _a_record(address: str) -> bytes:
    return b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(address)


def _servfail(query: bytes) -> bytes:
    query_id = struct.unpack(">H", query[:2])[0] if len(query) >= 2 else 0
    header = struct.pack(">HHHHHH", query_id, 0x8182, 1, 0, 0, 0)
    name = question_name(query)
    question = encode_dns_query(name or "invalid", 0)[12:]
    return header + question


def _forward_dns(payload: bytes, server: str, timeout: float) -> bytes:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(payload, (server, 53))
            reply, _address = sock.recvfrom(2048)
    except (TimeoutError, OSError) as exc:
        raise VpnError(f"DNS {server} не ответил: {exc}") from exc
    return reply


def _recv_exact(connection: socket.socket, size: int) -> bytes | None:
    chunks: list[bytes] = []
    pending = size
    while pending:
        part = connection.recv(pending)
        if not part:
            return None
        chunks.append(part)
        pending -= len(part)
    return b"".join(chunks)


def encode_dns_query(name: str, query_id: int) -> bytes:
    header = struct.pack(">HHHHHH", query_id & 0xFFFF, 0x0100, 1, 0, 0, 0)
    question = b"".join(
        bytes([len(label)]) + label.encode("ascii") for label in name.split(".") if label
    )
    return header + question + b"\x00" + struct.pack(">HH", 1, 1)


def parse_dns_a(payload: bytes) -> list[str]:
    """Достать IPv4-ответы из DNS-пакета. Ошибки сервера дают пустой список."""
    if len(payload) < 12:
        return []
    _query_id, flags, questions, answers, _ns, _extra = struct.unpack(">HHHHHH", payload[:12])
    if flags & 0x000F:
        return []
    offset = 12
    for _ in range(questions):
        offset = _skip_dns_name(payload, offset)
        offset += 4
    found: list[str] = []
    for _ in range(answers):
        offset = _skip_dns_name(payload, offset)
        if offset + 10 > len(payload):
            break
        record_type, _class, _ttl, length = struct.unpack(">HHIH", payload[offset : offset + 10])
        offset += 10
        data = payload[offset : offset + length]
        offset += length
        if record_type == 1 and len(data) == 4:
            found.append(socket.inet_ntoa(data))
    return found


def resolve_ipv4(name: str, server: str, timeout: float) -> list[str]:
    """Спросить A-записи у DNS, который уже маршрутизирован в туннель."""
    query = encode_dns_query(name, random.randint(0, 65535))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(query, (server, 53))
            payload, _address = sock.recvfrom(2048)
    except TimeoutError as exc:
        raise VpnError(f"DNS {server} не ответил за {timeout:.0f} с.") from exc
    except OSError as exc:
        raise VpnError(f"Не удалось спросить DNS {server}: {exc}") from exc
    return parse_dns_a(payload)


def redact_secrets(text: str) -> str:
    """Убрать из вывода строки с ключами, если wg-quick их всё же напечатал."""
    cleaned: list[str] = []
    for line in text.splitlines():
        lowered = line.lower().replace(" ", "")
        if any(marker in lowered for marker in _SECRET_MARKERS):
            cleaned.append("[секрет скрыт]")
        else:
            cleaned.append(line)
    return "\n".join(cleaned)


def _blocked_addresses(source: str) -> set[str]:
    blocked = set(interface_addresses(source))
    host = endpoint_host(source)
    if not host:
        return blocked
    if _is_ip(host):
        blocked.add(host)
        return blocked
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except socket.gaierror:
        logger.warning("Не удалось заранее узнать адрес сервера VPN %s.", host)
        return blocked
    blocked.update(item[4][0] for item in infos)
    return blocked


def _public_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and address.is_global


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _skip_dns_name(payload: bytes, offset: int) -> int:
    seen = 0
    while offset < len(payload) and seen < 128:
        length = payload[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length
        seen += 1
    return offset
