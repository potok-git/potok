"""Отбор записей по рейтингу, возрасту, жанру и качеству."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from potok.config import FilterSettings
from potok.rss import FeedItem

logger = logging.getLogger(__name__)

_QUALITY_LEVEL = re.compile(r"(\d{3,4})")
_QUALITY_KEEP = frozenset({"max", "average", "min"})


def quality_level(quality: str | None) -> int | None:
    """Число из подписи качества: 1080p → 1080. Без числа сравнить нельзя."""
    if not quality:
        return None
    found = _QUALITY_LEVEL.search(quality)
    if found is None:
        return None
    return int(found.group(1))


_AVERAGE_QUALITY = 1080


def choose_copy(items: Sequence[FeedItem], quality_keep: str) -> FeedItem:
    """Одна копия фильма: максимальное, среднее или минимальное качество.

    Среднее — ближайшее к 1080p. Поэтому и 720p с 1080p, и 1080p с 2160p
    оставляют 1080p. Если два качества одинаково близки к 1080p, берётся
    более высокое. Среди копий одного качества остаётся раздача с большим
    числом сидов.
    """
    if not items:
        raise ValueError("Нет копий, из которых можно выбрать качество.")
    mode = quality_keep if quality_keep in _QUALITY_KEEP else "max"
    known = [item for item in items if quality_level(item.quality) is not None]
    pool = known or list(items)
    levels = sorted({level for item in pool if (level := quality_level(item.quality)) is not None})
    if not levels:
        return max(pool, key=_seed_key)
    if mode == "min":
        target = levels[0]
    elif mode == "average":
        target = _average_level(levels)
    else:
        target = levels[-1]
    matching = [item for item in pool if quality_level(item.quality) == target]
    return max(matching, key=_seed_key)


def _average_level(levels: Sequence[int]) -> int:
    """Ближайшее к 1080p. При равной дистанции — более высокое."""
    return min(levels, key=lambda level: (abs(level - _AVERAGE_QUALITY), -level))


def _seed_key(item: FeedItem) -> tuple[int, str]:
    seeds = -1 if item.seeders is None else item.seeders
    return seeds, item.guid


def accept(item: FeedItem, filters: FilterSettings, *, now: datetime | None = None) -> bool:
    """True, если запись проходит порог рейтинга, возраст и список жанров."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    if filters.patterns.quality is not None and not item.quality:
        logger.debug("Отсев %s: качество не из заданного списка", item.title)
        return False

    if filters.min_rating is not None:
        if item.rating is None or item.rating < filters.min_rating:
            logger.debug(
                "Отсев %s: рейтинг %s ниже порога %s",
                item.title,
                item.rating,
                filters.min_rating,
            )
            return False

    if filters.max_age_days is not None:
        if item.published is None:
            logger.debug("Отсев %s: нет даты публикации", item.title)
            return False
        published = item.published
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age = moment - published
        if age > timedelta(days=filters.max_age_days):
            logger.debug("Отсев %s: старше %s дн.", item.title, filters.max_age_days)
            return False

    if filters.genres and not _genre_matches(item.genre, filters.genres):
        logger.debug("Отсев %s: жанр %s не из списка", item.title, item.genre)
        return False

    return True


def _genre_matches(genre: str | None, allowed: tuple[str, ...]) -> bool:
    """Совпадение по целому слову жанра, без учёта регистра."""
    parts = [part.strip().casefold() for part in (genre or "").replace("/", ",").split(",")]
    parts = [part for part in parts if part]
    wanted = {name.strip().casefold() for name in allowed if name.strip()}
    return any(part in wanted for part in parts)
