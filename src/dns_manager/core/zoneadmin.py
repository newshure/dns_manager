# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""zone 추가/삭제와 전달자 관리 (New Zone 마법사, Conditional Forwarders).

Windows DNS Manager 의 New Zone Wizard / Conditional Forwarders 에 대응한다.
파일 기반 서버이므로 **만들어질 결과물(named.conf 블록 + zone 파일 전문)을 먼저 보여주고**
확인 후 기록한다. 기록 경로는 apply.py 트랜잭션이다.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from . import apply as apply_mod
from . import confedit, detect, fileaudit, naming, service, zonetemplate
from .apply import ApplyError, ApplyResult
from .commands import CommandResult
from .zonetemplate import SoaValues, TemplateError

MASTER_TYPES = frozenset({"master", "slave", "stub", "forward"})


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def adopt_zone_dir_ownership(path: Path) -> None:
    """새로 만든 zone 파일에 그 디렉터리의 소유·권한 관례를 입힌다.

    앱은 root 로 돈다. 그대로 두면 새 파일이 root:root 0644 가 되는데,
    - named 가 **읽기는** 되지만 동적 갱신(allow-update)을 걸면 쓰지 못하고,
    - 같은 디렉터리의 다른 zone 파일(보통 root:named 0664)과 관례가 어긋나 혼란스럽다.
    그래서 디렉터리의 그룹과, 이웃 zone 파일의 권한을 따른다.
    """
    try:
        directory = path.parent
        gid = directory.stat().st_gid

        mode = 0o664
        for sibling in sorted(directory.glob("*.zone")) + sorted(directory.glob("*.rev")):
            if sibling == path:
                continue
            mode = sibling.stat().st_mode & 0o777
            break

        os.chown(path, -1, gid)
        os.chmod(path, mode)
    except (OSError, PermissionError):
        # 권한이 없으면 그대로 둔다 — 파일 생성 자체는 이미 끝났고,
        # 읽기는 가능하므로 서비스가 막히지는 않는다.
        pass


@dataclass
class ZoneSpec:
    """New Zone 마법사가 모은 입력."""

    name: str
    zone_type: str = "master"  # master | slave | stub | forward
    reverse: bool = False
    network_id: str | None = None  # 역방향일 때 정방향 순서의 network ID
    family: int = 4
    file_name: str | None = None
    masters: list[str] = field(default_factory=list)
    forwarders: list[str] = field(default_factory=list)
    forward_policy: str | None = None
    allow_update: list[str] = field(default_factory=lambda: ["none"])
    allow_transfer: list[str] = field(default_factory=lambda: ["none"])
    ttl: int = 3600
    primary_ns: str | None = None
    responsible: str = "hostmaster"
    name_servers: list[str] = field(default_factory=list)
    glue: dict[str, str] = field(default_factory=dict)


@dataclass
class ZonePreview:
    """마법사의 Summary 단계에서 보여줄 내용."""

    zone: str
    conf_block: str
    zone_file: str | None
    file_path: str | None
    checkzone: CommandResult | None = None

    @property
    def valid(self) -> bool:
        return self.checkzone is None or self.checkzone.ok

    @property
    def check_output(self) -> str:
        return self.checkzone.message if self.checkzone else ""


def resolve_zone_name(spec: ZoneSpec) -> str:
    """역방향이면 network ID 로부터 zone 이름을 만든다 (Windows 의 Network ID 입력과 동일)."""
    if spec.reverse and spec.network_id:
        return zonetemplate.network_to_zone(spec.network_id, spec.family)
    return zonetemplate.validate_zone_name(spec.name)


def _soa(spec: ZoneSpec, zone: str) -> SoaValues:
    return zonetemplate.default_soa(zone, spec.primary_ns, spec.responsible)


def ns_address_spec(spec: ZoneSpec, zone: str) -> None:
    """primary_ns 가 'name address' 형태로 들어오면 glue 로 분리한다(입력 편의)."""
    if not spec.primary_ns:
        return
    parts = spec.primary_ns.split()
    if len(parts) == 2:
        spec.primary_ns = parts[0]
        label = parts[0].rstrip(".")
        if zonetemplate.is_inside(label, zone):
            label = label[: -(len(zone) + 1)] or "@"
        spec.glue = {**spec.glue, label: parts[1]}


def _conf_block(spec: ZoneSpec, zone: str, file_name: str | None) -> str:
    if spec.zone_type == "forward":
        return confedit.render_zone_block(
            zone,
            "forward",
            forwarders=spec.forwarders,
            forward_policy=spec.forward_policy or "only",
            comment="dns_manager — 조건부 전달자",
        )
    if spec.zone_type in {"slave", "stub"}:
        return confedit.render_zone_block(
            zone, spec.zone_type, file=file_name, masters=spec.masters, comment="dns_manager"
        )
    return confedit.render_zone_block(
        zone,
        "master",
        file=file_name,
        allow_update=spec.allow_update,
        allow_transfer=spec.allow_transfer,
        comment="dns_manager",
    )


def preview(cfg: Config, spec: ZoneSpec) -> ZonePreview:
    """기록하지 않고 결과물만 만들어 돌려준다. 가능하면 named-checkzone 으로 미리 검증한다."""
    zone = resolve_zone_name(spec)
    ns_address_spec(spec, zone)
    if service.get_layout(cfg).get(zone) is not None:
        raise ApplyError(f"이미 있는 zone 입니다: {zone}")

    if spec.zone_type == "forward":
        if not spec.forwarders:
            raise ApplyError("전달자 IP 를 하나 이상 입력하세요.")
        return ZonePreview(zone=zone, conf_block=_conf_block(spec, zone, None), zone_file=None, file_path=None)

    file_name = spec.file_name or naming.default_file_name(cfg.bind, zone)
    directory = Path(cfg.bind.zone_dir or service.get_layout(cfg).directory)
    file_path = directory / file_name

    if spec.zone_type in {"slave", "stub"}:
        if not spec.masters:
            raise ApplyError(f"{spec.zone_type} zone 에는 master 서버 IP 가 필요합니다.")
        return ZonePreview(
            zone=zone, conf_block=_conf_block(spec, zone, file_name), zone_file=None, file_path=str(file_path)
        )

    soa = _soa(spec, zone)
    servers = spec.name_servers or [soa.primary]
    glue = dict(spec.glue)

    # zone 안쪽 NS 는 주소가 없으면 named-checkzone 이 zone 을 적재하지 않는다.
    # 주소를 못 받았으면 이 호스트의 IP 로 채운다 — 추측값이지만 미리보기에 그대로 드러나고,
    # 운영자가 확인 후에야 기록된다.
    if not glue:
        for server in servers:
            if not zonetemplate.is_inside(server, zone):
                continue
            address = zonetemplate.local_ipv4()
            if address is None:
                break
            label = server.rstrip(".")
            label = label[: -(len(zone) + 1)] or "@"
            glue[label] = address
            break
    zonetemplate.require_glue(zone, servers, glue)
    text = zonetemplate.render_zone_file(
        zone,
        soa,
        ttl=spec.ttl,
        name_servers=servers,
        glue=glue,
    )
    check = _check_new_zone_text(cfg, zone, directory, text)
    return ZonePreview(
        zone=zone,
        conf_block=_conf_block(spec, zone, file_name),
        zone_file=text,
        file_path=str(file_path),
        checkzone=check,
    )


def _check_new_zone_text(cfg: Config, zone: str, directory: Path, text: str) -> CommandResult:
    """아직 존재하지 않는 zone 파일을 임시 파일로 검증한다."""
    staged = directory / f".{zone}.preview"
    staged.write_text(text, encoding="utf-8")
    try:
        return apply_mod.check_zone(cfg, zone, staged)
    finally:
        staged.unlink(missing_ok=True)


def create_zone(cfg: Config, spec: ZoneSpec, *, author: str | None = None) -> tuple[ZonePreview, ApplyResult]:
    """zone 을 만든다: zone 파일 기록 → named.conf 블록 추가 → checkconf → reconfig.

    설정 검증에 실패하면 트랜잭션이 설정을 되돌리고, 여기서 만든 zone 파일도 지운다.
    """
    view = preview(cfg, spec)
    conf_path = Path(cfg.bind.zones_conf)
    created_file: Path | None = None

    if view.zone_file is not None and view.file_path:
        if not view.valid:
            raise ApplyError(f"zone 파일 검증 실패:\n{view.check_output}")
        target = Path(view.file_path)
        if target.exists():
            raise ApplyError(f"zone 파일이 이미 있습니다: {target}")
        target.write_text(view.zone_file, encoding="utf-8")
        adopt_zone_dir_ownership(target)
        created_file = target

    try:
        conf_text = conf_path.read_text(encoding="utf-8")
        result = apply_mod.apply_conf_text(
            cfg,
            confedit.add_zone_block(conf_text, view.conf_block),
            author=author,
            summary=f"zone 추가: {view.zone} ({spec.zone_type})",
        )
    except Exception:
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        raise

    if not result.ok and created_file is not None:
        created_file.unlink(missing_ok=True)  # 설정이 되돌아갔으므로 파일도 남기지 않는다
    return view, result


def delete_zone(
    cfg: Config, zone: str, *, delete_file: bool = False, author: str | None = None
) -> ApplyResult:
    """zone 정의를 지운다. zone 파일은 기본적으로 남긴다(실수 복구를 쉽게 하기 위함)."""
    entry = service.find_zone(cfg, zone)
    # zone 이 정의된 **실제 파일**에서 지운다. zones.conf 에만 기대면
    # 다른 include 에 적힌 zone 을 영영 지우지 못한다.
    conf_path = entry.source_file or Path(cfg.bind.zones_conf)
    try:
        conf_text = conf_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApplyError(f"설정 파일을 읽을 수 없습니다: {conf_path} ({exc.strerror})") from exc

    if not confedit.has_zone_block(conf_text, entry.name):
        raise ApplyError(
            f"'{entry.name}' 의 정의를 {conf_path} 에서 찾지 못했습니다. "
            f"다른 설정 파일에 있거나 이미 지워졌을 수 있습니다 — Files 탭에서 "
            f"어떤 설정 파일을 읽고 있는지 확인하세요."
        )

    result = apply_mod.apply_conf_text(
        cfg,
        confedit.remove_zone_block(conf_text, entry.name),
        path=conf_path,
        author=author,
        summary=f"zone 삭제: {entry.name} ({conf_path.name})",
    )
    if result.ok and delete_file and entry.file is not None:
        backup = Path(cfg.app.backup_dir) / entry.name.replace("/", "_")
        backup.mkdir(parents=True, exist_ok=True)
        target = backup / f"deleted-{_timestamp()}-{entry.file.name}"
        try:
            # zone 디렉터리와 백업 디렉터리는 다른 파일시스템일 수 있다(/var/named vs /var/lib).
            # os.replace 는 장치를 넘지 못하므로 복사 후 삭제한다.
            shutil.move(str(entry.file), str(target))
        except OSError as exc:
            result.error = f"설정은 지웠지만 zone 파일을 옮기지 못했습니다: {exc}"
        else:
            result.summary = f"{result.summary} (파일 → {target})"
    return result


# --------------------------- 전달자 ---------------------------


def options_file(cfg: Config) -> Path:
    """options 블록이 들어 있는 설정 파일."""
    named_conf = Path(cfg.bind.named_conf)
    found = detect.find_options_file(named_conf, root=Path(cfg.bind.chroot) if cfg.bind.chroot else None)
    if found is None:
        raise ApplyError(
            f"options 블록을 찾을 수 없습니다 ({named_conf} 와 그 include 들). "
            f"전역 전달자를 설정하려면 options 블록이 있어야 합니다."
        )
    return found


def _writable(path: Path) -> bool:
    """원자적 교체가 가능한지 — 파일과 **디렉터리** 양쪽에 쓰기 권한이 필요하다."""
    return os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)


def options_write_target_for(cfg: Config, keywords: list[str]) -> Path:
    """이 문들을 어디에 쓸지 고른다.

    **이미 options 안에 있는 문은 그 파일에서 고쳐야 한다.** 다른 include 에 또 쓰면
    같은 문이 두 번 생겨 named-checkconf 가 'redefined' 로 거절한다(실제로 겪었다).
    아직 없는 문만 쓰기 쉬운 include 에 넣는다.
    """
    options = options_file(cfg)
    text = options.read_text(encoding="utf-8")
    existing = [k for k in keywords if confedit.get_option(text, k) is not None]

    if existing:
        if not _writable(options):
            raise ApplyError(
                f"'{', '.join(existing)}' 는 {options} 안에 정의되어 있어 그 파일을 고쳐야 하는데 "
                f"쓸 수 없습니다.\n"
                f"  1) {options} 에서 해당 줄을 지우면, 앱이 관리하는 include 파일에 넣을 수 있습니다.\n"
                f"  2) 또는 {options} 와 그 디렉터리에 쓰기 권한을 주세요."
            )
        return options
    return options_write_target(cfg)


def options_write_target(cfg: Config) -> Path:
    """options 설정을 기록할 파일.

    named.conf 는 보통 root 전용이고, 서비스는 최소 권한으로 돌아야 한다(systemd
    ProtectSystem=strict 아래에서는 /etc 가 아예 읽기 전용이다). 그래서 options 블록 안에
    include 된 파일 중 쓸 수 있는 것을 우선 쓴다. 없으면 options 파일 자체를 시도한다.
    """
    options = options_file(cfg)
    text = options.read_text(encoding="utf-8")
    fileaudit.record(options, "read", "options-conf", detail="options 블록 탐색")

    for raw in confedit.option_includes(text):
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = options.parent / candidate
        if cfg.bind.chroot and candidate.is_absolute():
            candidate = Path(cfg.bind.chroot) / candidate.relative_to("/")
        if candidate.is_file() and _writable(candidate):
            return candidate

    if _writable(options):
        return options

    suggestion = Path(cfg.bind.zones_conf).parent / "options-forwarders.conf"
    raise ApplyError(
        f"options 설정 파일에 쓸 수 없습니다: {options}\n"
        f"다음 중 하나를 하세요:\n"
        f"  1) options 블록 안에 include \"{suggestion}\"; 를 추가하고 그 파일을 "
        f"실행 계정이 쓸 수 있게 둡니다 (권장 — named.conf 는 건드리지 않습니다).\n"
        f"  2) {options} 와 그 디렉터리에 쓰기 권한을 부여합니다."
    )


def set_server_forwarders(
    cfg: Config, forwarders: list[str], policy: str | None = None, *, author: str | None = None
) -> ApplyResult:
    """전역(기본) 전달자를 설정한다 — options 블록이 **실제로 들어 있는 파일**을 고친다.

    RedHat 계열은 named.conf 자신, Debian 계열은 named.conf.options 다.
    경로를 가정하지 않고 include 를 따라가며 찾는다.
    """
    path = options_write_target(cfg)
    text = path.read_text(encoding="utf-8")
    inside_options = path == options_file(cfg)

    if inside_options:
        # options 블록 안의 문을 교체한다.
        if forwarders:
            new_text = confedit.set_option(
                text, "forwarders", confedit.render_address_list("forwarders", forwarders)
            )
            new_text = confedit.set_option(new_text, "forward", f"forward {policy};" if policy else None)
        else:
            new_text = confedit.set_option(text, "forwarders", None)
            new_text = confedit.set_option(new_text, "forward", None)
    else:
        # options 안에 include 된 전용 파일 — 내용을 통째로 다시 쓴다.
        lines = ["// dns_manager 가 관리하는 전역 전달자 설정 (options 블록 안에서 include)"]
        if forwarders:
            lines.append(confedit.render_address_list("forwarders", forwarders))
            if policy:
                lines.append(f"forward {policy};")
        new_text = "\n".join(lines) + "\n"

    summary = f"기본 전달자 설정: {', '.join(forwarders)}" if forwarders else "기본 전달자 해제"

    return apply_mod.apply_conf_text(cfg, new_text, path=path, author=author, summary=summary)


def set_server_access(
    cfg: Config,
    *,
    listen_on: list[str] | None = None,
    listen_on_v6: list[str] | None = None,
    allow_query: list[str] | None = None,
    allow_recursion: list[str] | None = None,
    recursion: str | None = None,
    author: str | None = None,
) -> ApplyResult:
    """누가 질의할 수 있는지를 고친다 (options 의 listen-on / allow-query / recursion).

    "zone 은 만들었는데 외부에서 응답이 없다" 의 가장 흔한 원인이다.
    전달자와 같은 파일에 쓴다 — options 안에 include 된 쓰기 가능한 파일을 우선한다.
    """
    wanted = [
        keyword
        for keyword, values in (
            ("listen-on", listen_on),
            ("listen-on-v6", listen_on_v6),
            ("allow-query", allow_query),
            ("allow-recursion", allow_recursion),
            ("recursion", recursion),
        )
        if values is not None
    ]
    if not wanted:
        raise ApplyError("바꿀 값이 없습니다.")

    path = options_write_target_for(cfg, wanted)
    text = path.read_text(encoding="utf-8")
    fileaudit.record(path, "read", "options-conf", detail="수신·질의 설정 편집")
    inside_options = path == options_file(cfg)

    changes: list[str] = []

    def statement(keyword: str, values: list[str] | None) -> str | None:
        if values is None:
            return None
        if not values:
            return None  # 빈 목록이면 지운다 → BIND 기본값으로 돌아간다
        changes.append(f"{keyword}={', '.join(values)}")
        # listen-on 은 포트 표기를 유지해야 한다
        if keyword in {"listen-on", "listen-on-v6"}:
            return f"{keyword} port 53 {{ " + " ".join(f"{v};" for v in values) + " };"
        return confedit.render_address_list(keyword, values)

    if inside_options:
        new_text = text
        for keyword, values in (
            ("listen-on", listen_on),
            ("listen-on-v6", listen_on_v6),
            ("allow-query", allow_query),
            ("allow-recursion", allow_recursion),
        ):
            if values is not None:
                new_text = confedit.set_option(new_text, keyword, statement(keyword, values))
        if recursion is not None:
            changes.append(f"recursion={recursion}")
            new_text = confedit.set_option(
                new_text, "recursion", f"recursion {recursion};" if recursion else None
            )
    else:
        # options 안에 include 된 전용 파일 — 전달자 설정과 함께 쓰므로 기존 내용을 보존한다.
        new_text = text
        for keyword, values in (
            ("listen-on", listen_on),
            ("listen-on-v6", listen_on_v6),
            ("allow-query", allow_query),
            ("allow-recursion", allow_recursion),
        ):
            if values is None:
                continue
            line = statement(keyword, values)
            new_text = _replace_bare_statement(new_text, keyword, line)
        if recursion is not None:
            changes.append(f"recursion={recursion}")
            new_text = _replace_bare_statement(
                new_text, "recursion", f"recursion {recursion};" if recursion else None
            )

    if not changes:
        raise ApplyError("바꿀 값이 없습니다.")

    return apply_mod.apply_conf_text(
        cfg, new_text, path=path, author=author, summary="수신·질의 설정 변경: " + ", ".join(changes)
    )


def _replace_bare_statement(text: str, keyword: str, statement: str | None) -> str:
    """include 파일(블록 밖)에 있는 문 하나를 교체·추가·삭제한다."""
    import re

    pattern = re.compile(rf"^\s*{re.escape(keyword)}\b.*?;\s*$", re.MULTILINE | re.DOTALL)
    if pattern.search(text):
        return pattern.sub(statement if statement else "", text, count=1).replace("\n\n\n", "\n\n")
    if statement is None:
        return text
    return text.rstrip("\n") + f"\n{statement}\n"


def set_conditional_forwarder(
    cfg: Config,
    domain: str,
    forwarders: list[str],
    policy: str = "only",
    *,
    author: str | None = None,
) -> ApplyResult:
    """도메인별 전달자를 추가하거나 갱신한다 (`type forward` zone)."""
    if not forwarders:
        raise ApplyError("전달자 IP 를 하나 이상 입력하세요.")
    zone = zonetemplate.validate_zone_name(domain)

    existing = service.get_layout(cfg).get(zone)
    if existing is not None and not existing.is_forwarder:
        raise ApplyError(f"'{zone}' 은 이미 {existing.zone_type} zone 으로 정의되어 있습니다.")

    conf_path = Path(cfg.bind.zones_conf)
    text = conf_path.read_text(encoding="utf-8")
    block = confedit.render_zone_block(
        zone, "forward", forwarders=forwarders, forward_policy=policy, comment="dns_manager — 조건부 전달자"
    )
    new_text = (
        confedit.replace_zone_block(text, zone, block)
        if confedit.has_zone_block(text, zone)
        else confedit.add_zone_block(text, block)
    )
    action = "변경" if existing is not None else "추가"
    return apply_mod.apply_conf_text(
        cfg, new_text, author=author, summary=f"조건부 전달자 {action}: {zone} → {', '.join(forwarders)}"
    )


def delete_conditional_forwarder(cfg: Config, domain: str, *, author: str | None = None) -> ApplyResult:
    zone = domain.strip().rstrip(".")
    entry = service.get_layout(cfg).get(zone)
    if entry is None:
        raise ApplyError(f"전달자를 찾을 수 없습니다: {zone}")
    if not entry.is_forwarder:
        raise ApplyError(f"'{zone}' 은 조건부 전달자가 아닙니다 (type={entry.zone_type}).")

    conf_path = entry.source_file or Path(cfg.bind.zones_conf)
    text = conf_path.read_text(encoding="utf-8")
    return apply_mod.apply_conf_text(
        cfg,
        confedit.remove_zone_block(text, zone),
        path=conf_path,
        author=author,
        summary=f"조건부 전달자 삭제: {zone}",
    )


def update_zone_options(
    cfg: Config,
    zone: str,
    *,
    allow_update: list[str] | None = None,
    allow_transfer: list[str] | None = None,
    allow_query: list[str] | None = None,
    also_notify: list[str] | None = None,
    masters: list[str] | None = None,
    author: str | None = None,
) -> ApplyResult:
    """zone Properties 의 Zone Transfers / Dynamic updates 등을 고친다.

    named.conf 의 zone 블록을 통째로 다시 쓰되, 바꾸지 않은 항목은 현재 값을 유지한다.
    """
    entry = service.find_zone(cfg, zone)
    conf_path = entry.source_file or Path(cfg.bind.zones_conf)
    try:
        text = conf_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApplyError(f"설정 파일을 읽을 수 없습니다: {conf_path} ({exc.strerror})") from exc
    if not confedit.has_zone_block(text, entry.name):
        raise ApplyError(f"'{entry.name}' 의 정의를 {conf_path} 에서 찾지 못했습니다.")

    block = confedit.render_zone_block(
        entry.name,
        entry.zone_type,
        file=entry.file.name if entry.file else None,
        masters=list(masters if masters is not None else entry.masters),
        forwarders=list(entry.forwarders),
        forward_policy=entry.forward_policy,
        allow_update=list(allow_update if allow_update is not None else entry.allow_update) or ["none"],
        allow_transfer=list(allow_transfer if allow_transfer is not None else entry.allow_transfer) or ["none"],
        allow_query=list(allow_query) if allow_query else None,
        also_notify=list(also_notify if also_notify is not None else entry.also_notify),
        comment="dns_manager",
    )
    return apply_mod.apply_conf_text(
        cfg,
        confedit.replace_zone_block(text, entry.name, block),
        path=conf_path,
        author=author,
        summary=f"zone 설정 변경: {entry.name}",
    )
