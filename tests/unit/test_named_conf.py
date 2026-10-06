"""named.conf (named-checkconf -p 출력) 파서 테스트."""

from __future__ import annotations

from dns_manager.core import named_conf


def test_tokenize_strips_comments_and_keeps_structure():
    tokens = named_conf.tokenize('a { b; }; # comment\n// line\n/* block */ c "quoted value";')
    assert tokens == ["a", "{", "b", ";", "}", ";", "c", "quoted value", ";"]


def test_options_directory_and_forwarders(checkconf_text: str):
    statements = named_conf.parse(checkconf_text)
    options = named_conf.find_options(statements)
    assert options is not None
    assert options.value("directory") == "/var/named"
    assert options.values("forwarders") == ["192.168.2.1", "8.8.8.8"]
    assert options.value("forward") == "first"


def test_iter_zones_includes_view_scoped_zones(checkconf_text: str):
    statements = named_conf.parse(checkconf_text)
    found = named_conf.iter_zones(statements)
    names = {(view, stmt.tokens[1]) for view, stmt in found}
    assert (None, "example.local") in names
    assert ("internal", "split.local") in names


def test_zone_statement_values(checkconf_text: str):
    statements = named_conf.parse(checkconf_text)
    by_name = {stmt.tokens[1]: stmt for _, stmt in named_conf.iter_zones(statements)}
    assert by_name["example.local"].value("type") == "master"
    assert by_name["example.local"].values("allow-update") == ["none"]
    assert by_name["secondary.local"].values("masters") == ["192.168.2.50"]
    assert by_name["corp.partner.example"].values("forwarders") == ["10.9.8.7", "10.9.8.8"]
    assert by_name["corp.partner.example"].value("forward") == "only"


def test_parse_is_tolerant_of_listen_on_with_port(checkconf_text: str):
    """`listen-on port 53 { ... };` 처럼 토큰과 블록이 섞인 문도 깨지지 않아야 한다."""
    options = named_conf.find_options(named_conf.parse(checkconf_text))
    assert options is not None
    assert options.find("listen-on") is not None
