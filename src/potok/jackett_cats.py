"""Категории Jackett и подстановка их в конец ссылки ленты."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from potok.jackett_catalog import FORUMS, GROUPS

_OFFSET = 100_000
_SHOWN = frozenset({"Кинематограф", "Сериалы", "Мультипликация"})


@dataclass(frozen=True)
class FeedCategory:
    """Один раздел в списке галочек."""

    id: str
    label: str
    checked: bool


@dataclass(frozen=True)
class CategoryGroup:
    """Разделы одной полки трекера: кино, сериалы и так далее."""

    name: str
    items: tuple[FeedCategory, ...]

    @property
    def open(self) -> bool:
        """Группу с уже отмеченными разделами показываем раскрытой."""
        return any(item.checked for item in self.items)

    @property
    def picked(self) -> int:
        return sum(1 for item in self.items if item.checked)


def selected_category_ids(url: str) -> set[str]:
    """Номера из параметра cat= в ссылке ленты."""
    found: set[str] = set()
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if name.lower() == "cat":
            found.update(category_id_set(value))
    return found


def category_id_set(text: str) -> set[str]:
    """Номера через запятую. Всё, что не число, отбрасывается."""
    return {part.strip() for part in text.split(",") if part.strip().isdigit()}


def apply_categories(url: str, ids: list[str]) -> str:
    """Подставить отмеченные номера в cat=, остальные части ссылки не трогать."""
    parts = urlsplit(url.strip())
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    clean: list[str] = []
    seen: set[str] = set()
    for item in ids:
        text = str(item).strip()
        if text.isdigit() and text not in seen:
            seen.add(text)
            clean.append(text)
    value = ",".join(clean)
    replaced = False
    new_pairs: list[tuple[str, str]] = []
    for name, item in pairs:
        if name.lower() == "cat":
            if not replaced and value:
                new_pairs.append(("cat", value))
            replaced = True
            continue
        new_pairs.append((name, item))
    if value and not replaced:
        new_pairs.append(("cat", value))
    query = urlencode(new_pairs, safe=",")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def group_categories(xml_text: str, selected: set[str]) -> tuple[CategoryGroup, ...]:
    """Разобрать ответ Jackett t=caps в группы с человеческими названиями."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("Jackett вернул непонятный список категорий.") from exc
    categories = root.find("categories")
    if categories is None:
        return ()

    buckets: dict[str, list[FeedCategory]] = {name: [] for name in _SHOWN}
    for node in categories:
        raw_id = (node.get("id") or "").strip()
        if not raw_id.isdigit():
            continue
        number = int(raw_id)
        if number < _OFFSET:
            continue
        group, quality = FORUMS.get(number - _OFFSET, ("", ""))
        if group not in _SHOWN:
            continue
        label = _label(node.get("name") or raw_id, group, quality)
        buckets[group].append(FeedCategory(raw_id, label, raw_id in selected))

    _mark_duplicates(buckets)
    groups: list[CategoryGroup] = []
    for name in GROUPS:
        if name not in _SHOWN:
            continue
        items = buckets.get(name) or []
        if items:
            groups.append(CategoryGroup(name, tuple(items)))
    return tuple(groups)


def load_category_groups(config: object, store: object) -> tuple[tuple[CategoryGroup, ...], str]:
    """Спросить Jackett. Пустая строка ошибки значит, что список удалось показать."""
    from potok.config import AppConfig
    from potok.jackett import JackettClient, JackettError, resolve_api_key
    from potok.secrets import SecretStore

    if not isinstance(config, AppConfig) or not isinstance(store, SecretStore):
        return (), "Список категорий сейчас недоступен."
    if not config.jackett.rss_url:
        return (), "Сначала вставьте ссылку Torznab Feed. Тогда здесь появятся категории."
    stored = store.get(config.jackett.api_key_env) or ""
    key = stored.strip() or resolve_api_key(config.jackett)
    if not key:
        return (), "Список категорий появится, когда будет задан ключ Jackett."
    try:
        xml = JackettClient(config.jackett, key).read_caps()
        groups = group_categories(xml, selected_category_ids(config.jackett.rss_url))
    except (JackettError, ValueError):
        return (), "Jackett не отдал список категорий. Ссылку можно править вручную."
    if not groups:
        return (), "Jackett не прислал разделы этого трекера."
    return groups, ""


def _label(name: str, group: str, quality: str) -> str:
    text = name.strip()
    if text.startswith("|-"):
        text = text[2:].strip()
    if text.startswith("•"):
        text = text[1:].strip()
    if text.startswith(group + " "):
        text = text[len(group) + 1 :].strip()
    if quality and quality.casefold() not in text.casefold():
        text = f"{text} ({quality})"
    return text or name.strip()


def _mark_duplicates(buckets: dict[str, list[FeedCategory]]) -> None:
    """Одинаковые названия Jackett различает только номером раздела."""
    for name, items in buckets.items():
        counts: dict[str, int] = {}
        for item in items:
            counts[item.label] = counts.get(item.label, 0) + 1
        if all(count == 1 for count in counts.values()):
            continue
        updated: list[FeedCategory] = []
        for item in items:
            label = item.label
            if counts[label] > 1 and item.id.isdigit():
                forum = int(item.id) - _OFFSET if int(item.id) >= _OFFSET else int(item.id)
                label = f"{label} · {forum}"
            updated.append(FeedCategory(item.id, label, item.checked))
        buckets[name] = updated
