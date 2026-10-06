# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""진단 — "왜 이런 zone 이 보이지?" 에 답하기 위한 한 장짜리 보고서.

원격에서 문제를 쫓을 때 필요한 사실만 모은다: 앱이 실제로 어떤 설정 파일을 읽는지,
BIND 가 무엇이라고 답하는지, 각 zone 이 **어느 파일에 적혀 있는지**, 그 파일들이 지금
존재하는지. 추측 대신 이 출력을 보고 판단한다.

    dns_manager doctor          (제어 스크립트)
    python -m dns_manager --doctor
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from .config import Config, load_config
from .core import detect, fileaudit, settings


def _line(char: str = "─", width: int = 72) -> str:
    return char * width


def _section(title: str) -> str:
    return f"\n{title}\n{_line()}"


def _file_state(path: Path | str | None) -> str:
    if path in (None, ""):
        return "(미설정)"
    target = Path(str(path))
    info = fileaudit.stat_of(target)
    if not info["exists"]:
        return f"{target}  ← 없음"
    perm = "읽기" + ("·쓰기" if info["writable"] else " 전용" if info["readable"] else " 불가")
    return f"{target}  ({info['size']}B, {perm}, 수정 {info['mtime']})"


def _zone_truth(cfg: Config, entry) -> list[str]:
    """파일에 적힌 것과 **서버가 실제로 답하는 것**을 맞춰 본다.

    "파일에는 있는데 응답이 없다" 는 이 둘이 어긋났다는 뜻이다. 어느 쪽이 어긋났는지는
    추측으로 알 수 없다 — serial 을 서로 비교하고, 없는 이름을 하나 던져 본다.
    """
    from .core import server as server_mod
    from .core import service as service_mod
    from .core import zonefile

    out: list[str] = []
    try:
        status = server_mod.zone_status(cfg.bind, entry.name, entry.view)
    except Exception as exc:  # noqa: BLE001
        return [f"      적재 확인 실패: {exc}"]

    if not status.available:
        out.append(f"      적재      : ← 적재되지 않았습니다 ({status.raw.strip()[:80]})")
        out.append("        이 상태에서는 질의가 SERVFAIL 로 돌아옵니다. named 로그를 확인하세요.")
        return out

    out.append(f"      적재      : serial {status.serial} / {status.loaded or '시각 불명'}")

    file_serial = None
    has_wildcard = False
    try:
        content = zonefile.load(entry.file, entry.name)  # type: ignore[arg-type]
        file_serial = content.serial
        has_wildcard = any(r.name == "*" for r in content.records)
    except Exception:  # noqa: BLE001
        pass

    if service_mod.file_changed_since_load(entry, status):
        out.append("      ! 파일이 적재 이후에 바뀌었습니다 — named 는 옛 내용을 서비스 중입니다.")
        out.append(f"        해결: rndc reload {entry.name}")

    # 서버가 실제로 무엇을 답하는가
    answer = server_mod.query(cfg.bind, entry.name, "SOA")
    served_serial = None
    for line in answer.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 11 and parts[3] == "SOA":
            try:
                served_serial = int(parts[6])
            except ValueError:
                pass
            break
    out.append(f"      서버 응답 : SOA serial {served_serial if served_serial is not None else '없음'}")

    if served_serial is not None and file_serial is not None and served_serial != file_serial:
        out.append(
            f"      ! 서버가 답하는 serial({served_serial}) 과 파일의 serial({file_serial}) 이 다릅니다."
        )
        out.append(f"        서버는 다른 내용을 들고 있습니다 → rndc reload {entry.name}")

    if has_wildcard:
        probe = server_mod.query(cfg.bind, f"zz-probe-does-not-exist.{entry.name}", "A")
        status_code = ""
        for line in probe.stdout.splitlines():
            if "status:" in line:
                status_code = line.split("status:")[1].split(",")[0].strip()
                break
        out.append(f"      와일드카드: 파일에 있음 / 없는 이름 질의 결과 {status_code or '확인 불가'}")
        if status_code == "NXDOMAIN":
            out.append(
                "      ! 파일에는 와일드카드가 있는데 서버는 NXDOMAIN 을 돌려줍니다."
            )
            out.append(
                "        서버가 들고 있는 내용에 그 레코드가 없습니다 — reload 하거나,"
            )
            out.append(
                f"        named 가 읽는 파일이 이것인지 확인하세요: {entry.file}"
            )
    return out


def report(cfg: Config | None = None) -> str:
    cfg = cfg or load_config()
    out: list[str] = []

    out.append("dns_manager 진단 보고서")
    out.append(_line("="))
    out.append(f"호스트     : {platform.node()} / {platform.platform()}")
    out.append(f"파이썬     : {sys.version.split()[0]} ({sys.executable})")
    out.append(f"실행 계정  : uid={os.getuid()} gid={os.getgid()}")
    out.append(f"모듈 위치  : {Path(__file__).resolve().parent}")

    out.append(_section("1. 설정 (앱이 실제로 쓰는 값)"))
    out.append(f"설정 파일  : {_file_state(cfg.source)}")
    out.append(f"named.conf : {_file_state(cfg.bind.named_conf)}")
    out.append(f"zone 정의  : {_file_state(cfg.bind.zones_conf)}")
    out.append(f"zone 디렉터리: {_file_state(cfg.bind.zone_dir)}")
    out.append(f"rndc 키    : {_file_state(cfg.bind.rndc_key)}")
    out.append(f"chroot     : {cfg.bind.chroot or '(없음)'}")
    if cfg.warnings:
        out.append("")
        for warning in cfg.warnings:
            out.append(f"  ! {warning}")

    out.append(_section("2. 레이아웃 자동 감지 (파일 위치 기준)"))
    profile = detect.detect()
    if profile is None:
        out.append("알려진 배포판 배치를 찾지 못했습니다.")
        out.append("  RedHat 계열 /etc/named.conf · Debian 계열 /etc/bind/named.conf 중 어느 것도 없습니다.")
    else:
        out.append(f"계열       : {profile.family}")
        out.append(f"named.conf : {profile.named_conf}")
        out.append(f"zone 정의  : {profile.zones_conf} (include 되어 있음: {profile.zones_conf_included})")
        out.append(f"zone 디렉터리: {profile.zone_dir}")
        if Path(cfg.bind.named_conf) != profile.named_conf:
            out.append("")
            out.append(f"  ! 설정({cfg.bind.named_conf})과 감지({profile.named_conf})가 다릅니다.")
            out.append("    앱은 설정값을 씁니다. 의도한 것이 아니면 Settings 에서 고치세요.")

    out.append(_section("3. named.conf 가 include 하는 파일"))
    includes = detect.includes_of(Path(cfg.bind.named_conf), root=cfg.bind.chroot)
    if not includes:
        out.append("(없음 — named.conf 를 읽지 못했거나 include 가 없습니다)")
    for include in includes:
        out.append(f"  {_file_state(include)}")

    out.append(_section("4. BIND 가 보는 설정 (named-checkconf -p)"))
    try:
        from .core import layout as layout_mod

        found = layout_mod.discover(cfg.bind)
        result = found.checkconf
        out.append(f"종료코드   : {result.returncode}")
        if result.stderr.strip():
            out.append("오류 출력  :")
            for line in result.stderr.strip().splitlines()[:10]:
                out.append(f"  {line}")
        out.append(f"zone 디렉터리(options.directory): {found.directory}")

        out.append(_section("5. zone 목록과 그 정의 위치"))
        if not found.zones:
            out.append("(zone 이 없습니다)")
        for entry in found.zones:
            out.append(f"  {entry.name}  [{entry.zone_type}{'/' + entry.view if entry.view else ''}]")
            source = entry.source_file
            out.append(f"      정의 위치: {source if source else '← 설정 파일에서 찾지 못함'}")
            if entry.file is not None:
                state = fileaudit.stat_of(entry.file)
                out.append(
                    f"      zone 파일 : {entry.file}"
                    + ("" if state["exists"] else "  ← 없음")
                )
                out.extend(_zone_truth(cfg, entry))
            if source is None:
                out.append(
                    "      ! named-checkconf 는 이 zone 을 보는데 설정 파일에서는 찾지 못했습니다."
                )
                out.append(
                    "        앱이 읽는 named.conf 와 BIND 가 쓰는 named.conf 가 다를 수 있습니다."
                )
    except Exception as exc:  # noqa: BLE001 - 진단은 끝까지 출력해야 쓸모가 있다
        out.append(f"설정을 읽지 못했습니다: {type(exc).__name__}: {exc}")

    out.append(_section("6. BIND 도구"))
    for tool in settings.report(cfg).tools:
        out.append(f"  {tool.label:<28} {tool.value:<18} {tool.found or '← 찾지 못함'}")

    out.append(_section("7. 점검 요약"))
    check = settings.report(cfg)
    out.append(f"사용 준비  : {'예' if check.ready else '아니오'}")
    for problem in check.problems:
        out.append(f"  ! {problem}")

    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    print(report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
