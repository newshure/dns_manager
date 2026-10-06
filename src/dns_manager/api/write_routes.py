# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""쓰기 API — 모든 요청은 core.apply 트랜잭션을 지난다.

실패는 HTTP 오류가 아니라 결과 객체로 돌려주는 경우가 많다. named-checkzone 의
원문 출력과 diff 를 UI 가 그대로 보여줘야 하기 때문이다(500 으로 뭉개면 정보가 사라진다).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from pathlib import Path

from ..config import load_config
from ..core import apply as apply_mod
from ..core import editor, fileaudit, service, settings as settings_mod, zoneadmin
from ..core.apply import ApplyError, VersionConflict
from ..core.commands import CommandError
from ..core.history import History
from ..core.locking import LockBusy
from ..core.service import ZoneNotFound
from ..core.zoneedit import RecordNotFound, ZoneEditError
from ..core.zonetemplate import TemplateError
from .write_schemas import (
    ChangeOut,
    DelegationIn,
    SoaIn,
    ZoneOptionsIn,
    ServerAccessIn,
    SettingsIn,
    SettingsOut,
    EditOutcomeOut,
    ForwarderIn,
    RawSaveIn,
    RecordDeleteIn,
    RecordIn,
    ZoneCreateOut,
    ZonePreviewOut,
    ZoneSpecIn,
)
from .write_schemas import FileEventOut, FileRowOut, FilesOut, PathStatusOut, ToolStatusOut

router = APIRouter(prefix="/api")


def _cfg(request: Request):
    return request.app.state.config


def _author(request: Request) -> str | None:
    """앞단 프록시가 사용자명을 넘기면 감사 로그 주체로 쓴다."""
    header = _cfg(request).app.user_header
    return request.headers.get(header) or None


def _guard(fn):
    """도메인 예외를 적절한 HTTP 상태로 옮긴다."""
    try:
        return fn()
    except ZoneNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RecordNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LockBusy as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    except (ZoneEditError, ApplyError, TemplateError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CommandError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _settings_out(report: settings_mod.SettingsReport) -> SettingsOut:
    return SettingsOut(
        ready=report.ready,
        problems=report.problems,
        paths=[PathStatusOut(**vars(p)) for p in report.paths],
        tools=[ToolStatusOut(**vars(t)) for t in report.tools],
        detected_family=report.detected_family,
        detected=report.detected,
        config_path=report.config_path,
        config_writable=report.config_writable,
        zone_suffix=report.zone_suffix,
        reverse_suffix=report.reverse_suffix,
    )


@router.get("/files", response_model=FilesOut)
def list_files(
    request: Request,
    limit: int = Query(default=100, ge=1, le=300, description="접근 기록 개수"),
) -> FilesOut:
    """이 앱이 다루는 파일과 실제 접근 기록.

    "어떤 파일을 읽고 쓰는가" 는 권한 문제나 설정이 안 먹는 상황에서 가장 먼저 보는 정보다.
    키 파일은 경로와 역할만 보여주고 내용은 담지 않는다.
    """
    cfg = _cfg(request)
    summary = {row["path"]: row for row in fileaudit.summary()}

    rows: list[FileRowOut] = []
    for item in fileaudit.inventory(cfg):
        touched = summary.pop(item["path"], None)
        rows.append(
            FileRowOut(
                **{k: v for k, v in item.items() if k in FileRowOut.model_fields},
                **(
                    {
                        "reads": int(touched["reads"]),
                        "writes": int(touched["writes"]),
                        "last_at": str(touched["last_at"]),
                        "last_action": str(touched["last_action"]),
                    }
                    if touched
                    else {}
                ),
            )
        )

    # 목록에 없던 경로도 접근했다면 보여준다(백업 파일 등).
    for leftover in summary.values():
        stat = fileaudit.stat_of(str(leftover["path"]))
        rows.append(
            FileRowOut(
                path=str(leftover["path"]),
                role=str(leftover["role"]),
                role_label=str(leftover["role_label"]),
                note="지난 접근 기록 (현재 관리 대상 아님)",
                from_history=True,
                reads=int(leftover["reads"]),
                writes=int(leftover["writes"]),
                last_at=str(leftover["last_at"]),
                last_action=str(leftover["last_action"]),
                **{k: v for k, v in stat.items() if k in FileRowOut.model_fields},
            )
        )

    events = [
        FileEventOut(
            at=e.at, path=e.path, action=e.action, role=e.role,
            role_label=e.role_label, detail=e.detail, ok=e.ok,
        )
        for e in fileaudit.recent(limit)
    ]
    return FilesOut(files=rows, events=events, touched=sum(1 for r in rows if r.reads or r.writes))


@router.get("/settings", response_model=SettingsOut)
def get_settings(request: Request) -> SettingsOut:
    """현재 경로 설정과 점검 결과. 자동 감지가 맞지 않으면 여기서 직접 입력한다."""
    return _settings_out(settings_mod.report(_cfg(request)))


@router.put("/settings", response_model=SettingsOut)
def update_settings(request: Request, body: SettingsIn) -> SettingsOut:
    """경로를 저장하고 즉시 반영한다.

    저장 후 설정을 다시 읽어 앱 상태를 교체하므로, 재시작 없이 새 경로로 동작한다.
    """
    cfg = _cfg(request)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return _settings_out(settings_mod.report(cfg))

    try:
        target = settings_mod.save(cfg, updates)
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail=f"설정 파일에 쓸 수 없습니다: {exc}. 실행 계정의 권한을 확인하세요.",
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"설정을 저장할 수 없습니다: {exc}") from exc

    # 저장한 파일을 그대로 다시 읽어 반영한다(앱이 보는 값과 파일 내용을 일치시킨다).
    new_cfg = load_config(Path(target))
    request.app.state.config = new_cfg
    return _settings_out(settings_mod.report(new_cfg))


def _spec(body: ZoneSpecIn) -> zoneadmin.ZoneSpec:
    return zoneadmin.ZoneSpec(
        name=body.name,
        zone_type=body.type,
        reverse=body.reverse,
        network_id=body.network_id,
        family=body.family,
        file_name=body.file_name,
        masters=body.masters,
        forwarders=body.forwarders,
        forward_policy=body.forward_policy,
        allow_update=body.allow_update,
        allow_transfer=body.allow_transfer,
        ttl=body.ttl,
        primary_ns=body.primary_ns,
        responsible=body.responsible,
        name_servers=body.name_servers,
        glue=body.glue,
    )


def _preview_out(view: zoneadmin.ZonePreview) -> ZonePreviewOut:
    return ZonePreviewOut(
        zone=view.zone,
        conf_block=view.conf_block,
        zone_file=view.zone_file,
        file_path=view.file_path,
        valid=view.valid,
        check_output=view.check_output,
    )


@router.post("/zones/preview", response_model=ZonePreviewOut)
def preview_zone(request: Request, body: ZoneSpecIn) -> ZonePreviewOut:
    """New Zone 마법사의 Summary 단계 — 기록하지 않고 결과물만 만들어 보여준다."""
    cfg = _cfg(request)
    return _preview_out(_guard(lambda: zoneadmin.preview(cfg, _spec(body))))


@router.post("/zones", response_model=ZoneCreateOut, status_code=201)
def create_zone(request: Request, body: ZoneSpecIn) -> ZoneCreateOut:
    cfg = _cfg(request)
    view, result = _guard(lambda: zoneadmin.create_zone(cfg, _spec(body), author=_author(request)))
    from .write_schemas import ApplyResultOut

    return ZoneCreateOut(ok=result.ok, preview=_preview_out(view), result=ApplyResultOut.of(result))


@router.delete("/zones/{zone}", response_model=EditOutcomeOut)
def delete_zone(
    request: Request,
    zone: str,
    delete_file: bool = Query(default=False, description="zone 파일도 백업 디렉터리로 옮긴다"),
) -> EditOutcomeOut:
    cfg = _cfg(request)
    result = _guard(
        lambda: zoneadmin.delete_zone(cfg, zone, delete_file=delete_file, author=_author(request))
    )
    return EditOutcomeOut.of(editor.EditOutcome(primary=result))


@router.put("/server/access", response_model=EditOutcomeOut)
def set_server_access(request: Request, body: ServerAccessIn) -> EditOutcomeOut:
    """수신 주소·질의 허용 범위 — "외부에서 응답이 없다" 의 가장 흔한 원인."""
    cfg = _cfg(request)
    result = _guard(
        lambda: zoneadmin.set_server_access(
            cfg,
            listen_on=body.listen_on,
            listen_on_v6=body.listen_on_v6,
            allow_query=body.allow_query,
            allow_recursion=body.allow_recursion,
            recursion=body.recursion,
            author=_author(request),
        )
    )
    return EditOutcomeOut.of(editor.EditOutcome(primary=result))


@router.put("/forwarders/server", response_model=EditOutcomeOut)
def set_server_forwarders(request: Request, body: ForwarderIn) -> EditOutcomeOut:
    """전역(기본) 전달자 — named.conf 의 options 블록을 고친다."""
    cfg = _cfg(request)
    result = _guard(
        lambda: zoneadmin.set_server_forwarders(
            cfg, body.forwarders, body.policy, author=_author(request)
        )
    )
    return EditOutcomeOut.of(editor.EditOutcome(primary=result))


@router.put("/forwarders/{domain}", response_model=EditOutcomeOut)
def set_conditional_forwarder(request: Request, domain: str, body: ForwarderIn) -> EditOutcomeOut:
    """도메인별(조건부) 전달자 — `type forward` zone 으로 저장된다."""
    cfg = _cfg(request)
    result = _guard(
        lambda: zoneadmin.set_conditional_forwarder(
            cfg, domain, body.forwarders, body.policy or "only", author=_author(request)
        )
    )
    return EditOutcomeOut.of(editor.EditOutcome(primary=result))


@router.delete("/forwarders/{domain}", response_model=EditOutcomeOut)
def delete_conditional_forwarder(request: Request, domain: str) -> EditOutcomeOut:
    cfg = _cfg(request)
    result = _guard(lambda: zoneadmin.delete_conditional_forwarder(cfg, domain, author=_author(request)))
    return EditOutcomeOut.of(editor.EditOutcome(primary=result))


@router.get("/zones/{zone}/soa")
def get_soa(request: Request, zone: str, view: str | None = Query(default=None)) -> dict[str, str]:
    """Properties ▸ Start of Authority (SOA)."""
    cfg = _cfg(request)
    return _guard(lambda: editor.read_soa(cfg, zone, view))


@router.put("/zones/{zone}/soa", response_model=EditOutcomeOut)
def update_soa(
    request: Request, zone: str, body: SoaIn, view: str | None = Query(default=None)
) -> EditOutcomeOut:
    cfg = _cfg(request)
    outcome = _guard(
        lambda: editor.update_soa(
            cfg, zone, body.model_dump(exclude_none=True), view=view, author=_author(request)
        )
    )
    return EditOutcomeOut.of(outcome)


@router.put("/zones/{zone}/options", response_model=EditOutcomeOut)
def update_zone_options(request: Request, zone: str, body: ZoneOptionsIn) -> EditOutcomeOut:
    """Properties ▸ General / Zone Transfers — named.conf 의 zone 블록을 고친다."""
    cfg = _cfg(request)
    result = _guard(
        lambda: zoneadmin.update_zone_options(
            cfg,
            zone,
            allow_update=body.allow_update,
            allow_transfer=body.allow_transfer,
            allow_query=body.allow_query,
            also_notify=body.also_notify,
            masters=body.masters,
            author=_author(request),
        )
    )
    return EditOutcomeOut.of(editor.EditOutcome(primary=result))


@router.post("/zones/{zone}/delegations", response_model=EditOutcomeOut, status_code=201)
def add_delegation(
    request: Request, zone: str, body: DelegationIn, view: str | None = Query(default=None)
) -> EditOutcomeOut:
    """New Delegation — 하위 도메인을 다른 네임서버로 위임한다."""
    cfg = _cfg(request)
    outcome = _guard(
        lambda: editor.add_delegation(
            cfg,
            zone,
            body.name,
            [s.model_dump() for s in body.servers],
            ttl=body.ttl,
            view=view,
            author=_author(request),
        )
    )
    return EditOutcomeOut.of(outcome)


@router.post("/zones/{zone}/records", response_model=EditOutcomeOut, status_code=201)
def create_record(
    request: Request, zone: str, body: RecordIn, view: str | None = Query(default=None)
) -> EditOutcomeOut:
    cfg = _cfg(request)
    outcome = _guard(
        lambda: editor.add_record(
            cfg,
            zone,
            name=body.name,
            rtype=body.type,
            data=body.data,
            ttl=body.ttl,
            view=view,
            create_ptr=body.create_ptr,
            author=_author(request),
            expected_version=body.expected_version,
        )
    )
    return EditOutcomeOut.of(outcome)


@router.put("/zones/{zone}/records/{record_id}", response_model=EditOutcomeOut)
def update_record(
    request: Request, zone: str, record_id: str, body: RecordIn, view: str | None = Query(default=None)
) -> EditOutcomeOut:
    cfg = _cfg(request)
    outcome = _guard(
        lambda: editor.update_record(
            cfg,
            zone,
            record_id,
            name=body.name,
            rtype=body.type,
            data=body.data,
            ttl=body.ttl,
            view=view,
            author=_author(request),
            expected_version=body.expected_version,
        )
    )
    return EditOutcomeOut.of(outcome)


@router.post("/zones/{zone}/records/delete", response_model=EditOutcomeOut)
def delete_records(
    request: Request, zone: str, body: RecordDeleteIn, view: str | None = Query(default=None)
) -> EditOutcomeOut:
    """여러 건을 한 트랜잭션으로 지운다(그리드 다중 선택 삭제)."""
    cfg = _cfg(request)
    outcome = _guard(
        lambda: editor.delete_records(
            cfg,
            zone,
            body.ids,
            view=view,
            delete_ptr=body.delete_ptr,
            author=_author(request),
            expected_version=body.expected_version,
        )
    )
    return EditOutcomeOut.of(outcome)


@router.put("/zones/{zone}/raw", response_model=EditOutcomeOut)
def save_raw(
    request: Request, zone: str, body: RawSaveIn, view: str | None = Query(default=None)
) -> EditOutcomeOut:
    cfg = _cfg(request)
    outcome = _guard(
        lambda: editor.save_raw(
            cfg,
            zone,
            body.text,
            view=view,
            expected_version=body.expected_version,
            author=_author(request),
            bump_serial=body.bump_serial,
        )
    )
    return EditOutcomeOut.of(outcome)


@router.post("/zones/{zone}/reload")
def reload_zone(request: Request, zone: str, view: str | None = Query(default=None)) -> dict[str, object]:
    """Windows DNS Manager 의 zone ▸ Reload 에 대응."""
    cfg = _cfg(request)
    entry = _guard(lambda: service.find_zone(cfg, zone, view))
    result = apply_mod.reload_zone(cfg, entry.name)
    status = service.get_zone(cfg, zone, view).status
    return {
        "ok": result.ok,
        "output": result.message or result.stdout,
        "loaded_serial": status.serial,
    }


@router.get("/history", response_model=list[ChangeOut])
def list_history(
    request: Request,
    target: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[ChangeOut]:
    history = History(_cfg(request).app.state_dir / "history.sqlite3")
    return [ChangeOut.of(change) for change in history.list(target=target, limit=limit)]


@router.get("/history/{change_id}", response_model=ChangeOut)
def get_change(request: Request, change_id: int) -> ChangeOut:
    history = History(_cfg(request).app.state_dir / "history.sqlite3")
    change = history.get(change_id)
    if change is None:
        raise HTTPException(status_code=404, detail=f"이력을 찾을 수 없습니다: {change_id}")
    return ChangeOut.of(change, with_diff=True)


@router.post("/history/{change_id}/rollback", response_model=EditOutcomeOut)
def rollback_change(request: Request, change_id: int) -> EditOutcomeOut:
    """해당 변경 직전 내용으로 되돌린다. 되돌리기도 같은 트랜잭션을 거친다."""
    cfg = _cfg(request)
    history = History(cfg.app.state_dir / "history.sqlite3")
    change = history.get(change_id)
    if change is None:
        raise HTTPException(status_code=404, detail=f"이력을 찾을 수 없습니다: {change_id}")
    if change.kind != "zone":
        raise HTTPException(status_code=400, detail="zone 변경만 롤백할 수 있습니다.")

    entry = _guard(lambda: service.find_zone(cfg, change.target, change.view))
    result = _guard(lambda: apply_mod.rollback(cfg, entry, change_id, author=_author(request)))
    return EditOutcomeOut.of(editor.EditOutcome(primary=result))
