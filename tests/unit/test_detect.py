# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""배포판 BIND 레이아웃 감지 테스트 — 배포판 이름이 아니라 파일 위치로 판단한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from dns_manager.config import BindConfig
from dns_manager.core import detect

ROCKY_NAMED_CONF = """options {
\tdirectory "/var/named";
\tlisten-on port 53 { 127.0.0.1; };
};

zone "." IN {
\ttype hint;
\tfile "named.ca";
};

include "/etc/named.rfc1912.zones";
include "/etc/named/zones.conf";
"""

DEBIAN_NAMED_CONF = """include "/etc/bind/named.conf.options";
include "/etc/bind/named.conf.local";
include "/etc/bind/named.conf.default-zones";
"""

DEBIAN_OPTIONS = """options {
\tdirectory "/var/cache/bind";
\tdnssec-validation auto;
\tlisten-on-v6 { any; };
};
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def rocky(tmp_path: Path) -> Path:
    _write(tmp_path / "etc/named.conf", ROCKY_NAMED_CONF)
    _write(tmp_path / "etc/named.rfc1912.zones", 'zone "localhost" IN { type master; file "named.localhost"; };\n')
    _write(tmp_path / "etc/named/zones.conf", "// dns_manager\n")
    _write(tmp_path / "etc/rndc.key", 'key "rndc-key" { algorithm hmac-sha256; secret "x"; };\n')
    (tmp_path / "var/named").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def debian(tmp_path: Path) -> Path:
    _write(tmp_path / "etc/bind/named.conf", DEBIAN_NAMED_CONF)
    _write(tmp_path / "etc/bind/named.conf.options", DEBIAN_OPTIONS)
    _write(tmp_path / "etc/bind/named.conf.local", "// 사용자 zone\n")
    _write(tmp_path / "etc/bind/named.conf.default-zones", 'zone "." { type hint; file "/usr/share/dns/root.hints"; };\n')
    _write(tmp_path / "etc/bind/rndc.key", 'key "rndc-key" { algorithm hmac-sha256; secret "x"; };\n')
    (tmp_path / "var/cache/bind").mkdir(parents=True)
    return tmp_path


def test_detect_redhat_layout(rocky: Path):
    profile = detect.detect(root=rocky)
    assert profile is not None
    assert profile.family == "redhat"
    assert profile.named_conf == rocky / "etc/named.conf"
    assert profile.zone_dir == rocky / "var/named"
    assert profile.zones_conf == rocky / "etc/named/zones.conf"
    assert profile.zones_conf_included is True
    assert profile.rndc_key == rocky / "etc/rndc.key"


def test_detect_debian_layout(debian: Path):
    profile = detect.detect(root=debian)
    assert profile is not None
    assert profile.family == "debian"
    assert profile.named_conf == debian / "etc/bind/named.conf"
    assert profile.zone_dir == debian / "var/cache/bind"
    # Debian 은 배포판이 제공하는 named.conf.local 을 그대로 쓴다
    assert profile.zones_conf == debian / "etc/bind/named.conf.local"
    assert profile.zones_conf_included is True
    assert profile.rndc_key == debian / "etc/bind/rndc.key"


def test_options_file_is_named_conf_on_redhat(rocky: Path):
    found = detect.find_options_file(rocky / "etc/named.conf", root=rocky)
    assert found == rocky / "etc/named.conf"


def test_options_file_follows_include_on_debian(debian: Path):
    """Debian 은 options 가 named.conf 가 아니라 named.conf.options 에 있다."""
    found = detect.find_options_file(debian / "etc/bind/named.conf", root=debian)
    assert found == debian / "etc/bind/named.conf.options"


def test_includes_are_followed_recursively(rocky: Path):
    includes = detect.includes_of(rocky / "etc/named.conf", root=rocky)
    names = {p.name for p in includes}
    assert {"named.rfc1912.zones", "zones.conf"} <= names


def test_missing_include_files_are_skipped(tmp_path: Path):
    _write(tmp_path / "etc/named.conf", 'include "/nowhere/missing.conf";\noptions { directory "/var/named"; };\n')
    assert detect.includes_of(tmp_path / "etc/named.conf") == []
    assert detect.find_options_file(tmp_path / "etc/named.conf") == tmp_path / "etc/named.conf"


def test_detect_returns_none_without_bind(tmp_path: Path):
    assert detect.detect(root=tmp_path) is None


def test_zones_conf_not_included_is_reported(tmp_path: Path):
    """include 가 없으면 사용자가 추가해야 한다 — 조용히 넘어가면 안 된다."""
    _write(tmp_path / "etc/named.conf", 'options { directory "/var/named"; };\n')
    (tmp_path / "var/named").mkdir(parents=True)
    profile = detect.detect(root=tmp_path)
    assert profile is not None
    assert profile.zones_conf_included is False
    assert profile.zones_conf_exists is False


def test_apply_profile_fills_unset_values(rocky: Path):
    profile = detect.detect(root=rocky)
    cfg = detect.apply_profile(BindConfig(), profile, explicit=set())
    assert cfg.named_conf == rocky / "etc/named.conf"
    assert cfg.zones_conf == rocky / "etc/named/zones.conf"
    assert cfg.zone_dir == rocky / "var/named"


def test_apply_profile_respects_explicit_values(debian: Path):
    profile = detect.detect(root=debian)
    cfg = BindConfig(named_conf=Path("/custom/named.conf"))
    out = detect.apply_profile(cfg, profile, explicit={"named_conf"})
    assert out.named_conf == Path("/custom/named.conf")  # 사용자 지정은 유지
    assert out.zones_conf == debian / "etc/bind/named.conf.local"  # 나머지는 감지값


def test_detect_prefers_redhat_when_both_exist(tmp_path: Path):
    """두 레이아웃이 모두 있으면 /etc/named.conf 를 우선한다(후보 순서)."""
    _write(tmp_path / "etc/named.conf", ROCKY_NAMED_CONF)
    _write(tmp_path / "etc/bind/named.conf", DEBIAN_NAMED_CONF)
    (tmp_path / "var/named").mkdir(parents=True)
    profile = detect.detect(root=tmp_path)
    assert profile is not None and profile.family == "redhat"


def test_detect_survives_unreadable_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """권한 없는 경로를 만나도 감지가 죽으면 안 된다.

    일반 사용자로 띄웠을 때 /etc/named 접근 불가로 앱이 아예 기동하지 못한 적이 있다.
    """
    _write(tmp_path / "etc/named.conf", ROCKY_NAMED_CONF)
    (tmp_path / "var/named").mkdir(parents=True)

    real_is_file = Path.is_file

    def picky(self):
        if "zones.conf" in str(self):
            raise PermissionError(13, "Permission denied")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", picky)

    profile = detect.detect(root=tmp_path)
    assert profile is not None
    assert profile.family == "redhat"
    assert profile.zones_conf_exists is False  # 확인할 수 없으면 '없음' 으로 다룬다


def test_includes_skip_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write(tmp_path / "etc/named.conf", 'include "/etc/secret.conf";\noptions { };\n')
    real_is_file = Path.is_file
    monkeypatch.setattr(
        Path, "is_file",
        lambda self: (_ for _ in ()).throw(PermissionError(13, "nope")) if "secret" in str(self) else real_is_file(self),
    )
    assert detect.includes_of(tmp_path / "etc/named.conf", root=tmp_path) == []
