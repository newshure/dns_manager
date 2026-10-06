# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""zone 단위 쓰기 락.

동시 편집으로 서로의 변경을 덮어쓰는 것을 막는다. 락 파일은 zone 파일 디렉터리가 아니라
앱 상태 디렉터리에 둔다(zone 디렉터리에 잡다한 파일을 만들면 named 운영자가 혼란스럽다).
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
from contextlib import contextmanager
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class LockBusy(RuntimeError):
    """다른 작업이 같은 zone 을 쓰고 있다."""


def lock_name(zone: str, view: str | None = None) -> str:
    key = f"{view or ''}|{zone}".strip("|")
    safe = _SAFE.sub("_", key)[:80]
    # 이름이 잘려 충돌하는 경우를 대비해 해시를 덧붙인다.
    return f"{safe}.{hashlib.sha256(key.encode()).hexdigest()[:8]}.lock"


@contextmanager
def zone_lock(lock_dir: Path, zone: str, view: str | None = None):
    """zone 쓰기 락. 이미 잠겨 있으면 기다리지 않고 LockBusy 를 던진다."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / lock_name(zone, view)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockBusy(f"다른 작업이 '{zone}' 을 변경하는 중입니다. 잠시 후 다시 시도하세요.") from exc
            raise
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
