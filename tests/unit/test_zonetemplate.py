# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""New Zone 마법사의 템플릿 생성 테스트."""

from __future__ import annotations

import pytest

from dns_manager.core import zonetemplate as zt
from dns_manager.core.zonetemplate import TemplateError


def test_network_id_in_forward_order():
    """Windows 와 동일하게 정방향 순서로 입력받아 역방향 zone 이름을 만든다."""
    assert zt.network_to_zone("192.168.220") == "220.168.192.in-addr.arpa"
    assert zt.network_to_zone("10") == "10.in-addr.arpa"


def test_network_id_with_cidr():
    assert zt.network_to_zone("192.168.10.0/24") == "10.168.192.in-addr.arpa"
    assert zt.network_to_zone("172.16.0.0/16") == "16.172.in-addr.arpa"
    assert zt.network_to_zone("10.0.0.0/8") == "10.in-addr.arpa"


def test_classless_cidr_is_rejected_with_guidance():
    with pytest.raises(TemplateError, match="RFC 2317"):
        zt.network_to_zone("192.168.10.0/25")


def test_invalid_network_id():
    for bad in ("", "999.1", "a.b.c", "1.2.3.4.5"):
        with pytest.raises(TemplateError):
            zt.network_to_zone(bad)


def test_responsible_from_email():
    assert zt.responsible_from_email("admin@example.local") == "admin.example.local."
    # 로컬 파트의 점은 이스케이프해야 한다 (SOA rname 규칙)
    assert zt.responsible_from_email("first.last@example.local") == "first\\.last.example.local."
    assert zt.responsible_from_email("hostmaster.example.local.") == "hostmaster.example.local."


def test_validate_zone_name():
    assert zt.validate_zone_name("Example.Local.") == "Example.Local"
    for bad in ("", "has space.local", "a/b", "..", "zone;name"):
        with pytest.raises(TemplateError):
            zt.validate_zone_name(bad)


def test_default_soa():
    soa = zt.default_soa("new.local", "ns1.example.local.", "admin@example.local")
    assert soa.primary == "ns1.example.local."
    assert soa.responsible == "admin.example.local."
    assert soa.serial > 2026000000


def test_render_zone_file_is_parsable():
    """만들어진 파일은 dnspython 이 바로 읽을 수 있어야 한다."""
    import dns.zone

    soa = zt.default_soa("new.local")
    text = zt.render_zone_file("new.local", soa, glue={"ns1": "192.168.10.10"})
    zone = dns.zone.from_text(text, origin="new.local", relativize=False)
    names = {n.to_text() for n in zone.nodes}
    assert "new.local." in names and "ns1.new.local." in names


def test_render_includes_ns_and_glue():
    soa = zt.default_soa("new.local", "ns1.new.local.")
    text = zt.render_zone_file("new.local", soa, glue={"ns1": "10.0.0.1"})
    assert "IN  NS      ns1.new.local." in text
    assert "10.0.0.1" in text
    assert "$TTL 3600" in text


def test_render_ipv6_glue_uses_aaaa():
    soa = zt.default_soa("new.local")
    text = zt.render_zone_file("new.local", soa, glue={"ns1": "fd00::1"})
    assert "AAAA" in text


def test_in_zone_ns_without_glue_is_refused():
    """zone 안쪽 NS 에 주소가 없으면 named-checkzone 이 zone 을 적재하지 않는다.

    만들기 전에 막고 무엇이 필요한지 알려야 한다(실제 Debian 검증에서 드러난 결함).
    """
    with pytest.raises(TemplateError, match="glue"):
        zt.require_glue("new.local", ["ns1.new.local."], {})


def test_in_zone_ns_with_glue_is_accepted():
    zt.require_glue("new.local", ["ns1.new.local."], {"ns1": "10.0.0.1"})
    zt.require_glue("new.local", ["ns1.new.local."], {"ns1.new.local.": "10.0.0.1"})


def test_out_of_zone_ns_needs_no_glue():
    zt.require_glue("new.local", ["ns1.infra.example."], {})


def test_default_primary_ns_prefers_out_of_zone_name():
    """기본 네임서버는 zone 밖 이름이어야 glue 없이도 유효하다."""
    ns = zt.default_primary_ns("new.local")
    assert ns.endswith(".")
    assert not zt.is_inside(ns, "new.local") or ns == "ns1.new.local."


def test_is_inside():
    assert zt.is_inside("ns1.new.local", "new.local")
    assert zt.is_inside("new.local.", "new.local")
    assert not zt.is_inside("ns1.other.local", "new.local")
