"""zone 쓰기 락 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from dns_manager.core.locking import LockBusy, lock_name, zone_lock


def test_lock_is_created_and_released(tmp_path: Path):
    with zone_lock(tmp_path, "example.local") as path:
        assert path.exists()
    with zone_lock(tmp_path, "example.local"):
        pass  # 해제 후 다시 잡을 수 있어야 한다


def test_second_lock_on_same_zone_is_rejected(tmp_path: Path):
    with zone_lock(tmp_path, "example.local"):
        with pytest.raises(LockBusy):
            with zone_lock(tmp_path, "example.local"):
                pass


def test_different_zones_do_not_block(tmp_path: Path):
    with zone_lock(tmp_path, "a.local"), zone_lock(tmp_path, "b.local"):
        pass


def test_view_is_part_of_the_key(tmp_path: Path):
    assert lock_name("a.local", "internal") != lock_name("a.local", "external")
    with zone_lock(tmp_path, "a.local", "internal"), zone_lock(tmp_path, "a.local", "external"):
        pass


def test_unsafe_characters_are_sanitised(tmp_path: Path):
    name = lock_name("../../etc/passwd")
    assert "/" not in name and ".." not in name.split(".")[0]
