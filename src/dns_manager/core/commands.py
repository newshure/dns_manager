# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""외부 명령 실행 래퍼.

named-checkconf / named-checkzone / rndc / dig 는 BIND 가 제공하는 권위 있는 도구다.
검증·적용 로직을 재구현하지 않고 이들을 호출하며, 출력은 가공하지 않고 그대로 보존한다
(BIND 의 오류 메시지가 사용자에게 가장 정확한 정보다).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class CommandError(RuntimeError):
    """명령 실행 자체가 불가능한 경우(미설치, 타임아웃 등)."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def message(self) -> str:
        """사용자에게 보여줄 출력. stderr 우선, 없으면 stdout."""
        return (self.stderr.strip() or self.stdout.strip())


def which(program: str) -> str | None:
    return shutil.which(program)


def run(
    argv: list[str] | tuple[str, ...],
    *,
    timeout: float = 20.0,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> CommandResult:
    argv = tuple(str(a) for a in argv)
    try:
        proc = subprocess.run(  # noqa: S603 - 인자는 설정값과 검증된 zone 이름만
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"명령을 찾을 수 없습니다: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"명령 타임아웃({timeout}s): {' '.join(argv)}") from exc
    return CommandResult(argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
