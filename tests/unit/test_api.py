"""API 테스트.

named-checkconf / rndc 를 가짜 실행파일로 대체해 명령 호출 경로까지 실제로 통과시킨다
(모듈을 monkeypatch 하면 subprocess 래퍼의 버그를 놓치게 된다).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dns_manager.api.app import create_app
from dns_manager.config import AppConfig, BindConfig, Config

CHECKCONF_TEMPLATE = """options {{
\tdirectory "{zones_dir}";
\tforwarders {{
\t\t192.168.2.1;
\t}};
\tforward first;
}};
zone "example.local" IN {{
\ttype master;
\tfile "example.local.zone";
\tallow-update {{ none; }};
}};
zone "generate.local" IN {{
\ttype master;
\tfile "generate.zone";
}};
zone "corp.partner.example" IN {{
\ttype forward;
\tforward only;
\tforwarders {{ 10.9.8.7; }};
}};
"""

# 파일의 serial(2026100101)과 다른 값을 돌려줘 out_of_sync 경로를 검증한다.
RNDC_SCRIPT = """#!/bin/sh
case "$1" in
  status)
    echo "version: BIND 9.16.23-RH (fake)"
    echo "server is up and running"
    ;;
  zonestatus)
    if [ "$2" = "example.local" ]; then
      echo "name: example.local"
      echo "type: master"
      echo "serial: 2026100100"
      echo "loaded: Thu, 01 Oct 2026 01:00:00 GMT"
    else
      echo "rndc: 'zonestatus' failed: not found" >&2
      exit 1
    fi
    ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
"""

DIG_SCRIPT = """#!/bin/sh
echo ";; ANSWER SECTION:"
echo "www.example.local.\t3600\tIN\tA\t192.168.10.21"
"""


def _script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def client(tmp_path: Path, zones_dir: Path) -> TestClient:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    conf_out = tmp_path / "checkconf.txt"
    conf_out.write_text(CHECKCONF_TEMPLATE.format(zones_dir=zones_dir), encoding="utf-8")

    checkconf = _script(bin_dir / "named-checkconf", f'#!/bin/sh\ncat "{conf_out}"\n')
    rndc = _script(bin_dir / "rndc", RNDC_SCRIPT)
    dig = _script(bin_dir / "dig", DIG_SCRIPT)

    # 실제 레이아웃처럼 named.conf 와 include 를 만들어 둔다
    # (options 블록 위치 탐색이 실제 파일을 읽는다)
    named_conf = tmp_path / "named.conf"
    zones_conf = tmp_path / "zones.conf"
    zones_conf.write_text('// zones\n', encoding="utf-8")
    named_conf.write_text(
        f'options {{\n\tdirectory "{zones_dir}";\n}};\n\ninclude "{zones_conf}";\n', encoding="utf-8"
    )

    cfg = Config(
        bind=BindConfig(
            named_conf=named_conf,
            zones_conf=zones_conf,
            named_checkconf=str(checkconf),
            rndc=str(rndc),
            dig=str(dig),
        ),
        app=AppConfig(state_dir=tmp_path / "state", backup_dir=tmp_path / "state" / "backups"),
    )
    os.environ.pop("DNS_MANAGER_CONFIG", None)
    return TestClient(create_app(cfg))


def test_healthz(client: TestClient):
    assert client.get("/healthz").json()["status"] == "ok"


def test_console_page_renders(client: TestClient):
    res = client.get("/")
    assert res.status_code == 200
    assert "dns_manager" in res.text
    assert "View ▸ Advanced" in res.text


def test_status_reports_server_forwarders(client: TestClient):
    body = client.get("/api/status").json()
    assert body["running"] is True
    assert body["checkconf_ok"] is True
    assert body["server_forwarders"] == ["192.168.2.1"]
    assert body["server_forward_policy"] == "first"
    assert body["zone_count"] == 3


def test_zone_list_and_sync_state(client: TestClient):
    zones = {z["name"]: z for z in client.get("/api/zones").json()}
    assert zones["example.local"]["record_count"] > 0
    # 파일 serial 2026100101 vs 적재 2026100100 → reload 필요
    assert zones["example.local"]["file_serial"] == 2026100101
    assert zones["example.local"]["loaded_serial"] == 2026100100
    assert zones["example.local"]["out_of_sync"] is True
    assert zones["corp.partner.example"]["category"] == "conditional_forwarder"


def test_forwarders_endpoint_lists_both_scopes(client: TestClient):
    body = client.get("/api/forwarders").json()
    scopes = {f["scope"]: f for f in body}
    assert scopes["server"]["forwarders"] == ["192.168.2.1"]
    assert scopes["server"]["domain"] is None
    assert scopes["conditional"]["domain"] == "corp.partner.example"
    assert scopes["conditional"]["policy"] == "only"


def test_zone_detail_hides_soa_in_basic_view(client: TestClient):
    basic = client.get("/api/zones/example.local").json()
    assert all(r["type"] != "SOA" for r in basic["records"])
    assert basic["hidden_record_count"] >= 1

    advanced = client.get("/api/zones/example.local?advanced=true").json()
    assert any(r["type"] == "SOA" for r in advanced["records"])
    assert advanced["hidden_record_count"] == 0


def test_zone_detail_type_filter(client: TestClient):
    body = client.get("/api/zones/example.local?type=a").json()
    assert body["records"]
    assert {r["type"] for r in body["records"]} == {"A"}


def test_zone_detail_marks_generate_zone_not_table_editable(client: TestClient):
    body = client.get("/api/zones/generate.local").json()
    assert body["unsupported_directives"] == ["$GENERATE"]
    assert body["table_editable"] is False


def test_forward_zone_has_no_zone_file(client: TestClient):
    body = client.get("/api/zones/corp.partner.example").json()
    assert body["forwarders"] == ["10.9.8.7"]
    assert body["forward_policy"] == "only"
    assert body["problem"]
    assert body["records"] == []


def test_raw_zone_text(client: TestClient):
    body = client.get("/api/zones/example.local/raw").json()
    assert "SOA" in body["text"]
    assert body["version"]


def test_raw_rejects_zone_without_file(client: TestClient):
    assert client.get("/api/zones/corp.partner.example/raw").status_code == 409


def test_unknown_zone_is_404(client: TestClient):
    assert client.get("/api/zones/nope.local").status_code == 404


def test_query_endpoint(client: TestClient):
    body = client.get("/api/query?name=www.example.local&type=A").json()
    assert "192.168.10.21" in body["output"]


def test_record_types_listing(client: TestClient):
    body = client.get("/api/record-types").json()
    assert "A" in body["primary"] and "CAA" in body["other"]


def test_write_endpoints_are_registered(client: TestClient):
    """P2: 쓰기 경로가 열렸다. 각 경로는 core.apply 트랜잭션을 지난다."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/api/zones/{zone}/records"]
    assert "put" in paths["/api/zones/{zone}/records/{record_id}"]
    assert "post" in paths["/api/zones/{zone}/records/delete"]
    assert "put" in paths["/api/zones/{zone}/raw"]
    assert "post" in paths["/api/history/{change_id}/rollback"]


def test_server_forwarder_row_always_present(tmp_path: Path, zones_dir: Path):
    """전역 전달자가 설정되지 않아도 목록에 행이 나와야 한다.

    빠지면 '설정이 없음'과 '화면에 없음'을 구분할 수 없다.
    """
    bin_dir = tmp_path / "bin2"
    bin_dir.mkdir()
    conf = tmp_path / "noforward.txt"
    conf.write_text(
        f'options {{\n\tdirectory "{zones_dir}";\n}};\n'
        'zone "example.local" IN {\n\ttype master;\n\tfile "example.local.zone";\n};\n',
        encoding="utf-8",
    )
    checkconf = _script(bin_dir / "named-checkconf", f'#!/bin/sh\ncat "{conf}"\n')
    rndc = _script(bin_dir / "rndc", RNDC_SCRIPT)
    cfg = Config(bind=BindConfig(named_checkconf=str(checkconf), rndc=str(rndc)))
    client = TestClient(create_app(cfg))

    body = client.get("/api/forwarders").json()
    server_rows = [f for f in body if f["scope"] == "server"]
    assert len(server_rows) == 1
    assert server_rows[0]["forwarders"] == []
    assert server_rows[0]["configured"] is False
    assert server_rows[0]["domain"] is None


def test_configured_flag_true_when_forwarders_exist(client: TestClient):
    body = client.get("/api/forwarders").json()
    server = next(f for f in body if f["scope"] == "server")
    assert server["configured"] is True


def test_status_separates_checkconf_errors_from_parsed_config(client: TestClient):
    """정상일 때 '오류 출력'은 비어 있고, 설정 전문은 별도 필드로 간다."""
    body = client.get("/api/status").json()
    assert body["checkconf_ok"] is True
    assert body["checkconf_output"] == ""
    assert 'zone "example.local"' in body["parsed_config"]


def test_notice_endpoint_serves_third_party_attribution(client: TestClient):
    """MIT/BSD/ISC 는 저작권 고지 보존이 의무다. 배포본에서 확인 가능해야 한다."""
    res = client.get("/api/notice")
    assert res.status_code == 200
    body = res.text
    assert "dnspython" in body and "ISC" in body
    assert "Copyright (c) 2026 haedong" in body


def test_console_page_shows_copyright(client: TestClient):
    assert "© 2026 haedong" in client.get("/").text


def test_status_reports_detected_layout_fields(client: TestClient):
    """레이아웃 감지 결과가 상태에 실려야 한다(필드 누락을 조용히 넘기지 않기 위함)."""
    body = client.get("/api/status").json()
    assert "detected_family" in body
    assert "options_file" in body
    assert body["options_file"] is not None, "options 블록이 있는 파일을 찾아야 한다"


def test_dynamic_zone_is_synced_before_reading(tmp_path: Path, zones_dir: Path, monkeypatch):
    """동적 zone 은 journal 때문에 파일이 뒤처진다. 읽기 전에 rndc sync 로 반영해야
    화면이 서버 상태와 어긋나지 않는다."""
    from dns_manager.core import service

    calls: list[str] = []

    def fake_rndc(cfg, *args):
        calls.append(" ".join(args))
        from dns_manager.core.commands import CommandResult

        return CommandResult(argv=("rndc", *args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("dns_manager.core.apply._rndc", fake_rndc)

    from dns_manager.core.layout import ZoneEntry

    entry = ZoneEntry(
        name="dyn.local", zone_type="master", view=None, file=zones_dir / "example.local.zone",
        allow_update=("key", "ddns-key"),
    )
    # 적재 serial 이 파일보다 앞서면 sync 한다
    assert service.sync_if_stale(None, entry, 2026100105, 2026100101) is True
    assert calls == ["sync dyn.local"]

    calls.clear()
    # 같으면 건드리지 않는다 (불필요한 쓰기를 만들지 않는다)
    assert service.sync_if_stale(None, entry, 2026100101, 2026100101) is False
    assert calls == []

    calls.clear()
    static = ZoneEntry(name="s.local", zone_type="master", view=None, file=entry.file)
    assert service.sync_if_stale(None, static, 5, 1) is False
    assert calls == []


def test_notice_is_found_from_installed_layout(monkeypatch, tmp_path: Path, client: TestClient):
    """설치본(/opt/dns-manager/.venv)에서도 NOTICE.md 를 찾아야 한다.

    배포 메타데이터·설치 접두사까지 뒤진다. 못 찾으면 고지가 사라져 라이선스 의무를 어긴다.
    """
    from dns_manager.api import routes

    prefix = tmp_path / "opt" / "dns-manager"
    (prefix / ".venv").mkdir(parents=True)
    (prefix / "NOTICE.md").write_text("# NOTICE\ndnspython ... ISC\n", encoding="utf-8")
    monkeypatch.setattr(routes.sys, "prefix", str(prefix / ".venv"))

    assert any(c == prefix / "NOTICE.md" for c in routes._notice_candidates())


def test_files_endpoint_lists_targets_and_accesses(client: TestClient):
    """'어떤 파일을 읽고 쓰는가' 화면의 데이터."""
    from dns_manager.core import fileaudit

    fileaudit.clear()
    client.get("/api/zones")  # 읽기를 한 번 일으킨다

    body = client.get("/api/files").json()
    paths = {f["path"]: f for f in body["files"]}
    assert any(f["role"] == "named-conf" for f in body["files"])
    assert any(f["role"] == "zone-file" for f in body["files"])
    assert any(f["role"] == "state" for f in body["files"])

    # 실제 접근이 기록되어야 한다
    assert body["touched"] >= 1
    assert body["events"], "접근 기록이 있어야 한다"
    assert all("ok" in e and "role_label" in e for e in body["events"])

    zone_rows = [f for f in body["files"] if f["role"] == "zone-file" and f["exists"]]
    assert zone_rows and zone_rows[0]["readable"] is True


def test_files_endpoint_marks_missing_files(client: TestClient):
    body = client.get("/api/files").json()
    for row in body["files"]:
        if not row["exists"]:
            assert row["readable"] is False and row["size"] is None


def test_files_endpoint_does_not_expose_secrets(client: TestClient):
    """키 파일은 경로와 역할만 — 내용은 응답에 담지 않는다."""
    body = client.get("/api/files").text
    assert "secret " not in body
    for row in client.get("/api/files").json()["files"]:
        if row["role"] == "key-file":
            assert row["secret"] is True


def test_write_router_routes_are_registered(client: TestClient):
    """쓰기 라우터의 경로가 빠짐없이 앱에 올라와야 한다.

    배포 중 한 번은 설치 경로에 남은 옛 패키지가 설치본을 가려, 새 라우트만 조용히
    사라진 적이 있다. 목록을 못 박아 두면 그런 상황을 테스트로 잡지 못한다.
    """
    paths = set(client.get("/openapi.json").json()["paths"])
    expected = {
        "/api/files",
        "/api/settings",
        "/api/history",
        "/api/zones",
        "/api/zones/preview",
        "/api/zones/{zone}/soa",
        "/api/zones/{zone}/options",
        "/api/zones/{zone}/delegations",
        "/api/zones/{zone}/records",
        "/api/forwarders/server",
    }
    missing = expected - paths
    assert not missing, f"라우트가 등록되지 않았습니다: {sorted(missing)}"


def test_files_endpoint_marks_history_only_paths(client: TestClient):
    """이미 지운 파일이 접근 기록에 남아도 '문제'로 보이면 안 된다."""
    from dns_manager.core import fileaudit

    fileaudit.clear()
    fileaudit.record("/var/named/deleted.zone", "write", "zone-file")

    rows = {f["path"]: f for f in client.get("/api/files").json()["files"]}
    ghost = rows["/var/named/deleted.zone"]
    assert ghost["from_history"] is True
    assert ghost["exists"] is False
    # 현재 대상인 파일은 from_history 가 아니다
    assert all(not f["from_history"] for p, f in rows.items() if f["role"] == "named-conf")


def test_version_is_consistent_everywhere():
    """pyproject 와 패키지의 버전이 어긋나면 설치 파일 이름과 UI 표기가 달라진다."""
    import tomllib
    from pathlib import Path

    import dns_manager

    root = Path(dns_manager.__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():  # 설치본에서는 소스 트리가 없다
        import importlib.metadata

        assert importlib.metadata.version("dns-manager") == dns_manager.__version__
        return

    with pyproject.open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]
    assert declared == dns_manager.__version__, f"pyproject {declared} != 패키지 {dns_manager.__version__}"


def test_zone_file_changed_after_load_is_detected(tmp_path, zones_dir):
    """serial 을 올리지 않고 파일만 고치면 serial 비교로는 못 잡는다.

    "파일에는 있는데 응답은 NXDOMAIN" 의 실제 원인이었다. 적재 시각과 파일 수정 시각을
    비교하면 잡힌다.
    """
    import os
    import time
    from datetime import datetime, timedelta, timezone

    from dns_manager.core import server as server_mod
    from dns_manager.core import service
    from dns_manager.core.layout import ZoneEntry

    zone_file = tmp_path / "z.zone"
    zone_file.write_text("x", encoding="utf-8")
    entry = ZoneEntry(name="z.local", zone_type="master", view=None, file=zone_file)

    loaded_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    status = server_mod.ZoneStatus(
        name="z.local", available=True, serial=1,
        loaded=loaded_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
    )
    assert status.loaded_at is not None, "rndc 의 'last loaded:' 를 읽어야 한다"

    # 적재 이후에 파일을 고쳤다 → 잡혀야 한다
    assert service.file_changed_since_load(entry, status) is True

    # 적재가 파일보다 나중이면 정상
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    fresh = server_mod.ZoneStatus(
        name="z.local", available=True, serial=1,
        loaded=later.strftime("%a, %d %b %Y %H:%M:%S GMT"),
    )
    assert service.file_changed_since_load(entry, fresh) is False

    # 동적 zone 은 named 가 파일을 쓰므로 이 비교를 하지 않는다
    dynamic = ZoneEntry(
        name="z.local", zone_type="master", view=None, file=zone_file, allow_update=("key", "k")
    )
    assert service.file_changed_since_load(dynamic, status) is False


def test_rndc_last_loaded_is_parsed():
    """'last loaded:' 를 'loaded:' 로만 찾으면 영영 None 이 된다(실제 버그였다)."""
    from dns_manager.core import server as server_mod

    output = (
        "name: example.local\ntype: primary\nserial: 2026100370\n"
        "last loaded: Fri, 02 Oct 2026 00:28:01 GMT\ndynamic: no\n"
    )
    match = server_mod._LOADED_RE.search(output)
    assert match and "Fri, 02 Oct 2026" in match.group(1)
