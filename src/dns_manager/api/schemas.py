# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""API 응답 스키마 (읽기 전용 단계)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.records import APEX_LABEL
from ..core.service import ZoneDetail, ZoneSummary
from ..core.records import Record


class RecordOut(BaseModel):
    id: str
    name: str
    display_name: str = Field(description="Windows DNS Manager 표기: apex 는 (same as parent folder)")
    fqdn: str
    type: str
    rdclass: str
    ttl: int
    data: str
    fields: dict[str, str] = Field(default_factory=dict)
    advanced_only: bool = False

    @classmethod
    def of(cls, record: Record) -> "RecordOut":
        return cls(
            id=record.id,
            name=record.name,
            display_name=APEX_LABEL if record.is_apex else record.name,
            fqdn=record.fqdn,
            type=record.rtype,
            rdclass=record.rdclass,
            ttl=record.ttl,
            data=record.data,
            fields=record.fields,
            advanced_only=record.advanced_only,
        )


class ZoneSummaryOut(BaseModel):
    name: str
    view: str | None = None
    type: str
    category: str = Field(description="forward | reverse | conditional_forwarder | builtin")
    file: str | None = None
    editable: bool
    signed: bool
    dynamic: bool
    file_serial: int | None = None
    loaded_serial: int | None = None
    out_of_sync: bool = False
    file_changed_since_load: bool = Field(
        default=False, description="파일이 named 적재 이후에 바뀌었다 — reload 가 필요하다"
    )
    loaded_at: str | None = None
    record_count: int | None = None
    problem: str | None = None
    source_file: str | None = Field(default=None, description="이 zone 블록이 들어 있는 설정 파일")
    conventional_file: bool = Field(default=True, description="파일명이 .zone/.rev 규칙을 따르는지")
    expected_file_name: str | None = None

    @classmethod
    def of(cls, summary: ZoneSummary) -> "ZoneSummaryOut":
        return cls(
            name=summary.name,
            view=summary.view,
            type=summary.zone_type,
            category=summary.category,
            file=summary.file,
            editable=summary.editable,
            signed=summary.signed,
            dynamic=summary.dynamic,
            file_serial=summary.file_serial,
            loaded_serial=summary.loaded_serial,
            out_of_sync=summary.out_of_sync,
            file_changed_since_load=summary.file_changed_since_load,
            loaded_at=summary.loaded_at,
            record_count=summary.record_count,
            problem=summary.problem,
            source_file=summary.source_file,
            conventional_file=summary.conventional_file,
        )


class ZoneDetailOut(BaseModel):
    name: str
    view: str | None = None
    type: str
    category: str
    file: str | None = None
    source_file: str | None = Field(default=None, description="이 zone 블록이 들어 있는 설정 파일")
    editable: bool
    table_editable: bool
    signed: bool
    dynamic: bool
    version: str | None = Field(default=None, description="낙관적 동시성 제어 토큰 (mtime+해시)")
    file_serial: int | None = None
    loaded_serial: int | None = None
    out_of_sync: bool = False
    file_changed_since_load: bool = False
    loaded_at: str | None = None
    masters: list[str] = Field(default_factory=list)
    forwarders: list[str] = Field(default_factory=list)
    forward_policy: str | None = None
    allow_update: list[str] = Field(default_factory=list)
    allow_transfer: list[str] = Field(default_factory=list)
    unsupported_directives: list[str] = Field(default_factory=list)
    parse_error: str | None = None
    problem: str | None = None
    records: list[RecordOut] = Field(default_factory=list)
    hidden_record_count: int = 0

    @classmethod
    def of(cls, detail: ZoneDetail, records: list[Record], hidden: int) -> "ZoneDetailOut":
        content = detail.content
        file_serial = content.serial if content else None
        loaded = detail.status.serial
        return cls(
            name=detail.entry.name,
            view=detail.entry.view,
            type=detail.entry.zone_type,
            category=detail.entry.category,
            file=str(detail.entry.file) if detail.entry.file else None,
            source_file=str(detail.entry.source_file) if detail.entry.source_file else None,
            editable=detail.entry.editable,
            table_editable=bool(content and content.table_editable and detail.entry.editable),
            signed=detail.entry.is_signed,
            dynamic=detail.entry.dynamic,
            version=content.snapshot.version if content else None,
            file_serial=file_serial,
            loaded_serial=loaded,
            out_of_sync=bool(
                detail.file_changed_since_load
                or (file_serial is not None and loaded is not None and file_serial != loaded)
            ),
            file_changed_since_load=detail.file_changed_since_load,
            loaded_at=detail.status.loaded,
            masters=list(detail.entry.masters),
            forwarders=list(detail.entry.forwarders),
            forward_policy=detail.entry.forward_policy,
            allow_update=list(detail.entry.allow_update),
            allow_transfer=list(detail.entry.allow_transfer),
            unsupported_directives=list(content.unsupported) if content else [],
            parse_error=content.parse_error if content else None,
            problem=detail.problem,
            records=[RecordOut.of(r) for r in records],
            hidden_record_count=hidden,
        )


class RawZoneOut(BaseModel):
    name: str
    file: str
    version: str
    text: str
    unsupported_directives: list[str] = Field(default_factory=list)
    parse_error: str | None = None


class ServerStatusOut(BaseModel):
    running: bool
    version: str | None = None
    raw: str = ""
    named_conf: str
    zones_conf: str
    directory: str | None = None
    checkconf_ok: bool = True
    checkconf_output: str = Field(default="", description="named-checkconf 의 오류 출력(정상이면 빈 문자열)")
    parsed_config: str = Field(default="", description="named-checkconf -p 가 정규화한 설정 전문")
    zone_count: int = 0
    detected_family: str | None = Field(default=None, description="감지된 레이아웃: redhat | debian")
    config_warnings: list[str] = Field(
        default_factory=list, description="설정 경로가 사라졌거나 손봐야 하는 사항"
    )
    options_file: str | None = Field(default=None, description="options 블록이 들어 있는 파일")
    listen_on: list[str] = Field(default_factory=list)
    listen_on_v6: list[str] = Field(default_factory=list)
    allow_query: list[str] = Field(default_factory=list)
    allow_recursion: list[str] = Field(default_factory=list)
    recursion: str | None = None
    external_blockers: list[str] = Field(
        default_factory=list, description="외부 질의를 막고 있는 options 설정 이름"
    )
    server_forwarders: list[str] = Field(default_factory=list)
    server_forward_policy: str | None = None
    advanced_view_default: bool = False
    shutdown_after_idle: int = Field(default=0, description="유휴 자동 종료(초). 0 이면 쓰지 않음")
    idle_remaining: int | None = Field(default=None, description="자동 종료까지 남은 초")
    config_source: str | None = None


class ForwarderOut(BaseModel):
    scope: str = Field(description="server(전역) | conditional(도메인별)")
    domain: str | None = Field(default=None, description="조건부 전달자의 대상 도메인. 전역은 null")
    view: str | None = None
    forwarders: list[str] = Field(default_factory=list)
    policy: str | None = Field(default=None, description="only | first")
    configured: bool = Field(default=True, description="실제로 설정되어 있는지 (전역 전달자는 미설정이어도 행이 나온다)")

    @classmethod
    def of(cls, summary) -> "ForwarderOut":
        return cls(
            scope=summary.scope,
            domain=summary.domain,
            view=summary.view,
            forwarders=list(summary.forwarders),
            policy=summary.policy,
            configured=bool(summary.forwarders),
        )


class QueryOut(BaseModel):
    argv: list[str]
    returncode: int
    output: str
