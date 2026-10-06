# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""RFC 2136 동적 갱신 테스트 (네트워크 없이 요청 구성만 검증)."""

from __future__ import annotations

from pathlib import Path

import dns.rcode
import pytest

from dns_manager.config import BindConfig, Config
from dns_manager.core import dynamic
from dns_manager.core.dynamic import DynamicError
from dns_manager.core.layout import ZoneEntry

KEY_FILE = '''
key "rndc-key" {
	algorithm hmac-sha256;
	secret "cmVhbGx5LXNlY3JldC12YWx1ZQ==";
};

key "ddns-key" {
	algorithm hmac-sha512;
	secret "ZGRucy1rZXktc2VjcmV0LXZhbHVlLWhlcmU=";
};
'''


def test_parse_key_file_first_key():
    key = dynamic.parse_key_file(KEY_FILE)
    assert key is not None and key.name == "rndc-key"
    assert key.algorithm == "hmac-sha256"


def test_parse_key_file_by_name():
    key = dynamic.parse_key_file(KEY_FILE, "ddns-key")
    assert key is not None and key.algorithm == "hmac-sha512"


def test_parse_key_file_missing_name():
    assert dynamic.parse_key_file(KEY_FILE, "nope") is None


def test_key_name_from_allow_update():
    entry = ZoneEntry(name="d.local", zone_type="master", view=None, file=Path("/tmp/x"),
                      allow_update=("key", "ddns-key"))
    assert dynamic.key_name_for(entry) == "ddns-key"


def test_key_name_from_joined_token():
    entry = ZoneEntry(name="d.local", zone_type="master", view=None, file=Path("/tmp/x"),
                      allow_update=('key "ddns-key"',))
    assert dynamic.key_name_for(entry) == "ddns-key"


def test_key_name_absent_for_plain_acl():
    entry = ZoneEntry(name="d.local", zone_type="master", view=None, file=Path("/tmp/x"),
                      allow_update=("10.0.0.0/8",))
    assert dynamic.key_name_for(entry) is None


def test_load_key_reads_configured_key_file(tmp_path: Path):
    key_file = tmp_path / "rndc.key"
    key_file.write_text(KEY_FILE, encoding="utf-8")
    cfg = Config(bind=BindConfig(rndc_key=key_file, named_conf=tmp_path / "named.conf"))
    entry = ZoneEntry(name="d.local", zone_type="master", view=None, file=tmp_path / "d.zone",
                      allow_update=("key", "ddns-key"))
    assert dynamic.load_key(cfg, entry).name == "ddns-key"


def test_load_key_error_mentions_key_name(tmp_path: Path):
    cfg = Config(bind=BindConfig(rndc_key=None, named_conf=tmp_path / "named.conf"))
    (tmp_path / "named.conf").write_text("options { };\n", encoding="utf-8")
    entry = ZoneEntry(name="d.local", zone_type="master", view=None, file=tmp_path / "d.zone",
                      allow_update=("key", "ddns-key"))
    with pytest.raises(DynamicError, match="ddns-key"):
        dynamic.load_key(cfg, entry)


def _cfg_with_key(tmp_path: Path) -> tuple[Config, ZoneEntry]:
    key_file = tmp_path / "rndc.key"
    key_file.write_text(KEY_FILE, encoding="utf-8")
    cfg = Config(bind=BindConfig(rndc_key=key_file, named_conf=tmp_path / "named.conf"))
    entry = ZoneEntry(name="dyn.local", zone_type="master", view=None, file=tmp_path / "dyn.zone",
                      allow_update=("key", "ddns-key"))
    return cfg, entry


class _FakeResponse:
    def __init__(self, rcode: int = 0) -> None:
        self._rcode = rcode

    def rcode(self) -> int:
        return self._rcode


def test_add_record_builds_signed_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, entry = _cfg_with_key(tmp_path)
    captured = {}

    def fake_tcp(message, where, timeout=None):
        captured["message"] = message
        captured["where"] = where
        return _FakeResponse(0)

    monkeypatch.setattr("dns.query.tcp", fake_tcp)
    assert dynamic.add_record(cfg, entry, "www", "A", "10.0.0.1", 300) == "NOERROR"

    message = captured["message"]
    assert captured["where"] == "127.0.0.1"
    assert message.keyring is not None, "TSIG 서명 없이 보내면 서버가 거절한다"
    text = message.to_text()
    # dnspython 은 UPDATE 메시지에서 이름을 zone 기준 상대 표기로 쓴다(와이어는 절대 이름)
    assert "dyn.local. IN SOA" in text, "ZONE 절에 대상 zone 이 있어야 한다"
    assert "www 300 IN A 10.0.0.1" in text


def test_delete_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, entry = _cfg_with_key(tmp_path)
    captured = {}
    monkeypatch.setattr("dns.query.tcp", lambda m, w, timeout=None: (captured.update(m=m), _FakeResponse(0))[1])
    dynamic.delete_record(cfg, entry, "www", "A", "10.0.0.1")
    text = captured["m"].to_text()
    assert "www 0 NONE A 10.0.0.1" in text, "삭제는 class NONE 으로 표현된다"


def test_replace_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, entry = _cfg_with_key(tmp_path)
    captured = {}
    monkeypatch.setattr("dns.query.tcp", lambda m, w, timeout=None: (captured.update(m=m), _FakeResponse(0))[1])
    dynamic.replace_record(cfg, entry, "www", "A", "10.0.0.2", 60)
    assert "10.0.0.2" in captured["m"].to_text()


def test_refused_update_raises_with_rcode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, entry = _cfg_with_key(tmp_path)
    monkeypatch.setattr("dns.query.tcp", lambda m, w, timeout=None: _FakeResponse(dns.rcode.REFUSED))
    with pytest.raises(DynamicError, match="REFUSED"):
        dynamic.add_record(cfg, entry, "www", "A", "10.0.0.1")


def test_invalid_rdata_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, entry = _cfg_with_key(tmp_path)
    monkeypatch.setattr("dns.query.tcp", lambda m, w, timeout=None: _FakeResponse(0))
    with pytest.raises(DynamicError, match="올바르지 않습니다"):
        dynamic.add_record(cfg, entry, "www", "A", "999.999.999.999")


def test_network_failure_is_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import dns.exception

    cfg, entry = _cfg_with_key(tmp_path)

    def boom(*a, **kw):
        raise dns.exception.Timeout()

    monkeypatch.setattr("dns.query.tcp", boom)
    with pytest.raises(DynamicError, match="보내지 못했습니다"):
        dynamic.add_record(cfg, entry, "www", "A", "10.0.0.1")


def test_key_found_in_included_file(tmp_path: Path):
    """키는 named.conf 가 include 한 파일에 있는 경우가 많다 — 거기까지 찾아야 한다."""
    conf_dir = tmp_path / "etc" / "named"
    conf_dir.mkdir(parents=True)
    key_file = conf_dir / "ddns.key"
    key_file.write_text(KEY_FILE, encoding="utf-8")
    zones_conf = conf_dir / "zones.conf"
    zones_conf.write_text(f'include "{key_file}";\n', encoding="utf-8")
    named_conf = tmp_path / "etc" / "named.conf"
    named_conf.write_text(f'options {{ }};\ninclude "{zones_conf}";\n', encoding="utf-8")

    cfg = Config(bind=BindConfig(named_conf=named_conf, zones_conf=zones_conf, rndc_key=None))
    entry = ZoneEntry(name="dyn.local", zone_type="master", view=None, file=tmp_path / "dyn.zone",
                      allow_update=("key", "ddns-key"))
    assert dynamic.load_key(cfg, entry).name == "ddns-key"


def test_key_candidates_include_zones_conf_dir(tmp_path: Path):
    conf_dir = tmp_path / "etc" / "named"
    conf_dir.mkdir(parents=True)
    (conf_dir / "ddns.key").write_text(KEY_FILE, encoding="utf-8")
    named_conf = tmp_path / "etc" / "named.conf"
    named_conf.write_text("options { };\n", encoding="utf-8")
    cfg = Config(bind=BindConfig(named_conf=named_conf, zones_conf=conf_dir / "zones.conf"))
    assert any(p.name == "ddns.key" for p in dynamic.key_candidates(cfg))
