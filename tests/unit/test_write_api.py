"""쓰기 API 테스트 — 가짜 BIND 도구로 subprocess 경로까지 실제 통과시킨다."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dns_manager.api.app import create_app
from dns_manager.config import AppConfig, BindConfig, Config

CONF = """options {{
\tdirectory "{zones_dir}";
}};
zone "example.local" IN {{
\ttype master;
\tfile "example.local.zone";
}};
zone "10.168.192.in-addr.arpa" IN {{
\ttype master;
\tfile "10.168.192.in-addr.arpa.rev";
}};
zone "secondary.local" IN {{
\ttype slave;
\tfile "secondary.zone";
\tmasters {{ 10.0.0.1; }};
}};
"""

REVERSE_ZONE = """$TTL 3600
@       IN  SOA ns1.example.local. admin.example.local. (
                2026100101 3600 600 604800 3600 )
@       IN  NS  ns1.example.local.
10      IN  PTR ns1.example.local.
21      IN  PTR www.example.local.
"""

CHECKZONE = """#!/bin/sh
grep -qE '999\\.999|BADZONE' "$2" && { echo "$2:9: bad dotted quad" >&2; exit 1; }
echo "zone $1/IN: OK"
"""
CHECKCONF = """#!/bin/sh
cat "$CONF_OUT"
"""
RNDC = """#!/bin/sh
case "$1" in
  reload|reconfig) echo "zone reload queued" ;;
  zonestatus)
    f="$ZONES_DIR/$2.zone"
    [ -f "$f" ] || f="$ZONES_DIR/$2.rev"
    echo "name: $2"; echo "type: master"
    echo "serial: $(grep -oE '[0-9]{10}' "$f" 2>/dev/null | head -1)"
    ;;
  status) echo "version: fake" ;;
esac
"""


def _sh(path: Path, body: str, env: dict[str, str]) -> Path:
    exports = "".join(f'{k}="{v}"\nexport {k}\n' for k, v in env.items())
    path.write_text("#!/bin/sh\n" + exports + body.split("\n", 1)[1], encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def client(tmp_path: Path, zones_dir: Path) -> TestClient:
    named = tmp_path / "var-named"
    named.mkdir()
    (named / "example.local.zone").write_text(
        (zones_dir / "example.local.zone").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (named / "10.168.192.in-addr.arpa.rev").write_text(REVERSE_ZONE, encoding="utf-8")

    conf_out = tmp_path / "conf.txt"
    conf_out.write_text(CONF.format(zones_dir=named), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {"CONF_OUT": str(conf_out), "ZONES_DIR": str(named)}
    cfg = Config(
        bind=BindConfig(
            named_conf=tmp_path / "named.conf",
            zones_conf=tmp_path / "zones.conf",
            named_checkzone=str(_sh(bin_dir / "named-checkzone", CHECKZONE, env)),
            named_checkconf=str(_sh(bin_dir / "named-checkconf", CHECKCONF, env)),
            rndc=str(_sh(bin_dir / "rndc", RNDC, env)),
        ),
        app=AppConfig(state_dir=tmp_path / "state", backup_dir=tmp_path / "state" / "backups"),
    )
    client = TestClient(create_app(cfg))
    client.zones_dir = named  # type: ignore[attr-defined]
    return client


def zone_text(client: TestClient, name: str = "example.local.zone") -> str:
    return (client.zones_dir / name).read_text(encoding="utf-8")  # type: ignore[attr-defined]


def records(client: TestClient, zone: str = "example.local") -> list[dict]:
    return client.get(f"/api/zones/{zone}?advanced=true").json()["records"]


# --------------------------- 추가 ---------------------------


def test_create_record(client: TestClient):
    res = client.post("/api/zones/example.local/records", json={"name": "app", "type": "A", "data": "192.168.10.50"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["ok"] and body["result"]["status"] == "applied"
    assert body["result"]["serial_after"] > body["result"]["serial_before"]
    assert any(r["name"] == "app" and r["data"] == "192.168.10.50" for r in records(client))


def test_create_preserves_comments(client: TestClient):
    client.post("/api/zones/example.local/records", json={"name": "app", "type": "A", "data": "10.0.0.1"})
    assert zone_text(client).startswith("; 테스트용 정방향 zone")


def test_create_with_ttl(client: TestClient):
    client.post("/api/zones/example.local/records", json={"name": "ttl1", "type": "A", "data": "10.0.0.9", "ttl": 60})
    record = next(r for r in records(client) if r["name"] == "ttl1")
    assert record["ttl"] == 60


def test_create_mx_requires_preference(client: TestClient):
    res = client.post("/api/zones/example.local/records", json={"name": "@", "type": "MX", "data": "mail.x."})
    assert res.status_code == 400
    assert "MX" in res.json()["detail"]


def test_create_invalid_address_is_rejected_by_app(client: TestClient):
    before = zone_text(client)
    res = client.post("/api/zones/example.local/records", json={"name": "bad", "type": "A", "data": "999.999.999.999"})
    assert res.status_code == 400
    assert zone_text(client) == before


def test_create_duplicate_is_rejected(client: TestClient):
    payload = {"name": "dup", "type": "A", "data": "10.0.0.5"}
    assert client.post("/api/zones/example.local/records", json=payload).status_code == 201
    res = client.post("/api/zones/example.local/records", json=payload)
    assert res.status_code == 400 and "이미" in res.json()["detail"]


def test_create_on_slave_zone_is_refused(client: TestClient):
    res = client.post("/api/zones/secondary.local/records", json={"name": "x", "type": "A", "data": "10.0.0.1"})
    assert res.status_code == 400


def test_create_on_unknown_zone_is_404(client: TestClient):
    res = client.post("/api/zones/nope.local/records", json={"name": "x", "type": "A", "data": "10.0.0.1"})
    assert res.status_code == 404


# --------------------------- PTR 연동 ---------------------------


def test_create_host_with_associated_ptr(client: TestClient):
    """Windows DNS Manager 의 'Create associated pointer (PTR) record' 동작."""
    res = client.post(
        "/api/zones/example.local/records",
        json={"name": "ptrhost", "type": "A", "data": "192.168.10.60", "create_ptr": True},
    )
    body = res.json()
    assert body["ok"]
    assert len(body["related"]) == 1 and body["related"][0]["ok"]

    reverse = records(client, "10.168.192.in-addr.arpa")
    assert any(r["name"] == "60" and r["data"] == "ptrhost.example.local." for r in reverse)


def test_ptr_skipped_when_no_reverse_zone(client: TestClient):
    res = client.post(
        "/api/zones/example.local/records",
        json={"name": "far", "type": "A", "data": "10.9.9.9", "create_ptr": True},
    )
    body = res.json()
    assert body["ok"] and body["related"] == []
    assert any("역방향 zone이" in n or "역방향 zone 이" in n for n in body["notes"]), body["notes"]


def test_delete_host_also_deletes_ptr(client: TestClient):
    client.post(
        "/api/zones/example.local/records",
        json={"name": "ptrhost", "type": "A", "data": "192.168.10.61", "create_ptr": True},
    )
    target = next(r for r in records(client) if r["name"] == "ptrhost")
    res = client.post(
        "/api/zones/example.local/records/delete", json={"ids": [target["id"]], "delete_ptr": True}
    )
    assert res.json()["ok"]
    assert not any(r["name"] == "61" for r in records(client, "10.168.192.in-addr.arpa"))


# --------------------------- 수정 ---------------------------


def test_update_record_value(client: TestClient):
    target = next(r for r in records(client) if r["name"] == "www" and r["type"] == "A")
    res = client.put(
        f"/api/zones/example.local/records/{target['id']}",
        json={"name": "www", "type": "A", "data": "192.168.10.222"},
    )
    assert res.status_code == 200 and res.json()["ok"]
    assert any(r["data"] == "192.168.10.222" for r in records(client))
    assert not any(r["data"] == "192.168.10.21" for r in records(client))


def test_update_ttl_only(client: TestClient):
    target = next(r for r in records(client) if r["name"] == "mail" and r["type"] == "A")
    client.put(
        f"/api/zones/example.local/records/{target['id']}",
        json={"name": "mail", "type": "A", "data": target["data"], "ttl": 120},
    )
    assert next(r for r in records(client) if r["name"] == "mail" and r["type"] == "A")["ttl"] == 120


def test_update_rename(client: TestClient):
    target = next(r for r in records(client) if r["name"] == "api")
    client.put(
        f"/api/zones/example.local/records/{target['id']}",
        json={"name": "api2", "type": "A", "data": target["data"]},
    )
    names = {r["name"] for r in records(client)}
    assert "api2" in names and "api" not in names


def test_update_unknown_record_is_404(client: TestClient):
    res = client.put(
        "/api/zones/example.local/records/ffffffffffffffff",
        json={"name": "x", "type": "A", "data": "10.0.0.1"},
    )
    assert res.status_code == 404


def test_stale_version_is_conflict(client: TestClient):
    version = client.get("/api/zones/example.local").json()["version"]
    client.post("/api/zones/example.local/records", json={"name": "first", "type": "A", "data": "10.0.0.1"})
    res = client.post(
        "/api/zones/example.local/records",
        json={"name": "second", "type": "A", "data": "10.0.0.2", "expected_version": version},
    )
    assert res.status_code == 409


# --------------------------- 삭제 ---------------------------


def test_delete_single(client: TestClient):
    target = next(r for r in records(client) if r["name"] == "mail2")
    res = client.post("/api/zones/example.local/records/delete", json={"ids": [target["id"]]})
    assert res.json()["ok"]
    assert not any(r["name"] == "mail2" for r in records(client))


def test_delete_multiple_in_one_transaction(client: TestClient):
    rows = records(client)
    ids = [r["id"] for r in rows if r["name"] in {"mail2", "api"}]
    res = client.post("/api/zones/example.local/records/delete", json={"ids": ids})
    body = res.json()
    assert body["ok"]
    # 한 번의 변경으로 처리되어야 한다
    assert body["result"]["change_id"] is not None
    remaining = {r["name"] for r in records(client)}
    assert "mail2" not in remaining and "api" not in remaining


def test_delete_soa_is_refused(client: TestClient):
    soa = next(r for r in records(client) if r["type"] == "SOA")
    res = client.post("/api/zones/example.local/records/delete", json={"ids": [soa["id"]]})
    assert res.status_code == 400


# --------------------------- raw 저장 ---------------------------


def test_save_raw(client: TestClient):
    raw = client.get("/api/zones/example.local/raw").json()
    new_text = raw["text"] + "rawadd  IN  A  10.0.0.77\n"
    res = client.put(
        "/api/zones/example.local/raw", json={"text": new_text, "expected_version": raw["version"]}
    )
    assert res.status_code == 200 and res.json()["ok"]
    assert any(r["name"] == "rawadd" for r in records(client))


def test_save_raw_with_stale_version_conflicts(client: TestClient):
    raw = client.get("/api/zones/example.local/raw").json()
    client.post("/api/zones/example.local/records", json={"name": "other", "type": "A", "data": "10.0.0.3"})
    res = client.put(
        "/api/zones/example.local/raw", json={"text": raw["text"], "expected_version": raw["version"]}
    )
    assert res.status_code == 409


def test_save_raw_invalid_is_rejected_without_writing(client: TestClient):
    raw = client.get("/api/zones/example.local/raw").json()
    before = zone_text(client)
    res = client.put("/api/zones/example.local/raw", json={"text": raw["text"] + "BADZONE\n"})
    body = res.json()
    assert not body["ok"] and body["result"]["status"] == "rejected"
    assert "bad dotted quad" in body["result"]["check_output"]
    assert zone_text(client) == before


# --------------------------- 이력 / 롤백 ---------------------------


def test_history_lists_changes(client: TestClient):
    client.post("/api/zones/example.local/records", json={"name": "h1", "type": "A", "data": "10.0.0.11"})
    rows = client.get("/api/history").json()
    assert rows and rows[0]["target"] == "example.local"
    assert rows[0]["status"] == "applied"
    assert "h1" in rows[0]["summary"]


def test_history_detail_includes_diff(client: TestClient):
    created = client.post(
        "/api/zones/example.local/records", json={"name": "h2", "type": "A", "data": "10.0.0.12"}
    ).json()
    change = client.get(f"/api/history/{created['result']['change_id']}").json()
    assert "+h2" in change["diff"]
    assert change["has_backup"] is True


def test_rollback_restores_previous_state(client: TestClient):
    created = client.post(
        "/api/zones/example.local/records", json={"name": "tmp", "type": "A", "data": "10.0.0.13"}
    ).json()
    assert any(r["name"] == "tmp" for r in records(client))

    res = client.post(f"/api/history/{created['result']['change_id']}/rollback")
    assert res.status_code == 200 and res.json()["ok"]
    assert not any(r["name"] == "tmp" for r in records(client))


def test_rollback_unknown_change_is_404(client: TestClient):
    assert client.post("/api/history/9999/rollback").status_code == 404


def test_author_is_recorded_from_proxy_header(client: TestClient):
    client.post(
        "/api/zones/example.local/records",
        json={"name": "whois", "type": "A", "data": "10.0.0.14"},
        headers={"X-Forwarded-User": "haedong"},
    )
    assert client.get("/api/history").json()[0]["author"] == "haedong"


# --------------------------- reload ---------------------------


def test_reload_endpoint(client: TestClient):
    res = client.post("/api/zones/example.local/reload").json()
    assert res["ok"] and "queued" in res["output"]


# --------------------------- P4: SOA / 위임 / zone 옵션 ---------------------------


def test_read_soa(client: TestClient):
    soa = client.get("/api/zones/example.local/soa").json()
    assert soa["primary"] == "ns1.example.local."
    assert soa["responsible"] == "admin.example.local."
    assert int(soa["refresh"]) == 3600


def test_update_soa_fields(client: TestClient):
    res = client.put(
        "/api/zones/example.local/soa",
        json={"responsible": "dnsadmin@example.local", "refresh": 7200, "minimum": 600},
    )
    assert res.status_code == 200 and res.json()["ok"], res.text
    soa = client.get("/api/zones/example.local/soa").json()
    assert soa["responsible"] == "dnsadmin.example.local."
    assert int(soa["refresh"]) == 7200
    assert int(soa["minimum"]) == 600
    # 건드리지 않은 값은 유지
    assert soa["primary"] == "ns1.example.local."


def test_update_soa_bumps_serial_and_keeps_comments(client: TestClient):
    before = int(client.get("/api/zones/example.local/soa").json()["serial"])
    client.put("/api/zones/example.local/soa", json={"retry": 900})
    after = int(client.get("/api/zones/example.local/soa").json()["serial"])
    assert after > before, "SOA 를 고쳐도 serial 은 올라가야 한다"
    assert zone_text(client).startswith("; 테스트용 정방향 zone")


def test_soa_serial_is_not_user_settable(client: TestClient):
    """serial 을 직접 낮추면 secondary 가 갱신을 받지 못한다 — 입력으로 받지 않는다."""
    schema = client.get("/openapi.json").json()["components"]["schemas"]["SoaIn"]
    assert "serial" not in schema["properties"]


def test_add_delegation_with_glue(client: TestClient):
    res = client.post(
        "/api/zones/example.local/delegations",
        json={"name": "south", "servers": [{"name": "ns1.south.example.local.", "address": "192.168.10.40"}]},
    )
    assert res.status_code == 201, res.text
    rows = records(client)
    ns = [r for r in rows if r["type"] == "NS" and r["name"] == "south"]
    glue = [r for r in rows if r["name"] == "ns1.south" and r["type"] == "A"]
    assert ns and glue, "위임 구간 안쪽 네임서버에는 glue 가 필요하다"
    assert glue[0]["data"] == "192.168.10.40"


def test_delegation_to_outside_server_needs_no_glue(client: TestClient):
    res = client.post(
        "/api/zones/example.local/delegations",
        json={"name": "west", "servers": [{"name": "ns1.partner.example."}]},
    )
    assert res.status_code == 201, res.text
    rows = records(client)
    assert any(r["type"] == "NS" and r["name"] == "west" for r in rows)
    assert not any(r["name"].startswith("ns1.partner") for r in rows)


def test_delegation_rejects_glue_outside_delegation(client: TestClient):
    """위임 구간 밖 이름의 주소를 상위 zone 에 넣으면 권위 밖 데이터가 된다."""
    res = client.post(
        "/api/zones/example.local/delegations",
        json={"name": "east", "servers": [{"name": "ns1.other.example.", "address": "10.0.0.1"}]},
    )
    assert res.status_code == 400 and "밖" in res.json()["detail"]


def test_delegation_requires_glue_for_inside_server(client: TestClient):
    res = client.post(
        "/api/zones/example.local/delegations",
        json={"name": "north", "servers": [{"name": "ns1.north.example.local."}]},
    )
    assert res.status_code == 400 and "glue" in res.json()["detail"]


def test_delegation_accepts_fqdn_child_name(client: TestClient):
    res = client.post(
        "/api/zones/example.local/delegations",
        json={"name": "sub.example.local", "servers": [{"name": "ns1.partner.example."}]},
    )
    assert res.status_code == 201
    assert any(r["name"] == "sub" and r["type"] == "NS" for r in records(client))


def test_soa_responsible_accepts_email_form(client: TestClient):
    """운영자는 보통 메일 주소로 입력한다. SOA rname 표기로 바꿔 줘야 한다."""
    client.put("/api/zones/example.local/soa", json={"responsible": "first.last@example.local"})
    soa = client.get("/api/zones/example.local/soa").json()
    # 로컬 파트의 점은 이스케이프된다
    assert soa["responsible"] == "first\\.last.example.local."
