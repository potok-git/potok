from pathlib import Path

from potok.titles import release_title, russian_title, source_name


def test_russian_title_comes_from_torrent_filename(tmp_path: Path) -> None:
    folder = tmp_path / "metadata"
    folder.mkdir()
    name = (
        "7 пьес для черно-белого кино (Отар Иоселиани) "
        "[1982, Франция, арт-хаус, короткий метр, DVDRip] [rutracker-5550768].torrent"
    )
    file_name = "7_pjes_dlya_kino_320_[torrents.ru]_by_bm11.avi"
    (folder / name).write_bytes(f"header {file_name} tail".encode())
    (folder / "Другой фильм [2001, DVDRip].torrent").write_bytes(b"other.mkv")

    assert (
        russian_title(folder, "7_pjes_dlya_kino_320_[torrents.ru]_by_bm11")
        == "7 пьес для черно-белого кино (1982)"
    )


def test_source_name_keeps_genre_and_year(tmp_path: Path) -> None:
    folder = tmp_path / "metadata"
    folder.mkdir()
    name = "Фильм [2026, Комедия, WEB-DL 1080p].torrent"
    (folder / name).write_bytes(b"Animals.2026.1080p.mkv")
    assert source_name(folder, "Animals.2026.1080p.mkv") == "Фильм [2026, Комедия, WEB-DL 1080p]"


def test_card_title_drops_the_english_half_and_release_notes() -> None:
    name = (
        "Лего Звёздные войны: Мандалорец / Lego Star Wars: The Mandalorian "
        "(Крис Бакли) [2026, Мультфильм, WEB-DL 1080p] Dub (Даблин)"
    )
    assert release_title(name) == "Лего Звёздные войны: Мандалорец (2026)"


def test_latin_torrent_name_is_not_used(tmp_path: Path) -> None:
    folder = tmp_path / "metadata"
    folder.mkdir()
    (folder / "Fun with Dick and Jane [2005, WEB-DL].torrent").write_bytes(b"film.mkv")
    assert russian_title(folder, "film.mkv") is None
