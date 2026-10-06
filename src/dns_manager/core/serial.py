# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""SOA serial 정책.

규칙: `YYYYMMDDnn` (RFC 1912 2.2 권고 형식). 같은 날 100회를 넘기면 형식을 지키는 것보다
"단조 증가"가 중요하므로 단순 +1 로 넘어간다 — serial 이 줄어들면 secondary 가 갱신을
받지 못한다(RFC 1982 순환 비교).

serial 치환은 zone 파일 텍스트에 직접 한다. dnspython 으로 재직렬화하면 주석·서식이
사라지므로, 원문에서 serial 토큰만 바꾼다.
"""

from __future__ import annotations

import re
from datetime import date

MAX_SERIAL = 2**32 - 1

# SOA 레코드의 시작 지점. 이름/TTL/클래스는 생략될 수 있다.
_SOA_START = re.compile(r"^[^\s;]*[ \t]*(?:\d+[ \t]+)?(?:IN[ \t]+)?SOA\b", re.IGNORECASE | re.MULTILINE)


class SerialError(RuntimeError):
    pass


def next_serial(current: int | None, today: date | None = None) -> int:
    """다음 serial 값을 계산한다. 항상 current 보다 크다."""
    day = today or date.today()
    base = int(f"{day:%Y%m%d}") * 100

    if current is None:
        return base + 1
    if current < base:
        return base + 1
    if current < base + 99:
        return current + 1
    # 하루 100회 초과: 형식을 포기하고 증가만 보장한다.
    nxt = current + 1
    if nxt > MAX_SERIAL:
        raise SerialError("serial 이 32비트 상한에 도달했습니다. 수동 조정이 필요합니다.")
    return nxt


def replace_in_text(text: str, current: int, new: int) -> str:
    """zone 파일 텍스트에서 SOA 의 serial 토큰만 바꾼다.

    SOA 레코드 이후 처음 나오는 `current` 와 정확히 같은 정수 토큰을 치환한다.
    도메인 이름 안의 숫자는 독립 토큰이 아니므로 걸리지 않는다.
    """
    if current == new:
        return text

    match = _SOA_START.search(text)
    if match is None:
        raise SerialError("zone 파일에서 SOA 레코드를 찾을 수 없습니다.")

    head, tail = text[: match.end()], text[match.end() :]
    token = re.compile(rf"(?<![\w.-]){current}(?![\w.-])")
    replaced, count = token.subn(str(new), tail, count=1)
    if count == 0:
        raise SerialError(f"SOA 에서 현재 serial({current}) 을 찾지 못했습니다.")
    return head + replaced


def bump_text(text: str, current: int | None, today: date | None = None) -> tuple[str, int]:
    """텍스트의 serial 을 다음 값으로 올리고 (새 텍스트, 새 serial) 을 돌려준다."""
    new = next_serial(current, today)
    if current is None:
        raise SerialError("현재 serial 을 알 수 없어 증가시킬 수 없습니다.")
    return replace_in_text(text, current, new), new
