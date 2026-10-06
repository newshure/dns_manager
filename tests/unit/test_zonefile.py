"""zone 파일 읽기·파싱 테스트."""

from __future__ import annotations

from pathlib import Path

from dns_manager.core import zonefile
from dns_manager.core.records import APEX_LABEL


def test_load_example_zone(zones_dir: Path):
    content = zonefile.load(zones_dir / "example.local.zone", "example.local")
    assert content.parsable
    assert content.table_editable
    assert content.serial == 2026100101
    types = {r.rtype for r in content.records}
    assert {"SOA", "NS", "MX", "A", "AAAA", "CNAME", "TXT", "SRV", "CAA"} <= types


def test_soa_first_then_ns(zones_dir: Path):
    content = zonefile.load(zones_dir / "example.local.zone", "example.local")
    assert content.records[0].rtype == "SOA"
    assert content.records[1].rtype == "NS"


def test_apex_display_name(zones_dir: Path):
    content = zonefile.load(zones_dir / "example.local.zone", "example.local")
    soa = content.soa
    assert soa is not None
    assert soa.is_apex
    assert soa.display_name == APEX_LABEL


def test_per_record_ttl_preserved(zones_dir: Path):
    content = zonefile.load(zones_dir / "example.local.zone", "example.local")
    api = next(r for r in content.records if r.name == "api")
    assert api.ttl == 300
    www = next(r for r in content.records if r.name == "www" and r.rtype == "A")
    assert www.ttl == 3600


def test_mx_and_srv_fields_decomposed(zones_dir: Path):
    content = zonefile.load(zones_dir / "example.local.zone", "example.local")
    mx = next(r for r in content.records if r.rtype == "MX")
    assert mx.fields["Mail server priority"] == "20"
    srv = next(r for r in content.records if r.rtype == "SRV")
    assert srv.fields["Port number"] == "5060"


def test_record_id_is_stable_and_distinct(zones_dir: Path):
    content = zonefile.load(zones_dir / "example.local.zone", "example.local")
    ids = [r.id for r in content.records]
    assert len(ids) == len(set(ids))
    again = zonefile.load(zones_dir / "example.local.zone", "example.local")
    assert [r.id for r in again.records] == ids


def test_broken_zone_is_reported_not_raised(zones_dir: Path):
    content = zonefile.load(zones_dir / "broken.zone", "broken")
    assert not content.parsable
    assert content.parse_error
    assert content.records == ()


def test_generate_directive_blocks_table_editing(zones_dir: Path):
    content = zonefile.load(zones_dir / "generate.zone", "gen")
    assert "$GENERATE" in content.unsupported
    assert content.table_editable is False


def test_missing_file_raises_read_error(tmp_path: Path):
    try:
        zonefile.load(tmp_path / "nope.zone", "nope")
    except zonefile.ZoneReadError as exc:
        assert "nope.zone" in str(exc)
    else:
        raise AssertionError("ZoneReadError 가 발생해야 한다")


def test_version_changes_when_content_changes(tmp_path: Path, zones_dir: Path):
    target = tmp_path / "z.zone"
    target.write_text((zones_dir / "example.local.zone").read_text(encoding="utf-8"), encoding="utf-8")
    first = zonefile.load(target, "example.local").snapshot.version
    target.write_text(target.read_text(encoding="utf-8") + "extra IN A 10.0.0.1\n", encoding="utf-8")
    assert zonefile.load(target, "example.local").snapshot.version != first
