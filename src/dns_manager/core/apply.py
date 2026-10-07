# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""변경 트랜잭션 엔진 — 모든 쓰기가 통과하는 단일 경로.

테이블 편집·raw 편집·zone 추가·전달자 추가 어느 쪽에서 들어오든 여기를 지난다.
우회 경로를 만들지 않는 것이 이 모듈의 존재 이유다.

    lock → snapshot(백업) → serial 증가 → 임시파일 → named-checkzone
         → 원자적 교체 → rndc reload → 적용 확인 → 이력 기록
    실패 시: 백업 복구 → reload → BIND 의 원문 오류를 그대로 반환

원본 파일을 직접 열어 쓰지 않는다. named 가 반쯤 쓰인 파일을 읽는 상황을 구조적으로 배제한다.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from . import fileaudit
from . import serial as serial_mod
from . import server as server_mod
from . import zonefile
from .commands import CommandError, CommandResult, run
from .history import History
from .layout import ZoneEntry
from .locking import LockBusy, zone_lock


class ApplyError(RuntimeError):
    """변경을 적용할 수 없음. 원본은 그대로다."""


class VersionConflict(ApplyError):
    """앱이 읽은 뒤 외부에서 파일이 바뀌었다."""

    def __init__(self, message: str, current_text: str, diff: str) -> None:
        super().__init__(message)
        self.current_text = current_text
        self.diff = diff


@dataclass
class ApplyResult:
    """변경 결과. 실패해도 예외 대신 이 객체로 돌려주는 경우가 있다(검증 실패 등)."""

    ok: bool
    target: str
    status: str  # applied | rejected | rolled_back
    summary: str = ""
    serial_before: int | None = None
    serial_after: int | None = None
    backup: Path | None = None
    diff: str = ""
    checks: list[CommandResult] = field(default_factory=list)
    reload: CommandResult | None = None
    error: str | None = None
    change_id: int | None = None

    @property
    def check_output(self) -> str:
        """BIND 도구의 출력 원문. 가공하지 않는다."""
        return "\n".join(c.message for c in self.checks if c.message).strip()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_diff(before: str, after: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            n=3,
        )
    )


def _backup(cfg: Config, path: Path, target: str) -> Path:
    directory = Path(cfg.app.backup_dir) / target.replace("/", "_")
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"{_timestamp()}{path.suffix or '.bak'}"
    dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    fileaudit.record(dest, "backup", "backup", detail=f"{path} 의 사본")
    return dest


def _write_atomic(path: Path, text: str) -> None:
    """같은 디렉터리에 임시파일로 쓴 뒤 rename. rename 은 같은 파일시스템에서 원자적이다."""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".new")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        # 원본의 소유자·권한을 승계한다(named 가 읽어야 한다).
        if path.exists():
            stat = path.stat()
            os.chmod(tmp, stat.st_mode & 0o7777)
            try:
                os.chown(tmp, stat.st_uid, stat.st_gid)
            except PermissionError:
                pass  # 소유자 변경 권한이 없으면 그룹 권한에 의존한다
        os.replace(tmp, path)
        fileaudit.record(path, "write", "zone-file" if path.suffix in {".zone", ".rev"} else "zones-conf")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    # 디렉터리 엔트리도 내려써서 rename 을 영속화한다
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _stage(path: Path, text: str) -> Path:
    """검증용 임시 파일. 원본 옆에 두어 named-checkzone 의 상대 경로 해석을 맞춘다."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".check")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return Path(name)


def check_zone(cfg: Config, zone: str, path: Path) -> CommandResult:
    return run([cfg.bind.named_checkzone, zone, str(path)], timeout=cfg.bind.command_timeout)


def check_conf(cfg: Config) -> CommandResult:
    argv = [cfg.bind.named_checkconf]
    if cfg.bind.chroot:
        argv += ["-t", str(cfg.bind.chroot)]
    argv.append(str(cfg.bind.named_conf))
    return run(argv, timeout=cfg.bind.command_timeout)


def _rndc(cfg: Config, *args: str) -> CommandResult:
    argv = [cfg.bind.rndc]
    if cfg.bind.rndc_key:
        argv += ["-k", str(cfg.bind.rndc_key)]
    argv += list(args)
    return run(argv, timeout=cfg.bind.command_timeout)


def reload_zone(cfg: Config, zone: str) -> CommandResult:
    """zone 하나를 다시 읽게 한다 (Windows DNS Manager 의 zone ▸ Reload)."""
    return _rndc(cfg, "reload", zone)


def reconfig(cfg: Config) -> CommandResult:
    """설정을 다시 읽게 한다 (zone 추가/삭제 후)."""
    return _rndc(cfg, "reconfig")


def _history(cfg: Config) -> History:
    return History(Path(cfg.app.state_dir) / "history.sqlite3")


def needs_freeze(cfg: Config, entry: ZoneEntry) -> bool:
    """이 zone 의 파일을 고치려면 freeze 가 필요한가.

    동적 zone 은 named 가 journal 을 진실로 삼는다. 그대로 파일을 고치면 `rndc reload` 가
    'dynamic zone' 으로 거절되고 변경이 서비스되지 않는다. freeze 는 동적 갱신을 멈추고
    journal 을 파일에 반영하므로, 그 상태에서 고치고 thaw 로 다시 읽히면 된다.
    """
    from . import service  # 순환 import 를 피해 호출 시점에 가져온다

    try:
        status = server_mod.zone_status(cfg.bind, entry.name, entry.view)
    except CommandError:
        status = None
    return service.effective_dynamic(entry, status)


def _reload_failure_hint(message: str) -> str:
    """rndc reload 실패를 사람이 다음 행동을 고를 수 있는 말로 바꾼다.

    'dynamic zone' 은 특히 헷갈린다 — 파일은 제대로 고쳐졌는데 named 가 거절한 것이고,
    reload 를 몇 번 더 눌러도 결과는 같다. journal 을 쓰는 zone 이라는 뜻이다.
    """
    lowered = message.lower()
    if "dynamic zone" in lowered:
        return (
            "rndc reload 가 실패해 이전 내용으로 되돌렸습니다. "
            "이 zone 은 named 가 동적(journal)으로 다루므로 파일 직접 편집으로는 반영되지 않습니다. "
            "TSIG 키를 Settings 에 등록해 동적 갱신으로 고치거나, "
            "호스트에서 rndc freeze → 편집 → rndc thaw 순서로 진행하세요."
        )
    return "rndc reload 가 실패해 이전 내용으로 되돌렸습니다."


def apply_zone_text(
    cfg: Config,
    entry: ZoneEntry,
    new_text: str,
    *,
    expected_version: str | None = None,
    author: str | None = None,
    summary: str = "zone 파일 변경",
    bump_serial: bool = True,
    freeze: bool = True,
) -> ApplyResult:
    """zone 파일 전체를 새 내용으로 교체한다 — 모든 zone 쓰기의 공통 경로.

    expected_version 을 주면 그 사이 외부 변경이 있었는지 검사한다(낙관적 동시성).
    """
    if entry.file is None:
        raise ApplyError(f"'{entry.zone_type}' zone 은 zone 파일을 갖지 않습니다: {entry.name}")
    if entry.is_signed:
        raise ApplyError(f"DNSSEC 서명 zone 은 편집할 수 없습니다: {entry.name}")
    if entry.zone_type != "master":
        raise ApplyError(f"master zone 만 편집할 수 있습니다 (현재: {entry.zone_type})")

    path = entry.file
    history = _history(cfg)
    lock_dir = Path(cfg.app.state_dir) / "locks"

    # 동적 zone 이면 freeze 로 감싼다. 이 판단을 호출부에 맡기면 어딘가 한 곳이 빠지고,
    # 그 경로만 조용히 반영되지 않는다 — 실제로 그렇게 깨졌다.
    frozen = False
    if freeze and needs_freeze(cfg, entry):
        freeze_result = _rndc(cfg, "freeze", entry.name)
        if not freeze_result.ok:
            raise ApplyError(f"zone 을 freeze 하지 못했습니다: {freeze_result.message}")
        frozen = True

    result: ApplyResult | None = None
    try:
        result = _apply_zone_text_locked(
            cfg,
            entry,
            new_text,
            path=path,
            history=history,
            lock_dir=lock_dir,
            expected_version=expected_version,
            author=author,
            summary=summary,
            bump_serial=bump_serial,
        )
        return result
    finally:
        if frozen:
            thaw = _rndc(cfg, "thaw", entry.name)
            if not thaw.ok:
                # thaw 실패는 동적 갱신이 멈춘 채로 남는다는 뜻이다. 반드시 알려야 하지만,
                # 여기서 예외를 던지면 성공한 변경이 실패로 보인다. 결과에 붙인다.
                warning = (
                    f"경고: rndc thaw 에 실패했습니다 — {thaw.message}. "
                    f"이 zone 의 동적 갱신이 멈춰 있습니다. 호스트에서 "
                    f"rndc thaw {entry.name} 를 직접 실행하세요."
                )
                if result is not None:
                    result.error = f"{result.error}\n{warning}" if result.error else warning


def _apply_zone_text_locked(
    cfg: Config,
    entry: ZoneEntry,
    new_text: str,
    *,
    path: Path,
    history: History,
    lock_dir: Path,
    expected_version: str | None,
    author: str | None,
    summary: str,
    bump_serial: bool,
) -> ApplyResult:
    with zone_lock(lock_dir, entry.name, entry.view):
        current = zonefile.read_snapshot(path, entry.name)

        if expected_version is not None and expected_version != current.version:
            # 앱이 읽은 시점의 내용을 갖고 있지 않으므로(버전 토큰만 비교) diff 는
            # "보내려던 내용 vs 현재 파일" 로 만든다. 사용자가 무엇을 잃게 되는지 보여준다.
            raise VersionConflict(
                f"'{entry.name}' 이 앱에서 읽은 이후 외부에서 변경되었습니다. "
                f"현재 내용을 확인한 뒤 다시 저장하세요.",
                current_text=current.text,
                diff=make_diff(current.text, new_text, path.name),
            )

        serial_before = zonefile.parse(current).serial
        text = new_text
        serial_after = serial_before

        serial_warning = ""
        if bump_serial:
            # serial 증가는 "해석할 수 있을 때만" 한다. 해석에 실패했다고 여기서 막으면
            # 정작 사용자가 봐야 할 named-checkzone 의 정확한 오류를 가리게 된다.
            # 유효성 판정 권한은 BIND 에 있다.
            parsed_new = zonefile.parse(zonefile.ZoneSnapshot(entry.name, path, new_text, "", 0))
            base_serial = parsed_new.serial
            if base_serial is None:
                serial_warning = (
                    "SOA serial 을 자동으로 증가시키지 못했습니다"
                    + (f" ({parsed_new.parse_error})" if parsed_new.parse_error else " (SOA 를 찾을 수 없음)")
                    + ". serial 을 직접 확인하세요."
                )
                serial_after = serial_before
            else:
                # 새 내용의 serial 이 현재 파일보다 낮을 수 있다(롤백, 또는 옛 내용 붙여넣기).
                # 기준을 둘 중 큰 값으로 잡아야 결과 serial 이 이미 공개된 값보다 반드시 커진다.
                # serial 이 증가하지 않으면 secondary 가 전송을 받지 않는다(RFC 1982 순환 비교).
                effective = max(base_serial, serial_before or 0)
                try:
                    serial_after = serial_mod.next_serial(effective)
                    text = serial_mod.replace_in_text(new_text, base_serial, serial_after)
                except serial_mod.SerialError as exc:
                    raise ApplyError(str(exc)) from exc

        diff = make_diff(current.text, text, path.name)
        if not diff:
            return ApplyResult(
                ok=True,
                target=entry.name,
                status="applied",
                summary="변경 없음",
                serial_before=serial_before,
                serial_after=serial_before,
            )

        # --- 검증: 원본은 아직 건드리지 않았다 ---
        staged = _stage(path, text)
        try:
            check = check_zone(cfg, entry.name, staged)
        finally:
            staged.unlink(missing_ok=True)

        if not check.ok:
            result = ApplyResult(
                ok=False,
                target=entry.name,
                status="rejected",
                summary=summary,
                serial_before=serial_before,
                diff=diff,
                checks=[check],
                error="named-checkzone 검증에 실패했습니다. 원본은 변경되지 않았습니다.",
            )
            result.change_id = history.record(
                kind="zone",
                target=entry.name,
                view=entry.view,
                author=author,
                summary=summary,
                status="rejected",
                serial_before=serial_before,
                diff=diff,
                detail=check.message,
            )
            return result

        # --- 적용 ---
        backup = _backup(cfg, path, entry.name)
        _write_atomic(path, text)

        reload_result = _rndc(cfg, "reload", entry.name)
        if not reload_result.ok:
            _write_atomic(path, current.text)
            _rndc(cfg, "reload", entry.name)
            result = ApplyResult(
                ok=False,
                target=entry.name,
                status="rolled_back",
                summary=summary,
                serial_before=serial_before,
                serial_after=serial_after,
                backup=backup,
                diff=diff,
                checks=[check],
                reload=reload_result,
                error=_reload_failure_hint(reload_result.message),
            )
            result.change_id = history.record(
                kind="zone",
                target=entry.name,
                view=entry.view,
                author=author,
                summary=summary,
                status="rolled_back",
                serial_before=serial_before,
                serial_after=serial_after,
                backup=backup,
                diff=diff,
                detail=reload_result.message,
            )
            return result

        # --- 적용 확인: 서버가 실제로 새 serial 을 들었는지 ---
        detail = serial_warning
        try:
            status = server_mod.zone_status(cfg.bind, entry.name, entry.view)
            if status.serial is not None and serial_after is not None and status.serial != serial_after:
                detail = (
                    f"{detail}\n" if detail else ""
                ) + (
                    f"경고: 적재된 serial({status.serial}) 이 파일 serial({serial_after}) 과 다릅니다. "
                    f"reload 가 지연되었을 수 있습니다."
                )
        except CommandError as exc:
            detail = (f"{detail}\n" if detail else "") + f"적용 확인 실패: {exc}"

        result = ApplyResult(
            ok=True,
            target=entry.name,
            status="applied",
            summary=summary,
            serial_before=serial_before,
            serial_after=serial_after,
            backup=backup,
            diff=diff,
            checks=[check],
            reload=reload_result,
            error=detail or None,
        )
        result.change_id = history.record(
            kind="zone",
            target=entry.name,
            view=entry.view,
            author=author,
            summary=summary,
            status="applied",
            serial_before=serial_before,
            serial_after=serial_after,
            backup=backup,
            diff=diff,
            detail=detail,
        )
        return result


def apply_conf_text(
    cfg: Config,
    new_text: str,
    *,
    path: Path | None = None,
    expected_version: str | None = None,
    author: str | None = None,
    summary: str = "zone 설정 변경",
    reconfig: bool = True,
) -> ApplyResult:
    """설정 파일을 교체한다 — zone 추가/삭제, 전달자 변경의 공통 경로.

    기본 대상은 zone 정의 include 파일(zones.conf)이며, path 로 named.conf 자체도 다룰 수 있다
    (전역 전달자는 options 블록 안에 있어 named.conf 를 고쳐야 한다).

    zone 파일과 달리 named-checkconf 는 전체 설정을 본다. 따라서 임시파일로는 검증할 수 없고,
    실제 파일을 바꾼 뒤 검증하고 실패하면 즉시 되돌린다.
    """
    path = Path(path or cfg.bind.zones_conf)
    if not path.exists():
        raise ApplyError(
            f"설정 파일이 없습니다: {path}. named.conf 에 include 를 추가하고 파일을 만드세요."
        )

    history = _history(cfg)
    lock_dir = Path(cfg.app.state_dir) / "locks"

    with zone_lock(lock_dir, f"__conf__{path}"):  # 설정 파일 단위 락
        before = path.read_text(encoding="utf-8")
        current_version = zonefile.read_snapshot(path, "conf").version
        if expected_version is not None and expected_version != current_version:
            raise VersionConflict(
                f"{path} 가 앱에서 읽은 이후 외부에서 변경되었습니다.",
                current_text=before,
                diff="",
            )

        diff = make_diff(before, new_text, path.name)
        if not diff:
            return ApplyResult(ok=True, target=str(path), status="applied", summary="변경 없음")

        backup = _backup(cfg, path, "named.conf.d")
        _write_atomic(path, new_text)

        check = check_conf(cfg)
        if not check.ok:
            _write_atomic(path, before)
            result = ApplyResult(
                ok=False,
                target=str(path),
                status="rolled_back",
                summary=summary,
                backup=backup,
                diff=diff,
                checks=[check],
                error="named-checkconf 검증에 실패해 이전 설정으로 되돌렸습니다.",
            )
            result.change_id = history.record(
                kind="conf",
                target=str(path),
                author=author,
                summary=summary,
                status="rolled_back",
                backup=backup,
                diff=diff,
                detail=check.message,
            )
            return result

        reload_result = None
        if reconfig:
            reload_result = _rndc(cfg, "reconfig")
            if not reload_result.ok:
                _write_atomic(path, before)
                _rndc(cfg, "reconfig")
                result = ApplyResult(
                    ok=False,
                    target=str(path),
                    status="rolled_back",
                    summary=summary,
                    backup=backup,
                    diff=diff,
                    checks=[check],
                    reload=reload_result,
                    error="rndc reconfig 가 실패해 이전 설정으로 되돌렸습니다.",
                )
                result.change_id = history.record(
                    kind="conf",
                    target=str(path),
                    author=author,
                    summary=summary,
                    status="rolled_back",
                    backup=backup,
                    diff=diff,
                    detail=reload_result.message,
                )
                return result

        result = ApplyResult(
            ok=True,
            target=str(path),
            status="applied",
            summary=summary,
            backup=backup,
            diff=diff,
            checks=[check],
            reload=reload_result,
        )
        result.change_id = history.record(
            kind="conf",
            target=str(path),
            author=author,
            summary=summary,
            status="applied",
            backup=backup,
            diff=diff,
        )
        return result


def rollback(cfg: Config, entry: ZoneEntry, change_id: int, *, author: str | None = None) -> ApplyResult:
    """이력의 백업 시점으로 zone 을 되돌린다. 되돌리기도 동일한 트랜잭션을 거친다."""
    history = _history(cfg)
    change = history.get(change_id)
    if change is None:
        raise ApplyError(f"이력을 찾을 수 없습니다: {change_id}")
    if not change.backup:
        raise ApplyError(f"이 변경에는 백업이 없습니다: {change_id}")
    backup_path = Path(change.backup)
    if not backup_path.is_file():
        raise ApplyError(f"백업 파일이 없습니다: {backup_path}")

    text = backup_path.read_text(encoding="utf-8")
    return apply_zone_text(
        cfg,
        entry,
        text,
        author=author,
        summary=f"변경 #{change_id} 시점으로 롤백",
        bump_serial=True,
    )


__all__ = [
    "ApplyError",
    "reconfig",
    "reload_zone",
    "ApplyResult",
    "VersionConflict",
    "LockBusy",
    "apply_zone_text",
    "apply_conf_text",
    "check_conf",
    "check_zone",
    "make_diff",
    "rollback",
]
