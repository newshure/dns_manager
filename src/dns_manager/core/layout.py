# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""BIND 레이아웃 탐색 — 어떤 zone 이 어디에 정의되어 있는지 알아낸다.

zone 의 진실(source of truth)은 named.conf 와 zone 파일 자체다. 앱은 이를 DB 에
미러링하지 않고 매 요청 시 읽는다(외부 수동 편집과 불일치하지 않기 위함).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import BindConfig
from . import fileaudit, named_conf
from .commands import CommandError, CommandResult, run

# Windows DNS Manager 의 Forward / Reverse Lookup Zones 분류에 대응
REVERSE_SUFFIXES = (".in-addr.arpa", ".ip6.arpa")

# 트리에서 Conditional Forwarders 로 보여줄 zone 종류
FORWARD_ZONE_TYPES = frozenset({"forward"})
# 내부적으로 관리 대상에서 제외하는 BIND 내장 zone
BUILTIN_ZONES = frozenset({".", "localhost", "localhost.localdomain", "0.in-addr.arpa"})
# BIND 패키지가 기본 제공하는 zone 파일. 이 파일을 쓰는 zone(루트 힌트, localhost 정/역방향,
# 빈 zone)은 운영자가 편집할 대상이 아니므로 트리에서 감춘다.
BUILTIN_ZONE_FILES = frozenset(
    {"named.ca", "named.root", "named.localhost", "named.loopback", "named.empty"}
)


@dataclass(frozen=True)
class ZoneEntry:
    """named.conf 에 정의된 zone 하나의 메타데이터."""

    name: str
    zone_type: str  # master / slave / stub / forward / hint / redirect
    view: str | None
    file: Path | None
    masters: tuple[str, ...] = ()
    forwarders: tuple[str, ...] = ()
    forward_policy: str | None = None  # forward only | forward first
    allow_update: tuple[str, ...] = ()
    allow_transfer: tuple[str, ...] = ()
    allow_query: tuple[str, ...] = ()
    also_notify: tuple[str, ...] = ()
    update_policy: bool = False
    inline_signing: bool = False
    dnssec_policy: str | None = None
    auto_dnssec: str | None = None
    # 이 zone 블록이 실제로 들어 있는 설정 파일. named-checkconf -p 는 include 를 전개해
    # 출력하므로 출처가 사라진다 — 지우거나 고치려면 어느 파일인지 알아야 한다.
    source_file: Path | None = None

    @property
    def is_reverse(self) -> bool:
        lowered = self.name.lower().rstrip(".")
        return lowered.endswith(REVERSE_SUFFIXES)

    @property
    def is_forwarder(self) -> bool:
        return self.zone_type in FORWARD_ZONE_TYPES

    @property
    def is_builtin(self) -> bool:
        if self.name.lower().rstrip(".") in BUILTIN_ZONES or self.zone_type in {"hint", "redirect"}:
            return True
        return self.file is not None and self.file.name in BUILTIN_ZONE_FILES

    @property
    def is_signed(self) -> bool:
        """DNSSEC 서명 zone 여부. 서명 zone 은 v1 에서 읽기 전용으로 취급한다."""
        return self.inline_signing or bool(self.dnssec_policy and self.dnssec_policy != "none") or bool(self.auto_dnssec)

    @property
    def dynamic(self) -> bool:
        """동적 갱신이 허용된 zone. journal 때문에 파일 직접 편집이 위험하다."""
        if self.update_policy:
            return True
        return bool(self.allow_update) and tuple(self.allow_update) != ("none",)

    @property
    def editable(self) -> bool:
        """zone 파일 편집으로 관리할 수 있는지."""
        return self.zone_type == "master" and self.file is not None and not self.is_signed

    @property
    def category(self) -> str:
        """콘솔 트리 분류: forward / reverse / conditional_forwarder / builtin."""
        if self.is_builtin:
            return "builtin"
        if self.is_forwarder:
            return "conditional_forwarder"
        return "reverse" if self.is_reverse else "forward"


@dataclass(frozen=True)
class ServerAccess:
    """누가 이 서버에 질의할 수 있는가 — options 의 수신·접근 설정.

    "zone 은 만들었는데 왜 응답을 안 하지?" 의 가장 흔한 원인이다.
    Rocky 기본값은 listen-on 이 127.0.0.1, allow-query 가 localhost 라 외부에서 오는
    질의에 전혀 답하지 않는다. zone 을 어느 파일에 정의했는지와는 무관하다.
    """

    listen_on: tuple[str, ...] = ()
    listen_on_v6: tuple[str, ...] = ()
    allow_query: tuple[str, ...] = ()
    allow_recursion: tuple[str, ...] = ()
    recursion: str | None = None

    @staticmethod
    def _is_local_only(values: tuple[str, ...]) -> bool:
        if not values:
            return False
        local = {"127.0.0.1", "127.0.0.1/32", "::1", "::1/128", "localhost", "localnets"}
        return all(v.strip('"') in local for v in values)

    @property
    def listens_externally(self) -> bool:
        """외부 인터페이스에서 질의를 받는가. 설정이 없으면 BIND 기본값(any)이다."""
        if not self.listen_on and not self.listen_on_v6:
            return True
        return not (
            self._is_local_only(self.listen_on)
            and (not self.listen_on_v6 or self._is_local_only(self.listen_on_v6))
        )

    @property
    def answers_externally(self) -> bool:
        """외부에서 온 질의에 답하는가 (allow-query 기준). 설정이 없으면 any."""
        return not self._is_local_only(self.allow_query)

    @property
    def external_blockers(self) -> tuple[str, ...]:
        """외부 질의를 막고 있는 설정 이름. 비어 있으면 외부에서 쓸 수 있다."""
        blockers: list[str] = []
        if not self.listens_externally:
            blockers.append("listen-on")
        if not self.answers_externally:
            blockers.append("allow-query")
        return tuple(blockers)


@dataclass(frozen=True)
class ServerForwarding:
    """서버 전역 전달자 설정 (options { forwarders }).

    Windows DNS Manager 의 서버 Properties ▸ Forwarders 탭에 대응한다.
    도메인별 전달자(Conditional Forwarders)는 BIND 의 `type forward` zone 으로 표현되며
    ZoneEntry 쪽에서 다룬다.
    """

    forwarders: tuple[str, ...] = ()
    # forward only | forward first
    policy: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.forwarders)


@dataclass(frozen=True)
class Layout:
    """탐색된 BIND 레이아웃 전체."""

    directory: Path
    zones: tuple[ZoneEntry, ...]
    checkconf: CommandResult
    forwarding: ServerForwarding = ServerForwarding()
    access: ServerAccess = ServerAccess()

    def get(self, name: str, view: str | None = None) -> ZoneEntry | None:
        wanted = name.lower().rstrip(".")
        for zone in self.zones:
            if zone.name.lower().rstrip(".") != wanted:
                continue
            if view is not None and zone.view != view:
                continue
            return zone
        return None


def _parsed_config(cfg: BindConfig) -> CommandResult:
    """named-checkconf -p 로 include 가 전개된 설정을 얻는다."""
    argv = [cfg.named_checkconf, "-p"]
    if cfg.chroot:
        argv += ["-t", str(cfg.chroot)]
    argv.append(str(cfg.named_conf))
    return run(argv, timeout=cfg.command_timeout)


def _zone_file_path(cfg: BindConfig, directory: Path, raw_file: str | None) -> Path | None:
    if not raw_file:
        return None
    path = Path(raw_file)
    if not path.is_absolute():
        path = directory / path
    if cfg.chroot:
        path = cfg.chroot / path.relative_to("/")
    return path


def discover(cfg: BindConfig) -> Layout:
    """named.conf 를 읽어 zone 목록을 만든다.

    named-checkconf 가 실패하면(설정 오류·권한 부족) 예외를 던지는 대신 출력을 그대로
    담아 돌려준다 — UI 가 BIND 의 원문 오류를 보여줄 수 있어야 한다.
    """
    result = _parsed_config(cfg)
    fileaudit.record(
        cfg.named_conf, "read", "named-conf", detail="named-checkconf -p", ok=result.ok
    )
    if not result.ok:
        if not result.stdout.strip():
            raise CommandError(
                f"named.conf 를 읽을 수 없습니다 ({cfg.named_conf}). "
                f"named-checkconf 출력:\n{result.message}"
            )
    statements = named_conf.parse(result.stdout)

    options = named_conf.find_options(statements)
    directory = Path((options.value("directory") if options else None) or "/var/named")
    forwarding = ServerForwarding(
        forwarders=tuple(options.values("forwarders")) if options else (),
        policy=options.value("forward") if options else None,
    )
    access = ServerAccess(
        listen_on=tuple(options.values("listen-on")) if options else (),
        listen_on_v6=tuple(options.values("listen-on-v6")) if options else (),
        allow_query=tuple(options.values("allow-query")) if options else (),
        allow_recursion=tuple(options.values("allow-recursion")) if options else (),
        recursion=options.value("recursion") if options else None,
    )

    zones: list[ZoneEntry] = []
    for view, stmt in named_conf.iter_zones(statements):
        if len(stmt.tokens) < 2:
            continue
        name = stmt.tokens[1]
        zone_type = stmt.value("type") or ""
        zones.append(
            ZoneEntry(
                name=name,
                zone_type=zone_type,
                view=view,
                file=_zone_file_path(cfg, directory, stmt.value("file")),
                masters=tuple(stmt.values("masters") or stmt.values("primaries")),
                forwarders=tuple(stmt.values("forwarders")),
                forward_policy=stmt.value("forward"),
                allow_update=tuple(stmt.values("allow-update")),
                allow_transfer=tuple(stmt.values("allow-transfer")),
                allow_query=tuple(stmt.values("allow-query")),
                also_notify=tuple(stmt.values("also-notify")),
                update_policy=stmt.find("update-policy") is not None,
                inline_signing=(stmt.value("inline-signing") or "no").lower() in {"yes", "true"},
                dnssec_policy=stmt.value("dnssec-policy"),
                auto_dnssec=stmt.value("auto-dnssec"),
            )
        )

    zones = _attach_sources(cfg, zones)
    zones.sort(key=lambda z: (z.view or "", z.category, z.name.lower()))
    return Layout(
        directory=directory,
        zones=tuple(zones),
        checkconf=result,
        forwarding=forwarding,
        access=access,
    )


def _attach_sources(cfg: BindConfig, zones: list[ZoneEntry]) -> list[ZoneEntry]:
    """각 zone 이 어느 파일에 정의돼 있는지 찾아 붙인다.

    `named-checkconf -p` 는 include 를 전부 전개해 한 덩어리로 내보내므로 출처가 사라진다.
    출처를 모르면 "목록에는 보이는데 어디에도 없다" 는 상황에서 손쓸 수가 없고,
    zone 삭제도 특정 파일에만 기대게 된다.
    """
    from dataclasses import replace

    from . import confedit, detect

    root = Path(cfg.chroot) if cfg.chroot else None
    candidates = [Path(cfg.named_conf), *detect.includes_of(Path(cfg.named_conf), root=root)]

    texts: list[tuple[Path, str]] = []
    for path in candidates:
        try:
            texts.append((path, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue

    out: list[ZoneEntry] = []
    for entry in zones:
        source = None
        for path, text in texts:
            try:
                if confedit.has_zone_block(text, entry.name):
                    source = path
                    break
            except Exception:  # noqa: BLE001 - 출처 탐색이 목록 조회를 막아서는 안 된다
                continue
        out.append(replace(entry, source_file=source) if source else entry)
    return out
