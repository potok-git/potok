# Graph Report - Поток  (2026-10-09)

## Corpus Check
- 22 files · ~13,139 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 6 file(s) not represented in the graph (top: (none) 5, .example 1)

## Summary
- 417 nodes · 1200 edges · 12 communities (9 shown, 3 thin omitted)
- Extraction: 83% EXTRACTED · 17% INFERRED · 0% AMBIGUOUS · INFERRED: 208 edges (avg confidence: 0.91)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `467cca99`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- pipeline.py
- bot.py
- publish.py
- test_vpn.py
- config.py
- rss.py
- vk_video.py
- SeenStore
- potok
- accept
- test_post_stream_sends_chunks_even_if_socket_takes_them_in_parts

## God Nodes (most connected - your core abstractions)
1. `SeenStore` - 41 edges
2. `ConfigError` - 39 edges
3. `AppConfig` - 32 edges
4. `load_config()` - 32 edges
5. `VkError` - 30 edges
6. `FeedItem` - 26 edges
7. `run_fetch()` - 24 edges
8. `VpnManager` - 24 edges
9. `main()` - 22 edges
10. `deliver_and_notify()` - 20 edges

## Surprising Connections (you probably didn't know these)
- `test_resolve_telegram_requires_token()` --uses--> `ConfigError`  [INFERRED]
  tests/test_bot.py → src/potok/config.py
- `test_fetch_waits_for_approval_before_torrent()` --uses--> `FetchError`  [INFERRED]
  tests/test_pipeline.py → src/potok/pipeline.py
- `_item()` --uses--> `FeedItem`  [INFERRED]
  tests/test_bot.py → src/potok/rss.py
- `_item()` --uses--> `FeedItem`  [INFERRED]
  tests/test_filters.py → src/potok/rss.py
- `test_clear_download_queue_removes_torrent_file()` --uses--> `FeedItem`  [INFERRED]
  tests/test_pipeline.py → src/potok/rss.py

## Import Cycles
- None detected.

## Communities (12 total, 3 thin omitted)

### Community 0 - "pipeline.py"
Cohesion: 0.06
Nodes (21): vpn_text(), _config_path(), log_state(), main(), run_demo(), _setup_logging(), deliver_approved(), _download_approved() (+13 more)

### Community 1 - "bot.py"
Cohesion: 0.07
Nodes (41): _allowed(), approval_keyboard(), cmd_clear(), cmd_pending(), cmd_settings(), cmd_start(), cmd_status(), cmd_update() (+33 more)

### Community 2 - "publish.py"
Cohesion: 0.09
Nodes (34): _publish_command(), _aria_timeout(), build_aria2_command(), _changed_files(), deliver_and_notify(), default_notify(), download_with_aria2(), DownloadError (+26 more)

### Community 3 - "test_vpn.py"
Cohesion: 0.41
Nodes (8): _manager(), _ok(), test_down_when_already_down(), test_missing_wg_binary(), test_up_calls_wg_quick(), test_up_failure_hides_private_key(), test_up_missing_config(), test_up_skips_when_already_active()

### Community 4 - "config.py"
Cohesion: 0.10
Nodes (37): _api_version(), _bool_value(), _bounded_int(), _bounded_number(), _compile_pattern(), ConfigError, _credentials(), CredentialSettings (+29 more)

### Community 5 - "rss.py"
Cohesion: 0.17
Nodes (12): _entry_to_item(), _group(), _href_if_torrent(), _list(), parse_feed(), _published(), _rating(), _text() (+4 more)

### Community 6 - "vk_video.py"
Cohesion: 0.06
Nodes (30): VkSettings, _upload_one(), _api_message(), _as_int(), _check_upload_payload(), _clip(), _decode_json(), _disposition() (+22 more)

### Community 7 - "SeenStore"
Cohesion: 0.10
Nodes (13): _stored_urls(), SeenStore, StoredItem, _to_item(), _config(), test_clear_download_queue_removes_torrent_file(), test_fetch_waits_for_approval_before_torrent(), _item() (+5 more)

### Community 10 - "accept"
Cohesion: 0.33
Nodes (7): accept(), _item(), test_accepts_fresh_high_rating(), test_rejects_item_without_date_when_age_limited(), test_rejects_low_rating(), test_rejects_missing_rating_when_threshold_set(), test_rejects_old_item()

## Knowledge Gaps
- **1 isolated node(s):** `potok`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 111 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SeenStore` connect `SeenStore` to `pipeline.py`, `bot.py`, `publish.py`?**
  _High betweenness centrality (0.099) - this node is a cross-community bridge._
- **Are the 15 inferred relationships involving `SeenStore` (e.g. with `cmd_pending()` and `cmd_status()`) actually correct?**
  _`SeenStore` has 15 INFERRED edges - model-reasoned connections that need verification._
- **What connects `potok` to the rest of the system?**
  _1 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `pipeline.py` be split into smaller, more focused modules?**
  _Cohesion score 0.0642243328810493 - nodes in this community are weakly interconnected._
- **Why does `VkError` connect `vk_video.py` to `pipeline.py`, `bot.py`, `publish.py`?**
  _High betweenness centrality (0.088) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `ConfigError` (e.g. with `cmd_update()` and `_publish()`) actually correct?**
  _`ConfigError` has 10 INFERRED edges - model-reasoned connections that need verification._
- **Should `bot.py` be split into smaller, more focused modules?**
  _Cohesion score 0.06573426573426573 - nodes in this community are weakly interconnected._