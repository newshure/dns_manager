"""브라우저 UI 점검 (Playwright + Chromium).

실제 BIND 를 관리 중인 dns_manager 인스턴스를 대상으로, 콘솔 트리 → zone 선택 →
레코드 테이블 → Advanced 토글 → Raw/Properties 탭 → 전달자 목록 흐름을 확인한다.

사용법:
    BASE=http://127.0.0.1:8100 python tests/smoke/ui_check.py [--shots DIR]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8100")
ZONE = os.environ.get("ZONE", "example.local")
REVERSE_ZONE = os.environ.get("REVERSE_ZONE", "10.168.192.in-addr.arpa")

results: list[tuple[bool, str]] = []
console_errors: list[str] = []

# 브라우저는 2xx 가 아닌 응답을 전부 콘솔 오류로 적는다. 앱이 그 응답을 받아 배너로
# 처리하는 경우(의도된 거부 등)까지 실패로 보면 음성 테스트를 할 수 없다.
# 진짜로 봐야 할 것은 처리되지 않은 JS 예외와 앱이 직접 찍은 console.error 다.
IGNORED_CONSOLE = ("Failed to load resource",)


def note_console(text: str) -> None:
    if not any(pattern in text for pattern in IGNORED_CONSOLE):
        console_errors.append(text)


PAGE: "Page | None" = None
SHOT_DIR: "Path | None" = None


def check(desc: str, fn) -> None:
    try:
        fn()
        results.append((True, desc))
        print(f"  ok   {desc}")
    except Exception as exc:  # noqa: BLE001 - 점검 도구이므로 모든 실패를 모아 보고한다
        results.append((False, desc))
        print(f"  FAIL {desc}: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
        _dump_failure(desc)


def _dump_failure(desc: str) -> None:
    """실패 순간의 화면 상태를 남긴다. 나중에 추측하지 않기 위해서다."""
    if PAGE is None:
        return
    try:
        banner = PAGE.locator("#banner")
        if banner.is_visible():
            print(f"       배너: {banner.inner_text()[:200]}")
        overlay = PAGE.locator("#modal-overlay")
        if overlay.is_visible():
            print(f"       모달 열림: {PAGE.locator('#modal-title').inner_text()[:80]}")
            print(f"       모달 본문: {PAGE.locator('#modal-body').inner_text()[:200]}")
        print(f"       현재 탭: {PAGE.locator('#tabs button.active').inner_text()}")
        print(f"       표 행 수: {PAGE.locator('table.grid tbody tr').count()}")
        if SHOT_DIR is not None:
            safe = "".join(c if c.isalnum() else "_" for c in desc)[:40]
            path = SHOT_DIR / f"fail-{safe}.png"
            PAGE.screenshot(path=str(path), full_page=True)
            print(f"       화면: {path}")
    except Exception as exc:  # noqa: BLE001 - 진단 출력이 점검을 막으면 안 된다
        print(f"       (상태를 남기지 못했습니다: {exc})")


def select_row(page: Page, name: str) -> None:
    """레코드 행을 고르고, 실제로 선택됐는지 확인한다.

    표를 다시 그리는 중에 클릭이 들어가면 교체되는 DOM 을 때려 선택이 조용히
    무시된다. 그 상태로 Delete 를 누르면 '직전에 고른 다른 레코드' 가 지워진다.
    선택 표시(tr.selected)를 확인하고 넘어가야 뒤따르는 단정이 거짓말을 하지 않는다.
    """
    row = page.locator(f'table.grid tbody tr[data-record]:has-text("{name}")').first
    row.click()
    try:
        expect(row).to_have_class(re.compile(r"\bselected\b"), timeout=3000)
    except AssertionError:
        # 다시 그리기와 겹쳤다면 한 번 더. 두 번째에도 안 되면 진짜 문제다.
        page.locator(f'table.grid tbody tr[data-record]:has-text("{name}")').first.click()
        expect(
            page.locator(f'table.grid tbody tr[data-record]:has-text("{name}")').first
        ).to_have_class(re.compile(r"\bselected\b"), timeout=3000)


def expect_gone(page: Page, name: str, timeout: int = 10000) -> None:
    """표에서 그 이름의 행이 사라질 때까지 기다린다.

    실패했을 때 어느 레코드였는지 알 수 있어야 한다 — 같은 모양의 단정이 한 점검에
    여럿 있으면 메시지만 보고는 범인을 못 고른다.
    """
    rows = page.locator(f'table.grid tbody tr:has-text("{name}")')
    try:
        expect(rows).to_have_count(0, timeout=timeout)
    except AssertionError as exc:
        raise AssertionError(f"{name} 행이 표에 남았다 ({rows.count()}건)") from exc


def select_zone(page: Page, zone: str) -> None:
    page.click(f'.tree .node:has(.label:text-is("{zone}"))')
    expect(page.locator("#detail-title")).to_have_text(zone, timeout=5000)


# 이 점검이 만들었다가 중간 실패로 남길 수 있는 것들. 시작 전에 지운다.
LEFTOVER_RECORDS = ("uitest", "uialias", "refreshprobe", "uisub", "ns1.uisub", "rawadded", "rawdyn", "uidyn")
LEFTOVER_ZONES = ("uizone.test",)


def cleanup_leftovers() -> None:
    """앞선 실행이 남긴 흔적을 지운다.

    한 번 실패하면 그 잔여물 때문에 다음 실행이 통째로 무너진다(중복 레코드 → 추가 실패 →
    이후 단계 연쇄 실패). 점검은 몇 번을 돌려도 같은 결과여야 한다.
    """
    import json
    import urllib.error
    import urllib.request

    def call(path: str, method: str = "GET", body: dict | None = None):
        request = urllib.request.Request(
            BASE + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError:
            return None
        except OSError:
            return None

    for zone in LEFTOVER_ZONES:
        call(f"/api/zones/{zone}?delete_file=true", "DELETE")

    for zone in (ZONE, REVERSE_ZONE, "dynamic.local"):
        detail = call(f"/api/zones/{zone}?advanced=true")
        if not detail:
            continue
        ids = {r["id"] for r in detail["records"] if r["name"] in LEFTOVER_RECORDS}
        # 짝이 되는 PTR 도 함께 남는다
        ids |= {
            r["id"]
            for r in detail["records"]
            if r["type"] == "PTR" and any(name in r["data"] for name in LEFTOVER_RECORDS)
        }
        if ids:
            call(f"/api/zones/{zone}/records/delete", "POST", {"ids": sorted(ids)})
            print(f"  (정리) {zone} 에서 {len(ids)}건 제거")


def run(page: Page, shots: Path | None) -> None:
    cleanup_leftovers()
    page.goto(BASE, wait_until="networkidle")

    check("콘솔 페이지 제목", lambda: expect(page).to_have_title("dns_manager"))
    check("named 실행 중 표시", lambda: expect(page.locator("#server-state .up")).to_be_visible(timeout=5000))
    check("Forward Lookup Zones 노드", lambda: expect(page.locator('.tree .label:text-is("Forward Lookup Zones")')).to_be_visible())
    check("Reverse Lookup Zones 노드", lambda: expect(page.locator('.tree .label:text-is("Reverse Lookup Zones")')).to_be_visible())
    check("Conditional Forwarders 노드", lambda: expect(page.locator('.tree .label:text-is("Conditional Forwarders")')).to_be_visible())
    check("서버 선택 시 zone 목록 표시", lambda: expect(page.locator("table.grid")).to_be_visible(timeout=5000))
    if shots:
        page.screenshot(path=str(shots / "01-server.png"), full_page=True)

    check("정방향 zone 선택", lambda: select_zone(page, ZONE))
    check("레코드 테이블 렌더", lambda: expect(page.locator("table.grid tbody tr").first).to_be_visible(timeout=5000))
    check("Name/Type/Data 열", lambda: expect(page.locator("table.grid th")).to_contain_text(["Name", "Type", "Data"]))
    check("www A 레코드 표시", lambda: expect(page.locator('table.grid tbody tr:has-text("www")').first).to_be_visible())
    check("기본 보기에서 SOA 숨김", lambda: expect(page.locator('table.grid tbody tr:has-text("SOA")')).to_have_count(0))
    if shots:
        page.screenshot(path=str(shots / "02-records.png"), full_page=True)

    def toggle_advanced() -> None:
        page.check("#advanced-view")
        expect(page.locator('table.grid tbody tr:has-text("SOA")').first).to_be_visible(timeout=5000)
        expect(page.locator('table.grid th:has-text("TTL")')).to_be_visible()

    check("Advanced 보기에서 SOA·TTL 노출", toggle_advanced)
    check("apex 표기 (same as parent folder)",
          lambda: expect(page.locator('table.grid td.apex').first).to_contain_text("same as parent folder"))
    if shots:
        page.screenshot(path=str(shots / "03-advanced.png"), full_page=True)

    def filter_by_type() -> None:
        page.select_option("#type-filter", "A")
        rows = page.locator("table.grid tbody tr")
        expect(rows.first).to_be_visible(timeout=5000)
        types = page.locator("table.grid tbody td.type").all_text_contents()
        assert set(types) == {"A"}, f"필터 결과에 다른 타입이 섞였다: {sorted(set(types))}"

    check("타입 필터 동작", filter_by_type)

    def search_records() -> None:
        # 검색은 이름과 데이터를 함께 본다. apex MX 의 데이터(mail.example.local)도 정상 일치다.
        page.select_option("#type-filter", "")
        page.fill("#record-search", "mail")
        expect(page.locator("table.grid tbody tr").first).to_be_visible(timeout=5000)
        rows = page.locator("table.grid tbody tr").all()
        assert rows, "검색 결과가 비었다"
        for row in rows:
            name = row.locator("td.name").inner_text()
            data = row.locator("td.data").inner_text()
            assert "mail" in name.lower() or "mail" in data.lower(), f"무관한 행: {name} / {data}"
        # 일치하지 않는 레코드는 빠져야 한다
        assert all("ns1" != row.locator("td.name").inner_text() for row in rows)
        page.fill("#record-search", "")

    check("검색 동작", search_records)

    def sort_by_type() -> None:
        page.click('table.grid th[data-sort="type"]')
        types = page.locator("table.grid tbody td.type").all_text_contents()
        assert types == sorted(types), f"정렬되지 않았다: {types}"

    check("열 정렬 동작", sort_by_type)

    def open_raw() -> None:
        page.click('#tabs button[data-tab="raw"]')
        editor = page.locator("#raw-editor")
        expect(editor).to_be_visible(timeout=5000)
        assert "SOA" in editor.input_value()

    check("Raw 탭에서 zone 파일 전문 표시", open_raw)
    if shots:
        page.screenshot(path=str(shots / "04-raw.png"), full_page=True)

    def open_properties() -> None:
        page.click('#tabs button[data-tab="properties"]')
        expect(page.locator(".detail-body")).to_contain_text("Start of Authority", timeout=5000)
        expect(page.locator(".detail-body")).to_contain_text("Zone Transfers")
        expect(page.locator("#p-primary")).to_be_visible()

    check("Properties 탭 (General/SOA/Zone Transfers)", open_properties)
    if shots:
        page.screenshot(path=str(shots / "05-properties.png"), full_page=True)

    check("역방향 zone 선택", lambda: select_zone(page, REVERSE_ZONE))
    check("PTR 레코드 표시", lambda: expect(page.locator('table.grid tbody tr:has-text("PTR")').first).to_be_visible(timeout=5000))

    def open_forwarders() -> None:
        page.click('.tree .node:has(.label:text-is("Conditional Forwarders"))')
        expect(page.locator("#detail-title")).to_have_text("Conditional Forwarders", timeout=5000)
        expect(page.locator("table.grid")).to_be_visible()
        expect(page.locator("table.grid th")).to_contain_text(["Domain", "Scope", "Forwarder IPs", "Policy"])

    check("전달자 목록 화면", open_forwarders)
    check("New Conditional Forwarder 버튼 존재",
          lambda: expect(page.locator('#detail-actions button:has-text("New Conditional Forwarder")')).to_be_visible())
    if shots:
        page.screenshot(path=str(shots / "06-forwarders.png"), full_page=True)

    def naming_convention_column() -> None:
        page.click('.tree .node[data-id="server"]')
        expect(page.locator("table.grid")).to_be_visible(timeout=5000)
        files = page.locator("table.grid tbody td.data").all_text_contents()
        assert any(f.strip().endswith(".zone") for f in files), f"정방향 .zone 파일이 없다: {files}"
        assert any(f.strip().endswith(".rev") for f in files), f"역방향 .rev 파일이 없다: {files}"

    check("파일명 규칙 (.zone / .rev)", naming_convention_column)

    def default_forwarder_row() -> None:
        page.click('.tree .node:has(.label:text-is("Conditional Forwarders"))')
        expect(page.locator("table.grid tr.server-row")).to_be_visible(timeout=5000)
        expect(page.locator("table.grid tr.server-row")).to_contain_text("Default forwarders")

    check("기본(전역) 전달자 행 상시 표시", default_forwarder_row)

    def modal_lookup() -> None:
        page.click("#btn-lookup")
        expect(page.locator("#modal-overlay")).to_be_visible(timeout=5000)
        expect(page.locator("#modal-title")).to_contain_text("nslookup")
        page.fill("#lookup-name", f"www.{ZONE}")
        page.select_option("#lookup-type", "A")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#lookup-result pre")).to_contain_text("192.168.10.21", timeout=10000)

    check("모달 nslookup (네이티브 dialog 미사용)", modal_lookup)

    def modal_escape_closes() -> None:
        page.keyboard.press("Escape")
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=3000)
        assert page.locator("dialog").count() == 0, "네이티브 <dialog> 요소가 남아 있다"

    check("모달 ESC 닫기 / dialog 요소 없음", modal_escape_closes)

    def server_properties() -> None:
        page.click('.tree .node[data-id="server"]')
        page.click('#tabs button[data-tab="properties"]')
        expect(page.locator("dl.props")).to_contain_text("Forwarders", timeout=5000)
        expect(page.locator("dl.props")).to_contain_text("named-checkconf")
        # 서식 있는 출력은 pre 블록으로 pretty 하게
        expect(page.locator("dl.props pre.block").first).to_be_visible()

    check("서버 Properties (전역 Forwarders·pretty 출력)", server_properties)

    def zone_conf_preview() -> None:
        select_zone(page, ZONE)
        page.click('#tabs button[data-tab="properties"]')
        block = page.locator("pre.block.conf")
        expect(block).to_be_visible(timeout=5000)
        expect(block).to_contain_text(f'zone "{ZONE}"')
        expect(block.locator(".tok-kw").first).to_be_visible()

    check("zone 블록 pretty 미리보기", zone_conf_preview)

    def raw_editor_loads_file() -> None:
        page.click('#tabs button[data-tab="raw"]')
        editor = page.locator("#raw-editor")
        expect(editor).to_be_visible(timeout=5000)
        text = editor.input_value()
        assert "SOA" in text and "$TTL" in text, "zone 파일 전문이 실려야 한다"
        expect(page.locator("#btn-raw-save")).to_be_enabled()

    check("raw 편집기 로드", raw_editor_loads_file)
    if shots:
        page.screenshot(path=str(shots / "07-server-properties.png"), full_page=True)

    # ---------------- P2: 편집 흐름 ----------------
    # 테스트 서버의 테스트 zone 을 실제로 변경한다. 각 단계는 스스로 뒷정리한다.

    HOST = "uitest"
    ADDR = "192.168.10.123"

    def open_draft_row() -> None:
        select_zone(page, ZONE)
        page.click("#btn-new-record")
        # 대화상자가 아니라 표 안에 입력 행이 생겨야 한다
        expect(page.locator("table.grid tr.draft")).to_be_visible(timeout=3000)
        expect(page.locator("#modal-overlay")).to_be_hidden()
        expect(page.locator("#draft-name")).to_be_focused()
        expect(page.locator("#draft-ptr")).to_be_visible()

    check("테이블에 입력 행 추가 (모달 아님)", open_draft_row)

    def ptr_option_follows_type() -> None:
        page.select_option("#draft-type", "CNAME")
        expect(page.locator(".ptr-opt")).to_be_hidden()
        page.select_option("#draft-type", "A")
        expect(page.locator(".ptr-opt")).to_be_visible()

    check("타입에 따라 PTR 옵션 노출", ptr_option_follows_type)

    def create_host_with_ptr() -> None:
        page.fill("#draft-name", HOST)
        page.fill("#draft-data", ADDR)
        page.check("#draft-ptr")
        page.click("#draft-commit")
        expect(page.locator("table.grid tr.draft")).to_have_count(0, timeout=10000)
        expect(page.locator(f'table.grid tbody tr:has-text("{HOST}")').first).to_be_visible(timeout=10000)
        expect(page.locator("#banner")).to_contain_text("적용됨")

    check("입력 행에서 레코드 추가 + PTR 자동 생성", create_host_with_ptr)

    def cancel_draft_row() -> None:
        page.click("#btn-new-record")
        expect(page.locator("table.grid tr.draft")).to_be_visible(timeout=3000)
        page.keyboard.press("Escape")
        expect(page.locator("table.grid tr.draft")).to_have_count(0, timeout=3000)

    check("입력 행 취소 (Esc)", cancel_draft_row)

    def ptr_exists_in_reverse_zone() -> None:
        select_zone(page, REVERSE_ZONE)
        expect(page.locator(f'table.grid tbody tr:has-text("{HOST}")').first).to_be_visible(timeout=5000)

    check("역방향 zone 에 PTR 생성 확인", ptr_exists_in_reverse_zone)

    def inline_edit_ttl() -> None:
        select_zone(page, ZONE)
        page.check("#advanced-view")
        row = page.locator(f'table.grid tbody tr:has-text("{HOST}")').first
        row.locator("td.ttl").dblclick()
        cell = page.locator("input.cell-edit")
        expect(cell).to_be_visible(timeout=3000)
        cell.fill("90")
        cell.press("Enter")
        expect(page.locator("#banner")).to_contain_text("적용됨", timeout=10000)
        expect(page.locator(f'table.grid tbody tr:has-text("{HOST}") td.ttl')).to_have_text("90", timeout=5000)

    check("인라인 셀 편집 (TTL)", inline_edit_ttl)

    # Properties 편집은 별도 레코드(CNAME)로 한다 — 호스트 레코드의 주소를 바꾸면
    # 먼저 만든 PTR 이 짝을 잃어, 뒤의 "삭제 시 PTR 동반 삭제" 검증이 흐려진다.
    ALIAS = "uialias"

    def create_alias_and_edit_properties() -> None:
        page.click("#btn-new-record")
        expect(page.locator("table.grid tr.draft")).to_be_visible(timeout=3000)
        page.fill("#draft-name", ALIAS)
        page.select_option("#draft-type", "CNAME")
        page.fill("#draft-data", f"{HOST}.{ZONE}.")
        page.keyboard.press("Enter")  # Enter 로도 확정된다
        expect(page.locator("table.grid tr.draft")).to_have_count(0, timeout=10000)
        expect(page.locator(f'table.grid tbody tr:has-text("{ALIAS}")').first).to_be_visible(timeout=10000)

        page.locator(f'table.grid tbody tr:has-text("{ALIAS}")').first.click()
        page.click("#btn-properties")
        expect(page.locator("#modal-title")).to_contain_text("CNAME")
        page.fill("#f-data", f"www.{ZONE}.")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=10000)
        expect(page.locator(f'table.grid tbody tr:has-text("{ALIAS}")').first).to_contain_text(
            f"www.{ZONE}.", timeout=5000
        )

    check("입력 행으로 CNAME 추가 (Enter) + Properties 수정", create_alias_and_edit_properties)

    def invalid_edit_shows_bind_error() -> None:
        row = page.locator(f'table.grid tbody tr:has-text("{HOST}")').first
        row.locator("td.data").dblclick()
        cell = page.locator("input.cell-edit")
        expect(cell).to_be_visible(timeout=3000)
        cell.fill("999.999.999.999")
        cell.press("Enter")
        expect(page.locator("#banner.error")).to_be_visible(timeout=10000)
        # 원본은 그대로여야 한다
        expect(page.locator(f'table.grid tbody tr:has-text("{HOST}")').first).to_contain_text(ADDR)

    check("잘못된 값은 거부되고 원본 유지", invalid_edit_shows_bind_error)

    def raw_edit_and_save() -> None:
        page.click('#tabs button[data-tab="raw"]')
        editor = page.locator("#raw-editor")
        expect(editor).to_be_visible(timeout=5000)
        editor.fill(editor.input_value() + "rawadded   IN  A   192.168.10.199\n")
        page.click("#btn-raw-save")
        expect(page.locator("#banner")).to_contain_text("적용됨", timeout=10000)
        page.click('#tabs button[data-tab="records"]')
        expect(page.locator('table.grid tbody tr:has-text("rawadded")').first).to_be_visible(timeout=5000)

    check("raw 편집기 저장", raw_edit_and_save)

    def history_shows_changes() -> None:
        page.click('#tabs button[data-tab="history"]')
        expect(page.locator("table.grid tbody tr").first).to_be_visible(timeout=5000)
        expect(page.locator("table.grid")).to_contain_text("raw zone 파일 저장")
        page.locator("[data-diff]").first.click()
        expect(page.locator("pre.block.diff")).to_be_visible(timeout=5000)
        expect(page.locator("pre.block.diff .d-add").first).to_be_visible()
        page.keyboard.press("Escape")

    check("History 탭 · diff 보기", history_shows_changes)

    def rollback_from_history() -> None:
        page.locator("[data-rollback]").first.click()
        expect(page.locator("#modal-title")).to_contain_text("되돌리기")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=10000)
        page.click('#tabs button[data-tab="records"]')
        expect(page.locator('table.grid tbody tr:has-text("rawadded")')).to_have_count(0, timeout=5000)

    check("History 에서 되돌리기", rollback_from_history)

    def delete_with_ptr() -> None:
        # 먼저 CNAME 정리
        alias_row = page.locator(f'table.grid tbody tr:has-text("{ALIAS}")')
        if alias_row.count():
            select_row(page, ALIAS)
            page.click("#btn-delete-record")
            page.click('#modal-foot button[data-action="submit"]')
            expect(page.locator("#modal-overlay")).to_be_hidden(timeout=10000)
            # 모달이 닫혀도 목록 새로 고침은 아직 진행 중이다. 행이 사라지는 것을 보고 넘어간다.
            expect_gone(page, ALIAS)

        select_row(page, HOST)
        expect(page.locator("#btn-delete-record")).to_be_enabled(timeout=5000)
        page.click("#btn-delete-record")
        expect(page.locator("#modal-title")).to_contain_text("삭제")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=10000)
        expect_gone(page, HOST)

    check("레코드 삭제 (연결된 PTR 포함)", delete_with_ptr)

    def ptr_also_removed() -> None:
        select_zone(page, REVERSE_ZONE)
        expect(page.locator(f'table.grid tbody tr:has-text("{HOST}")')).to_have_count(0, timeout=5000)

    check("역방향 zone 의 PTR 도 삭제됨", ptr_also_removed)

    def zone_reload_button() -> None:
        select_zone(page, ZONE)
        page.click("#btn-reload-zone")
        expect(page.locator("#banner")).to_contain_text("rndc reload", timeout=10000)

    check("zone Reload 버튼", zone_reload_button)

    # ---------------- P3: zone / 전달자 / 설정 ----------------

    NEW_ZONE = "uizone.test"

    def create_zone_with_preview() -> None:
        page.click('.tree .node[data-id="server"]')
        page.click('#tabs button[data-tab="zones"]')
        page.click("#btn-new-zone")
        expect(page.locator("table.grid tr.draft")).to_be_visible(timeout=3000)
        page.fill("#zdraft-name", NEW_ZONE)
        page.click("#zdraft-commit")
        # 기록 전에 결과물을 보여준다
        expect(page.locator("#modal-title")).to_contain_text(NEW_ZONE, timeout=10000)
        expect(page.locator("pre.block.conf")).to_contain_text(f'zone "{NEW_ZONE}"')
        expect(page.locator("pre.block.zone")).to_contain_text("SOA")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=15000)
        expect(page.locator(f'.tree .label:text-is("{NEW_ZONE}")')).to_be_visible(timeout=10000)

    check("New Zone — 미리보기 후 생성", create_zone_with_preview)

    def new_zone_is_usable() -> None:
        select_zone(page, NEW_ZONE)
        page.click("#btn-new-record")
        page.fill("#draft-name", "www")
        page.fill("#draft-data", "10.77.0.1")
        page.keyboard.press("Enter")
        expect(page.locator('table.grid tbody tr:has-text("www")').first).to_be_visible(timeout=10000)

    check("생성한 zone 에 레코드 추가", new_zone_is_usable)

    def delete_zone() -> None:
        page.click('.tree .node[data-id="server"]')
        page.click('#tabs button[data-tab="zones"]')
        page.locator(f'tr[data-zone="{NEW_ZONE}"]').click()
        page.click("#btn-delete-zone")
        expect(page.locator("#modal-title")).to_contain_text("zone 삭제")
        page.check('input[name="delete_file"]')
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=15000)
        expect(page.locator(f'.tree .label:text-is("{NEW_ZONE}")')).to_have_count(0, timeout=10000)

    check("zone 삭제 (파일 포함)", delete_zone)

    FWD_DOMAIN = "uifwd.test"

    def add_conditional_forwarder() -> None:
        page.click('.tree .node:has(.label:text-is("Conditional Forwarders"))')
        page.click("#btn-new-forwarder")
        expect(page.locator("table.grid tr.draft")).to_be_visible(timeout=3000)
        page.fill("#fdraft-domain", FWD_DOMAIN)
        page.fill("#fdraft-ips", "10.9.8.7, 10.9.8.8")
        page.click("#fdraft-commit")
        expect(page.locator(f'table.grid tbody tr:has-text("{FWD_DOMAIN}")').first).to_be_visible(timeout=10000)
        expect(page.locator(f'tr:has-text("{FWD_DOMAIN}")').first).to_contain_text("10.9.8.7")

    check("조건부 전달자 추가 (입력 행)", add_conditional_forwarder)

    def edit_server_forwarders() -> None:
        page.click("#btn-edit-server-fwd")
        expect(page.locator("#fedit-ips")).to_be_visible(timeout=3000)
        original = page.locator("#fedit-ips").input_value()
        page.fill("#fedit-ips", "168.126.63.1, 8.8.4.4")
        page.click("#fedit-commit")
        expect(page.locator("tr.server-row")).to_contain_text("8.8.4.4", timeout=10000)
        # 원래대로 되돌린다
        page.click("#btn-edit-server-fwd")
        page.fill("#fedit-ips", original)
        page.click("#fedit-commit")
        expect(page.locator("tr.server-row")).to_contain_text("168.126.63.1", timeout=10000)

    check("기본(전역) 전달자 편집", edit_server_forwarders)

    def delete_conditional_forwarder() -> None:
        page.locator(f'[data-fwd-del="{FWD_DOMAIN}"]').click()
        expect(page.locator("#modal-title")).to_contain_text("전달자 삭제")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=10000)
        expect(page.locator(f'table.grid tbody tr:has-text("{FWD_DOMAIN}")')).to_have_count(0, timeout=10000)

    check("조건부 전달자 삭제", delete_conditional_forwarder)

    def settings_tab_shows_detection() -> None:
        page.click('.tree .node[data-id="server"]')
        page.click('#tabs button[data-tab="settings"]')
        expect(page.locator("table.grid.settings").first).to_be_visible(timeout=5000)
        expect(page.locator(".detail-body")).to_contain_text("자동 감지 결과")
        expect(page.locator('[data-setting="named_conf"]')).to_have_value("/etc/named.conf")
        expect(page.locator('[data-setting="zones_conf"]')).to_have_value("/etc/named/zones.conf")
        expect(page.locator("#detail-actions .ok-text")).to_contain_text("설정 정상")

    check("Settings — 레이아웃 감지·경로 입력", settings_tab_shows_detection)

    # ---------------- P4: Properties 편집 / 위임 / 동적 zone ----------------

    def edit_soa_properties() -> None:
        select_zone(page, ZONE)
        page.click('#tabs button[data-tab="properties"]')
        expect(page.locator("#p-primary")).to_be_visible(timeout=5000)
        expect(page.locator(".detail-body")).to_contain_text("Start of Authority")
        expect(page.locator(".detail-body")).to_contain_text("Zone Transfers")

        page.fill("#p-retry", "900")
        page.fill("#p-responsible", "dnsadmin@example.local")
        page.click("#btn-props-save")
        expect(page.locator("#banner")).to_contain_text("속성을 저장", timeout=15000)
        expect(page.locator("#p-retry")).to_have_value("900", timeout=5000)
        expect(page.locator("#p-responsible")).to_have_value("dnsadmin.example.local.")

    check("Properties — SOA 편집", edit_soa_properties)

    def edit_zone_transfers() -> None:
        page.fill("#p-allow-transfer", "10.9.9.9")
        page.click("#btn-props-save")
        expect(page.locator("#banner")).to_contain_text("속성을 저장", timeout=15000)
        # BIND 는 bare IP 를 /32 로 정규화한다(named-checkconf -p). 그 형태로 되돌아온다.
        expect(page.locator("pre.block.conf")).to_contain_text("allow-transfer { 10.9.9.9", timeout=5000)
        # 원복. 배너 문구는 앞선 저장과 같아 구분이 되지 않으므로 **내용**으로 확인한다.
        page.fill("#p-allow-transfer", "none")
        page.click("#btn-props-save")
        expect(page.locator("pre.block.conf")).to_contain_text("allow-transfer { none; };", timeout=15000)

    check("Properties — Zone Transfers 편집", edit_zone_transfers)

    def add_delegation() -> None:
        page.click("#btn-new-delegation")
        expect(page.locator("#modal-title")).to_contain_text("New Delegation")
        page.fill("#d-name", "uisub")
        page.fill("#d-server", f"ns1.uisub.{ZONE}.")
        page.fill("#d-address", "192.168.10.44")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=15000)

        page.click('#tabs button[data-tab="records"]')
        expect(page.locator('table.grid tbody tr:has-text("uisub")').first).to_be_visible(timeout=10000)
        rows = page.locator('table.grid tbody tr:has-text("uisub")').all_text_contents()
        assert any("NS" in r for r in rows), f"NS 레코드가 있어야 한다: {rows}"
        assert any("192.168.10.44" in r for r in rows), f"glue 레코드가 있어야 한다: {rows}"

    check("New Delegation — NS + glue 생성", add_delegation)

    def cleanup_delegation() -> None:
        page.keyboard.down("Control")
        for row in page.locator('table.grid tbody tr:has-text("uisub")').all():
            row.click()
        page.keyboard.up("Control")
        expect(page.locator("#btn-delete-record")).to_be_enabled(timeout=5000)
        page.click("#btn-delete-record")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=15000)
        expect(page.locator('table.grid tbody tr:has-text("uisub")')).to_have_count(0, timeout=10000)

    check("위임 레코드 정리 (다중 선택 삭제)", cleanup_delegation)

    def dynamic_zone_uses_rfc2136() -> None:
        select_zone(page, "dynamic.local")
        expect(page.locator(".notice.warn")).to_contain_text("동적 갱신", timeout=5000)
        page.click("#btn-new-record")
        page.fill("#draft-name", "uidyn")
        page.fill("#draft-data", "192.168.10.231")
        page.keyboard.press("Enter")
        expect(page.locator('table.grid tbody tr:has-text("uidyn")').first).to_be_visible(timeout=15000)
        expect(page.locator("#banner")).to_contain_text("RFC 2136", timeout=5000)

    check("동적 zone — RFC 2136 으로 추가", dynamic_zone_uses_rfc2136)

    def dynamic_record_cleanup() -> None:
        page.locator('table.grid tbody tr:has-text("uidyn")').first.click()
        expect(page.locator("#btn-delete-record")).to_be_enabled(timeout=5000)
        page.click("#btn-delete-record")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=15000)
        expect(page.locator('table.grid tbody tr:has-text("uidyn")')).to_have_count(0, timeout=10000)

    check("동적 zone — 레코드 삭제", dynamic_record_cleanup)

    def refresh_picks_up_external_change() -> None:
        """Refresh 는 서버를 다시 읽어야 한다 — 앱 밖에서 바뀐 내용이 보여야 의미가 있다."""
        import json
        import urllib.request

        select_zone(page, ZONE)
        expect(page.locator('table.grid tbody tr:has-text("www")').first).to_be_visible(timeout=5000)
        assert page.locator('table.grid tbody tr:has-text("refreshprobe")').count() == 0

        # 앱 밖(다른 클라이언트)에서 레코드를 추가한다
        request = urllib.request.Request(
            f"{BASE}/api/zones/{ZONE}/records",
            method="POST",
            data=json.dumps({"name": "refreshprobe", "type": "A", "data": "10.9.9.9"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            assert json.loads(response.read())["ok"]

        page.click("#btn-refresh")
        expect(page.locator('table.grid tbody tr:has-text("refreshprobe")').first).to_be_visible(timeout=10000)

    check("Refresh — 외부 변경 반영", refresh_picks_up_external_change)

    def refresh_cleanup() -> None:
        row = page.locator('table.grid tbody tr:has-text("refreshprobe")').first
        row.click()
        expect(page.locator("#btn-delete-record")).to_be_enabled(timeout=5000)
        page.click("#btn-delete-record")
        page.click('#modal-foot button[data-action="submit"]')
        expect(page.locator("#modal-overlay")).to_be_hidden(timeout=10000)
        expect(page.locator('table.grid tbody tr:has-text("refreshprobe")')).to_have_count(0, timeout=10000)

    check("Refresh 검증용 레코드 정리", refresh_cleanup)

    def files_tab_lists_targets() -> None:
        page.click('.tree .node[data-id="server"]')
        page.click('#tabs button[data-tab="files"]')
        # 탭이 바뀌어도 이전 화면의 표가 잠시 남는다. Files 화면 고유 요소를 기다린다.
        expect(page.locator("#btn-files-events")).to_be_visible(timeout=10000)
        expect(page.locator('table.grid th:text-is("역할")')).to_be_visible(timeout=5000)
        body = page.locator(".detail-body").inner_text()
        assert "named.conf" in body, "BIND 주 설정이 목록에 있어야 한다"
        assert "zone 파일" in body, "zone 파일 역할이 보여야 한다"
        expect(page.locator("#detail-actions")).to_contain_text("대상 파일")

    check("Files — 대상 파일 목록", files_tab_lists_targets)

    def files_tab_shows_access_log() -> None:
        page.click("#btn-files-events")
        expect(page.locator('table.grid th:has-text("시각")')).to_be_visible(timeout=5000)
        rows = page.locator("table.grid tbody tr").count()
        assert rows > 0, "접근 기록이 있어야 한다"
        expect(page.locator(".detail-body")).to_contain_text("비밀값은 어디에도 남기지 않습니다")

    check("Files — 접근 기록", files_tab_shows_access_log)

    def files_tab_marks_secret_files() -> None:
        page.click("#btn-files-list")
        expect(page.locator('table.grid th:text-is("역할")')).to_be_visible(timeout=5000)
        # 키 파일이 있으면 '비밀' 배지로 표시되고 내용은 어디에도 없다
        body = page.locator(".detail-body").inner_text()
        if ".key" in body:
            expect(page.locator('.badge.warn:text-is("비밀")').first).to_be_visible()

    check("Files — 키 파일 표시", files_tab_marks_secret_files)

    def idle_badge_counts_down() -> None:
        # 기본 10분. 배지가 보이고, 남은 시간을 서버에 되묻지 않아야 한다.
        badge = page.locator("#idle-note")
        expect(badge).to_be_visible(timeout=5000)
        expect(badge).to_contain_text("자동 종료")
        assert "유휴" in badge.inner_text() or "곧" in badge.inner_text()
        # 남은 시간을 세는 타이머가 요청을 만들지 않는지 — 마우스를 움직여도 마찬가지
        seen: list[str] = []
        page.on("request", lambda req: seen.append(req.url))
        page.mouse.move(400, 400)
        page.mouse.move(700, 500)
        page.wait_for_timeout(1500)
        assert not seen, f"마우스 이동만으로 요청이 생겼다: {seen}"

    check("유휴 자동 종료 배지 · 마우스 이동은 활동이 아님", idle_badge_counts_down)

    def real_action_extends_idle() -> None:
        # 새로 고침(실질적 액션)은 /api/status 를 부르고, 서버의 유휴 시계가 되돌아간다.
        before = page.evaluate("fetch('/api/status').then(r => r.json()).then(j => j.idle_remaining)")
        page.wait_for_timeout(2500)
        page.click("#btn-refresh")
        expect(page.locator("#server-state")).not_to_contain_text("상태 확인 중", timeout=5000)
        after = page.evaluate("fetch('/api/status').then(r => r.json()).then(j => j.idle_remaining)")
        assert after >= before - 1.5, f"액션 뒤에 유휴 시간이 더 줄었다: {before} -> {after}"

    check("실질적 액션은 유휴 시간을 연장", real_action_extends_idle)

    def shutdown_modal_can_be_cancelled() -> None:
        # 실제로 내리면 뒤따르는 점검을 못 하므로 취소까지만 확인한다.
        page.click("#btn-shutdown")
        expect(page.locator("#modal-title")).to_contain_text("종료", timeout=5000)
        expect(page.locator("#modal-body")).to_contain_text("dns_manager start")
        page.click('#modal-foot button[data-action="close"]')
        expect(page.locator("#modal-overlay")).to_be_hidden()
        expect(page.locator("#gone")).to_be_hidden()

    check("종료 버튼 — 확인 모달 · 취소", shutdown_modal_can_be_cancelled)

    def copyright_and_notice() -> None:
        expect(page.locator(".statusbar .copyright")).to_contain_text("© 2026 haedong")
        expect(page.locator(".statusbar .copyright")).to_contain_text("theknowledges.net")
        page.click("#btn-notice")
        expect(page.locator("#modal-title")).to_contain_text("NOTICE", timeout=5000)
        expect(page.locator("#modal-body")).to_contain_text("dnspython", timeout=5000)
        page.keyboard.press("Escape")

    check("저작권 표시 · 제3자 고지", copyright_and_notice)

    check(
        "처리되지 않은 JS 오류 없음",
        lambda: (_ for _ in ()).throw(AssertionError("; ".join(console_errors))) if console_errors else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=Path, default=None, help="스크린샷 저장 디렉터리")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)

    print(f"dns_manager UI 점검 — {BASE}")
    with sync_playwright() as pw:
        # Playwright 번들 브라우저가 없을 수 있으므로(폐쇄망·버전 불일치) 시스템 chromium 을 우선 사용한다.
        executable = os.environ.get("CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
        launch_args = {"headless": not args.headed}
        if executable:
            launch_args["executable_path"] = executable
        browser = pw.chromium.launch(**launch_args)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        globals()["PAGE"] = page
        globals()["SHOT_DIR"] = args.shots
        page.on("console", lambda msg: note_console(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda err: console_errors.append(str(err)))  # 미처리 예외는 항상 실패
        try:
            run(page, args.shots)
        finally:
            browser.close()

    failed = [desc for ok, desc in results if not ok]
    print(f"\n결과: {len(results) - len(failed)} 통과, {len(failed)} 실패")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
