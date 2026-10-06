# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""파일 접근 기록.

"이 앱이 어떤 파일을 읽고 쓰는가" 는 운영자가 가장 먼저 확인하고 싶은 것이다.
설정이 안 먹을 때, 권한 문제를 쫓을 때, 변경이 어디에 기록됐는지 확인할 때 모두 이 목록을 본다.

메모리에만 둔다(최근 N건). 디스크에 또 하나의 로그를 만들 이유가 없고, 변경 이력은
history.sqlite3 에 이미 남는다. 여기 담는 것은 "접근했다" 는 사실뿐 — **내용은 담지 않는다.**
키 파일처럼 비밀값이 든 파일도 경로와 역할만 기록한다.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_EVENTS = 300

# 역할 이름은 UI 에 그대로 나온다. 운영자가 보는 말로 적는다.
ROLES = {
    "app-config": "앱 설정",
    "named-conf": "BIND 주 설정",
    "named-include": "BIND include",
    "zones-conf": "zone 정의",
    "options-conf": "options 설정",
    "zone-file": "zone 파일",
    "key-file": "TSIG/rndc 키",
    "backup": "백업",
    "state": "앱 상태(이력 DB)",
    "notice": "제3자 고지",
}


@dataclass(frozen=True)
class AccessEvent:
    at: str
    path: str
    action: str  # read | write | backup | delete
    role: str
    detail: str = ""
    ok: bool = True

    @property
    def role_label(self) -> str:
        return ROLES.get(self.role, self.role)


_events: deque[AccessEvent] = deque(maxlen=MAX_EVENTS)
_lock = threading.Lock()


def record(path: Path | str, action: str, role: str, *, detail: str = "", ok: bool = True) -> None:
    """접근 한 건을 기록한다. 실패해도 본래 작업을 방해하지 않는다."""
    try:
        event = AccessEvent(
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            path=str(path),
            action=action,
            role=role,
            detail=detail,
            ok=ok,
        )
        with _lock:
            _events.append(event)
    except Exception:  # noqa: BLE001 - 기록 실패가 기능을 막아서는 안 된다
        pass


def recent(limit: int = 100, *, path: str | None = None) -> list[AccessEvent]:
    with _lock:
        events = list(_events)
    if path:
        events = [e for e in events if e.path == path]
    return list(reversed(events))[:limit]


def clear() -> None:
    with _lock:
        _events.clear()


def summary() -> list[dict[str, object]]:
    """파일별 요약: 역할, 마지막 접근 시각, 읽기/쓰기 횟수."""
    with _lock:
        events = list(_events)

    rows: dict[str, dict[str, object]] = {}
    for event in events:
        row = rows.setdefault(
            event.path,
            {"path": event.path, "role": event.role, "role_label": event.role_label,
             "reads": 0, "writes": 0, "last_at": "", "last_action": "", "failures": 0},
        )
        if event.action == "read":
            row["reads"] = int(row["reads"]) + 1
        else:
            row["writes"] = int(row["writes"]) + 1
        if not event.ok:
            row["failures"] = int(row["failures"]) + 1
        row["last_at"] = event.at
        row["last_action"] = event.action
    return sorted(rows.values(), key=lambda r: str(r["last_at"]), reverse=True)


def stat_of(path: Path | str) -> dict[str, object]:
    """파일의 현재 상태. 없거나 못 읽어도 예외를 던지지 않는다."""
    target = Path(path)
    try:
        info = target.stat()
    except OSError:
        return {"exists": False, "size": None, "mtime": None, "readable": False, "writable": False}
    return {
        "exists": True,
        "size": info.st_size,
        "mtime": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "readable": os.access(target, os.R_OK),
        # 원자적 교체에는 디렉터리 쓰기 권한도 필요하다
        "writable": os.access(target, os.W_OK) and os.access(target.parent, os.W_OK),
    }


def inventory(cfg) -> list[dict[str, object]]:
    """이 앱이 다루는 파일 목록 — 실제로 접근했는지와 무관하게 "대상" 전체.

    접근 기록만 보면 "아직 안 읽은 파일" 이 안 보인다. 둘을 함께 봐야
    "왜 이 설정이 안 먹지" 같은 물음에 답할 수 있다.
    """
    from . import detect

    rows: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(path, role: str, note: str = "", secret: bool = False) -> None:
        if path is None:
            return
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        row: dict[str, object] = {
            "path": key,
            "role": role,
            "role_label": ROLES.get(role, role),
            "note": note,
            "secret": secret,
        }
        row.update(stat_of(path))
        rows.append(row)

    add(cfg.source, "app-config", "앱 설정 파일")
    named_conf = Path(cfg.bind.named_conf)
    add(named_conf, "named-conf", "BIND 주 설정 (named-checkconf -p 로 읽는다)")

    chroot = Path(cfg.bind.chroot) if cfg.bind.chroot else None
    for include in detect.includes_of(named_conf, root=chroot):
        role = "zones-conf" if include == Path(cfg.bind.zones_conf) else "named-include"
        note = "zone 블록을 추가·삭제하는 파일" if role == "zones-conf" else "named.conf 가 include"
        add(include, role, note, secret=include.suffix == ".key")

    add(cfg.bind.zones_conf, "zones-conf", "zone 블록을 추가·삭제하는 파일")
    add(cfg.bind.rndc_key, "key-file", "rndc 제어용 키 (내용은 표시하지 않는다)", secret=True)

    try:
        options = detect.find_options_file(named_conf, root=chroot)
        add(options, "options-conf", "options 블록이 들어 있는 파일 (전역 전달자)")
    except OSError:
        pass

    try:
        from . import service

        for entry in service.get_layout(cfg).zones:
            if entry.file is None:
                continue
            note = "동적 zone (journal 과 함께 쓰인다)" if entry.dynamic else f"{entry.zone_type} zone"
            add(entry.file, "zone-file", f"{entry.name} — {note}")
            journal = Path(str(entry.file) + ".jnl")
            if journal.exists():
                add(journal, "zone-file", f"{entry.name} — 동적 갱신 journal (named 가 관리)")
    except Exception:  # noqa: BLE001 - 목록 조회 실패가 이 화면을 막지 않게 한다
        pass

    add(Path(cfg.app.state_dir) / "history.sqlite3", "state", "변경 이력·감사 로그")
    add(Path(cfg.app.backup_dir), "backup", "변경 전 백업이 쌓이는 디렉터리")
    return rows
