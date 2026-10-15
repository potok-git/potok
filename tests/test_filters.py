from datetime import UTC, datetime

from potok.config import FilterSettings, MetadataPatterns
from potok.filters import accept, choose_copy
from potok.rss import FeedItem

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
FILTERS = FilterSettings(
    min_rating=7,
    max_age_days=14,
    patterns=MetadataPatterns(None, None, None),
)


def _item(**kwargs: object) -> FeedItem:
    base = dict(
        guid="demo:1",
        feed_name="demo",
        title="Example",
        link="https://example.com/1",
        published=datetime(2026, 10, 7, tzinfo=UTC),
        torrent_url="https://example.com/a.torrent",
        rating=8.0,
        quality="1080p",
        genre="drama",
    )
    base.update(kwargs)
    return FeedItem(**base)  # type: ignore[arg-type]


def test_accepts_fresh_high_rating() -> None:
    assert accept(_item(), FILTERS, now=NOW) is True


def test_rejects_low_rating() -> None:
    assert accept(_item(rating=6.9), FILTERS, now=NOW) is False


def test_quality_pattern_drops_other_copies() -> None:
    import re

    selected = FilterSettings(
        min_rating=None,
        max_age_days=None,
        patterns=MetadataPatterns(None, re.compile(r"2160p|1080p"), None),
    )
    assert accept(_item(quality="1080p"), selected, now=NOW) is True
    assert accept(_item(quality=None), selected, now=NOW) is False


def test_rejects_missing_rating_when_threshold_set() -> None:
    assert accept(_item(rating=None), FILTERS, now=NOW) is False


def test_rejects_old_item() -> None:
    old = datetime(2026, 9, 1, tzinfo=UTC)
    assert accept(_item(published=old), FILTERS, now=NOW) is False


def test_rejects_item_without_date_when_age_limited() -> None:
    assert accept(_item(published=None), FILTERS, now=NOW) is False


def test_choose_copy_keeps_one_quality() -> None:
    low = _item(guid="low", quality="720p", seeders=120)
    high = _item(guid="high", quality="1080p", seeders=30)
    ultra = _item(guid="ultra", quality="2160p", seeders=4)
    copies = [low, high, ultra]
    assert choose_copy(copies, "max").guid == "ultra"
    assert choose_copy(copies, "average").guid == "high"
    assert choose_copy(copies, "min").guid == "low"
    assert choose_copy([low, high], "max").guid == "high"
    assert choose_copy([low, high], "average").guid == "high"
    assert choose_copy([high, ultra], "average").guid == "high"
    near_low = _item(guid="near-low", quality="720p", seeders=5)
    near_high = _item(guid="near-high", quality="1440p", seeders=5)
    assert choose_copy([near_low, near_high], "average").guid == "near-high"


def test_genre_list_keeps_only_named_genres() -> None:
    selected = FilterSettings(
        min_rating=None,
        max_age_days=None,
        patterns=MetadataPatterns(None, None, None),
        genres=("драма", "комедия"),
    )
    assert accept(_item(genre="Драма"), selected, now=NOW) is True
    assert accept(_item(genre="драма, боевик"), selected, now=NOW) is True
    assert accept(_item(genre="мелодрама"), selected, now=NOW) is False
    assert accept(_item(genre=None), selected, now=NOW) is False
