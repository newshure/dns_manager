# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""쓰기 API 요청·응답 스키마."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.apply import ApplyResult
from ..core.editor import EditOutcome
from ..core.history import ChangeRecord


class RecordIn(BaseModel):
    name: str = Field(default="@", description="origin 기준 상대 이름. apex 는 @")
    type: str
    data: str = Field(description="rdata 텍스트 (예: MX 는 '10 mail.example.local.')")
    ttl: int | None = Field(default=None, ge=0, le=2147483647)
    # Windows DNS Manager 의 New Host 대화상자 체크박스에 대응
    create_ptr: bool = False
    expected_version: str | None = Field(default=None, description="낙관적 동시성 토큰")


class RecordDeleteIn(BaseModel):
    ids: list[str] = Field(min_length=1)
    delete_ptr: bool = Field(default=False, description="짝이 되는 PTR 도 삭제 (Windows 동작)")
    expected_version: str | None = None


class RawSaveIn(BaseModel):
    text: str
    expected_version: str | None = None
    bump_serial: bool = True


class ApplyResultOut(BaseModel):
    ok: bool
    target: str
    status: str
    summary: str = ""
    serial_before: int | None = None
    serial_after: int | None = None
    diff: str = ""
    check_output: str = ""
    reload_output: str = ""
    error: str | None = None
    change_id: int | None = None
    backup: str | None = None

    @classmethod
    def of(cls, result: ApplyResult) -> "ApplyResultOut":
        return cls(
            ok=result.ok,
            target=result.target,
            status=result.status,
            summary=result.summary,
            serial_before=result.serial_before,
            serial_after=result.serial_after,
            diff=result.diff,
            check_output=result.check_output,
            reload_output=(result.reload.message if result.reload else ""),
            error=result.error,
            change_id=result.change_id,
            backup=str(result.backup) if result.backup else None,
        )


class EditOutcomeOut(BaseModel):
    ok: bool
    result: ApplyResultOut
    related: list[ApplyResultOut] = Field(default_factory=list, description="PTR 등 부수 변경")
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, outcome: EditOutcome) -> "EditOutcomeOut":
        return cls(
            ok=outcome.ok,
            result=ApplyResultOut.of(outcome.primary),
            related=[ApplyResultOut.of(r) for r in outcome.related],
            notes=outcome.notes,
        )


class ChangeOut(BaseModel):
    id: int
    ts: str
    kind: str
    target: str
    view: str | None = None
    author: str | None = None
    summary: str
    serial_before: int | None = None
    serial_after: int | None = None
    status: str
    detail: str = ""
    has_backup: bool = False
    diff: str = ""

    @classmethod
    def of(cls, change: ChangeRecord, *, with_diff: bool = False) -> "ChangeOut":
        return cls(
            id=change.id,
            ts=change.ts,
            kind=change.kind,
            target=change.target,
            view=change.view,
            author=change.author,
            summary=change.summary,
            serial_before=change.serial_before,
            serial_after=change.serial_after,
            status=change.status,
            detail=change.detail,
            has_backup=bool(change.backup),
            diff=change.diff if with_diff else "",
        )


class ZoneSpecIn(BaseModel):
    """New Zone 마법사의 입력 (Windows DNS Manager 의 단계 순서를 그대로 받는다)."""

    name: str = Field(default="", description="정방향 zone 이름. 역방향이면 network_id 로 대체")
    type: str = Field(default="master", description="master | slave | stub | forward")
    reverse: bool = False
    network_id: str | None = Field(default=None, description="정방향 순서 (예: 192.168.10 또는 192.168.10.0/24)")
    family: int = Field(default=4, description="4 | 6")
    file_name: str | None = Field(default=None, description="비우면 규칙대로 .zone/.rev")
    masters: list[str] = Field(default_factory=list)
    forwarders: list[str] = Field(default_factory=list)
    forward_policy: str | None = Field(default=None, description="only | first")
    allow_update: list[str] = Field(default_factory=lambda: ["none"])
    allow_transfer: list[str] = Field(default_factory=lambda: ["none"])
    ttl: int = Field(default=3600, ge=0)
    primary_ns: str | None = None
    responsible: str = "hostmaster"
    name_servers: list[str] = Field(default_factory=list)
    glue: dict[str, str] = Field(default_factory=dict, description="{이름: 주소} — NS 의 glue 레코드")


class ZonePreviewOut(BaseModel):
    zone: str
    conf_block: str = Field(description="named.conf 에 추가될 블록 전문")
    zone_file: str | None = Field(default=None, description="만들어질 zone 파일 전문")
    file_path: str | None = None
    valid: bool = True
    check_output: str = ""


class ZoneCreateOut(BaseModel):
    ok: bool
    preview: ZonePreviewOut
    result: ApplyResultOut


class ForwarderIn(BaseModel):
    forwarders: list[str] = Field(default_factory=list, description="비우면 해제(전역만 해당)")
    policy: str | None = Field(default=None, description="only | first")


class PathStatusOut(BaseModel):
    key: str
    label: str
    description: str = ""
    value: str | None = None
    exists: bool = False
    readable: bool = False
    writable: bool = False
    required: bool = True
    ok: bool = False
    note: str = ""


class ToolStatusOut(BaseModel):
    key: str
    label: str
    value: str
    found: str | None = None
    required: bool = True
    ok: bool = False


class SettingsOut(BaseModel):
    ready: bool
    problems: list[str] = Field(default_factory=list)
    paths: list[PathStatusOut] = Field(default_factory=list)
    tools: list[ToolStatusOut] = Field(default_factory=list)
    detected_family: str | None = None
    detected: dict[str, str] = Field(default_factory=dict)
    config_path: str | None = None
    config_writable: bool = False
    zone_suffix: str = ".zone"
    reverse_suffix: str = ".rev"


class SettingsIn(BaseModel):
    """자동 감지로 맞지 않는 배치에서 직접 입력하는 경로들."""

    named_conf: str | None = None
    zones_conf: str | None = None
    zone_dir: str | None = None
    rndc_key: str | None = None
    chroot: str | None = None
    zone_suffix: str | None = None
    reverse_suffix: str | None = None
    named_checkzone: str | None = None
    named_checkconf: str | None = None
    rndc: str | None = None
    dig: str | None = None


class SoaIn(BaseModel):
    """SOA 편집. serial 은 받지 않는다 — 트랜잭션이 자동으로 증가시킨다."""

    primary: str | None = Field(default=None, description="Primary server (mname)")
    responsible: str | None = Field(default=None, description="Responsible person (rname)")
    refresh: int | None = Field(default=None, ge=0)
    retry: int | None = Field(default=None, ge=0)
    expire: int | None = Field(default=None, ge=0)
    minimum: int | None = Field(default=None, ge=0)


class ZoneOptionsIn(BaseModel):
    """zone Properties 의 General / Zone Transfers 항목."""

    allow_update: list[str] | None = None
    allow_transfer: list[str] | None = None
    allow_query: list[str] | None = None
    also_notify: list[str] | None = None
    masters: list[str] | None = None


class DelegationServerIn(BaseModel):
    name: str = Field(description="위임받을 네임서버 FQDN")
    address: str = Field(default="", description="위임 구간 안쪽 이름일 때 필요한 glue 주소")


class DelegationIn(BaseModel):
    """New Delegation — 하위 도메인 위임."""

    name: str = Field(description="하위 도메인 이름 (상위 zone 기준 상대 이름)")
    servers: list[DelegationServerIn] = Field(min_length=1)
    ttl: int | None = Field(default=None, ge=0)


class FileRowOut(BaseModel):
    path: str
    role: str
    role_label: str
    note: str = ""
    secret: bool = Field(default=False, description="비밀값이 들어 있어 내용을 보여주지 않는다")
    from_history: bool = Field(
        default=False,
        description="현재 대상은 아니고 접근 기록에만 남은 경로(삭제된 zone 파일, 백업 등)",
    )
    exists: bool = False
    size: int | None = None
    mtime: str | None = None
    readable: bool = False
    writable: bool = False
    reads: int = 0
    writes: int = 0
    last_at: str | None = None
    last_action: str | None = None


class FileEventOut(BaseModel):
    at: str
    path: str
    action: str
    role: str
    role_label: str
    detail: str = ""
    ok: bool = True


class FilesOut(BaseModel):
    files: list[FileRowOut] = Field(default_factory=list)
    events: list[FileEventOut] = Field(default_factory=list)
    touched: int = Field(default=0, description="실제로 읽거나 쓴 파일 수")


class ServerAccessIn(BaseModel):
    """누가 이 서버에 질의할 수 있는가 (options 의 수신·접근 설정).

    빈 목록은 "그 설정을 지움" = BIND 기본값(any)으로 되돌림을 뜻한다.
    """

    listen_on: list[str] | None = Field(default=None, description='예: ["any"] 또는 ["127.0.0.1", "192.168.2.1"]')
    listen_on_v6: list[str] | None = None
    allow_query: list[str] | None = Field(default=None, description='예: ["any"] 또는 ["localhost", "10.0.0.0/8"]')
    allow_recursion: list[str] | None = None
    recursion: str | None = Field(default=None, description="yes | no")
