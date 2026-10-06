# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""설정 경로 점검과 저장.

자동 감지가 닿지 않는 배치(소스 설치, chroot, 사내 커스텀 경로)에서는 운영자가 직접
경로를 넣어야 한다. 그때 "무엇이 왜 부족한지" 를 분명히 보여주는 것이 이 모듈의 일이다.

쓰기 권한까지 확인하는 이유: 원자적 교체는 **디렉터리** 쓰기 권한을 요구한다.
파일만 쓰기 가능한 상태로 운영하면 적용 단계에서야 실패한다.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig, BindConfig, Config
from . import detect, fileaudit
from .commands import which

# 설정 화면에서 입력받는 경로들
PATH_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("named_conf", "named.conf", "BIND 주 설정 파일"),
    ("zones_conf", "zone 정의 파일", "zone 블록을 추가·삭제할 include 파일 (Debian 은 named.conf.local)"),
    ("zone_dir", "zone 디렉터리", "zone 파일이 놓이는 디렉터리 (비우면 options.directory 사용)"),
    ("rndc_key", "rndc 키", "비우면 rndc 기본 설정을 사용"),
    ("chroot", "chroot 경로", "chroot 로 운영할 때만 입력"),
)

BINARIES: tuple[tuple[str, str, bool], ...] = (
    ("named_checkzone", "zone 파일 문법 검증", True),
    ("named_checkconf", "설정 문법 검증", True),
    ("rndc", "reload / reconfig / 상태 조회", True),
    ("dig", "질의 확인 (nslookup 패널)", False),
)


@dataclass(frozen=True)
class PathStatus:
    key: str
    label: str
    description: str
    value: str | None
    exists: bool = False
    readable: bool = False
    writable: bool = False
    is_dir: bool = False
    required: bool = True
    ok: bool = False
    note: str = ""


@dataclass(frozen=True)
class ToolStatus:
    key: str
    label: str
    value: str
    found: str | None
    required: bool
    ok: bool


@dataclass(frozen=True)
class SettingsReport:
    paths: list[PathStatus]
    tools: list[ToolStatus]
    detected_family: str | None
    detected: dict[str, str]
    config_path: str | None
    config_writable: bool
    zone_suffix: str
    reverse_suffix: str
    config_warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return all(p.ok for p in self.paths if p.required) and all(t.ok for t in self.tools if t.required)

    @property
    def problems(self) -> list[str]:
        out = list(self.config_warnings)
        out += [f"{p.label}: {p.note}" for p in self.paths if p.required and not p.ok]
        out += [f"{t.label}: '{t.value}' 를 찾을 수 없습니다." for t in self.tools if t.required and not t.ok]
        return out


def _dir_writable(path: Path) -> bool:
    """디렉터리에 임시 파일을 만들 수 있는지 실제로 시험한다(권한 비트만으로는 부족하다)."""
    if not path.is_dir():
        return False
    try:
        fd, name = tempfile.mkstemp(dir=path, prefix=".dns-manager-check.")
        os.close(fd)
        Path(name).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _check_path(key: str, label: str, description: str, value: Path | None) -> PathStatus:
    required = key in {"named_conf", "zones_conf"}
    if value is None:
        return PathStatus(
            key=key,
            label=label,
            description=description,
            value=None,
            required=required,
            ok=not required,
            note="미설정" if not required else "경로를 입력하세요.",
        )

    path = Path(value)
    try:
        exists = path.exists()
        is_dir = path.is_dir()
    except OSError:
        # 상위 디렉터리에 접근 권한이 없으면 들여다볼 수조차 없다.
        return PathStatus(
            key=key, label=label, description=description, value=str(path),
            required=required, ok=False,
            note="접근 권한이 없어 확인할 수 없습니다. 실행 계정 권한을 확인하세요.",
        )
    readable = os.access(path, os.R_OK) if exists else False
    writable = _dir_writable(path) if is_dir else (_dir_writable(path.parent) and os.access(path, os.W_OK)) if exists else False

    note = ""
    ok = True
    if not exists:
        ok = not required
        note = "파일이 없습니다." if not required else "파일이 없습니다. 경로를 확인하세요."
    elif not readable:
        ok = False
        note = "읽을 수 없습니다. 실행 계정이 named 그룹인지 확인하세요."
    elif key in {"zones_conf", "zone_dir"} and not writable:
        ok = False
        note = (
            "쓸 수 없습니다. 원자적 교체는 디렉터리 쓰기 권한이 필요합니다 "
            f"(chgrp named {path.parent if not is_dir else path} && chmod g+w 같은 조치)."
        )
    return PathStatus(
        key=key,
        label=label,
        description=description,
        value=str(path),
        exists=exists,
        readable=readable,
        writable=writable,
        is_dir=is_dir,
        required=required,
        ok=ok,
        note=note,
    )


def report(cfg: Config) -> SettingsReport:
    bind = cfg.bind
    values: dict[str, Path | None] = {
        "named_conf": Path(bind.named_conf),
        "zones_conf": Path(bind.zones_conf),
        "zone_dir": bind.zone_dir,
        "rndc_key": bind.rndc_key,
        "chroot": bind.chroot,
    }
    paths = [_check_path(key, label, desc, values[key]) for key, label, desc in PATH_FIELDS]

    tools = []
    for key, label, required in BINARIES:
        value = getattr(bind, key)
        found = value if Path(value).is_absolute() and Path(value).exists() else which(value)
        tools.append(
            ToolStatus(key=key, label=label, value=value, found=found, required=required, ok=bool(found))
        )

    profile = detect.detect()
    detected = (
        {
            "family": profile.family,
            "named_conf": str(profile.named_conf),
            "zones_conf": str(profile.zones_conf),
            "zone_dir": str(profile.zone_dir),
            "rndc_key": str(profile.rndc_key) if profile.rndc_key else "",
            "zones_conf_included": "yes" if profile.zones_conf_included else "no",
        }
        if profile
        else {}
    )

    config_path = cfg.source
    writable = bool(config_path and _dir_writable(Path(config_path).parent) and os.access(config_path, os.W_OK))

    return SettingsReport(
        paths=paths,
        tools=tools,
        detected_family=cfg.detected_family or (profile.family if profile else None),
        detected=detected,
        config_path=str(config_path) if config_path else None,
        config_writable=writable,
        zone_suffix=bind.zone_suffix,
        reverse_suffix=bind.reverse_suffix,
        config_warnings=cfg.warnings,
    )


# --------------------------- 저장 ---------------------------


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_toml(bind: dict[str, object], app: dict[str, object]) -> str:
    lines = ["# dns_manager 설정 — 앱의 Settings 화면에서 저장됨", ""]
    for section, values in (("bind", bind), ("app", app)):
        if not values:
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            if value in (None, ""):
                continue
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def save(cfg: Config, updates: dict[str, object], *, config_path: Path | None = None) -> Path:
    """설정 파일에 값을 병합해 저장한다. 파일이 없으면 만든다."""
    import tomllib

    target = Path(config_path or cfg.source or "/etc/dns-manager/config.toml")
    existing: dict[str, dict[str, object]] = {"bind": {}, "app": {}}
    if target.is_file():
        with target.open("rb") as fh:
            loaded = tomllib.load(fh)
        existing["bind"].update(loaded.get("bind") or {})
        existing["app"].update(loaded.get("app") or {})

    for key, value in updates.items():
        section = "app" if key in {"host", "port", "state_dir", "backup_dir", "advanced_view_default", "user_header"} else "bind"
        if value in (None, ""):
            existing[section].pop(key, None)
        else:
            existing[section][key] = value

    text = render_toml(existing["bind"], existing["app"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".toml.new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    fileaudit.record(target, "write", "app-config", detail="Settings 저장")
    return target
