"""실제 BIND 대상 통합 테스트.

named 가 동작하고 테스트 zone(example.local)이 쓰기 가능한 호스트에서만 실행된다.
그 외 환경에서는 통째로 skip 한다.

    DNS_MANAGER_LIVE=1 .venv/bin/python -m pytest tests/integration -q

경고: 지정된 zone 을 실제로 변경한다. 운영 zone 을 대상으로 돌리지 말 것.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from dns_manager.config import AppConfig, BindConfig, Config
from dns_manager.core import apply as apply_mod
from dns_manager.core import layout as layout_mod
from dns_manager.core import server as server_mod
from dns_manager.core import zonefile

ZONE = os.environ.get("LIVE_ZONE", "example.local")

pytestmark = pytest.mark.skipif(
    os.environ.get("DNS_MANAGER_LIVE") != "1",
    reason="DNS_MANAGER_LIVE=1 이고 실제 BIND 가 있는 호스트에서만 실행",
)


@pytest.fixture(scope="module")
def cfg(tmp_path_factory) -> Config:
    for tool in ("named-checkzone", "named-checkconf", "rndc"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} 가 없습니다")
    state = tmp_path_factory.mktemp("state")
    return Config(
        bind=BindConfig(),
        app=AppConfig(state_dir=state, backup_dir=state / "backups"),
    )


@pytest.fixture(scope="module")
def entry(cfg: Config):
    found = layout_mod.discover(cfg.bind).get(ZONE)
    if found is None or found.file is None:
        pytest.skip(f"테스트 zone 을 찾을 수 없습니다: {ZONE}")
    if not os.access(found.file, os.W_OK):
        pytest.skip(f"zone 파일에 쓰기 권한이 없습니다: {found.file}")
    # 원자적 교체는 같은 디렉터리에 임시파일을 만든다
    if not os.access(found.file.parent, os.W_OK):
        pytest.skip(f"zone 디렉터리에 쓰기 권한이 없습니다: {found.file.parent}")
    return found


@pytest.fixture
def restore(cfg: Config, entry):
    """각 테스트 후 원래 내용으로 되돌린다."""
    original = entry.file.read_text(encoding="utf-8")
    yield
    if entry.file.read_text(encoding="utf-8") != original:
        apply_mod.apply_zone_text(cfg, entry, original, summary="테스트 정리", author="pytest")


def test_add_record_is_served_by_named(cfg: Config, entry, restore):
    """레코드를 추가하면 named 가 실제로 응답해야 한다 — 끝에서 끝까지 확인."""
    before = entry.file.read_text(encoding="utf-8")
    new_text = before + "livetest   IN  A   192.168.10.77\n"

    result = apply_mod.apply_zone_text(cfg, entry, new_text, author="pytest", summary="통합 테스트")

    assert result.ok, result.error or result.check_output
    assert result.serial_after > result.serial_before

    status = server_mod.zone_status(cfg.bind, ZONE)
    assert status.serial == result.serial_after, "named 가 새 serial 을 적재하지 않았다"

    answer = server_mod.query(cfg.bind, f"livetest.{ZONE}", "A")
    assert "192.168.10.77" in answer.stdout, answer.stdout


def test_invalid_record_is_rejected_and_zone_still_serves(cfg: Config, entry, restore):
    """잘못된 레코드는 거부되고, 원본과 운영 중인 응답이 모두 보존되어야 한다."""
    before = entry.file.read_text(encoding="utf-8")
    bad = before + "broken   IN  A   999.999.999.999\n"

    result = apply_mod.apply_zone_text(cfg, entry, bad, author="pytest")

    assert not result.ok and result.status == "rejected"
    assert entry.file.read_text(encoding="utf-8") == before
    assert "near" in result.check_output or "bad" in result.check_output.lower()

    answer = server_mod.query(cfg.bind, f"www.{ZONE}", "A")
    assert "192.168.10.21" in answer.stdout


def test_rollback_restores_and_serves_old_data(cfg: Config, entry, restore):
    before = entry.file.read_text(encoding="utf-8")
    added = apply_mod.apply_zone_text(
        cfg, entry, before + "tmphost  IN  A  192.168.10.88\n", author="pytest", summary="임시 추가"
    )
    assert added.ok
    assert "192.168.10.88" in server_mod.query(cfg.bind, f"tmphost.{ZONE}", "A").stdout

    rolled = apply_mod.rollback(cfg, entry, added.change_id, author="pytest")

    assert rolled.ok
    assert rolled.serial_after > added.serial_after
    assert "tmphost" not in entry.file.read_text(encoding="utf-8")
    answer = server_mod.query(cfg.bind, f"tmphost.{ZONE}", "A")
    assert "192.168.10.88" not in answer.stdout


def test_version_conflict_against_external_edit(cfg: Config, entry, restore):
    stale = zonefile.read_snapshot(entry.file, ZONE).version
    # 외부 편집 흉내
    entry.file.write_text(entry.file.read_text(encoding="utf-8") + "; 외부 편집\n", encoding="utf-8")

    with pytest.raises(apply_mod.VersionConflict):
        apply_mod.apply_zone_text(
            cfg, entry, entry.file.read_text(encoding="utf-8"), expected_version=stale, author="pytest"
        )


def test_conf_rollback_keeps_named_running(cfg: Config):
    """잘못된 설정을 넣어도 즉시 복구되고 named 는 계속 떠 있어야 한다."""
    conf = Path(cfg.bind.zones_conf)
    if not (os.access(conf, os.W_OK) and os.access(conf.parent, os.W_OK)):
        pytest.skip(f"설정 파일/디렉터리에 쓰기 권한이 없습니다: {conf}")
    before = conf.read_text(encoding="utf-8")

    result = apply_mod.apply_conf_text(cfg, before + "\nthis is not valid config;\n", author="pytest")

    assert not result.ok and result.status == "rolled_back"
    assert conf.read_text(encoding="utf-8") == before
    assert server_mod.status(cfg.bind).running, "named 가 멈췄다"
