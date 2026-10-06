# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""zone 추가/삭제·전달자 API 테스트.

named-checkconf 가짜는 실제 zones.conf / named.conf 를 읽어 파싱 결과를 출력한다.
따라서 앱이 파일을 고치면 다음 조회에 그대로 반영된다 — 실제 BIND 의 동작과 같은 흐름.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dns_manager.api.app import create_app
from dns_manager.config import AppConfig, BindConfig, Config
from dns_manager.core import confedit

NAMED_CONF = """options {
\tdirectory "{zones_dir}";
\tlisten-on port 53 { 127.0.0.1; };
};

include "{zones_conf}";
"""

ZONES_CONF = '''// dns_manager 관리 대상
zone "example.local" IN {
    type master;
    file "example.local.zone";
    allow-update { none; };
};

// 조건부 전달자
zone "partner.example" IN {
    type forward;
    forward only;
    forwarders { 192.168.2.1; };
};
'''

# named-checkconf 흉내: options 를 읽고 include 를 펼친다. BADCONF 가 있으면 실패.
# 실제 named-checkconf -p 처럼 include 를 **모두** 전개한다.
# 한 파일만 펼치면 "다른 include 에 적힌 zone" 을 다루는 경로를 시험할 수 없다.
CHECKCONF = """#!/bin/sh
if grep -q BADCONF "$NAMED_CONF" "$ZONES_CONF" 2>/dev/null; then
  echo "$ZONES_CONF:1: syntax error near 'BADCONF'" >&2
  exit 1
fi
sed -e '/^include/d' "$NAMED_CONF"
grep -oE '^include "[^"]+"' "$NAMED_CONF" | sed -e 's/^include "//' -e 's/"$//' | while read -r f; do
  [ -f "$f" ] && cat "$f"
done
"""
CHECKZONE = """#!/bin/sh
grep -qE '999\\.999|BADZONE' "$2" && { echo "$2:3: bad dotted quad" >&2; exit 1; }
echo "zone $1/IN: loaded serial $(grep -oE '[0-9]{10}' "$2" | head -1)"
echo OK
"""
RNDC = """#!/bin/sh
case "$1" in
  reload|reconfig) echo "server reload successful" ;;
  zonestatus) echo "name: $2"; echo "type: master" ;;
  status) echo "version: fake" ;;
esac
"""


def _sh(path: Path, body: str, env: dict[str, str]) -> Path:
    exports = "".join(f'{k}="{v}"\nexport {k}\n' for k, v in env.items())
    path.write_text("#!/bin/sh\n" + exports + body.split("\n", 1)[1], encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def env(tmp_path: Path, zones_dir: Path):
    named_dir = tmp_path / "var-named"
    named_dir.mkdir()
    (named_dir / "example.local.zone").write_text(
        (zones_dir / "example.local.zone").read_text(encoding="utf-8"), encoding="utf-8"
    )

    zones_conf = tmp_path / "zones.conf"
    zones_conf.write_text(ZONES_CONF, encoding="utf-8")
    named_conf = tmp_path / "named.conf"
    named_conf.write_text(
        NAMED_CONF.replace("{zones_dir}", str(named_dir)).replace("{zones_conf}", str(zones_conf)),
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shell_env = {"NAMED_CONF": str(named_conf), "ZONES_CONF": str(zones_conf)}
    cfg = Config(
        bind=BindConfig(
            named_conf=named_conf,
            zones_conf=zones_conf,
            named_checkconf=str(_sh(bin_dir / "named-checkconf", CHECKCONF, shell_env)),
            named_checkzone=str(_sh(bin_dir / "named-checkzone", CHECKZONE, shell_env)),
            rndc=str(_sh(bin_dir / "rndc", RNDC, shell_env)),
        ),
        app=AppConfig(state_dir=tmp_path / "state", backup_dir=tmp_path / "state" / "backups"),
    )
    client = TestClient(create_app(cfg))
    return client, named_dir, zones_conf, named_conf


def zone_names(client: TestClient) -> set[str]:
    return {z["name"] for z in client.get("/api/zones").json()}


# --------------------------- 미리보기 ---------------------------


def test_preview_shows_conf_block_and_zone_file(env):
    client, *_ = env
    res = client.post("/api/zones/preview", json={"name": "new.local", "type": "master"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert 'zone "new.local" IN {' in body["conf_block"]
    assert "SOA" in body["zone_file"] and "$TTL 3600" in body["zone_file"]
    assert body["file_path"].endswith("new.local.zone")
    assert body["valid"] is True


def test_preview_does_not_write_anything(env):
    client, named_dir, zones_conf, _ = env
    before_conf = zones_conf.read_text(encoding="utf-8")
    before_files = set(p.name for p in named_dir.iterdir())
    client.post("/api/zones/preview", json={"name": "new.local"})
    assert zones_conf.read_text(encoding="utf-8") == before_conf
    assert set(p.name for p in named_dir.iterdir()) == before_files


def test_preview_reverse_zone_from_network_id(env):
    client, *_ = env
    body = client.post(
        "/api/zones/preview", json={"reverse": True, "network_id": "192.168.220", "type": "master"}
    ).json()
    assert body["zone"] == "220.168.192.in-addr.arpa"
    # 역방향은 .rev 확장자 규칙을 따른다
    assert body["file_path"].endswith("220.168.192.in-addr.arpa.rev")


def test_preview_rejects_existing_zone(env):
    client, *_ = env
    res = client.post("/api/zones/preview", json={"name": "example.local"})
    assert res.status_code == 400 and "이미" in res.json()["detail"]


def test_preview_rejects_classless_reverse(env):
    client, *_ = env
    res = client.post("/api/zones/preview", json={"reverse": True, "network_id": "192.168.10.0/25"})
    assert res.status_code == 400 and "RFC 2317" in res.json()["detail"]


def test_preview_slave_requires_masters(env):
    client, *_ = env
    assert client.post("/api/zones/preview", json={"name": "s.local", "type": "slave"}).status_code == 400


# --------------------------- 생성 ---------------------------


def test_create_master_zone(env):
    client, named_dir, zones_conf, _ = env
    res = client.post("/api/zones", json={"name": "new.local", "type": "master"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["ok"] and body["result"]["status"] == "applied"

    assert (named_dir / "new.local.zone").is_file()
    assert 'zone "new.local"' in zones_conf.read_text(encoding="utf-8")
    assert "new.local" in zone_names(client)
    # 기존 설정은 보존
    assert "example.local" in zone_names(client)


def test_created_zone_is_immediately_editable(env):
    client, *_ = env
    client.post("/api/zones", json={"name": "new.local", "type": "master"})
    res = client.post("/api/zones/new.local/records", json={"name": "www", "type": "A", "data": "10.0.0.1"})
    assert res.status_code == 201, res.text
    assert any(r["name"] == "www" for r in client.get("/api/zones/new.local").json()["records"])


def test_create_reverse_zone_uses_rev_suffix(env):
    client, named_dir, *_ = env
    res = client.post("/api/zones", json={"reverse": True, "network_id": "192.168.220", "type": "master"})
    assert res.status_code == 201
    assert (named_dir / "220.168.192.in-addr.arpa.rev").is_file()


def test_create_slave_zone_has_no_zone_file(env):
    client, named_dir, zones_conf, _ = env
    res = client.post("/api/zones", json={"name": "sec.local", "type": "slave", "masters": ["10.0.0.9"]})
    assert res.status_code == 201
    assert not (named_dir / "sec.local.zone").exists()
    assert "masters { 10.0.0.9; };" in zones_conf.read_text(encoding="utf-8")


def test_create_rolls_back_file_when_conf_fails(env):
    """설정 검증이 실패하면 설정도 zone 파일도 남지 않아야 한다."""
    client, named_dir, zones_conf, _ = env
    before = zones_conf.read_text(encoding="utf-8")
    res = client.post("/api/zones", json={"name": "BADCONF.local", "type": "master"})
    body = res.json()
    assert not body["ok"] and body["result"]["status"] == "rolled_back"
    assert zones_conf.read_text(encoding="utf-8") == before
    assert not (named_dir / "BADCONF.local.zone").exists()


def test_create_duplicate_is_rejected(env):
    client, *_ = env
    assert client.post("/api/zones", json={"name": "example.local"}).status_code == 400


# --------------------------- 삭제 ---------------------------


def test_delete_zone_keeps_file_by_default(env):
    client, named_dir, zones_conf, _ = env
    res = client.delete("/api/zones/example.local")
    assert res.status_code == 200 and res.json()["ok"]
    assert "example.local" not in zone_names(client)
    assert (named_dir / "example.local.zone").is_file(), "기본적으로 파일은 남긴다"
    assert "partner.example" in zones_conf.read_text(encoding="utf-8")


def test_delete_zone_with_file_moves_it_to_backup(env):
    client, named_dir, _, _ = env
    res = client.delete("/api/zones/example.local?delete_file=true")
    assert res.json()["ok"]
    assert not (named_dir / "example.local.zone").exists()


def test_delete_unknown_zone_is_404(env):
    client, *_ = env
    assert client.delete("/api/zones/nope.local").status_code == 404


# --------------------------- 전달자 ---------------------------


def test_set_server_forwarders(env):
    client, _, _, named_conf = env
    res = client.put("/api/forwarders/server", json={"forwarders": ["8.8.8.8", "1.1.1.1"], "policy": "first"})
    assert res.status_code == 200 and res.json()["ok"], res.text
    text = named_conf.read_text(encoding="utf-8")
    assert "forwarders { 8.8.8.8; 1.1.1.1; };" in text
    assert "forward first;" in text
    # options 의 다른 설정은 보존
    assert 'listen-on port 53 { 127.0.0.1; };' in text

    status = client.get("/api/status").json()
    assert status["server_forwarders"] == ["8.8.8.8", "1.1.1.1"]
    assert status["server_forward_policy"] == "first"


def test_replace_server_forwarders(env):
    client, _, _, named_conf = env
    client.put("/api/forwarders/server", json={"forwarders": ["8.8.8.8"]})
    client.put("/api/forwarders/server", json={"forwarders": ["9.9.9.9"], "policy": "only"})
    text = named_conf.read_text(encoding="utf-8")
    # 문자열 검색이 아니라 options 블록에서 확인한다(경로 문자열에 섞일 수 있다)
    option = confedit.get_option(text, "forwarders")
    assert option is not None
    assert "9.9.9.9" in option.text and "8.8.8.8" not in option.text
    assert text.count("forwarders {") == 1


def test_clear_server_forwarders(env):
    client, _, _, named_conf = env
    client.put("/api/forwarders/server", json={"forwarders": ["8.8.8.8"], "policy": "first"})
    res = client.put("/api/forwarders/server", json={"forwarders": []})
    assert res.json()["ok"]
    text = named_conf.read_text(encoding="utf-8")
    assert confedit.get_option(text, "forwarders") is None
    assert confedit.get_option(text, "forward") is None
    # 다른 설정은 보존
    assert confedit.get_option(text, "directory") is not None
    assert client.get("/api/status").json()["server_forwarders"] == []


def test_add_conditional_forwarder(env):
    client, _, zones_conf, _ = env
    res = client.put("/api/forwarders/corp.example", json={"forwarders": ["10.9.8.7"], "policy": "only"})
    assert res.status_code == 200 and res.json()["ok"], res.text
    assert "corp.example" in zones_conf.read_text(encoding="utf-8")

    forwarders = {f["domain"]: f for f in client.get("/api/forwarders").json()}
    assert forwarders["corp.example"]["forwarders"] == ["10.9.8.7"]
    assert forwarders["corp.example"]["policy"] == "only"


def test_update_conditional_forwarder_replaces_block(env):
    client, _, zones_conf, _ = env
    res = client.put("/api/forwarders/partner.example", json={"forwarders": ["10.1.1.1", "10.1.1.2"]})
    assert res.json()["ok"]
    text = zones_conf.read_text(encoding="utf-8")
    assert text.count('zone "partner.example"') == 1
    assert "10.1.1.1" in text and "192.168.2.1" not in text


def test_conditional_forwarder_requires_addresses(env):
    client, *_ = env
    assert client.put("/api/forwarders/x.example", json={"forwarders": []}).status_code == 400


def test_conditional_forwarder_cannot_shadow_master_zone(env):
    client, *_ = env
    res = client.put("/api/forwarders/example.local", json={"forwarders": ["10.0.0.1"]})
    assert res.status_code == 400 and "master" in res.json()["detail"]


def test_delete_conditional_forwarder(env):
    client, _, zones_conf, _ = env
    res = client.delete("/api/forwarders/partner.example")
    assert res.status_code == 200 and res.json()["ok"]
    assert "partner.example" not in zones_conf.read_text(encoding="utf-8")
    # 주석도 함께 사라진다
    assert "// 조건부 전달자" not in zones_conf.read_text(encoding="utf-8")


def test_delete_non_forwarder_is_refused(env):
    client, *_ = env
    res = client.delete("/api/forwarders/example.local")
    assert res.status_code == 400


def test_forwarder_changes_are_recorded_in_history(env):
    client, *_ = env
    client.put("/api/forwarders/server", json={"forwarders": ["8.8.8.8"]}, headers={"X-Forwarded-User": "haedong"})
    row = client.get("/api/history").json()[0]
    assert row["kind"] == "conf"
    assert row["author"] == "haedong"
    assert "전달자" in row["summary"]


def test_in_zone_ns_gets_glue_automatically(env):
    """zone 안쪽 NS 에는 glue 가 자동으로 붙고, 미리보기에 그대로 드러나야 한다."""
    client, *_ = env
    body = client.post(
        "/api/zones/preview", json={"name": "new.local", "primary_ns": "ns1.new.local."}
    ).json()
    assert body["valid"] is True
    assert "ns1" in body["zone_file"]
    assert " A " in body["zone_file"] or "AAAA" in body["zone_file"]


def test_in_zone_ns_without_address_is_refused_when_ip_unknown(monkeypatch, env):
    """주소를 알 수 없으면 조용히 넘어가지 않고 무엇이 필요한지 알린다."""
    from dns_manager.core import zonetemplate

    monkeypatch.setattr(zonetemplate, "local_ipv4", lambda: None)
    client, *_ = env
    res = client.post(
        "/api/zones/preview", json={"name": "new.local", "primary_ns": "ns1.new.local."}
    )
    assert res.status_code == 400
    assert "glue" in res.json()["detail"]


def test_preview_accepts_ns_with_address_shorthand(env):
    """'이름 주소' 형태로 입력하면 glue 레코드로 분리한다."""
    client, *_ = env
    body = client.post(
        "/api/zones/preview", json={"name": "new.local", "primary_ns": "ns1.new.local. 10.0.0.1"}
    ).json()
    assert "ns1" in body["zone_file"] and "10.0.0.1" in body["zone_file"]
    assert body["valid"] is True


def test_default_zone_passes_checkzone(env):
    """기본값으로 만든 zone 은 named-checkzone 을 통과해야 한다.

    zone 안쪽 NS 를 쓰게 되면 glue 를 자동으로 채우되, 미리보기에 그대로 드러나야 한다.
    """
    client, *_ = env
    body = client.post("/api/zones/preview", json={"name": "plain.local"}).json()
    assert body["valid"] is True, body["check_output"]
    text = body["zone_file"]
    assert "NS" in text
    if "ns1.plain.local." in text:
        assert "ns1" in text.split("NS")[-1] or " A " in text, "zone 안쪽 NS 에는 glue 가 있어야 한다"


def test_server_forwarders_prefer_writable_include(tmp_path: Path, zones_dir: Path, env):
    """named.conf 에 쓰기 권한이 없어도 options 안의 include 파일로 관리할 수 있어야 한다.

    서비스는 최소 권한으로 돈다(systemd ProtectSystem=strict 아래에서 /etc 는 읽기 전용).
    """
    client, _, _, named_conf = env
    include = named_conf.parent / "options-forwarders.conf"
    include.write_text("// managed\n", encoding="utf-8")
    named_conf.write_text(
        named_conf.read_text(encoding="utf-8").replace(
            "options {", f'options {{\n\tinclude "{include}";', 1
        ),
        encoding="utf-8",
    )

    res = client.put("/api/forwarders/server", json={"forwarders": ["8.8.8.8"], "policy": "first"})
    assert res.status_code == 200 and res.json()["ok"], res.text
    # named.conf 자체는 건드리지 않는다
    assert "8.8.8.8" not in named_conf.read_text(encoding="utf-8")
    assert "forwarders { 8.8.8.8; };" in include.read_text(encoding="utf-8")
    assert "forward first;" in include.read_text(encoding="utf-8")


def test_options_write_target_error_explains_options(tmp_path: Path, env, monkeypatch):
    """쓸 수 없으면 무엇을 하면 되는지 알려야 한다."""
    from dns_manager.core import zoneadmin

    monkeypatch.setattr(zoneadmin, "_writable", lambda path: False)
    client, *_ = env
    res = client.put("/api/forwarders/server", json={"forwarders": ["8.8.8.8"]})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "include" in detail and "쓰기 권한" in detail


def test_delete_file_moves_zone_file_to_backup(env):
    """삭제한 zone 파일은 사라지는 게 아니라 백업으로 옮겨져야 한다.

    실제 서버에서 /var/named 와 /var/lib 가 다른 마운트라 os.replace 가
    'Invalid cross-device link' 로 실패했다 → shutil.move 로 바꿨다.
    """
    client, named_dir, *_ = env
    original = (named_dir / "example.local.zone").read_text(encoding="utf-8")

    body = client.delete("/api/zones/example.local?delete_file=true").json()
    assert body["ok"] and body["result"]["error"] is None, body["result"]["error"]
    assert not (named_dir / "example.local.zone").exists()

    backups = list(Path(client.app.state.config.app.backup_dir).rglob("deleted-*"))
    assert len(backups) == 1, backups
    assert backups[0].read_text(encoding="utf-8") == original


def test_delete_file_failure_is_reported_not_swallowed(env, monkeypatch):
    """파일을 못 옮기면 조용히 성공으로 끝내지 않고 알려야 한다."""
    from dns_manager.core import zoneadmin

    def boom(src, dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(zoneadmin.shutil, "move", boom)
    client, named_dir, *_ = env

    body = client.delete("/api/zones/example.local?delete_file=true").json()
    assert body["ok"], "설정 삭제 자체는 성공했다"
    assert "옮기지 못했습니다" in (body["result"]["error"] or "")
    assert (named_dir / "example.local.zone").exists(), "실패했으면 원본은 남아 있어야 한다"


# --------------------------- zone Properties (옵션) ---------------------------


def test_update_zone_transfers(env):
    client, _, zones_conf, _ = env
    res = client.put(
        "/api/zones/example.local/options",
        json={"allow_transfer": ["10.0.0.1", "10.0.0.2"], "also_notify": ["10.0.0.1"]},
    )
    assert res.status_code == 200 and res.json()["ok"], res.text
    text = zones_conf.read_text(encoding="utf-8")
    assert "allow-transfer { 10.0.0.1; 10.0.0.2; };" in text
    assert "also-notify { 10.0.0.1; };" in text

    detail = client.get("/api/zones/example.local").json()
    assert detail["allow_transfer"] == ["10.0.0.1", "10.0.0.2"]


def test_update_zone_options_keeps_file_and_type(env):
    client, _, zones_conf, _ = env
    client.put("/api/zones/example.local/options", json={"allow_transfer": ["none"]})
    text = zones_conf.read_text(encoding="utf-8")
    assert 'file "example.local.zone";' in text
    assert "type master;" in text
    # 다른 zone 블록은 그대로
    assert 'zone "partner.example"' in text


def test_enable_dynamic_update_via_options(env):
    client, *_ = env
    res = client.put("/api/zones/example.local/options", json={"allow_update": ["10.0.0.0/8"]})
    assert res.json()["ok"]
    detail = client.get("/api/zones/example.local").json()
    assert detail["allow_update"] == ["10.0.0.0/8"]
    assert detail["dynamic"] is True


def test_update_options_on_unknown_zone(env):
    client, *_ = env
    assert client.put("/api/zones/nope.local/options", json={"allow_transfer": ["none"]}).status_code == 404


def test_bare_ip_is_normalized_by_bind(env):
    """BIND 는 allow-transfer 의 bare IP 를 /32 로 정규화한다.

    앱이 저장한 값과 되읽은 값이 다를 수 있다는 것을 기억해 두기 위한 기록.
    (가짜 checkconf 는 정규화하지 않으므로 여기서는 저장 형태만 확인한다)
    """
    client, _, zones_conf, _ = env
    client.put("/api/zones/example.local/options", json={"allow_transfer": ["10.9.9.9"]})
    assert "allow-transfer { 10.9.9.9; };" in zones_conf.read_text(encoding="utf-8")


def test_new_zone_file_adopts_directory_ownership(env):
    """root 로 돌면 새 zone 파일이 root:root 0644 가 된다.

    named 가 읽기는 되지만 동적 갱신을 걸면 쓰지 못하고, 이웃 파일과 관례도 어긋난다.
    디렉터리 그룹과 이웃 zone 파일의 권한을 따라야 한다.
    """
    import os

    client, named_dir, *_ = env
    # 이웃 zone 파일의 관례를 0664 로 둔다
    os.chmod(named_dir / "example.local.zone", 0o664)

    res = client.post("/api/zones", json={"name": "owned.local", "type": "master"})
    assert res.status_code == 201, res.text

    created = named_dir / "owned.local.zone"
    assert created.is_file()
    assert created.stat().st_mode & 0o777 == 0o664, "이웃 zone 파일의 권한을 따라야 한다"
    assert created.stat().st_gid == named_dir.stat().st_gid, "zone 디렉터리의 그룹을 따라야 한다"


def test_adopt_ownership_is_silent_without_permission(tmp_path: Path, monkeypatch):
    """권한이 없어도 zone 생성 자체를 실패시키지 않는다."""
    import os

    from dns_manager.core import zoneadmin

    target = tmp_path / "x.zone"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(os, "chown", lambda *a, **kw: (_ for _ in ()).throw(PermissionError()))
    zoneadmin.adopt_zone_dir_ownership(target)  # 예외가 새어 나오면 안 된다


# --------------------------- zone 정의 출처 ---------------------------


def test_zone_reports_defining_file(env):
    """목록에 보이는 zone 이 어느 파일에 적혀 있는지 알려야 한다."""
    client, _, zones_conf, _ = env
    rows = {z["name"]: z for z in client.get("/api/zones").json()}
    assert rows["example.local"]["source_file"] == str(zones_conf)
    assert client.get("/api/zones/example.local").json()["source_file"] == str(zones_conf)


def test_zone_defined_in_other_include_can_be_deleted(env, tmp_path: Path):
    """zones.conf 가 아닌 다른 include 에 적힌 zone 도 지울 수 있어야 한다.

    한 파일에만 기대면 "목록에는 보이는데 지울 수 없는" zone 이 생긴다.
    """
    client, named_dir, zones_conf, named_conf = env
    extra = named_conf.parent / "extra-zones.conf"
    extra.write_text(
        '// 다른 곳에 적힌 zone\nzone "elsewhere.local" IN {\n    type master;\n'
        f'    file "{named_dir}/elsewhere.zone";\n}};\n',
        encoding="utf-8",
    )
    (named_dir / "elsewhere.zone").write_text(
        "$TTL 60\n@ IN SOA ns1.elsewhere.local. a.elsewhere.local. ( 1 2 3 4 5 )\n"
        "@ IN NS ns1.elsewhere.local.\n",
        encoding="utf-8",
    )
    named_conf.write_text(
        named_conf.read_text(encoding="utf-8") + f'\ninclude "{extra}";\n', encoding="utf-8"
    )
    rows = {z["name"]: z for z in client.get("/api/zones").json()}
    assert "elsewhere.local" in rows, "다른 include 에 적힌 zone 도 목록에 나와야 한다"
    assert rows["elsewhere.local"]["source_file"] == str(extra)
    res = client.delete("/api/zones/elsewhere.local")
    assert res.status_code == 200 and res.json()["ok"], res.text
    assert "elsewhere.local" not in extra.read_text(encoding="utf-8")
    assert 'zone "example.local"' in zones_conf.read_text(encoding="utf-8")


# --------------------------- 수신·질의 설정 ---------------------------


def test_status_reports_external_blockers(env):
    """외부 질의를 막고 있는 설정을 짚어 줘야 한다."""
    client, _, _, named_conf = env
    named_conf.write_text(
        named_conf.read_text(encoding="utf-8").replace(
            "options {",
            'options {\n\tlisten-on port 53 { 127.0.0.1; };\n\tallow-query { localhost; };',
            1,
        ),
        encoding="utf-8",
    )
    body = client.get("/api/status").json()
    assert set(body["external_blockers"]) == {"listen-on", "allow-query"}
    assert body["allow_query"] == ["localhost"]


def test_listen_on_with_port_is_parsed_correctly(env):
    """`listen-on port 53 { ... }` 에서 'port'·'53' 을 주소로 세면 판단이 틀어진다."""
    client, *_ = env
    body = client.get("/api/status").json()
    assert body["listen_on"] == ["127.0.0.1"], body["listen_on"]
    # 픽스처는 루프백에만 수신한다 → 외부 질의를 못 받는다
    assert body["external_blockers"] == ["listen-on"]


def test_no_restrictions_means_open(env):
    """제한이 없으면 BIND 기본값(any)이고 외부에서 쓸 수 있다."""
    client, _, _, named_conf = env
    named_conf.write_text(
        named_conf.read_text(encoding="utf-8").replace("\tlisten-on port 53 { 127.0.0.1; };\n", ""),
        encoding="utf-8",
    )
    assert client.get("/api/status").json()["external_blockers"] == []


def test_set_server_access_opens_queries(env):
    client, _, _, named_conf = env
    named_conf.write_text(
        named_conf.read_text(encoding="utf-8").replace(
            "options {",
            'options {\n\tlisten-on port 53 { 127.0.0.1; };\n\tallow-query { localhost; };',
            1,
        ),
        encoding="utf-8",
    )
    assert client.get("/api/status").json()["external_blockers"]

    res = client.put("/api/server/access", json={"listen_on": ["any"], "allow_query": ["any"]})
    assert res.status_code == 200 and res.json()["ok"], res.text

    text = named_conf.read_text(encoding="utf-8")
    assert "listen-on port 53 { any; };" in text
    assert "allow-query { any; };" in text
    assert client.get("/api/status").json()["external_blockers"] == []


def test_set_server_access_records_history(env):
    client, _, _, named_conf = env
    client.put(
        "/api/server/access",
        json={"allow_query": ["10.0.0.0/8"]},
        headers={"X-Forwarded-User": "haedong"},
    )
    row = client.get("/api/history").json()[0]
    assert "수신·질의" in row["summary"] and row["author"] == "haedong"


def test_set_server_access_requires_a_change(env):
    client, *_ = env
    assert client.put("/api/server/access", json={}).status_code == 400
