"""Первый запуск портала: шаги мастера /setup, без второго хранилища секретов."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from potok.config import AppConfig
from potok.jackett import JackettClient, JackettError, resolve_api_key
from potok.secrets import WG_SECRET, SecretStore
from potok.settings_io import SettingsError, read_wireguard, resolve_wg_path
from potok.users import DEFAULT_NAME, DEFAULT_PASSWORD, UserStore

LATER_FLAG = "onboarding_later"
DONE_FLAG = "onboarding_done"
JACKETT_UI = "http://localhost:9117"
STEPS: tuple[str, ...] = ("password", "jackett", "tracker", "feed", "notify", "vpn")
REQUIRED = STEPS[:4]
TITLES = {
    "password": "Пароль",
    "jackett": "Ключ Jackett",
    "tracker": "Трекер",
    "feed": "Лента",
    "notify": "Куда слать решения",
    "vpn": "VPN",
    "done": "Готово",
}


@dataclass(frozen=True)
class OnboardingState:
    """Что уже заполнено и какой шаг показывать."""

    filled: dict[str, bool]
    later: bool
    finished: bool

    def required_done(self) -> bool:
        return all(self.filled[name] for name in REQUIRED)

    def first_open(self) -> str:
        for name in STEPS:
            if not self.filled[name]:
                return name
        return "done"


def later_path(data_dir: Path) -> Path:
    return data_dir / LATER_FLAG


def done_path(data_dir: Path) -> Path:
    return data_dir / DONE_FLAG


def mark_later(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    later_path(data_dir).write_text("1\n", encoding="utf-8")


def mark_finished(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    done_path(data_dir).write_text("1\n", encoding="utf-8")


def later_chosen(data_dir: Path) -> bool:
    return later_path(data_dir).is_file()


def wizard_finished(data_dir: Path) -> bool:
    return done_path(data_dir).is_file()


def password_changed(users: UserStore) -> bool:
    """Шаг закрыт, когда вход potok / potok уже не проходит."""
    return not users.authenticate(DEFAULT_NAME, DEFAULT_PASSWORD)


def jackett_key_set(config: AppConfig, secrets: SecretStore) -> bool:
    return bool(resolve_api_key(config.jackett) or secrets.get(config.jackett.api_key_env))


def tracker_names(config: AppConfig, secrets: SecretStore) -> tuple[str, ...]:
    """Имена трекеров из Jackett. При ошибке — пустой список, мастер не падает."""
    key = resolve_api_key(config.jackett) or (secrets.get(config.jackett.api_key_env) or "")
    if not key.strip():
        return ()
    settings = replace(config.jackett, timeout_seconds=min(config.jackett.timeout_seconds, 4.0))
    try:
        return JackettClient(settings, key).configured_indexers()
    except JackettError:
        return ()


def tracker_ready(config: AppConfig, secrets: SecretStore) -> bool:
    """True, если в Jackett уже есть хотя бы один добавленный трекер."""
    return bool(tracker_names(config, secrets))


def feed_set(config: AppConfig) -> bool:
    return bool(config.jackett.rss_url.strip())


def notify_set(config: AppConfig, secrets: SecretStore) -> bool:
    telegram = bool(secrets.get(config.telegram.token_env)) and bool(
        secrets.get(config.telegram.chat_id_env)
    )
    vk = bool(secrets.get(config.vk.token_env)) and config.vk.app_id is not None
    tmdb = bool(secrets.get(config.tmdb.api_key_env))
    return telegram and vk and tmdb


def vpn_set(config: AppConfig, config_path: Path, secrets: SecretStore) -> bool:
    path = resolve_wg_path(config_path, config.vpn.config_path)
    try:
        text = read_wireguard(path, secrets)
    except SettingsError:
        text = secrets.get(WG_SECRET) or ""
    cleaned = text.strip()
    if not cleaned or "REPLACE_WITH" in cleaned:
        return False
    return "[Interface]" in cleaned and "PrivateKey" in cleaned


def inspect(
    *,
    users: UserStore,
    config: AppConfig,
    secrets: SecretStore,
    config_path: Path,
    data_dir: Path,
) -> OnboardingState:
    secrets.apply_to_environ()
    filled = {
        "password": password_changed(users),
        "jackett": jackett_key_set(config, secrets),
        "tracker": tracker_ready(config, secrets),
        "feed": feed_set(config),
        "notify": notify_set(config, secrets),
        "vpn": vpn_set(config, config_path, secrets),
    }
    return OnboardingState(
        filled=filled,
        later=later_chosen(data_dir),
        finished=wizard_finished(data_dir),
    )


def needs_setup(
    users: UserStore,
    config: AppConfig,
    secrets: SecretStore,
    data_dir: Path,
) -> bool:
    """Нужен ли мастер после входа. Jackett по сети не спрашиваем: это бы тормозило все страницы."""
    if later_chosen(data_dir):
        return False
    if not password_changed(users):
        return True
    if not jackett_key_set(config, secrets):
        return True
    if not feed_set(config):
        return True
    return False


def force_setup(state: OnboardingState) -> bool:
    """Пока обязательные шаги пустые и «Позже» не нажимали — после входа открывать мастер."""
    return not state.required_done() and not state.later and not state.finished


def show_banner(state: OnboardingState) -> bool:
    return state.later and not state.finished


def step_number(step: str) -> int:
    if step == "done":
        return len(STEPS)
    try:
        return STEPS.index(step) + 1
    except ValueError:
        return 1


def previous_step(step: str) -> str:
    if step == "done":
        return STEPS[-1]
    if step not in STEPS:
        return STEPS[0]
    index = STEPS.index(step)
    if index == 0:
        return STEPS[0]
    return STEPS[index - 1]
