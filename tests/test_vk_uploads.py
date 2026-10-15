from pathlib import Path

from potok.vk_uploads import UploadBoard, UploadOrder


def test_tick_stores_upload_speed(tmp_path: Path, monkeypatch) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr("potok.vk_uploads.time.time", lambda: clock["now"])
    board = UploadBoard(tmp_path / "vk_uploads.json")
    job_id = board.begin(name="film.mkv", title="Фильм")
    clock["now"] = 1_003.0
    board.tick(job_id, 6_000_000, 20_000_000)
    assert board.jobs()[0]["speed"] == 2_000_000
    assert board.jobs()[0]["percent"] == 30

    clock["now"] = 1_004.0
    board.tick(job_id, 6_100_000, 20_000_000)
    assert board.jobs()[0]["speed"] == 2_000_000
    assert board.job(job_id)["sent"] == 6_000_000

    clock["now"] = 1_006.0
    board.tick(job_id, 9_000_000, 20_000_000)
    assert board.jobs()[0]["speed"] == 1_000_000
    assert board.jobs()[0]["percent"] == 45
    assert board.jobs()[0]["eta"] == "меньше минуты"

    clock["now"] = 1_008.0
    board.tick(job_id, 10_000_000, 130_000_000)
    assert board.jobs()[0]["speed"] == 500_000
    assert board.jobs()[0]["eta"] == "4 мин"


def test_progress_and_stop_share_one_file(tmp_path: Path) -> None:
    path = tmp_path / "vk_uploads.json"
    board = UploadBoard(path)
    job_id = board.begin(name="film.mkv", title="Фильм")
    board.tick(job_id, 40, 100)
    board.remember(job_id, 5, 9)

    other = UploadBoard(path)
    assert other.jobs()[0]["percent"] == 40
    assert other.jobs()[0]["status"] == "uploading"
    stopped = other.request_stop(job_id)
    assert stopped is not None
    assert stopped["owner_id"] == 5
    assert stopped["video_id"] == 9

    board.tick(job_id, 80, 100)
    assert board.is_stop_requested(job_id)
    assert board.jobs()[0]["status"] == "stopping"
    assert board.jobs()[0]["percent"] == 40

    board.set_status(job_id, "stopped", message="Остановлено")
    assert board.jobs()[0]["status"] == "stopped"
    assert board.jobs()[0]["message"] == "Остановлено"


def test_stuck_stop_becomes_stopped(tmp_path: Path, monkeypatch) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr("potok.vk_uploads.time.time", lambda: clock["now"])
    board = UploadBoard(tmp_path / "vk_uploads.json")
    job_id = board.begin(name="film.mkv", title="Фильм")
    board.request_stop(job_id)
    board.settle_stops()
    assert board.jobs()[0]["status"] == "stopping"

    clock["now"] = 1_020.0
    board.settle_stops()
    assert board.jobs()[0]["status"] == "stopped"
    assert board.jobs()[0]["message"] == "Остановлено"


def test_upload_order_moves_and_keeps_new_hashes_at_the_end(tmp_path: Path) -> None:
    first = "ab" * 20
    second = "cd" * 20
    third = "ef" * 20
    order = UploadOrder(tmp_path / "vk_order.json")
    assert order.apply([first, second]) == [first, second]
    assert order.move([first, second], first, "down") == [second, first]
    assert order.arranged([second, first, third]) == [second, first, third]
    order.forget(first)
    assert order.arranged([third, second, first]) == [second, third, first]


def test_drop_removes_finished_row_only(tmp_path: Path) -> None:
    board = UploadBoard(tmp_path / "vk_uploads.json")
    live = board.begin(name="live.mkv", title="Идёт")
    done = board.begin(name="done.mkv", title="Готово")
    board.set_status(done, "error", message="обрыв")
    assert board.drop(live) is None
    assert board.drop(done) is not None
    assert [item["id"] for item in board.jobs()] == [live]
