"""zone 파일명 규칙 테스트 — 정방향 .zone / 역방향 .rev"""

from __future__ import annotations

import pytest

from dns_manager.config import BindConfig
from dns_manager.core import naming


@pytest.fixture
def cfg() -> BindConfig:
    return BindConfig()


def test_forward_zone_uses_zone_suffix(cfg: BindConfig):
    assert naming.default_file_name(cfg, "example.local") == "example.local.zone"


def test_reverse_ipv4_zone_uses_rev_suffix(cfg: BindConfig):
    assert naming.default_file_name(cfg, "10.168.192.in-addr.arpa") == "10.168.192.in-addr.arpa.rev"


def test_reverse_ipv6_zone_uses_rev_suffix(cfg: BindConfig):
    assert naming.default_file_name(cfg, "8.b.d.0.1.0.0.2.ip6.arpa") == "8.b.d.0.1.0.0.2.ip6.arpa.rev"


def test_trailing_dot_is_stripped(cfg: BindConfig):
    assert naming.default_file_name(cfg, "example.local.") == "example.local.zone"


def test_custom_suffixes_are_honoured():
    cfg = BindConfig(zone_suffix=".db", reverse_suffix=".ptr")
    assert naming.default_file_name(cfg, "example.local") == "example.local.db"
    assert naming.default_file_name(cfg, "10.in-addr.arpa") == "10.in-addr.arpa.ptr"


def test_path_traversal_is_rejected(cfg: BindConfig):
    for bad in ("../etc/passwd", "a/b", "..", "."):
        with pytest.raises(ValueError):
            naming.default_file_name(cfg, bad)


def test_empty_name_rejected(cfg: BindConfig):
    with pytest.raises(ValueError):
        naming.default_file_name(cfg, "  ")


def test_follows_convention(cfg: BindConfig):
    assert naming.follows_convention(cfg, "example.local", "/var/named/example.local.zone")
    assert naming.follows_convention(cfg, "10.168.192.in-addr.arpa", "/var/named/10.168.192.in-addr.arpa.rev")
    # 역방향인데 .zone 을 쓰고 있으면 규칙 위반(단, 편집은 계속 허용된다)
    assert not naming.follows_convention(cfg, "10.168.192.in-addr.arpa", "/var/named/10.168.192.in-addr.arpa.zone")


def test_is_reverse_zone(cfg: BindConfig):
    assert naming.is_reverse_zone("0.168.192.IN-ADDR.ARPA.")
    assert not naming.is_reverse_zone("arpa.example.com")
