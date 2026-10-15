"""Команды: fetch, bot, publish, portal, demo, up, down, status."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from potok.config import AppConfig, ConfigError, load_config
from potok.logs import attach_log_file, worker_log_path
from potok.pipeline import FetchError, run_fetch
from potok.portal import serve
from potok.publish import DownloadError, NotifyError, deliver_and_notify, publish_pending
from potok.stream import StreamError
from potok.vk_video import VkError
from potok.vpn import VpnError, VpnManager

logger = logging.getLogger("potok")


def load_local_env(path: Path | None = None) -> None:
    """Подхватить .env из папки проекта. Уже заданные переменные не перезаписываются."""
    file = path or Path(".env")
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    parser = argparse.ArgumentParser(
        description="Ленты RSS и переключение WireGuard в рабочем контейнере Поток.",
    )
    parser.add_argument(
        "command",
        choices=(
            "fetch",
            "bot",
            "watch",
            "publish",
            "portal",
            "demo",
            "up",
            "down",
            "status",
            "probe",
        ),
        help=(
            "fetch забирает RSS и ставит новые записи на согласование; "
            "bot слушает кнопки в Telegram; "
            "watch опрашивает ленту сам, без Telegram; "
            "publish догружает одобренные видео в VK; "
            "portal открывает веб-портал стриминга; "
            "demo показывает постоянный туннель только для выбранных доменов; "
            "up включает такие маршруты; down выключает туннель; "
            "status только смотрит; probe проверяет туннель и не выключает его."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Путь к config.yaml. По умолчанию берётся переменная POTOK_CONFIG.",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Готовое видео для команды publish. aria2c не запускается.",
    )
    parser.add_argument(
        "--torrent",
        type=Path,
        default=None,
        help="Файл .torrent для команды publish: сначала aria2c, потом VK.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(_config_path(args.config))
    except ConfigError as exc:
        _setup_logging("INFO")
        logger.error("%s", exc)
        return 1

    _setup_logging(config.log_level)
    if args.command != "portal":
        attach_log_file(worker_log_path(config.database_path))
    _apply_secrets(config)
    if args.command != "publish" and (args.file is not None or args.torrent is not None):
        logger.error("--file и --torrent работают только с командой publish.")
        return 1
    vpn = VpnManager(
        interface=config.vpn.interface,
        config_path=config.vpn.config_path,
        timeout_seconds=config.vpn.command_timeout_seconds,
        domains=config.routing.vpn_domains,
    )

    try:
        if args.command == "fetch":
            from potok.bot import notifier_from_env

            report = run_fetch(config, vpn, notify=notifier_from_env(config))
            logger.info(
                "Готово: на согласовании %s, уже были %s, отфильтровано %s, ошибок %s.",
                len(report.accepted),
                report.skipped_seen,
                report.skipped_filter,
                report.failed,
            )
            if report.failed:
                return 1
        elif args.command == "bot":
            from potok.bot import run_bot

            _arm_vpn(vpn)
            asyncio.run(run_bot(config, config_path=_config_path(args.config)))
        elif args.command == "watch":
            from potok.bot import run_watch

            _arm_vpn(vpn)
            asyncio.run(run_watch(config, config_path=_config_path(args.config)))
        elif args.command == "publish":
            _publish_command(config, args.file, args.torrent)
        elif args.command == "portal":
            serve(config, config_path=_config_path(args.config))
        elif args.command == "demo":
            run_demo(config, vpn)
        elif args.command == "up":
            logger.info("%s", vpn.ensure())
            log_state(vpn)
        elif args.command == "down":
            vpn.down()
            log_state(vpn)
        elif args.command == "probe":
            print(vpn.probe())
        else:
            log_state(vpn)
    except (
        VpnError,
        FetchError,
        ConfigError,
        DownloadError,
        VkError,
        NotifyError,
        StreamError,
    ) as exc:
        logger.error("%s", exc)
        return 1
    return 0


def _publish_command(config: AppConfig, file: Path | None, torrent: Path | None) -> None:
    if file is not None and torrent is not None:
        raise ConfigError("Укажите что-то одно: --file или --torrent.")
    if file is not None:
        deliver_and_notify(config, file, title=file.stem)
        return
    if torrent is not None:
        deliver_and_notify(config, torrent, title=torrent.stem)
        return
    failures = publish_pending(config)
    if failures:
        raise DownloadError(f"Не удалось опубликовать записей: {failures}.")


def _arm_vpn(vpn: VpnManager) -> None:
    """Поднять маршруты. Если туннель ещё не готов, служба всё равно стартует и повторит позже."""
    try:
        logger.info("%s", vpn.ensure())
    except VpnError as exc:
        logger.error("Маршруты VPN: %s", exc)


def run_demo(config: AppConfig, vpn: VpnManager) -> None:
    """Показать, что туннель остаётся включённым только для выбранных доменов."""
    logger.info(
        "Туннель постоянный, через VPN — %s. Остальной трафик идёт по прямому каналу.",
        ", ".join(vpn.route_names()),
    )
    logger.info("%s", vpn.ensure())
    log_state(vpn)
    logger.info("Демонстрация закончена. Туннель не выключаю.")


def log_state(vpn: VpnManager) -> None:
    if vpn.is_up():
        logger.info(
            "Интерфейс %s активен. Через него идут только выбранные домены, остальное — напрямую.",
            vpn.interface,
        )
        return
    logger.info("Интерфейс %s выключен, трафик контейнера идёт напрямую.", vpn.interface)


def _apply_secrets(config: AppConfig) -> None:
    from potok.secrets import SecretError, SecretStore

    try:
        SecretStore(config.data_dir()).apply_to_environ()
    except SecretError as exc:
        logger.error("%s", exc)


def _config_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("POTOK_CONFIG")
    if env:
        return Path(env)
    return Path("/etc/potok/config.yaml")


def _setup_logging(level_name: str) -> None:
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
