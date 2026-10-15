"""Русское название для видео: из имени файла .torrent, без технического хвоста."""

from __future__ import annotations

import re
from pathlib import Path

from potok.tmdb import movie_query

_TITLE_LIMIT = 128
# Так программа сама называет файл из ленты: 16 знаков a–f и цифр.
_AUTO_STEM = re.compile(r"^[0-9a-f]{16}$", re.IGNORECASE)
_QUALITY = re.compile(r"\b(2160p|1080p|720p|480p)\b", re.IGNORECASE)


def has_cyrillic(text: str) -> bool:
    return any("\u0400" <= char <= "\u04ff" for char in text)


def russian_title(metadata_dir: Path, video_name: str) -> str | None:
    """Найти раздачу, внутри которой лежит этот файл, и вернуть короткое русское имя.

    Имя берётся из названия файла .torrent: «7 пьес для черно-белого кино (1982)».
    Если русского названия нет, возвращает None.
    """
    path = _matching_torrent(metadata_dir, video_name)
    if path is None:
        return None
    title = release_title(path.stem)
    if title and has_cyrillic(title):
        return title
    return None


def source_name(metadata_dir: Path, video_name: str) -> str | None:
    """Полное имя файла .torrent без расширения: в нём остаются жанр и год."""
    path = _matching_torrent(metadata_dir, video_name)
    if path is None:
        return None
    return path.stem


def is_auto_torrent_name(name: str) -> bool:
    """Файл из ленты: короткое имя из 16 знаков, без человекочитаемого названия."""
    return bool(_AUTO_STEM.fullmatch(Path(name).stem))


def is_manual_video(metadata_dir: Path, video_name: str) -> bool:
    """Видео лежит в раздаче, которую добавили вручную, а не из ленты."""
    return matching_manual_torrent(metadata_dir, video_name) is not None


def matching_manual_torrent(metadata_dir: Path, video_name: str) -> Path | None:
    """Файл .torrent с обычным именем, внутри которого есть это видео."""
    return _matching_torrent(metadata_dir, video_name, skip_auto=True)


def quality_from_name(name: str) -> str | None:
    """Качество из имени: 1080p, 720p и похожие."""
    found = _QUALITY.search(name)
    if found is None:
        return None
    return found.group(1).lower()


def _matching_torrent(
    metadata_dir: Path, video_name: str, *, skip_auto: bool = False
) -> Path | None:
    needle = Path(video_name).name
    stem = Path(needle).stem
    if len(stem) < 4 or not metadata_dir.is_dir():
        return None
    needles = _needles(needle, stem)
    for path in sorted(metadata_dir.glob("*.torrent")):
        if skip_auto and is_auto_torrent_name(path.name):
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if any(piece in blob for piece in needles):
            return path
    return None


def _needles(filename: str, stem: str) -> tuple[bytes, ...]:
    found: list[bytes] = []
    for text in (filename, stem):
        for encoding in ("utf-8", "cp1251"):
            try:
                raw = text.encode(encoding)
            except UnicodeEncodeError:
                continue
            if raw not in found:
                found.append(raw)
    return tuple(found)


def release_title(name: str) -> str:
    """Короткое имя раздачи: без качества и скобок, с годом если он был."""
    text = name.strip()
    if text.lower().endswith(".torrent"):
        text = text[: -len(".torrent")]
    return _short_title(text)


def _short_title(torrent_name: str) -> str:
    title, year = movie_query(torrent_name)
    if not title:
        return ""
    if year is not None and str(year) not in title:
        title = f"{title} ({year})"
    return title[:_TITLE_LIMIT]
