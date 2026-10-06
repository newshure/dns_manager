# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""파일 접근 기록 테스트 — 비밀값이 새지 않는 것이 가장 중요하다."""

from __future__ import annotations

from pathlib import Path

import pytest

from dns_manager.core import fileaudit


@pytest.fixture(autouse=True)
def clean():
    fileaudit.clear()
    yield
    fileaudit.clear()


def test_record_and_recent():
    fileaudit.record("/etc/named.conf", "read", "named-conf", detail="named-checkconf -p")
    events = fileaudit.recent()
    assert len(events) == 1
    assert events[0].path == "/etc/named.conf"
    assert events[0].role_label == "BIND 주 설정"


def test_recent_is_newest_first():
    for i in range(3):
        fileaudit.record(f"/f{i}", "read", "zone-file")
    assert [e.path for e in fileaudit.recent()] == ["/f2", "/f1", "/f0"]


def test_ring_buffer_drops_oldest():
    for i in range(fileaudit.MAX_EVENTS + 20):
        fileaudit.record(f"/f{i}", "read", "zone-file")
    events = fileaudit.recent(limit=fileaudit.MAX_EVENTS + 50)
    assert len(events) == fileaudit.MAX_EVENTS
    assert events[-1].path == "/f20"


def test_summary_counts_reads_and_writes():
    fileaudit.record("/a.zone", "read", "zone-file")
    fileaudit.record("/a.zone", "read", "zone-file")
    fileaudit.record("/a.zone", "write", "zone-file")
    fileaudit.record("/b.zone", "write", "zone-file", ok=False)

    rows = {r["path"]: r for r in fileaudit.summary()}
    assert rows["/a.zone"]["reads"] == 2 and rows["/a.zone"]["writes"] == 1
    assert rows["/b.zone"]["failures"] == 1


def test_filter_by_path():
    fileaudit.record("/a", "read", "zone-file")
    fileaudit.record("/b", "read", "zone-file")
    assert [e.path for e in fileaudit.recent(path="/b")] == ["/b"]


def test_record_never_raises():
    class Weird:
        def __str__(self):
            raise RuntimeError("경로를 문자열로 못 바꿈")

    fileaudit.record(Weird(), "read", "zone-file")  # 예외가 새어 나오면 본래 작업이 멈춘다
    assert fileaudit.recent() == []


def test_stat_of_missing_file(tmp_path: Path):
    info = fileaudit.stat_of(tmp_path / "nope")
    assert info["exists"] is False and info["readable"] is False


def test_stat_of_existing_file(tmp_path: Path):
    target = tmp_path / "f.zone"
    target.write_text("x", encoding="utf-8")
    info = fileaudit.stat_of(target)
    assert info["exists"] is True and info["size"] == 1
    assert info["readable"] is True and info["writable"] is True


def test_secret_contents_are_never_recorded(tmp_path: Path):
    """키 파일을 읽어도 기록에는 경로와 키 이름만 남아야 한다."""
    from dns_manager.config import BindConfig, Config
    from dns_manager.core import dynamic
    from dns_manager.core.layout import ZoneEntry

    secret = "cmVhbGx5LXNlY3JldC12YWx1ZQ=="
    key_file = tmp_path / "ddns.key"
    key_file.write_text(
        f'key "ddns-key" {{\n\talgorithm hmac-sha256;\n\tsecret "{secret}";\n}};\n', encoding="utf-8"
    )
    cfg = Config(bind=BindConfig(rndc_key=key_file, named_conf=tmp_path / "named.conf"))
    entry = ZoneEntry(name="d.local", zone_type="master", view=None, file=tmp_path / "d.zone",
                      allow_update=("key", "ddns-key"))

    assert dynamic.load_key(cfg, entry).secret == secret  # 앱은 값을 쓴다
    events = fileaudit.recent()
    assert events, "키 파일 접근은 기록되어야 한다"
    blob = " ".join(f"{e.path} {e.detail}" for e in events)
    assert secret not in blob, "비밀값이 기록에 남으면 안 된다"
    assert "ddns-key" in blob and str(key_file) in blob
