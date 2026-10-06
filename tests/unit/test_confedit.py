# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""설정 블록 편집 테스트 — 건드리지 않은 설정은 그대로 남아야 한다."""

from __future__ import annotations

import pytest

from dns_manager.core import confedit
from dns_manager.core.confedit import BlockNotFound

ZONES_CONF = '''// dns_manager 관리 대상
// 두 번째 주석 줄

zone "example.local" IN {
    type master;
    file "example.local.zone";
    allow-update { none; };
};

// 조건부 전달자
zone "partner.example" IN {
    type forward;
    forward only;
    forwarders { 192.168.2.1; 192.168.2.2; };
};

zone "10.168.192.in-addr.arpa" IN {
    type master;
    file "10.168.192.in-addr.arpa.rev";
};
'''

NAMED_CONF = '''options {
\tlisten-on port 53 { 127.0.0.1; };
\tdirectory "/var/named";
\t// 주석 안의 중괄호 { 는 무시되어야 한다
\tallow-query { localhost; };
};

logging {
\tchannel default_debug {
\t\tfile "data/named.run";
\t};
};

include "/etc/named/zones.conf";
'''


def test_top_level_blocks(the=None):
    blocks = confedit.top_level_blocks(ZONES_CONF)
    assert [b.name for b in blocks] == ["example.local", "partner.example", "10.168.192.in-addr.arpa"]
    assert all(b.keyword == "zone" for b in blocks)


def test_find_zone_block_ignores_trailing_dot_and_case():
    assert confedit.find_zone_block(ZONES_CONF, "Example.Local.").name == "example.local"


def test_missing_zone_block_raises():
    with pytest.raises(BlockNotFound):
        confedit.find_zone_block(ZONES_CONF, "nope.local")


def test_remove_zone_block_takes_its_comment(the=None):
    out = confedit.remove_zone_block(ZONES_CONF, "partner.example")
    assert "partner.example" not in out
    assert "// 조건부 전달자" not in out
    # 나머지는 보존
    assert "example.local" in out and "10.168.192.in-addr.arpa" in out
    assert out.startswith("// dns_manager 관리 대상")


def test_remove_does_not_eat_unrelated_comments():
    out = confedit.remove_zone_block(ZONES_CONF, "example.local")
    assert "// dns_manager 관리 대상" in out
    assert "// 조건부 전달자" in out


def test_add_zone_block():
    block = confedit.render_zone_block("new.local", "master", file="new.local.zone")
    out = confedit.add_zone_block(ZONES_CONF, block)
    assert confedit.has_zone_block(out, "new.local")
    assert out.endswith("};\n")


def test_replace_zone_block():
    block = confedit.render_zone_block(
        "partner.example", "forward", forwarders=["10.1.1.1"], forward_policy="first"
    )
    out = confedit.replace_zone_block(ZONES_CONF, "partner.example", block)
    assert "10.1.1.1" in out and "192.168.2.1" not in out
    assert "example.local" in out


def test_options_parsing_ignores_braces_in_comments():
    options = confedit.find_options(NAMED_CONF)
    assert options.keyword == "options"
    assert 'directory "/var/named";' in options.body


def test_set_option_inserts_into_options():
    out = confedit.set_option(NAMED_CONF, "forwarders", "forwarders { 8.8.8.8; };")
    assert "forwarders { 8.8.8.8; };" in out
    assert confedit.find_options(out)  # 여전히 유효한 구조
    assert 'directory "/var/named";' in out
    assert "logging {" in out


def test_set_option_replaces_existing():
    once = confedit.set_option(NAMED_CONF, "forwarders", "forwarders { 8.8.8.8; };")
    twice = confedit.set_option(once, "forwarders", "forwarders { 1.1.1.1; };")
    assert twice.count("forwarders") == 1
    assert "1.1.1.1" in twice and "8.8.8.8" not in twice


def test_set_option_removes():
    once = confedit.set_option(NAMED_CONF, "forwarders", "forwarders { 8.8.8.8; };")
    removed = confedit.set_option(once, "forwarders", None)
    assert "forwarders" not in removed
    assert removed.count("\n\n") == NAMED_CONF.count("\n\n"), "빈 줄 구조가 바뀌면 안 된다"


def test_set_option_keeps_indentation():
    out = confedit.set_option(NAMED_CONF, "forwarders", "forwarders { 8.8.8.8; };")
    line = next(l for l in out.splitlines() if "forwarders" in l)
    assert line.startswith("\t"), f"들여쓰기가 유지되어야 한다: {line!r}"


def test_render_zone_block_forward():
    block = confedit.render_zone_block(
        "corp.example", "forward", forwarders=["10.9.8.7", "10.9.8.8"], forward_policy="only"
    )
    assert 'zone "corp.example" IN {' in block
    assert "type forward;" in block
    assert "forwarders { 10.9.8.7; 10.9.8.8; };" in block
    assert "forward only;" in block


def test_render_zone_block_master_defaults():
    block = confedit.render_zone_block("a.local", "master", file="a.local.zone", allow_update=["none"])
    assert 'file "a.local.zone";' in block
    assert "allow-update { none; };" in block


def test_quoted_braces_do_not_break_scanning():
    text = 'zone "a{b}.local" IN {\n    type master;\n    file "x.zone";\n};\n'
    blocks = confedit.top_level_blocks(text)
    assert len(blocks) == 1 and blocks[0].name == "a{b}.local"
