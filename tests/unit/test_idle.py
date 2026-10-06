# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""유휴 자동 종료 테스트.

이 도구에는 인증이 없다. 작업이 끝났는데 띄워 둔 채 잊으면 그 포트에 닿는 누구나
DNS 를 고칠 수 있다. 스스로 내려가는 장치가 제대로 도는지 확인한다.
"""

from __future__ import annotations

import asyncio

import pytest

from dns_manager.core import idle
from dns_manager.core.idle import IdleWatch, parse_duration


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30m", 1800),
        ("2h", 7200),
        ("90s", 90),
        ("45", 2700),  # 단위를 안 쓰면 분
        ("1.5h", 5400),
        ("0", 0),
        ("off", 0),
        ("none", 0),
        ("", 0),
        ("  ", 0),
    ],
)
def test_parse_duration(text: str, seconds: int):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["abc", "30x", "-5m", "m"])
def test_parse_duration_rejects_garbage(bad: str):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_watch_starts_fresh():
    state = IdleWatch(timeout=60)
    assert state.expired is False
    assert 59 <= state.remaining <= 60


def test_touch_resets_the_clock(monkeypatch: pytest.MonkeyPatch):
    now = [1000.0]
    monkeypatch.setattr(idle.time, "monotonic", lambda: now[0])

    state = IdleWatch(timeout=60)
    now[0] += 59
    assert state.expired is False

    state.touch()  # 요청이 왔다
    now[0] += 59
    assert state.expired is False, "요청이 오면 시계가 되돌아가야 한다"

    now[0] += 2
    assert state.expired is True


def test_timeout_zero_never_expires(monkeypatch: pytest.MonkeyPatch):
    now = [1000.0]
    monkeypatch.setattr(idle.time, "monotonic", lambda: now[0])
    state = IdleWatch(timeout=0)
    now[0] += 10_000
    assert state.expired is False, "0 이면 자동 종료를 쓰지 않는다"


def test_watch_signals_when_idle(monkeypatch: pytest.MonkeyPatch):
    """유휴 시간이 넘으면 자기 자신에게 종료 신호를 보낸다."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(idle.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(idle, "CHECK_INTERVAL", 0.01)

    state = IdleWatch(timeout=0.02)
    asyncio.run(asyncio.wait_for(idle.watch(state), timeout=3))

    assert sent, "종료 신호를 보내야 한다"
    assert sent[0][1] == idle.signal.SIGTERM, "SIGTERM 이어야 정상 종료 절차를 밟는다"


def test_watch_stops_when_asked(monkeypatch: pytest.MonkeyPatch):
    sent: list = []
    monkeypatch.setattr(idle.os, "kill", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(idle, "CHECK_INTERVAL", 0.01)

    state = IdleWatch(timeout=5)

    async def run():
        task = asyncio.create_task(idle.watch(state))
        await asyncio.sleep(0.05)
        state.stopping = True
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())
    assert sent == [], "정지 요청을 받았으면 종료 신호를 보내지 않는다"
