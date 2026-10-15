import re
from datetime import UTC, datetime

from potok.config import MetadataPatterns
from potok.rss import genre_text, genres_from_title, magnet_link, parse_feed

PATTERNS = MetadataPatterns(
    rating=re.compile(r"(?i)(?:rating|рейтинг)[:\s]*(\d+(?:[.,]\d+)?)"),
    quality=re.compile(r"(?i)\b(2160p|1080p|720p|480p)\b"),
    genre=re.compile(r"(?i)(?:genre|жанр)[:\s]+([^,\]\n]+)"),
)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Demo</title>
    <item>
      <title>Example 1080p rating: 8,5 genre: drama</title>
      <link>https://example.com/item-1</link>
      <guid>item-1</guid>
      <pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate>
      <enclosure url="https://example.com/files/a.torrent" type="application/x-bittorrent"/>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom</title>
  <entry>
    <title>Clip 720p рейтинг: 6.0 жанр: комедия</title>
    <id>urn:item-2</id>
    <updated>2026-10-07T12:00:00Z</updated>
    <link rel="alternate" href="https://example.com/item-2"/>
    <link rel="enclosure" type="application/x-bittorrent" href="https://example.com/files/b.torrent"/>
    <summary>https://example.com/files/ignored.txt</summary>
  </entry>
</feed>
""".encode()


def test_parse_rss_extracts_fields() -> None:
    items = parse_feed(RSS, feed_name="demo", patterns=PATTERNS)

    assert len(items) == 1
    item = items[0]
    assert item.guid == "demo:item-1"
    assert item.title.startswith("Example")
    assert item.link == "https://example.com/item-1"
    assert item.torrent_url == "https://example.com/files/a.torrent"
    assert item.rating == 8.5
    assert item.quality == "1080p"
    assert item.genre == "drama"
    assert item.published == datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def test_parse_atom_extracts_fields() -> None:
    items = parse_feed(ATOM, feed_name="atom", patterns=PATTERNS)

    assert len(items) == 1
    item = items[0]
    assert item.guid == "atom:urn:item-2"
    assert item.quality == "720p"
    assert item.rating == 6.0
    assert item.genre == "комедия"
    assert item.torrent_url == "https://example.com/files/b.torrent"


def test_torrent_link_in_description() -> None:
    payload = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><item>
      <title>Plain</title>
      <guid>plain</guid>
      <description>file https://cdn.example.com/x.torrent end</description>
    </item></channel></rss>
    """
    items = parse_feed(payload, feed_name="demo", patterns=PATTERNS)
    assert items[0].torrent_url == "https://cdn.example.com/x.torrent"
    assert items[0].rating is None


def test_rutracker_magnet_is_recognized() -> None:
    link = (
        "magnet:?xt=urn:btih:262aa9e19151461068c542e7c5ae3ac01a379726"
        "&tr=http%3A%2F%2Frutracker.ru%2Fbt%2Fannounce.php%3Fuk%3D56WtM1NE1U"
    )
    assert magnet_link(link) == link
    assert magnet_link("https://example.com/a.torrent") is None


def test_genres_come_from_the_title_bracket() -> None:
    payload = """<?xml version="1.0"?>
    <rss version="2.0"><channel><item>
      <title>Фильм [2026, Комедия, криминал, WEB-DL 720p | MVO]</title>
      <guid>movie</guid>
      <link>magnet:?xt=urn:btih:abc</link>
    </item></channel></rss>
    """.encode()
    items = parse_feed(payload, feed_name="demo", patterns=PATTERNS)
    assert items[0].genre == "Комедия, Криминал"
    assert items[0].quality == "720p"


def test_seeders_size_and_rutracker_topic() -> None:
    payload = """<?xml version="1.0"?>
    <rss version="2.0"><channel><item>
      <title>Фильм [2026, Драма, WEB-DL 1080p]</title>
      <guid>movie</guid>
      <link>magnet:?xt=urn:btih:abc</link>
      <comments>http://rutracker.ru/viewtopic.php?t=213433</comments>
      <size>10737418240</size>
      <torznab:attr name="category" value="2000" />
      <torznab:attr name="seeders" value="42" />
      <torznab:attr name="peers" value="3" />
    </item></channel></rss>
    """.encode()
    items = parse_feed(payload, feed_name="demo", patterns=PATTERNS)
    assert items[0].seeders == 42
    assert items[0].size_bytes == 10737418240
    assert items[0].topic_url == "http://rutracker.ru/viewtopic.php?t=213433"


def test_genre_list_drops_quality_country_and_case_copies() -> None:
    title = (
        "Фильм [2024, Австралия, США, боевик, Боевик, военный, Военный, "
        "4K, UHD, UHD DCPRip, UHD WEB-DL-HEVC, WEB-DL-HEVC]"
    )
    assert genres_from_title(title) == "Боевик, Военный"
    stored = "боевик, Австралия, UHD WEB-DL-HEVC, Боевик, триллер"
    assert genre_text(stored) == "Боевик, Триллер"
    mixed = "Kомедия, Tриллер, триллеp, США, Перу, 4K, HDrezka Studio, ремейк, фэнтази, 202"
    assert genre_text(mixed) == "Комедия, Триллер, Фэнтези"


def test_old_rutracker_org_link_is_rewritten() -> None:
    from potok.rss import rutracker_topic

    fixed = rutracker_topic("https://rutracker.org/forum/viewtopic.php?t=212341")
    assert fixed == "http://rutracker.ru/viewtopic.php?t=212341"
