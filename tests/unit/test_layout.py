"""레이아웃 탐색 테스트. named-checkconf 실행은 가짜 결과로 대체한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from dns_manager.config import BindConfig
from dns_manager.core import layout as layout_mod
from dns_manager.core.commands import CommandResult


@pytest.fixture
def layout(monkeypatch: pytest.MonkeyPatch, checkconf_text: str) -> layout_mod.Layout:
    monkeypatch.setattr(
        layout_mod,
        "_parsed_config",
        lambda cfg: CommandResult(argv=("named-checkconf", "-p"), returncode=0, stdout=checkconf_text, stderr=""),
    )
    return layout_mod.discover(BindConfig())


def test_directory_and_zone_count(layout: layout_mod.Layout):
    assert layout.directory == Path("/var/named")
    assert {z.name for z in layout.zones} >= {"example.local", "secondary.local", "corp.partner.example"}


def test_server_wide_forwarders_are_captured(layout: layout_mod.Layout):
    assert layout.forwarding.configured
    assert layout.forwarding.forwarders == ("192.168.2.1", "8.8.8.8")
    assert layout.forwarding.policy == "first"


def test_conditional_forwarder_zone(layout: layout_mod.Layout):
    zone = layout.get("corp.partner.example")
    assert zone is not None
    assert zone.is_forwarder
    assert zone.category == "conditional_forwarder"
    assert zone.forwarders == ("10.9.8.7", "10.9.8.8")
    assert zone.forward_policy == "only"
    assert zone.editable is False


def test_zone_file_path_resolution(layout: layout_mod.Layout):
    assert layout.get("example.local").file == Path("/var/named/example.local.zone")
    assert layout.get("secondary.local").file == Path("/var/named/slaves/secondary.local.zone")


def test_reverse_zone_classification(layout: layout_mod.Layout):
    zone = layout.get("10.168.192.in-addr.arpa")
    assert zone.is_reverse
    assert zone.category == "reverse"


def test_signed_zone_is_not_editable(layout: layout_mod.Layout):
    zone = layout.get("signed.local")
    assert zone.is_signed
    assert zone.editable is False


def test_dynamic_zone_detected(layout: layout_mod.Layout):
    assert layout.get("dynamic.local").dynamic is True
    assert layout.get("example.local").dynamic is False


def test_builtin_zone_excluded_from_categories(layout: layout_mod.Layout):
    assert layout.get("localhost").is_builtin is True


def test_bind_default_zone_files_are_builtin(layout: layout_mod.Layout):
    """Rocky 기본 named.conf 의 localhost 정/역방향 zone 은 트리에 노출하지 않는다."""
    zone = layout.get("localhost")
    assert zone.is_builtin


def test_view_scoped_zone_lookup(layout: layout_mod.Layout):
    assert layout.get("split.local", "internal") is not None
    assert layout.get("split.local", "external") is None


def test_chroot_prefixes_zone_file(monkeypatch: pytest.MonkeyPatch, checkconf_text: str):
    monkeypatch.setattr(
        layout_mod,
        "_parsed_config",
        lambda cfg: CommandResult(argv=("named-checkconf",), returncode=0, stdout=checkconf_text, stderr=""),
    )
    cfg = BindConfig(chroot=Path("/var/named/chroot"))
    found = layout_mod.discover(cfg)
    assert found.get("example.local").file == Path("/var/named/chroot/var/named/example.local.zone")


def test_loopback_reverse_zone_is_builtin(layout: layout_mod.Layout):
    zone = layout.get("1.0.0.127.in-addr.arpa")
    assert zone is not None
    assert zone.is_builtin is True


def test_root_hint_zone_is_builtin(layout: layout_mod.Layout):
    assert layout.get(".").is_builtin is True
