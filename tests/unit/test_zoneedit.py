"""레코드 단위 편집 테스트 — 핵심 요구는 '건드리지 않은 줄은 그대로'."""

from __future__ import annotations

from pathlib import Path

import pytest

from dns_manager.core import zoneedit
from dns_manager.core.zoneedit import RecordNotFound, ZoneEditError

ORIGIN = "example.local"

TRICKY = '''; 머리말 주석
$TTL 3600
@       IN  SOA ns1.example.local. admin.example.local. (
                2026100101  ; serial
                3600 600 604800 3600 )

; 웹 서버들
www         IN  A       192.168.10.21
            IN  A       192.168.10.22
www         IN  TXT     "a;b (not a comment)"
api     300 IN  A       192.168.10.31

$ORIGIN sub.example.local.
host1       IN  A       10.1.1.1
'''


@pytest.fixture
def zone_text(zones_dir: Path) -> str:
    return (zones_dir / "example.local.zone").read_text(encoding="utf-8")


def ids(text: str, origin: str = ORIGIN) -> dict[str, zoneedit.RecordLocation]:
    return {f"{loc.record.name}/{loc.record.rtype}/{loc.record.data}": loc for loc in zoneedit.index_records(text, origin)}


# --------------------------- 색인 ---------------------------


def test_index_finds_every_record(zone_text: str):
    locations = zoneedit.index_records(zone_text, ORIGIN)
    assert len(locations) == 12
    assert {loc.record.rtype for loc in locations} == {"SOA", "NS", "MX", "A", "AAAA", "CNAME", "TXT", "SRV", "CAA"}


def test_multiline_soa_span(zone_text: str):
    soa = next(loc for loc in zoneedit.index_records(zone_text, ORIGIN) if loc.record.rtype == "SOA")
    assert soa.end - soa.start == 2
    assert "SOA" in soa.raw and "3600 )" in soa.raw


def test_per_record_ttl_is_read(zone_text: str):
    api = next(loc for loc in zoneedit.index_records(zone_text, ORIGIN) if loc.record.name == "api")
    assert api.record.ttl == 300


def test_ids_match_the_read_path(zone_text: str, zones_dir: Path):
    """편집 색인의 id 와 조회 API 의 id 가 같아야 편집 대상을 지목할 수 있다."""
    from dns_manager.core import zonefile

    content = zonefile.load(zones_dir / "example.local.zone", ORIGIN)
    assert {r.id for r in content.records} == {loc.id for loc in zoneedit.index_records(zone_text, ORIGIN)}


def test_semicolon_inside_quotes_is_not_a_comment():
    locations = zoneedit.index_records(TRICKY, ORIGIN)
    txt = next(loc for loc in locations if loc.record.rtype == "TXT")
    assert "not a comment" in txt.record.data


def test_owner_inherited_from_previous_line():
    locations = zoneedit.index_records(TRICKY, ORIGIN)
    a_records = [loc for loc in locations if loc.record.rtype == "A" and loc.record.name == "www"]
    assert len(a_records) == 2
    assert {loc.record.data for loc in a_records} == {"192.168.10.21", "192.168.10.22"}


def test_origin_directive_is_honoured():
    locations = zoneedit.index_records(TRICKY, ORIGIN)
    host1 = next(loc for loc in locations if loc.record.data == "10.1.1.1")
    assert host1.record.fqdn == "host1.sub.example.local."


def test_unparsable_lines_are_skipped():
    text = "$TTL 60\n@ IN SOA ns1. a. (1 1 1 1 1)\nthis is garbage\nwww IN A 10.0.0.1\n"
    locations = zoneedit.index_records(text, ORIGIN)
    assert [loc.record.rtype for loc in locations] == ["SOA", "A"]


# --------------------------- 삭제 ---------------------------


def test_delete_removes_only_that_line(zone_text: str):
    target = ids(zone_text)["mail2/CNAME/mail.example.local."]
    out = zoneedit.delete_records(zone_text, ORIGIN, [target.id])

    assert "mail2" not in out
    # 나머지는 글자 하나 바뀌지 않아야 한다
    assert out == zone_text.replace(target.raw, "")


def test_delete_preserves_comments_and_blank_lines():
    target = ids(TRICKY)["api/A/192.168.10.31"]
    out = zoneedit.delete_records(TRICKY, ORIGIN, [target.id])
    assert "; 머리말 주석" in out
    assert "; 웹 서버들" in out
    assert "; serial" in out


def test_delete_multiple(zone_text: str):
    index = ids(zone_text)
    targets = [index["mail2/CNAME/mail.example.local."].id, index["api/A/192.168.10.31"].id]
    out = zoneedit.delete_records(zone_text, ORIGIN, targets)
    assert "mail2" not in out and "api" not in out
    assert "www" in out


def test_delete_keeps_owner_for_following_record():
    """소유자 이름을 제공하던 줄을 지우면, 다음 줄에 이름을 명시해 의미를 보존해야 한다."""
    first = ids(TRICKY)["www/A/192.168.10.21"]
    out = zoneedit.delete_records(TRICKY, ORIGIN, [first.id])

    remaining = [loc for loc in zoneedit.index_records(out, ORIGIN) if loc.record.rtype == "A" and loc.record.data == "192.168.10.22"]
    assert len(remaining) == 1
    assert remaining[0].record.name == "www", "이름이 유실되어 다른 레코드가 되어 버렸다"


def test_delete_soa_is_refused(zone_text: str):
    soa = next(loc for loc in zoneedit.index_records(zone_text, ORIGIN) if loc.record.rtype == "SOA")
    with pytest.raises(ZoneEditError, match="SOA"):
        zoneedit.delete_records(zone_text, ORIGIN, [soa.id])


def test_delete_unknown_id(zone_text: str):
    with pytest.raises(RecordNotFound):
        zoneedit.delete_records(zone_text, ORIGIN, ["deadbeefdeadbeef"])


# --------------------------- 교체 / 추가 ---------------------------


def test_replace_record(zone_text: str):
    target = ids(zone_text)["www/A/192.168.10.21"]
    line = zoneedit.format_record("www", 600, "A", "192.168.10.99")
    out = zoneedit.replace_record(zone_text, ORIGIN, target.id, line)

    index = ids(out)
    assert "www/A/192.168.10.99" in index
    assert "www/A/192.168.10.21" not in index
    assert index["www/A/192.168.10.99"].record.ttl == 600
    assert "; 테스트용 정방향 zone" in out


def test_replace_multiline_record_collapses_to_one(zone_text: str):
    """여러 줄 레코드를 바꿀 때 원래 줄 범위가 전부 교체되어야 한다."""
    soa = next(loc for loc in zoneedit.index_records(zone_text, ORIGIN) if loc.record.rtype == "SOA")
    line = zoneedit.format_record("@", None, "SOA", "ns1.example.local. admin.example.local. 2026100199 3600 600 604800 3600")
    out = zoneedit.replace_record(zone_text, ORIGIN, soa.id, line)
    assert out.count("SOA") == 1
    assert "604800" in out
    assert "; refresh" not in out


def test_append_record(zone_text: str):
    out = zoneedit.append_record(zone_text, zoneedit.format_record("new", None, "A", "10.0.0.7"))
    assert out.startswith(zone_text)
    assert "new/A/10.0.0.7" in ids(out)


def test_append_adds_newline_when_missing():
    out = zoneedit.append_record("www IN A 10.0.0.1", "x IN A 10.0.0.2\n")
    assert out == "www IN A 10.0.0.1\nx IN A 10.0.0.2\n"


# --------------------------- 정규화 / 검증 ---------------------------


def test_normalize_relative_name():
    record = zoneedit.normalize("www", "A", "192.168.10.21", ORIGIN)
    assert record.name == "www"
    assert record.fqdn == "www.example.local."


def test_normalize_apex():
    assert zoneedit.normalize("@", "TXT", '"hello"', ORIGIN).name == "@"


def test_normalize_rejects_bad_address():
    with pytest.raises(ZoneEditError, match="A 레코드"):
        zoneedit.normalize("www", "A", "999.999.999.999", ORIGIN)


def test_normalize_rejects_bad_type():
    with pytest.raises(ZoneEditError):
        zoneedit.normalize("www", "NOPE", "x", ORIGIN)


def test_normalize_mx_requires_preference():
    with pytest.raises(ZoneEditError):
        zoneedit.normalize("@", "MX", "mail.example.local.", ORIGIN)
    assert zoneedit.normalize("@", "MX", "10 mail.example.local.", ORIGIN).data.startswith("10 ")


def test_normalized_id_matches_index_id(zone_text: str):
    """정규화로 만든 id 가 색인의 id 와 같아야 '추가 후 즉시 수정'이 가능하다."""
    record = zoneedit.normalize("www", "A", "192.168.10.21", ORIGIN)
    assert record.id == ids(zone_text)["www/A/192.168.10.21"].id
