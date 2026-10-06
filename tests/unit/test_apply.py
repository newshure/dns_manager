"""트랜잭션 엔진 테스트.

named-checkzone / named-checkconf / rndc 는 가짜 실행파일로 대체하되, subprocess 경로는
실제로 통과시킨다. 가짜 도구의 동작은 제어 파일로 바꾼다(검증 실패·reload 실패 재현).
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from dns_manager.config import AppConfig, BindConfig, Config
from dns_manager.core import apply as apply_mod
from dns_manager.core import zonefile
from dns_manager.core.history import History
from dns_manager.core.layout import ZoneEntry
from dns_manager.core.locking import LockBusy, zone_lock

ZONE_NAME = "example.local"

# 제어 파일(fail_checkzone / fail_reload / fail_checkconf)이 있으면 해당 명령이 실패한다.
CHECKZONE = """#!/bin/sh
if [ -f "$CTRL/fail_checkzone" ]; then
  echo "dns_rdata_fromtext: $2:7: near 'bogus': bad dotted quad" >&2
  echo "zone $1/IN: loading from master file $2 failed: bad dotted quad" >&2
  exit 1
fi
grep -q BADZONE "$2" && { echo "zone $1/IN: not loaded due to errors." >&2; exit 1; }
echo "zone $1/IN: loaded serial $(grep -oE '[0-9]{10}' "$2" | head -1)"
echo OK
"""

CHECKCONF = """#!/bin/sh
if [ -f "$CTRL/fail_checkconf" ]; then
  echo "/etc/named/zones.conf:3: unknown option 'bogus'" >&2
  exit 1
fi
grep -q BADCONF "$CTRL/zones.conf" 2>/dev/null && { echo "zones.conf: syntax error" >&2; exit 1; }
cat "$CTRL/parsed.txt" 2>/dev/null
exit 0
"""

RNDC = """#!/bin/sh
case "$1" in
  reload|reconfig)
    if [ -f "$CTRL/fail_reload" ]; then
      echo "rndc: 'reload' failed: not found" >&2
      exit 1
    fi
    echo "zone reload queued"
    ;;
  zonestatus)
    echo "name: $2"
    echo "type: master"
    echo "serial: $(grep -oE '[0-9]{10}' "$CTRL/zonefile" | head -1)"
    ;;
  status) echo "version: fake" ;;
esac
"""


def _script(path: Path, body: str, ctrl: Path) -> Path:
    path.write_text(f'#!/bin/sh\nCTRL="{ctrl}"\nexport CTRL\n' + body.split("\n", 1)[1], encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def env(tmp_path: Path, zones_dir: Path):
    """가짜 BIND 환경. (cfg, entry, ctrl, zone_path) 를 돌려준다."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ctrl = tmp_path / "ctrl"
    ctrl.mkdir()
    named_dir = tmp_path / "var-named"
    named_dir.mkdir()

    zone_path = named_dir / "example.local.zone"
    zone_path.write_text((zones_dir / "example.local.zone").read_text(encoding="utf-8"), encoding="utf-8")
    (ctrl / "zonefile").symlink_to(zone_path)

    zones_conf = tmp_path / "zones.conf"
    zones_conf.write_text(
        'zone "example.local" IN {\n    type master;\n    file "example.local.zone";\n};\n', encoding="utf-8"
    )
    (ctrl / "zones.conf").symlink_to(zones_conf)

    cfg = Config(
        bind=BindConfig(
            named_conf=tmp_path / "named.conf",
            zones_conf=zones_conf,
            named_checkzone=str(_script(bin_dir / "named-checkzone", CHECKZONE, ctrl)),
            named_checkconf=str(_script(bin_dir / "named-checkconf", CHECKCONF, ctrl)),
            rndc=str(_script(bin_dir / "rndc", RNDC, ctrl)),
        ),
        app=AppConfig(state_dir=tmp_path / "state", backup_dir=tmp_path / "state" / "backups"),
    )
    entry = ZoneEntry(name=ZONE_NAME, zone_type="master", view=None, file=zone_path)
    return cfg, entry, ctrl, zone_path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def add_record(text: str, line: str = "newhost     IN  A       192.168.10.99\n") -> str:
    return text + line


# --------------------------- 정상 적용 ---------------------------


def test_apply_writes_and_bumps_serial(env):
    cfg, entry, _ctrl, path = env
    before = read(path)
    result = apply_mod.apply_zone_text(cfg, entry, add_record(before), author="tester", summary="A 레코드 추가")

    assert result.ok and result.status == "applied"
    assert result.serial_after > result.serial_before
    assert "newhost" in read(path)
    assert str(result.serial_after) in read(path)
    assert result.reload.ok


def test_comments_and_formatting_are_preserved(env):
    cfg, entry, _ctrl, path = env
    apply_mod.apply_zone_text(cfg, entry, add_record(read(path)))
    after = read(path)
    assert after.startswith("; 테스트용 정방향 zone")
    assert "$TTL 3600" in after


def test_backup_is_created_with_original_content(env):
    cfg, entry, _ctrl, path = env
    before = read(path)
    result = apply_mod.apply_zone_text(cfg, entry, add_record(before))
    assert result.backup and result.backup.is_file()
    assert read(result.backup) == before


def test_diff_is_recorded(env):
    cfg, entry, _ctrl, path = env
    result = apply_mod.apply_zone_text(cfg, entry, add_record(read(path)))
    assert "+newhost" in result.diff
    assert result.diff.startswith("---")


def test_no_change_is_a_noop(env):
    cfg, entry, _ctrl, path = env
    mtime = path.stat().st_mtime_ns
    result = apply_mod.apply_zone_text(cfg, entry, read(path), bump_serial=False)
    assert result.ok and result.summary == "변경 없음"
    assert path.stat().st_mtime_ns == mtime


def test_history_records_applied_change(env):
    cfg, entry, _ctrl, path = env
    result = apply_mod.apply_zone_text(cfg, entry, add_record(read(path)), author="tester", summary="추가")
    history = History(Path(cfg.app.state_dir) / "history.sqlite3")
    rows = history.list(target=ZONE_NAME)
    assert rows and rows[0].id == result.change_id
    assert rows[0].status == "applied"
    assert rows[0].author == "tester"
    assert rows[0].serial_after == result.serial_after


# --------------------------- 검증 실패 ---------------------------


def test_invalid_zone_is_rejected_without_touching_original(env):
    cfg, entry, _ctrl, path = env
    before = read(path)
    result = apply_mod.apply_zone_text(cfg, entry, add_record(before, "BADZONE\n"), bump_serial=False)

    assert not result.ok and result.status == "rejected"
    assert read(path) == before, "검증 실패 시 원본이 변경되면 안 된다"
    assert "not loaded due to errors" in result.check_output


def test_checkzone_error_text_is_passed_through_verbatim(env):
    cfg, entry, ctrl, path = env
    (ctrl / "fail_checkzone").touch()
    result = apply_mod.apply_zone_text(cfg, entry, add_record(read(path)))
    assert "bad dotted quad" in result.check_output
    assert "near 'bogus'" in result.check_output


def test_rejected_change_is_recorded(env):
    cfg, entry, _ctrl, path = env
    result = apply_mod.apply_zone_text(cfg, entry, add_record(read(path), "BADZONE\n"), bump_serial=False)
    history = History(Path(cfg.app.state_dir) / "history.sqlite3")
    assert history.get(result.change_id).status == "rejected"


def test_no_staging_files_are_left_behind(env):
    cfg, entry, _ctrl, path = env
    apply_mod.apply_zone_text(cfg, entry, add_record(read(path), "BADZONE\n"), bump_serial=False)
    leftovers = [p.name for p in path.parent.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"임시 파일이 남았다: {leftovers}"


# --------------------------- reload 실패 → 롤백 ---------------------------


def test_reload_failure_rolls_back(env):
    cfg, entry, ctrl, path = env
    before = read(path)
    (ctrl / "fail_reload").touch()

    result = apply_mod.apply_zone_text(cfg, entry, add_record(before))

    assert not result.ok and result.status == "rolled_back"
    assert read(path) == before, "reload 실패 시 원본으로 복구되어야 한다"
    assert "되돌렸습니다" in result.error


def test_rolled_back_change_is_recorded(env):
    cfg, entry, ctrl, path = env
    (ctrl / "fail_reload").touch()
    result = apply_mod.apply_zone_text(cfg, entry, add_record(read(path)))
    history = History(Path(cfg.app.state_dir) / "history.sqlite3")
    assert history.get(result.change_id).status == "rolled_back"


# --------------------------- 동시성 ---------------------------


def test_version_conflict_is_detected(env):
    cfg, entry, _ctrl, path = env
    stale = zonefile.read_snapshot(path, ZONE_NAME).version
    path.write_text(add_record(read(path), "; 외부에서 수동 편집\n"), encoding="utf-8")

    with pytest.raises(apply_mod.VersionConflict) as exc:
        apply_mod.apply_zone_text(cfg, entry, add_record(read(path)), expected_version=stale)
    assert "외부에서 변경" in str(exc.value)


def test_matching_version_is_accepted(env):
    cfg, entry, _ctrl, path = env
    version = zonefile.read_snapshot(path, ZONE_NAME).version
    result = apply_mod.apply_zone_text(cfg, entry, add_record(read(path)), expected_version=version)
    assert result.ok


def test_concurrent_write_is_blocked_by_lock(env):
    cfg, entry, _ctrl, path = env
    lock_dir = Path(cfg.app.state_dir) / "locks"
    with zone_lock(lock_dir, ZONE_NAME, None):
        with pytest.raises(LockBusy):
            apply_mod.apply_zone_text(cfg, entry, add_record(read(path)))


# --------------------------- 편집 금지 대상 ---------------------------


def test_signed_zone_cannot_be_edited(env):
    cfg, _entry, _ctrl, path = env
    signed = ZoneEntry(name=ZONE_NAME, zone_type="master", view=None, file=path, inline_signing=True)
    with pytest.raises(apply_mod.ApplyError, match="서명"):
        apply_mod.apply_zone_text(cfg, signed, add_record(read(path)))


def test_slave_zone_cannot_be_edited(env):
    cfg, _entry, _ctrl, path = env
    slave = ZoneEntry(name=ZONE_NAME, zone_type="slave", view=None, file=path)
    with pytest.raises(apply_mod.ApplyError, match="master"):
        apply_mod.apply_zone_text(cfg, slave, add_record(read(path)))


def test_forward_zone_without_file_cannot_be_edited(env):
    cfg, _entry, _ctrl, _path = env
    fwd = ZoneEntry(name="corp.example", zone_type="forward", view=None, file=None)
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.apply_zone_text(cfg, fwd, "whatever")


# --------------------------- 롤백 ---------------------------


def test_rollback_restores_previous_content(env):
    cfg, entry, _ctrl, path = env
    original = read(path)
    first = apply_mod.apply_zone_text(cfg, entry, add_record(original), summary="추가")
    assert "newhost" in read(path)

    result = apply_mod.rollback(cfg, entry, first.change_id, author="tester")

    assert result.ok
    assert "newhost" not in read(path)
    # 되돌려도 serial 은 이미 공개된 값보다 커야 한다.
    # (백업의 옛 serial 을 기준으로 올리면 현재 값과 같아져 secondary 가 갱신을 못 받는다)
    assert result.serial_after > first.serial_after
    assert str(result.serial_after) in read(path)


def test_lower_serial_in_new_text_is_lifted_above_current(env):
    """옛 내용을 붙여넣어도 serial 은 현재 값보다 커져야 한다."""
    cfg, entry, _ctrl, path = env
    original = read(path)
    first = apply_mod.apply_zone_text(cfg, entry, add_record(original))
    assert first.serial_after > first.serial_before

    second = apply_mod.apply_zone_text(cfg, entry, original)  # serial 이 더 낮은 원본
    assert second.ok
    assert second.serial_after > first.serial_after


def test_rollback_of_unknown_change_raises(env):
    cfg, entry, _ctrl, _path = env
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.rollback(cfg, entry, 99999)


# --------------------------- 설정 파일(zones.conf) ---------------------------


def test_apply_conf_adds_zone_block(env):
    cfg, _entry, _ctrl, _path = env
    conf = Path(cfg.bind.zones_conf)
    new = read(conf) + '\nzone "new.local" IN {\n    type master;\n    file "new.local.zone";\n};\n'
    result = apply_mod.apply_conf_text(cfg, new, summary="zone 추가")
    assert result.ok and result.status == "applied"
    assert "new.local" in read(conf)


def test_apply_conf_rolls_back_on_checkconf_failure(env):
    cfg, _entry, _ctrl, _path = env
    conf = Path(cfg.bind.zones_conf)
    before = read(conf)
    result = apply_mod.apply_conf_text(cfg, before + "\nBADCONF\n", summary="잘못된 설정")

    assert not result.ok and result.status == "rolled_back"
    assert read(conf) == before, "checkconf 실패 시 이전 설정으로 복구되어야 한다"
    assert "syntax error" in result.check_output


def test_apply_conf_rolls_back_on_reconfig_failure(env):
    cfg, _entry, ctrl, _path = env
    conf = Path(cfg.bind.zones_conf)
    before = read(conf)
    (ctrl / "fail_reload").touch()
    result = apply_mod.apply_conf_text(cfg, before + '\nzone "x.local" IN { type master; file "x"; };\n')

    assert not result.ok and result.status == "rolled_back"
    assert read(conf) == before


def test_apply_conf_version_conflict(env):
    cfg, _entry, _ctrl, _path = env
    conf = Path(cfg.bind.zones_conf)
    stale = zonefile.read_snapshot(conf, "conf").version
    conf.write_text(read(conf) + "// 외부 편집\n", encoding="utf-8")
    with pytest.raises(apply_mod.VersionConflict):
        apply_mod.apply_conf_text(cfg, read(conf) + "// 앱 편집\n", expected_version=stale)


# --------------------------- 해석 불가 입력 ---------------------------


def test_unparsable_input_is_judged_by_named_checkzone(env):
    """dnspython 이 해석하지 못하는 입력도 named-checkzone 이 판정해야 한다.

    앱이 먼저 가로채면 사용자가 봐야 할 BIND 의 정확한 오류가 가려진다.
    """
    cfg, entry, _ctrl, path = env
    before = read(path)
    result = apply_mod.apply_zone_text(cfg, entry, add_record(before, "BADZONE  IN  A  999.999.999.999\n"))

    assert not result.ok and result.status == "rejected"
    assert read(path) == before
    assert "not loaded due to errors" in result.check_output


def test_text_without_soa_still_reaches_checkzone(env):
    cfg, entry, _ctrl, path = env
    before = read(path)
    result = apply_mod.apply_zone_text(cfg, entry, "BADZONE\n$TTL 60\n@ IN NS ns1.example.local.\n")
    assert not result.ok and result.status == "rejected"
    assert read(path) == before


def test_applied_without_serial_bump_is_warned(env):
    """serial 을 올리지 못한 채 적용되면 결과에 분명히 남아야 한다."""
    cfg, entry, _ctrl, path = env
    # SOA 없이도 가짜 checkzone 은 통과시킨다 → 경고가 붙은 채 적용된다
    result = apply_mod.apply_zone_text(cfg, entry, "$TTL 60\n@ IN NS ns1.example.local.\n")
    assert result.ok
    assert "serial" in (result.error or "")
