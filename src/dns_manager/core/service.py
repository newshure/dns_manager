# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""조회 서비스 — 콘솔 트리와 레코드 목록을 만들어낸다.

layout(named.conf) + zonefile(zone 파일) + server(rndc) 를 합쳐 UI 가 바로 쓸 수 있는
형태로 돌려준다. 캐시하지 않는다: zone 파일이 진실이며 외부 변경을 즉시 반영해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Config
from . import layout as layout_mod
from . import naming
from . import records as records_mod
from . import server as server_mod
from . import zonefile
from .commands import CommandError
from .layout import Layout, ZoneEntry
from .records import Record
from .zonefile import ZoneContent


class ZoneNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ZoneSummary:
    """콘솔 트리와 zone 목록에 쓰는 요약."""

    name: str
    view: str | None
    zone_type: str
    category: str
    file: str | None
    editable: bool
    signed: bool
    dynamic: bool
    file_serial: int | None
    loaded_serial: int | None
    record_count: int | None
    problem: str | None
    source_file: str | None = None
    # 파일이 적재 시각 이후에 바뀌었는가. serial 을 올리지 않은 수정도 이걸로 잡힌다.
    file_changed_since_load: bool = False
    loaded_at: str | None = None
    # 파일명이 .zone/.rev 규칙을 따르는지. 어긋나도 편집은 허용하고 표시만 한다.
    conventional_file: bool = True

    @property
    def out_of_sync(self) -> bool:
        """reload 가 필요한가.

        serial 차이뿐 아니라 **파일 수정 시각**도 본다. 파일을 고치면서 serial 을 올리지
        않는 경우가 흔한데(손으로 편집, 다른 도구), 그러면 serial 비교로는 아무 일도
        없는 것처럼 보이고 named 는 옛 내용을 계속 서비스한다.
        """
        if self.file_changed_since_load:
            return True
        if self.file_serial is None or self.loaded_serial is None:
            return False
        return self.file_serial != self.loaded_serial


@dataclass(frozen=True)
class ZoneDetail:
    entry: ZoneEntry
    content: ZoneContent | None
    status: server_mod.ZoneStatus
    problem: str | None = None

    @property
    def file_changed_since_load(self) -> bool:
        return file_changed_since_load(self.entry, self.status)

    @property
    def records(self) -> tuple[Record, ...]:
        return self.content.records if self.content else ()


def _summarize(cfg: Config, entry: ZoneEntry, *, with_status: bool = True) -> ZoneSummary:
    file_serial: int | None = None
    record_count: int | None = None
    problem: str | None = None

    if entry.file is not None:
        try:
            content = zonefile.load(entry.file, entry.name)
            file_serial = content.serial
            record_count = len(content.records)
            problem = content.parse_error
        except zonefile.ZoneReadError as exc:
            problem = str(exc)

    loaded_serial: int | None = None
    changed_since_load = False
    loaded_at_text: str | None = None
    # named 에 못 물어볼 때는 설정과 journal 만으로 판단한다.
    is_dynamic = effective_dynamic(entry)
    if with_status:
        try:
            status = server_mod.zone_status(cfg.bind, entry.name, entry.view)
            loaded_serial = status.serial
            loaded_at_text = status.loaded
            is_dynamic = effective_dynamic(entry, status)
            changed_since_load = file_changed_since_load(entry, status)
            if entry.file is not None and sync_if_stale(cfg, entry, loaded_serial, file_serial, dynamic=is_dynamic):
                content = zonefile.load(entry.file, entry.name)
                file_serial = content.serial
                record_count = len(content.records)
        except (CommandError, zonefile.ZoneReadError):
            loaded_serial = None

    conventional = True
    if entry.file is not None and entry.zone_type == "master":
        conventional = naming.follows_convention(cfg.bind, entry.name, entry.file)

    return ZoneSummary(
        name=entry.name,
        view=entry.view,
        zone_type=entry.zone_type,
        category=entry.category,
        file=str(entry.file) if entry.file else None,
        editable=entry.editable,
        signed=entry.is_signed,
        dynamic=is_dynamic,
        file_serial=file_serial,
        loaded_serial=loaded_serial,
        record_count=record_count,
        problem=problem,
        source_file=str(entry.source_file) if entry.source_file else None,
        file_changed_since_load=changed_since_load,
        loaded_at=loaded_at_text,
        conventional_file=conventional,
    )


def get_layout(cfg: Config) -> Layout:
    return layout_mod.discover(cfg.bind)


def list_zones(cfg: Config, *, include_builtin: bool = False, with_status: bool = True) -> list[ZoneSummary]:
    layout = get_layout(cfg)
    return [
        _summarize(cfg, entry, with_status=with_status)
        for entry in layout.zones
        if include_builtin or not entry.is_builtin
    ]


def find_zone(cfg: Config, name: str, view: str | None = None) -> ZoneEntry:
    entry = get_layout(cfg).get(name, view)
    if entry is None:
        raise ZoneNotFound(f"zone 을 찾을 수 없습니다: {name}")
    return entry


def effective_dynamic(entry: ZoneEntry, status: server_mod.ZoneStatus | None = None) -> bool:
    """named 가 이 zone 을 동적으로 다루는가.

    named.conf 의 zone 블록만 봐서는 틀린다. allow-update 가 options 에 전역으로
    걸려 있거나, view 에서 상속되거나, 예전에 동적이었다가 journal 만 남은 zone 은
    zone 블록에 아무 흔적이 없는데도 named 는 동적으로 취급한다.

    이 구분을 틀리면 조용히 깨진다. 파일을 고치고 `rndc reload` 를 보내면 named 가
    'dynamic zone' 으로 거절하고, 변경은 서비스되지 않는데 적재 시각은 그대로 남아
    "reload 가 필요합니다" 가 영원히 사라지지 않는다.

    그래서 순서를 둔다: named 가 말하는 사실(zonestatus) → journal 존재 → named.conf.
    """
    if status is not None and status.dynamic is not None:
        return status.dynamic
    return entry.dynamic or entry.has_journal


def file_changed_since_load(entry: ZoneEntry, status: server_mod.ZoneStatus) -> bool:
    """zone 파일이 named 가 읽어들인 뒤에 바뀌었는가.

    "파일에는 있는데 응답은 NXDOMAIN" 의 가장 흔한 원인이다. serial 을 올리지 않고
    파일만 고치면 serial 비교로는 아무 문제가 없어 보이지만 named 는 옛 내용을 서비스한다.
    동적 zone 은 named 자신이 파일을 쓰므로(dump-interval) 이 비교가 의미 없다 — 제외한다.
    """
    if entry.file is None or effective_dynamic(entry, status):
        return False
    loaded_at = status.loaded_at
    if loaded_at is None:
        return False
    try:
        mtime = datetime.fromtimestamp(entry.file.stat().st_mtime, timezone.utc)
    except OSError:
        return False
    # rndc 는 초 단위로 내보낸다. 같은 초에 벌어진 일은 어긋남으로 보지 않는다.
    return (mtime - loaded_at).total_seconds() > 1


def sync_if_stale(
    cfg: Config,
    entry: ZoneEntry,
    loaded_serial: int | None,
    file_serial: int | None,
    *,
    dynamic: bool | None = None,
) -> bool:
    """동적 zone 의 journal 을 파일에 반영한다.

    allow-update 가 걸린 zone 은 변경이 journal(.jnl)에 쌓이고 zone 파일은 뒤처진다.
    파일만 읽으면 **화면이 거짓말을 한다** — 방금 동적 갱신으로 넣은 레코드가 안 보인다.
    그래서 적재 serial 이 파일보다 앞서 있으면 `rndc sync` 로 먼저 내려 쓴다
    (Windows DNS Manager 의 'Update Server Data File' 에 대응).
    """
    if not (entry.dynamic if dynamic is None else dynamic):
        return False
    if loaded_serial is None or file_serial is None:
        return False
    if loaded_serial == file_serial:
        return False
    from . import apply as apply_mod

    result = apply_mod._rndc(cfg, "sync", entry.name)  # noqa: SLF001 - 같은 패키지
    return result.ok


def get_zone(cfg: Config, name: str, view: str | None = None) -> ZoneDetail:
    entry = find_zone(cfg, name, view)
    content: ZoneContent | None = None
    problem: str | None = None
    if entry.file is not None:
        try:
            content = zonefile.load(entry.file, entry.name)
        except zonefile.ZoneReadError as exc:
            problem = str(exc)
    else:
        problem = f"'{entry.zone_type}' 종류의 zone 은 zone 파일을 갖지 않습니다."

    try:
        status = server_mod.zone_status(cfg.bind, entry.name, entry.view)
    except CommandError as exc:
        status = server_mod.ZoneStatus(name=entry.name, available=False, raw=str(exc))

    # 동적 zone 이 journal 때문에 뒤처져 있으면 반영한 뒤 다시 읽는다.
    if content is not None and sync_if_stale(
        cfg, entry, status.serial, content.serial, dynamic=effective_dynamic(entry, status)
    ):
        try:
            content = zonefile.load(entry.file, entry.name)  # type: ignore[arg-type]
        except zonefile.ZoneReadError as exc:
            problem = str(exc)

    return ZoneDetail(entry=entry, content=content, status=status, problem=problem)


def filter_records(records: tuple[Record, ...], *, advanced: bool, rtype: str | None = None) -> list[Record]:
    """기본 보기에서는 Advanced 전용 타입을 숨긴다(Windows View ▸ Advanced 대응)."""
    out = []
    for record in records:
        if not advanced and record.advanced_only:
            continue
        if not advanced and record.rtype in records_mod.DNSSEC_TYPES:
            continue
        if rtype and record.rtype != rtype:
            continue
        out.append(record)
    return out


def server_status(cfg: Config) -> server_mod.ServerStatus:
    return server_mod.status(cfg.bind)


@dataclass(frozen=True)
class ForwarderSummary:
    """전달자 한 건. 전역 전달자는 domain 이 None."""

    domain: str | None
    view: str | None
    forwarders: tuple[str, ...]
    policy: str | None  # forward only | forward first
    scope: str  # server | conditional

    @property
    def label(self) -> str:
        return self.domain or "(server-wide)"


def list_forwarders(cfg: Config) -> list[ForwarderSummary]:
    """서버 전역 전달자 + 도메인별 조건부 전달자를 한 목록으로 돌려준다.

    Windows DNS Manager 에서는 전자가 서버 Properties ▸ Forwarders 탭,
    후자가 Conditional Forwarders 노드다. BIND 에서는 전자가 options { forwarders },
    후자가 `type forward` zone 으로 표현된다.
    """
    layout = get_layout(cfg)
    out: list[ForwarderSummary] = []
    # 전역(기본) 전달자는 미설정이어도 항상 한 행으로 보여준다.
    # 목록에서 빠지면 "설정이 없는 것"과 "화면에 없는 것"을 구분할 수 없다.
    out.append(
        ForwarderSummary(
            domain=None,
            view=None,
            forwarders=layout.forwarding.forwarders,
            policy=layout.forwarding.policy,
            scope="server",
        )
    )
    for entry in layout.zones:
        if not entry.is_forwarder:
            continue
        out.append(
            ForwarderSummary(
                domain=entry.name,
                view=entry.view,
                forwarders=entry.forwarders,
                policy=entry.forward_policy,
                scope="conditional",
            )
        )
    return out
