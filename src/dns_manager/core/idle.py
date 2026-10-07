# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""유휴 자동 종료.

이 도구에는 인증이 없다. 작업 시간 동안만 띄우는 것이 전제이므로, 띄워 둔 채 잊었을 때
스스로 내려가게 한다. 서비스로 등록해 상시 구동하는 물건이 아니다.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass

CHECK_INTERVAL = 15.0


@dataclass
class IdleWatch:
    """마지막 요청 이후 얼마나 지났는지 지켜본다."""

    timeout: float
    last_seen: float = 0.0
    stopping: bool = False

    def __post_init__(self) -> None:
        self.touch()

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_seen

    @property
    def remaining(self) -> float:
        return max(0.0, self.timeout - self.idle_seconds)

    @property
    def expired(self) -> bool:
        return self.timeout > 0 and self.idle_seconds >= self.timeout


async def watch(state: IdleWatch, on_expire=None) -> None:
    """유휴 시간이 넘으면 자기 자신에게 종료 신호를 보낸다.

    uvicorn 이 SIGTERM 을 받아 정상 종료 절차를 밟는다 — 진행 중인 요청을 끊지 않는다.
    """
    while not state.stopping:
        await asyncio.sleep(min(CHECK_INTERVAL, max(1.0, state.remaining)))
        if state.stopping:
            return
        if state.expired:
            minutes = state.timeout / 60
            print(
                f"유휴 {minutes:.0f}분이 지나 종료합니다. "
                f"다시 쓰려면 dns_manager start 로 띄우세요.",
                flush=True,
            )
            if on_expire is not None:
                on_expire()
            os.kill(os.getpid(), signal.SIGTERM)
            return


def parse_duration(text: str) -> int:
    """'30m' · '2h' · '90s' · '45' 를 초로 바꾼다. 빈 값이나 0 은 '쓰지 않음'."""
    value = (text or "").strip().lower()
    if not value or value in {"0", "off", "none"}:
        return 0
    units = {"s": 1, "m": 60, "h": 3600}
    if value[-1] in units:
        number, factor = value[:-1], units[value[-1]]
    else:
        number, factor = value, 60  # 단위를 안 쓰면 분으로 본다
    try:
        amount = float(number)
    except ValueError as exc:
        raise ValueError(f"시간 형식이 올바르지 않습니다: {text} (예: 30m, 2h, 90s)") from exc
    if amount < 0:
        raise ValueError("시간은 0 이상이어야 합니다.")
    return int(amount * factor)
