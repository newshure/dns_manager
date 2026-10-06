"""변경 이력 저장소 테스트."""

from __future__ import annotations

from pathlib import Path

from dns_manager.core.history import History


def make(tmp_path: Path) -> History:
    return History(tmp_path / "state" / "history.sqlite3")


def test_db_is_created_with_parent_dirs(tmp_path: Path):
    history = make(tmp_path)
    assert history.path.is_file()


def test_record_and_get(tmp_path: Path):
    history = make(tmp_path)
    change_id = history.record(
        kind="zone",
        target="example.local",
        summary="A 추가",
        status="applied",
        author="tester",
        serial_before=1,
        serial_after=2,
        diff="--- a\n+++ b\n",
    )
    row = history.get(change_id)
    assert row is not None
    assert row.target == "example.local"
    assert row.status == "applied"
    assert row.serial_after == 2
    assert row.diff.startswith("--- a")


def test_list_is_newest_first_and_filterable(tmp_path: Path):
    history = make(tmp_path)
    for i in range(3):
        history.record(kind="zone", target="a.local", summary=f"{i}", status="applied")
    history.record(kind="zone", target="b.local", summary="other", status="applied")

    rows = history.list()
    assert len(rows) == 4
    assert rows[0].target == "b.local"

    only_a = history.list(target="a.local")
    assert len(only_a) == 3
    assert [r.summary for r in only_a] == ["2", "1", "0"]


def test_limit_is_applied(tmp_path: Path):
    history = make(tmp_path)
    for i in range(10):
        history.record(kind="zone", target="a.local", summary=str(i), status="applied")
    assert len(history.list(limit=4)) == 4


def test_unknown_id_returns_none(tmp_path: Path):
    assert make(tmp_path).get(12345) is None


def test_reopening_keeps_rows(tmp_path: Path):
    make(tmp_path).record(kind="conf", target="zones.conf", summary="x", status="applied")
    assert len(make(tmp_path).list()) == 1
