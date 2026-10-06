# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""편집 서비스 — UI 의 조작을 트랜잭션 엔진 호출로 옮긴다.

레코드 추가/수정/삭제, raw 저장, 그리고 Windows DNS Manager 의
"Create associated pointer (PTR) record" 에 해당하는 역방향 zone 연동을 담당한다.

여기서 파일을 직접 쓰지 않는다. 모든 변경은 apply.py 의 트랜잭션을 지난다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import dns.exception
import dns.name
import dns.reversename

from ..config import Config
from . import apply as apply_mod
from . import dynamic, service, zoneedit, zonefile, zonetemplate
from .apply import ApplyError, ApplyResult
from .layout import ZoneEntry
from .records import Record

PTR_CAPABLE = frozenset({"A", "AAAA"})


@dataclass
class EditOutcome:
    """하나의 조작 결과. PTR 처럼 부수 변경이 있으면 함께 담는다."""

    primary: ApplyResult
    related: list[ApplyResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.primary.ok


def _entry(cfg: Config, zone: str, view: str | None) -> ZoneEntry:
    entry = service.find_zone(cfg, zone, view)
    if not entry.editable:
        if entry.is_signed:
            raise ApplyError(f"DNSSEC 서명 zone 은 편집할 수 없습니다: {entry.name}")
        raise ApplyError(f"편집할 수 없는 zone 입니다: {entry.name} (type={entry.zone_type})")
    return entry


def _dynamic_outcome(entry: ZoneEntry, summary: str, rcode: str, note: str) -> EditOutcome:
    """동적 갱신 결과를 파일 편집과 같은 모양으로 돌려준다(UI 가 한 가지만 다루도록)."""
    result = ApplyResult(
        ok=True,
        target=entry.name,
        status="applied",
        summary=summary,
        reload=None,
        error=None,
    )
    return EditOutcome(primary=result, notes=[note, f"서버 응답: {rcode}"])


def uses_dynamic_update(cfg: Config, entry: ZoneEntry) -> bool:
    """이 zone 을 RFC 2136 으로 고쳐야 하는지.

    allow-update 가 걸린 zone 은 named 가 journal 에 변경을 쌓는다. 파일을 직접 고치면
    journal 과 어긋나므로 프로토콜로 고친다. 키를 못 찾으면 파일 경로로 돌아가되,
    그때는 호출부가 journal 을 먼저 반영(rndc sync)해야 한다.
    """
    if not entry.dynamic:
        return False
    try:
        dynamic.load_key(cfg, entry)
        return True
    except dynamic.DynamicError:
        return False


def _text(entry: ZoneEntry) -> tuple[str, str]:
    snapshot = zonefile.read_snapshot(entry.file, entry.name)  # type: ignore[arg-type]
    return snapshot.text, snapshot.version


def _record_line(record: Record, ttl: int | None) -> str:
    return zoneedit.format_record(record.name, ttl, record.rtype, record.data, record.rdclass)


# --------------------------- 역방향(PTR) 연동 ---------------------------


def find_reverse_zone(cfg: Config, address: str) -> tuple[ZoneEntry, str] | None:
    """IP 를 담는 역방향 zone 과 그 안에서의 상대 이름을 찾는다.

    Windows DNS Manager 는 해당 역방향 zone 이 있을 때만 PTR 체크박스를 켠다.
    같은 동작을 위해 "없으면 None" 을 돌려준다.
    """
    try:
        reverse = dns.reversename.from_address(address)
    except (dns.exception.SyntaxError, ValueError):
        return None

    best: tuple[ZoneEntry, str] | None = None
    best_depth = -1
    for entry in service.get_layout(cfg).zones:
        if not entry.is_reverse or not entry.editable:
            continue
        try:
            origin = dns.name.from_text(entry.name)
        except dns.exception.DNSException:
            continue
        if not reverse.is_subdomain(origin):
            continue
        depth = len(origin.labels)
        if depth > best_depth:  # 더 구체적인 zone 을 고른다
            best_depth = depth
            best = (entry, reverse.relativize(origin).to_text())
    return best


def _add_ptr(cfg: Config, address: str, target_fqdn: str, author: str | None) -> tuple[ApplyResult | None, str]:
    found = find_reverse_zone(cfg, address)
    if found is None:
        return None, f"{address} 를 담는 역방향 zone 이 없어 PTR 을 만들지 않았습니다."
    entry, name = found

    text, _version = _text(entry)
    existing = [
        loc for loc in zoneedit.index_records(text, entry.name)
        if loc.record.rtype == "PTR" and loc.record.name == name
    ]
    if any(loc.record.data == target_fqdn for loc in existing):
        return None, f"PTR 이 이미 있습니다: {name}.{entry.name} → {target_fqdn}"

    line = zoneedit.format_record(name, None, "PTR", target_fqdn)
    result = apply_mod.apply_zone_text(
        cfg,
        entry,
        zoneedit.append_record(text, line),
        author=author,
        summary=f"PTR 추가: {name} → {target_fqdn}",
    )
    note = f"PTR 추가: {name}.{entry.name} → {target_fqdn}" if result.ok else f"PTR 추가 실패: {result.error}"
    return result, note


def _delete_ptr(cfg: Config, address: str, target_fqdn: str, author: str | None) -> tuple[ApplyResult | None, str]:
    """Windows 와 동일하게, 호스트 레코드를 지우면 짝이 되는 PTR 도 지운다."""
    found = find_reverse_zone(cfg, address)
    if found is None:
        return None, ""
    entry, name = found

    text, _version = _text(entry)
    targets = [
        loc.id
        for loc in zoneedit.index_records(text, entry.name)
        if loc.record.rtype == "PTR" and loc.record.name == name and loc.record.data == target_fqdn
    ]
    if not targets:
        return None, ""

    result = apply_mod.apply_zone_text(
        cfg,
        entry,
        zoneedit.delete_records(text, entry.name, targets),
        author=author,
        summary=f"PTR 삭제: {name} → {target_fqdn}",
    )
    note = f"PTR 삭제: {name}.{entry.name}" if result.ok else f"PTR 삭제 실패: {result.error}"
    return result, note


# --------------------------- 레코드 조작 ---------------------------


def add_record(
    cfg: Config,
    zone: str,
    *,
    name: str,
    rtype: str,
    data: str,
    ttl: int | None = None,
    view: str | None = None,
    create_ptr: bool = False,
    author: str | None = None,
    expected_version: str | None = None,
) -> EditOutcome:
    entry = _entry(cfg, zone, view)
    record = zoneedit.normalize(name, rtype, data, entry.name)

    if uses_dynamic_update(cfg, entry):
        rcode = dynamic.add_record(cfg, entry, record.name, record.rtype, record.data, ttl or 3600)
        outcome = _dynamic_outcome(
            entry,
            f"{record.rtype} 추가: {record.name} {record.data}",
            rcode,
            "동적 갱신(RFC 2136)으로 적용했습니다 — 이 zone 은 allow-update 가 걸려 있습니다.",
        )
        if create_ptr and record.rtype in PTR_CAPABLE:
            ptr_result, note = _add_ptr(cfg, record.data, record.fqdn, author)
            if ptr_result is not None:
                outcome.related.append(ptr_result)
            if note:
                outcome.notes.append(note)
        return outcome

    text, version = _text(entry)
    if any(
        loc.record.name == record.name and loc.record.rtype == record.rtype and loc.record.data == record.data
        for loc in zoneedit.index_records(text, entry.name)
    ):
        raise ApplyError(f"같은 레코드가 이미 있습니다: {record.name} {record.rtype} {record.data}")

    new_text = zoneedit.append_record(text, _record_line(record, ttl))
    result = apply_mod.apply_zone_text(
        cfg,
        entry,
        new_text,
        expected_version=expected_version or version,
        author=author,
        summary=f"{record.rtype} 추가: {record.name} {record.data}",
    )

    outcome = EditOutcome(primary=result)
    if result.ok and create_ptr and record.rtype in PTR_CAPABLE:
        ptr_result, note = _add_ptr(cfg, record.data, record.fqdn, author)
        if ptr_result is not None:
            outcome.related.append(ptr_result)
        if note:
            outcome.notes.append(note)
    return outcome


def update_record(
    cfg: Config,
    zone: str,
    record_id: str,
    *,
    name: str,
    rtype: str,
    data: str,
    ttl: int | None = None,
    view: str | None = None,
    author: str | None = None,
    expected_version: str | None = None,
) -> EditOutcome:
    entry = _entry(cfg, zone, view)
    text, version = _text(entry)
    target = zoneedit.find(zoneedit.index_records(text, entry.name), record_id)
    record = zoneedit.normalize(name, rtype, data, entry.name)

    if uses_dynamic_update(cfg, entry):
        # 이름·타입이 그대로면 교체, 바뀌었으면 지우고 더한다.
        if target.record.name == record.name and target.record.rtype == record.rtype:
            rcode = dynamic.replace_record(cfg, entry, record.name, record.rtype, record.data, ttl or target.record.ttl)
        else:
            dynamic.delete_record(cfg, entry, target.record.name, target.record.rtype, target.record.data)
            rcode = dynamic.add_record(cfg, entry, record.name, record.rtype, record.data, ttl or target.record.ttl)
        return _dynamic_outcome(
            entry,
            f"{target.record.rtype} 수정: {target.record.name} → {record.name} {record.data}",
            rcode,
            "동적 갱신(RFC 2136)으로 적용했습니다.",
        )

    new_text = zoneedit.replace_record(text, entry.name, record_id, _record_line(record, ttl))
    result = apply_mod.apply_zone_text(
        cfg,
        entry,
        new_text,
        expected_version=expected_version or version,
        author=author,
        summary=(
            f"{target.record.rtype} 수정: {target.record.name} {target.record.data}"
            f" → {record.name} {record.data}"
        ),
    )
    return EditOutcome(primary=result)


def delete_records(
    cfg: Config,
    zone: str,
    record_ids: list[str],
    *,
    view: str | None = None,
    delete_ptr: bool = False,
    author: str | None = None,
    expected_version: str | None = None,
) -> EditOutcome:
    entry = _entry(cfg, zone, view)
    text, version = _text(entry)
    locations = zoneedit.index_records(text, entry.name)
    targets = [zoneedit.find(locations, rid) for rid in record_ids]

    if uses_dynamic_update(cfg, entry):
        rcode = "NOERROR"
        for target in targets:
            if target.record.rtype == "SOA":
                raise ApplyError("SOA 레코드는 삭제할 수 없습니다.")
            rcode = dynamic.delete_record(
                cfg, entry, target.record.name, target.record.rtype, target.record.data
            )
        outcome = _dynamic_outcome(
            entry,
            "레코드 삭제: " + ", ".join(f"{t.record.name} {t.record.rtype}" for t in targets),
            rcode,
            "동적 갱신(RFC 2136)으로 적용했습니다.",
        )
        if delete_ptr:
            for target in targets:
                if target.record.rtype not in PTR_CAPABLE:
                    continue
                ptr_result, note = _delete_ptr(cfg, target.record.data, target.record.fqdn, author)
                if ptr_result is not None:
                    outcome.related.append(ptr_result)
                if note:
                    outcome.notes.append(note)
        return outcome

    new_text = zoneedit.delete_records(text, entry.name, record_ids)
    summary = "레코드 삭제: " + ", ".join(f"{t.record.name} {t.record.rtype}" for t in targets)
    result = apply_mod.apply_zone_text(
        cfg,
        entry,
        new_text,
        expected_version=expected_version or version,
        author=author,
        summary=summary,
    )

    outcome = EditOutcome(primary=result)
    if result.ok and delete_ptr:
        for target in targets:
            if target.record.rtype not in PTR_CAPABLE:
                continue
            ptr_result, note = _delete_ptr(cfg, target.record.data, target.record.fqdn, author)
            if ptr_result is not None:
                outcome.related.append(ptr_result)
            if note:
                outcome.notes.append(note)
    return outcome


def save_raw(
    cfg: Config,
    zone: str,
    text: str,
    *,
    view: str | None = None,
    expected_version: str | None = None,
    author: str | None = None,
    bump_serial: bool = True,
    freeze: bool = True,
) -> EditOutcome:
    """raw 저장. 동적 zone 은 freeze → 저장 → thaw 로 journal 과의 충돌을 피한다."""
    entry = _entry(cfg, zone, view)

    if entry.dynamic and freeze:
        return _save_raw_frozen(cfg, entry, text, expected_version, author, bump_serial)

    result = apply_mod.apply_zone_text(
        cfg,
        entry,
        text,
        expected_version=expected_version,
        author=author,
        summary="raw zone 파일 저장",
        bump_serial=bump_serial,
    )
    return EditOutcome(primary=result)


def _save_raw_frozen(
    cfg: Config,
    entry: ZoneEntry,
    text: str,
    expected_version: str | None,
    author: str | None,
    bump_serial: bool,
) -> EditOutcome:
    """동적 zone 의 파일을 안전하게 바꾼다.

    `rndc freeze` 는 동적 갱신을 멈추고 journal 을 파일에 반영한다. 그 상태에서 파일을 고치고
    `rndc thaw` 로 다시 읽힌다. 이 절차 없이 파일을 고치면 journal 이 변경을 덮어쓴다.
    """
    freeze = apply_mod._rndc(cfg, "freeze", entry.name)  # noqa: SLF001 - 같은 패키지
    if not freeze.ok:
        raise ApplyError(f"zone 을 freeze 하지 못했습니다: {freeze.message}")
    try:
        result = apply_mod.apply_zone_text(
            cfg,
            entry,
            text,
            expected_version=expected_version,
            author=author,
            summary="raw zone 파일 저장 (freeze/thaw)",
            bump_serial=bump_serial,
        )
    finally:
        thaw = apply_mod._rndc(cfg, "thaw", entry.name)  # noqa: SLF001
    notes = ["동적 zone 이라 freeze → 저장 → thaw 순서로 적용했습니다."]
    if not thaw.ok:
        notes.append(f"경고: thaw 에 실패했습니다 — {thaw.message}. 동적 갱신이 멈춰 있습니다.")
    return EditOutcome(primary=result, notes=notes)


# --------------------------- zone Properties ---------------------------


def _soa_location(text: str, zone: str):
    for location in zoneedit.index_records(text, zone):
        if location.record.rtype == "SOA":
            return location
    raise ApplyError(f"'{zone}' 에서 SOA 레코드를 찾을 수 없습니다.")


def read_soa(cfg: Config, zone: str, view: str | None = None) -> dict[str, str]:
    """Properties ▸ Start of Authority (SOA) 탭에 채울 값."""
    entry = service.find_zone(cfg, zone, view)
    if entry.file is None:
        raise ApplyError(f"'{zone}' 은 zone 파일을 갖지 않습니다.")
    text, _ = _text(entry)
    record = _soa_location(text, entry.name).record
    parts = record.data.split()
    labels = ["primary", "responsible", "serial", "refresh", "retry", "expire", "minimum"]
    return dict(zip(labels, parts[:7]))


def update_soa(
    cfg: Config,
    zone: str,
    values: dict[str, object],
    *,
    view: str | None = None,
    author: str | None = None,
    expected_version: str | None = None,
) -> EditOutcome:
    """SOA 를 편집한다. serial 은 받지 않는다 — 트랜잭션이 자동으로 증가시킨다.

    (사용자가 serial 을 직접 낮추면 secondary 가 갱신을 받지 못한다)
    """
    entry = _entry(cfg, zone, view)
    text, version = _text(entry)
    location = _soa_location(text, entry.name)
    current = location.record.data.split()

    def pick(key: str, index: int) -> str:
        raw = values.get(key)
        return str(raw).strip() if raw not in (None, "") else current[index]

    def fqdn(raw: str) -> str:
        return raw if raw.endswith(".") else f"{raw}."

    def rname(raw: str) -> str:
        # 운영자는 보통 메일 주소로 입력한다. SOA 의 rname 표기로 바꿔 주지 않으면
        # '@' 가 이스케이프된 엉뚱한 이름이 된다.
        return zonetemplate.responsible_from_email(raw) if "@" in raw else fqdn(raw)

    soa = zonetemplate.SoaValues(
        primary=fqdn(pick("primary", 0)),
        responsible=rname(pick("responsible", 1)),
        serial=int(current[2]),  # 그대로 두면 엔진이 올린다
        refresh=int(pick("refresh", 3)),
        retry=int(pick("retry", 4)),
        expire=int(pick("expire", 5)),
        minimum=int(pick("minimum", 6)),
    )

    lines = text.splitlines(keepends=True)
    new_text = "".join(lines[: location.start]) + zonetemplate.render_soa(soa) + "".join(lines[location.end :])

    result = apply_mod.apply_zone_text(
        cfg,
        entry,
        new_text,
        expected_version=expected_version or version,
        author=author,
        summary="SOA 변경",
    )
    return EditOutcome(primary=result)


# --------------------------- 위임 (New Delegation) ---------------------------


def add_delegation(
    cfg: Config,
    zone: str,
    child: str,
    servers: list[dict[str, str]],
    *,
    ttl: int | None = None,
    view: str | None = None,
    author: str | None = None,
) -> EditOutcome:
    """하위 도메인을 다른 네임서버로 위임한다 (Windows DNS Manager 의 New Delegation).

    상위 zone 에 NS 레코드를 넣고, 네임서버 이름이 위임 구간 **안쪽**이면 glue 레코드도
    함께 넣는다. glue 가 없으면 해석이 끊긴다(위임의 고전적 함정).
    """
    entry = _entry(cfg, zone, view)
    label = child.strip().rstrip(".")
    if not label:
        raise ApplyError("위임할 하위 도메인 이름을 입력하세요.")
    if label.lower().endswith("." + entry.name.lower()):
        label = label[: -(len(entry.name) + 1)]
    if not servers:
        raise ApplyError("위임받을 네임서버를 하나 이상 입력하세요.")

    child_fqdn = f"{label}.{entry.name}".rstrip(".")
    text, version = _text(entry)
    lines: list[str] = []

    for server in servers:
        name = str(server.get("name", "")).strip()
        address = str(server.get("address", "")).strip()
        if not name:
            raise ApplyError("네임서버 이름을 입력하세요.")
        target = name if name.endswith(".") else f"{name}."
        zoneedit.normalize(label, "NS", target, entry.name)  # 형식 검증
        lines.append(zoneedit.format_record(label, ttl, "NS", target))

        inside = target.rstrip(".").lower().endswith(child_fqdn.lower())
        if inside:
            if not address:
                raise ApplyError(
                    f"'{target}' 는 위임 구간 안쪽 이름이므로 glue 주소가 필요합니다. "
                    f"네임서버 주소를 함께 입력하세요."
                )
            glue_name = target.rstrip(".")[: -(len(entry.name) + 1)]
            rtype = "AAAA" if ":" in address else "A"
            zoneedit.normalize(glue_name, rtype, address, entry.name)
            lines.append(zoneedit.format_record(glue_name, ttl, rtype, address))
        elif address:
            # 위임 구간 밖이면 glue 를 넣어서는 안 된다(권위 밖 데이터).
            raise ApplyError(
                f"'{target}' 는 위임 구간({child_fqdn}) 밖이므로 이 zone 에 주소를 넣을 수 없습니다. "
                f"주소는 비우고 해당 zone 에서 관리하세요."
            )

    new_text = text
    for line in lines:
        new_text = zoneedit.append_record(new_text, line)

    result = apply_mod.apply_zone_text(
        cfg,
        entry,
        new_text,
        expected_version=version,
        author=author,
        summary=f"위임 추가: {child_fqdn} → " + ", ".join(s.get("name", "") for s in servers),
    )
    return EditOutcome(primary=result)
