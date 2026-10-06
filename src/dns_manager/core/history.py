# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""변경 이력·감사 로그 (SQLite).

**zone 레코드를 미러링하지 않는다.** 여기에 담는 것은 "무엇을 언제 누가 바꿨고 결과가
무엇이었나"뿐이다. zone 의 진실은 언제나 zone 파일이다.

Windows DNS Manager 에는 없는 기능이지만, 파일 기반 BIND 에서는 스냅샷·diff·시점 복구가
가능하므로 운영상 가치가 가장 크다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    kind          TEXT    NOT NULL,   -- zone | conf
    target        TEXT    NOT NULL,   -- zone 이름 또는 설정 파일 경로
    view          TEXT,
    author        TEXT,
    summary       TEXT    NOT NULL,
    serial_before INTEGER,
    serial_after  INTEGER,
    backup        TEXT,
    diff          TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL,   -- applied | rejected | rolled_back
    detail        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_changes_target_ts ON changes(target, ts DESC);
CREATE INDEX IF NOT EXISTS idx_changes_ts ON changes(ts DESC);
"""


@dataclass(frozen=True)
class ChangeRecord:
    id: int
    ts: str
    kind: str
    target: str
    view: str | None
    author: str | None
    summary: str
    serial_before: int | None
    serial_after: int | None
    backup: str | None
    diff: str
    status: str
    detail: str


class History:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def record(
        self,
        *,
        kind: str,
        target: str,
        summary: str,
        status: str,
        view: str | None = None,
        author: str | None = None,
        serial_before: int | None = None,
        serial_after: int | None = None,
        backup: Path | str | None = None,
        diff: str = "",
        detail: str = "",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO changes (ts, kind, target, view, author, summary, serial_before,"
                " serial_after, backup, diff, status, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    kind,
                    target,
                    view,
                    author,
                    summary,
                    serial_before,
                    serial_after,
                    str(backup) if backup else None,
                    diff,
                    status,
                    detail,
                ),
            )
            return int(cur.lastrowid)

    def list(self, *, target: str | None = None, limit: int = 100) -> list[ChangeRecord]:
        sql = "SELECT * FROM changes"
        args: list[object] = []
        if target:
            sql += " WHERE target = ?"
            args.append(target)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [ChangeRecord(**dict(row)) for row in conn.execute(sql, args)]

    def get(self, change_id: int) -> ChangeRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        return ChangeRecord(**dict(row)) if row else None
