# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""읽기 전용 API 라우트 (P0).

쓰기 경로는 P1 의 트랜잭션 엔진이 들어온 뒤 추가한다. 지금 단계에서 파일을 쓰는
엔드포인트는 의도적으로 존재하지 않는다.
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ..core import fileaudit, naming, service
from ..core.commands import CommandError
from ..core.records import OTHER_TYPES, PRIMARY_TYPES
from ..core.service import ZoneNotFound
from .schemas import ForwarderOut, QueryOut, RawZoneOut, ServerStatusOut, ZoneDetailOut, ZoneSummaryOut

router = APIRouter(prefix="/api")


def _cfg(request: Request):
    return request.app.state.config


@router.get("/status", response_model=ServerStatusOut)
def get_status(request: Request) -> ServerStatusOut:
    cfg = _cfg(request)
    status = service.server_status(cfg)
    directory: str | None = None
    server_forwarders: list[str] = []
    server_forward_policy: str | None = None
    access = None
    checkconf_ok = True
    checkconf_output = ""
    parsed_config = ""
    zone_count = 0
    options_path: str | None = None
    try:
        from ..core import zoneadmin

        options_path = str(zoneadmin.options_file(cfg))
    except Exception:  # noqa: BLE001 - 상태 표시용이므로 실패해도 나머지는 보여준다
        options_path = None
    try:
        layout = service.get_layout(cfg)
        directory = str(layout.directory)
        checkconf_ok = layout.checkconf.ok
        # -p 는 정상일 때 정규화된 설정 전문을 stdout 으로 쏟아낸다.
        # 검증 결과 칸에는 오류만 담고, 전문은 따로 보여준다.
        checkconf_output = layout.checkconf.stderr.strip()
        parsed_config = layout.checkconf.stdout
        zone_count = sum(1 for z in layout.zones if not z.is_builtin)
        server_forwarders = list(layout.forwarding.forwarders)
        server_forward_policy = layout.forwarding.policy
        access = layout.access
    except CommandError as exc:
        checkconf_ok = False
        checkconf_output = str(exc)

    return ServerStatusOut(
        running=status.running,
        version=status.version,
        raw=status.raw,
        named_conf=str(cfg.bind.named_conf),
        zones_conf=str(cfg.bind.zones_conf),
        directory=directory,
        checkconf_ok=checkconf_ok,
        checkconf_output=checkconf_output,
        zone_count=zone_count,
        detected_family=cfg.detected_family,
        config_warnings=list(cfg.warnings),
        options_file=options_path,
        parsed_config=parsed_config,
        listen_on=list(access.listen_on) if access else [],
        listen_on_v6=list(access.listen_on_v6) if access else [],
        allow_query=list(access.allow_query) if access else [],
        allow_recursion=list(access.allow_recursion) if access else [],
        recursion=access.recursion if access else None,
        external_blockers=list(access.external_blockers) if access else [],
        server_forwarders=server_forwarders,
        server_forward_policy=server_forward_policy,
        advanced_view_default=cfg.app.advanced_view_default,
        shutdown_after_idle=cfg.app.shutdown_after_idle,
        idle_remaining=(
            int(getattr(request.app.state, "idle").remaining)
            if getattr(request.app.state, "idle", None) is not None
            and request.app.state.idle.timeout > 0
            else None
        ),
        config_source=str(cfg.source) if cfg.source else None,
    )


def _notice_candidates() -> list[Path]:
    """NOTICE.md 가 있을 만한 자리.

    설치 형태마다 다르다: 소스 트리에서 실행, /opt/dns-manager 에 설치(venv 는 그 아래),
    또는 wheel 로만 설치(배포 메타데이터의 licenses/ 에 들어간다 — pyproject 의
    license-files 로 동봉하고 있다).
    """
    here = Path(__file__).resolve()
    candidates = [parent / "NOTICE.md" for parent in here.parents[2:5]]
    candidates.append(Path(sys.prefix).parent / "NOTICE.md")  # /opt/dns-manager/.venv → /opt/dns-manager
    candidates.append(Path.cwd() / "NOTICE.md")
    try:
        files = importlib.metadata.files("dns-manager") or []
        candidates.extend(
            Path(str(f.locate())) for f in files if f.name == "NOTICE.md"
        )
    except importlib.metadata.PackageNotFoundError:
        pass
    return candidates


@router.get("/notice", response_class=PlainTextResponse)
def get_notice() -> str:
    """제3자 오픈소스 고지. MIT/BSD/ISC 는 저작권 고지 보존을 의무로 요구한다."""
    for candidate in _notice_candidates():
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8")
                fileaudit.record(candidate, "read", "notice")
                return text
        except OSError:
            continue
    return (
        "NOTICE.md 를 찾을 수 없습니다.\n"
        "원본: https://github.com/newshure/dns_manager/blob/development_mc/NOTICE.md\n"
    )


@router.get("/naming")
def get_naming(request: Request) -> dict[str, str]:
    """New Zone 마법사의 기본 파일명 규칙 — 정방향 .zone / 역방향 .rev."""
    cfg = _cfg(request)
    return {"zone_suffix": cfg.bind.zone_suffix, "reverse_suffix": cfg.bind.reverse_suffix}


@router.get("/record-types")
def get_record_types() -> dict[str, list[str]]:
    """New Record 메뉴 / Other New Records… 목록 구성용."""
    return {"primary": list(PRIMARY_TYPES), "other": list(OTHER_TYPES)}


@router.get("/forwarders", response_model=list[ForwarderOut])
def list_forwarders(request: Request) -> list[ForwarderOut]:
    """전달자 목록 — 전역(options forwarders) + 조건부(type forward zone)."""
    cfg = _cfg(request)
    try:
        return [ForwarderOut.of(f) for f in service.list_forwarders(cfg)]
    except CommandError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/zones", response_model=list[ZoneSummaryOut])
def list_zones(
    request: Request,
    include_builtin: bool = Query(default=False),
    with_status: bool = Query(default=True, description="rndc zonestatus 조회 포함 여부"),
) -> list[ZoneSummaryOut]:
    cfg = _cfg(request)
    try:
        summaries = service.list_zones(cfg, include_builtin=include_builtin, with_status=with_status)
    except CommandError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    out: list[ZoneSummaryOut] = []
    for summary in summaries:
        item = ZoneSummaryOut.of(summary)
        if summary.zone_type == "master":
            try:
                item.expected_file_name = naming.default_file_name(cfg.bind, summary.name)
            except ValueError:
                item.expected_file_name = None
        out.append(item)
    return out


@router.get("/zones/{zone}", response_model=ZoneDetailOut)
def get_zone(
    request: Request,
    zone: str,
    view: str | None = Query(default=None),
    advanced: bool = Query(default=False, description="View ▸ Advanced 에 해당"),
    type: str | None = Query(default=None, description="레코드 타입 필터"),
) -> ZoneDetailOut:
    cfg = _cfg(request)
    try:
        detail = service.get_zone(cfg, zone, view)
    except ZoneNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CommandError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    shown = service.filter_records(detail.records, advanced=advanced, rtype=type.upper() if type else None)
    hidden = len(detail.records) - len(shown) if not type else 0
    return ZoneDetailOut.of(detail, shown, max(hidden, 0))


@router.get("/zones/{zone}/raw", response_model=RawZoneOut)
def get_zone_raw(request: Request, zone: str, view: str | None = Query(default=None)) -> RawZoneOut:
    cfg = _cfg(request)
    try:
        detail = service.get_zone(cfg, zone, view)
    except ZoneNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail.content is None:
        raise HTTPException(status_code=409, detail=detail.problem or "zone 파일이 없습니다.")
    content = detail.content
    return RawZoneOut(
        name=detail.entry.name,
        file=str(content.snapshot.path),
        version=content.snapshot.version,
        text=content.snapshot.text,
        unsupported_directives=list(content.unsupported),
        parse_error=content.parse_error,
    )


@router.get("/query", response_model=QueryOut)
def dns_query(
    request: Request,
    name: str = Query(min_length=1),
    type: str = Query(default="A"),
    server: str = Query(default="127.0.0.1"),
) -> QueryOut:
    """Windows DNS Manager 의 'Launch nslookup' 대응 — 실제 응답 확인용."""
    cfg = _cfg(request)
    from ..core import server as server_mod

    try:
        result = server_mod.query(cfg.bind, name, type.upper(), server)
    except CommandError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return QueryOut(argv=list(result.argv), returncode=result.returncode, output=result.stdout or result.stderr)
