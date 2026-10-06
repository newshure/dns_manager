"""설정 로딩 테스트."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dns_manager.config import load_config


def test_defaults_when_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("DNS_MANAGER_CONFIG", raising=False)
    monkeypatch.setattr("dns_manager.config.DEFAULT_CONFIG_PATHS", (tmp_path / "absent.toml",))
    cfg = load_config()
    assert cfg.source is None
    assert cfg.app.host == "0.0.0.0"
    assert cfg.bind.named_conf == Path("/etc/named.conf")


def test_toml_values_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DNS_MANAGER_CONFIG", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        '[bind]\nnamed_conf = "/tmp/named.conf"\nchroot = "/chroot"\ncommand_timeout = 5.5\n'
        '[app]\nport = 9001\nadvanced_view_default = true\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.bind.named_conf == Path("/tmp/named.conf")
    assert cfg.bind.chroot == Path("/chroot")
    assert cfg.bind.command_timeout == 5.5
    assert cfg.app.port == 9001
    assert cfg.app.advanced_view_default is True
    assert cfg.source == path


def test_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "config.toml"
    path.write_text('[app]\nport = 9001\n', encoding="utf-8")
    monkeypatch.setenv("DNS_MANAGER_APP_PORT", "9999")
    monkeypatch.setenv("DNS_MANAGER_BIND_ZONES_CONF", "/etc/named/custom.conf")
    cfg = load_config(path)
    assert cfg.app.port == 9999
    assert cfg.bind.zones_conf == Path("/etc/named/custom.conf")


def test_unreadable_config_raises_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if os.geteuid() == 0:
        pytest.skip("root 는 권한 검사를 우회한다")
    path = tmp_path / "config.toml"
    path.write_text("[app]\n", encoding="utf-8")
    path.chmod(0o000)
    with pytest.raises(PermissionError, match="읽을 수 없습니다"):
        load_config(path)


def test_invalid_toml_raises_value_error(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("[app\nport = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="문법 오류"):
        load_config(path)


def test_empty_chroot_string_means_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DNS_MANAGER_CONFIG", raising=False)
    path = tmp_path / "config.toml"
    path.write_text('[bind]\nchroot = ""\n', encoding="utf-8")
    assert load_config(path).bind.chroot is None


def test_default_host_listens_on_all_interfaces(monkeypatch, tmp_path: Path):
    """0.0.0.0 수신이 기본값이다(요구사항). 인증은 방화벽·프록시가 담당한다."""
    monkeypatch.delenv("DNS_MANAGER_CONFIG", raising=False)
    monkeypatch.setattr("dns_manager.config.DEFAULT_CONFIG_PATHS", (tmp_path / "absent.toml",))
    assert load_config().app.host == "0.0.0.0"


def _layout(tmp_path: Path) -> Path:
    """가짜 RedHat 계열 BIND 레이아웃."""
    (tmp_path / "etc/named").mkdir(parents=True)
    (tmp_path / "etc/named.conf").write_text('options { directory "/var/named"; };\n', encoding="utf-8")
    (tmp_path / "etc/named/zones.conf").write_text("// zones\n", encoding="utf-8")
    (tmp_path / "var/named").mkdir(parents=True)
    return tmp_path


def _pin_detection(monkeypatch: pytest.MonkeyPatch, root: Path | None) -> None:
    """감지가 가짜 트리를 보도록 고정한다.

    원본을 먼저 붙잡아 재귀를 피하고, 호출자가 넘기는 root 는 무시하고 **고정 트리**를 본다.
    """
    from dns_manager.core import detect

    original = detect.detect
    if root is None:
        monkeypatch.setattr(detect, "detect", lambda root=None: None)
    else:
        monkeypatch.setattr(detect, "detect", lambda root=None, _fixed=root: original(root=_fixed))


def test_dead_named_conf_falls_back_to_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """설정의 named.conf 가 사라졌으면 감지 결과로 돌아가야 한다.

    서버를 옮기거나 BIND 를 재설치하면 예전 경로가 그대로 남아, 앱이 지워진 파일을
    계속 들여다보며 "zone 이 없다" 고만 한다.
    """
    root = _layout(tmp_path)
    _pin_detection(monkeypatch, root)

    config = tmp_path / "config.toml"
    config.write_text(
        '[bind]\nnamed_conf = "/etc/bind/named.conf"\nzones_conf = "/etc/bind/zones"\n', encoding="utf-8"
    )
    cfg = load_config(config)

    assert cfg.bind.named_conf == root / "etc/named.conf"
    assert cfg.bind.zones_conf == root / "etc/named/zones.conf"
    assert any("named.conf" in w for w in cfg.warnings)
    assert any("zone 정의" in w for w in cfg.warnings)


def test_dead_paths_without_detection_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """감지도 실패하면 고치지 못하지만, 무엇이 문제인지는 반드시 알려야 한다."""
    _pin_detection(monkeypatch, None)
    config = tmp_path / "config.toml"
    config.write_text('[bind]\nnamed_conf = "/gone/named.conf"\n', encoding="utf-8")

    cfg = load_config(config)
    assert cfg.bind.named_conf == Path("/gone/named.conf")  # 고칠 수 없으면 그대로 둔다
    assert any("찾을 수 없습니다" in w for w in cfg.warnings)


def test_live_paths_produce_no_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _layout(tmp_path)
    _pin_detection(monkeypatch, root)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[bind]\nnamed_conf = "{root}/etc/named.conf"\nzones_conf = "{root}/etc/named/zones.conf"\n',
        encoding="utf-8",
    )
    assert load_config(config).warnings == ()


def test_dead_zone_dir_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _layout(tmp_path)
    _pin_detection(monkeypatch, root)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[bind]\nnamed_conf = "{root}/etc/named.conf"\nzones_conf = "{root}/etc/named/zones.conf"\n'
        'zone_dir = "/var/cache/bind"\n',
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert cfg.bind.zone_dir == root / "var/named"
    assert any("zone 디렉터리" in w for w in cfg.warnings)


def test_dead_rndc_key_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """없는 키 파일을 계속 들고 있으면 동적 갱신이 엉뚱한 이유로 실패한다."""
    root = _layout(tmp_path)
    _pin_detection(monkeypatch, root)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[bind]\nnamed_conf = "{root}/etc/named.conf"\nzones_conf = "{root}/etc/named/zones.conf"\n'
        'rndc_key = "/etc/bind/rndc.key"\n',
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert cfg.bind.rndc_key is None
    assert any("rndc 키" in w for w in cfg.warnings)
