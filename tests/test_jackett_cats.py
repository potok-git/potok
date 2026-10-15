"""Галочки категорий собирают конец ссылки Jackett."""

from potok.jackett_cats import apply_categories, group_categories, selected_category_ids
from potok.settings_io import apply_settings

_CAPS = """<?xml version="1.0"?>
<caps>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2040" name="Movies/HD"/>
    </category>
    <category id="101730" name=" |- Зарубежные фильмы"/>
    <category id="101725" name=" |- Зарубежные фильмы"/>
    <category id="101677" name="Кинематограф Зарубежные фильмы (2022)"/>
    <category id="101635" name=" |- Electronic music"/>
  </categories>
</caps>
"""


def test_groups_use_human_names_and_skip_generic_buckets() -> None:
    groups = {group.name: group for group in group_categories(_CAPS, {"101730"})}
    films = groups["Кинематограф"]
    labels = [item.label for item in films.items]
    assert "Movies/HD" not in labels
    assert "Зарубежные фильмы · 1730" in labels
    assert "Зарубежные фильмы · 1725" in labels
    assert "Зарубежные фильмы (2022)" in labels
    checked = next(item for item in films.items if item.id == "101730")
    assert checked.checked
    assert films.open
    assert films.picked == 1
    assert "Музыка" not in groups
    assert "Игры" not in groups
    assert "Для взрослых" not in groups


def test_apply_categories_replaces_only_the_tail() -> None:
    url = "http://jackett:9117/api?t=search&cat=1,2"
    updated = apply_categories(url, ["101730", "101730", "bad", "101725"])
    assert updated == "http://jackett:9117/api?t=search&cat=101730,101725"
    assert selected_category_ids(updated) == {"101730", "101725"}
    cleared = apply_categories(url, [])
    assert "cat=" not in cleared
    assert "t=search" in cleared


def test_checked_boxes_rewrite_the_link_when_it_was_not_edited() -> None:
    raw = {"jackett": {"jackett_rss_url": "http://jackett:9117/api?t=search&cat=1,2"}}
    updated, secrets, wireguard = apply_settings(
        raw,
        {
            "jackett_rss_url": "http://jackett:9117/api?t=search&cat=1,2",
            "jackett_cats_shown": "1",
            "jackett_cat_base": "2,1",
            "jackett_cat": ["101730", "101677"],
        },
    )
    assert secrets == {}
    assert wireguard is None
    assert updated["jackett"]["jackett_rss_url"].endswith("cat=101730,101677")


def test_edited_link_keeps_categories_typed_into_it() -> None:
    raw = {"jackett": {}}
    updated, _, _ = apply_settings(
        raw,
        {
            "jackett_rss_url": "http://jackett:9117/api?t=search&cat=9",
            "jackett_cats_shown": "1",
            "jackett_cat_base": "1,2",
            "jackett_cat": ["101730"],
        },
    )
    assert updated["jackett"]["jackett_rss_url"] == "http://jackett:9117/api?t=search&cat=9"
