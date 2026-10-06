/* dns_manager 콘솔 (P0: 읽기 전용)
 *
 * Windows Server DNS Manager 의 조작 흐름을 따른다:
 *  - 좌측 콘솔 트리: DNS > 서버 > Forward/Reverse Lookup Zones > Conditional Forwarders
 *  - 우측 상세: 레코드 테이블 (Name / Type / Data / TTL / Status)
 *  - View ▸ Advanced 토글로 SOA·DNSSEC 레코드 노출
 * P1 에서 편집·Apply 를 더한다. 지금은 쓰기 엔드포인트가 존재하지 않는다.
 */
"use strict";

const state = {
  status: null,
  zones: [],
  forwarders: [],
  selection: { kind: "server", zone: null, view: null },
  tab: "records",
  advanced: document.body.dataset.advancedDefault === "true",
  typeFilter: "",
  search: "",
  sort: { key: "default", dir: 1 },
  collapsed: new Set(),
  // 서버에서 가져온 상세 데이터 캐시.
  // 필터·검색·정렬은 순수 클라이언트 작업이므로 재조회 없이 이 캐시로 다시 그린다
  // (매 입력마다 재조회하면 입력 포커스를 잃고 반영도 늦어진다).
  cache: null,
  selected: new Set(),   // 선택된 레코드 id (다중 선택 삭제용)
  draft: null,           // 추가 중인 레코드 행 (테이블 안에서 직접 입력)
  zoneDraft: null,       // 추가 중인 zone 행
  fwdDraft: null,        // 추가 중인 조건부 전달자 행
  fwdEdit: null,         // 편집 중인 전달자 (domain 또는 "__server__")
  selectedZone: null,    // zone 목록에서 선택된 zone
  settings: null,        // /api/settings 결과
  files: null,           // /api/files 결과 (앱이 읽고 쓴 파일)
  filesView: "files",    // files | events
  history: [],
  busy: false,
};

const el = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch (_) { /* 본문 없음 */ }
    throw new Error(detail);
  }
  return res.json();
}

let bannerTimer = null;
function banner(message, kind) {
  const node = el("banner");
  if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
  if (!message) { node.classList.add("hidden"); return; }
  node.textContent = message;
  node.className = "banner" + (kind === "error" ? " error" : kind === "ok" ? " ok" : "");
  // 성공 알림은 잠시 뒤 사라진다. 경고·오류는 사용자가 치울 때까지 남는다.
  if (kind === "ok") bannerTimer = setTimeout(() => node.classList.add("hidden"), 6000);
}

function setStatusbar(text) { el("statusbar").textContent = text; }

/* ---------------- 콘솔 트리 ---------------- */

const CATEGORY_NODES = [
  { key: "forward", label: "Forward Lookup Zones", icon: "📁" },
  { key: "reverse", label: "Reverse Lookup Zones", icon: "📁" },
];

function zoneBadge(zone) {
  if (zone.problem) return ' <span class="badge err">!</span>';
  if (zone.out_of_sync)
    return zone.file_changed_since_load
      ? ' <span class="badge err">파일 변경됨 — reload 필요</span>'
      : ' <span class="badge warn">reload 필요</span>';
  if (zone.signed) return ' <span class="badge">signed</span>';
  if (zone.dynamic) return ' <span class="badge">dynamic</span>';
  return "";
}

function treeNode(opts) {
  const { id, label, icon, selected, hasChildren, collapsed, extra = "" } = opts;
  const twisty = hasChildren ? (collapsed ? "▶" : "▼") : "";
  return (
    `<div class="node${selected ? " selected" : ""}" data-id="${esc(id)}">` +
    `<span class="twisty" data-twisty="${esc(id)}">${twisty}</span>` +
    `<span class="icon">${icon}</span><span class="label">${esc(label)}</span>${extra}</div>`
  );
}

function renderTree() {
  const sel = state.selection;
  const serverName = state.status?.version ? "BIND" : "DNS server";
  const isCollapsed = (id) => state.collapsed.has(id);

  let html = "<li>";
  html += treeNode({
    id: "server",
    label: `${serverName} (${state.status?.named_conf ?? "named.conf"})`,
    icon: "🖥",
    selected: sel.kind === "server",
    hasChildren: true,
    collapsed: isCollapsed("server"),
  });

  if (!isCollapsed("server")) {
    html += "<ul>";
    for (const cat of CATEGORY_NODES) {
      const zones = state.zones.filter((z) => z.category === cat.key);
      const catId = `cat:${cat.key}`;
      html += `<li class="${isCollapsed(catId) ? "collapsed" : ""}">`;
      html += treeNode({
        id: catId,
        label: cat.label,
        icon: cat.icon,
        selected: sel.kind === "category" && sel.zone === cat.key,
        hasChildren: zones.length > 0,
        collapsed: isCollapsed(catId),
        extra: ` <span class="badge">${zones.length}</span>`,
      });
      if (zones.length) {
        html += "<ul>";
        for (const zone of zones) {
          html += "<li>" + treeNode({
            id: `zone:${zone.name}|${zone.view ?? ""}`,
            label: zone.name,
            icon: zone.editable ? "🗂" : "🔒",
            selected: sel.kind === "zone" && sel.zone === zone.name && (sel.view ?? "") === (zone.view ?? ""),
            hasChildren: false,
            extra: zoneBadge(zone),
          }) + "</li>";
        }
        html += "</ul>";
      }
      html += "</li>";
    }

    // Conditional Forwarders — Windows DNS Manager 와 동일 위치
    const conditional = state.forwarders.filter((f) => f.scope === "conditional");
    html += `<li>` + treeNode({
      id: "forwarders",
      label: "Conditional Forwarders",
      icon: "↪",
      selected: sel.kind === "forwarders",
      hasChildren: false,
      extra: ` <span class="badge">${conditional.length}</span>`,
    }) + "</li>";

    html += "</ul>";
  }
  html += "</li>";
  el("tree").innerHTML = html;
}

/* ---------------- 상세: 탭 ---------------- */

function renderTabs() {
  const sel = state.selection;
  let tabs = [];
  if (sel.kind === "zone") tabs = [["records", "Records"], ["raw", "Raw"], ["properties", "Properties"], ["history", "History"]];
  else if (sel.kind === "server")
    tabs = [["zones", "Zones"], ["forwarders", "Forwarders"], ["properties", "Server"],
            ["files", "Files"], ["settings", "Settings"], ["history", "History"]];
  else if (sel.kind === "forwarders") tabs = [["forwarders", "Forwarders"]];
  else tabs = [["zones", "Zones"]];

  if (!tabs.some(([k]) => k === state.tab)) state.tab = tabs[0][0];
  el("tabs").innerHTML = tabs
    .map(([key, label]) => `<button data-tab="${key}" class="${state.tab === key ? "active" : ""}">${label}</button>`)
    .join("");
}

/* ---------------- 상세: 레코드 그리드 ---------------- */

const COLUMNS = [
  { key: "display_name", label: "Name", cls: "name" },
  { key: "type", label: "Type", cls: "type" },
  { key: "data", label: "Data", cls: "data" },
  { key: "ttl", label: "TTL", cls: "ttl", advanced: true },
  { key: "status", label: "Status", cls: "status" },
];

function visibleColumns() {
  return COLUMNS.filter((c) => !c.advanced || state.advanced);
}

function sortRecords(records) {
  const { key, dir } = state.sort;
  if (key === "default") return records;
  return [...records].sort((a, b) => {
    const av = key === "ttl" ? a.ttl : String(a[key] ?? "").toLowerCase();
    const bv = key === "ttl" ? b.ttl : String(b[key] ?? "").toLowerCase();
    return av < bv ? -dir : av > bv ? dir : 0;
  });
}

function renderRecords(detail) {
  const editable = detail.table_editable;
  const selectedCount = state.selected.size;
  const actions = [];
  // 레코드 추가는 대화상자가 아니라 테이블 맨 아래 입력 행으로 한다.
  actions.push(
    `<button type="button" id="btn-new-record" ${editable && !state.draft ? "" : "disabled"} ` +
      `title="${editable ? "테이블에 입력 행을 추가합니다" : "이 zone 은 테이블 편집을 지원하지 않습니다"}">New Record</button>`
  );
  actions.push(
    `<button type="button" id="btn-delete-record" ${editable && selectedCount ? "" : "disabled"}>` +
      `Delete${selectedCount > 1 ? ` (${selectedCount})` : ""}</button>`
  );
  actions.push(`<button type="button" id="btn-properties" ${selectedCount === 1 ? "" : "disabled"}>Properties…</button>`);
  actions.push(`<button type="button" id="btn-reload-zone">Reload</button>`);
  const types = [...new Set(detail.records.map((r) => r.type))].sort();
  actions.push(
    `<span class="spacer"></span>` +
    `<select id="type-filter"><option value="">모든 타입</option>` +
    types.map((t) => `<option ${state.typeFilter === t ? "selected" : ""}>${t}</option>`).join("") +
    `</select>` +
    `<input type="search" id="record-search" placeholder="이름·데이터 검색" value="${esc(state.search)}">`
  );
  const focused = document.activeElement?.id === "record-search";
  const caret = focused ? document.activeElement.selectionStart : null;
  el("detail-actions").innerHTML = actions.join("");
  if (focused) {
    const input = el("record-search");
    input.focus();
    if (caret !== null) input.setSelectionRange(caret, caret);
  }

  let rows = detail.records;
  if (state.typeFilter) rows = rows.filter((r) => r.type === state.typeFilter);
  if (state.search) {
    const q = state.search.toLowerCase();
    rows = rows.filter((r) => r.name.toLowerCase().includes(q) || r.data.toLowerCase().includes(q));
  }
  rows = sortRecords(rows);

  const notices = [];
  if (detail.problem) notices.push(`<div class="notice error">${esc(detail.problem)}</div>`);
  if (detail.parse_error)
    notices.push(`<div class="notice error">zone 파일을 해석할 수 없습니다 — ${esc(detail.parse_error)}</div>`);
  if (detail.unsupported_directives?.length)
    notices.push(
      `<div class="notice warn">${esc(detail.unsupported_directives.join(", "))} 지시자가 있어 테이블 편집이 제한됩니다. Raw 탭을 사용하세요.</div>`
    );
  if (detail.signed)
    notices.push(`<div class="notice warn">DNSSEC 서명 zone 이므로 읽기 전용으로 표시합니다.</div>`);
  if (detail.dynamic)
    notices.push(
      `<div class="notice warn">동적 갱신이 허용된 zone 입니다(allow-update). 파일 편집 전 journal 동기화가 필요합니다.</div>`
    );
  if (detail.file_changed_since_load)
    notices.push(
      `<div class="notice error">zone 파일이 named 가 읽어들인 뒤에 바뀌었습니다 ` +
        `(적재 ${esc(detail.loaded_at ?? "?")}). <strong>지금 보이는 내용과 서버가 답하는 내용이 다릅니다.</strong> ` +
        `Reload 를 누르세요. serial 이 같아도 내용은 다를 수 있습니다.</div>`
    );
  else if (detail.out_of_sync)
    notices.push(
      `<div class="notice warn">파일 serial ${esc(detail.file_serial)} ≠ 적재된 serial ${esc(detail.loaded_serial)} — reload 가 필요합니다.</div>`
    );
  if (!detail.table_editable && detail.editable === false && detail.type !== "master")
    notices.push(`<div class="notice">'${esc(detail.type)}' zone 은 이 서버에서 편집 대상이 아닙니다.</div>`);

  const cols = visibleColumns();
  const head = cols
    .map((c) => {
      const arrow = state.sort.key === c.key ? (state.sort.dir > 0 ? "▲" : "▼") : "";
      return `<th data-sort="${c.key}">${c.label} <span class="sort">${arrow}</span></th>`;
    })
    .join("");

  const canEdit = (record) => detail.table_editable && record.type !== "SOA";
  const body = rows
    .map((r) => {
      const cells = cols
        .map((c) => {
          const editableCell = canEdit(r) && ["display_name", "ttl", "data"].includes(c.key);
          const attrs = editableCell ? ` data-edit="${c.key}" title="더블클릭하면 편집합니다"` : "";
          if (c.key === "status") return `<td class="status"></td>`;
          if (c.key === "display_name")
            return `<td class="name${r.name === "@" ? " apex" : ""}"${attrs}>${esc(r.display_name)}</td>`;
          if (c.key === "ttl") return `<td class="ttl"${attrs}>${esc(r.ttl)}</td>`;
          return `<td class="${c.cls}"${attrs}>${esc(r[c.key])}</td>`;
        })
        .join("");
      const selected = state.selected.has(r.id) ? " selected" : "";
      return `<tr data-record="${esc(r.id)}" class="${selected}">${cells}</tr>`;
    })
    .join("");

  const hidden = detail.hidden_record_count
    ? `<div class="notice">기본 보기에서 ${detail.hidden_record_count}개 레코드(SOA·DNSSEC 등)를 숨겼습니다. View ▸ Advanced 로 표시합니다.</div>`
    : "";

  const draftRow = state.draft ? draftRowHtml(cols) : "";
  el("detail-body").innerHTML =
    notices.join("") +
    (rows.length || draftRow
      ? `<table class="grid"><thead><tr>${head}</tr></thead><tbody>${body}${draftRow}</tbody></table>`
      : `<div class="empty">표시할 레코드가 없습니다.</div>`) +
    hidden;

  if (state.draft) {
    const first = el("draft-name");
    if (first && document.activeElement?.tagName !== "INPUT") first.focus();
  }

  setStatusbar(
    `${detail.name} · ${detail.type} · ${rows.length}/${detail.records.length} 레코드 · ` +
    `파일 ${detail.file ?? "-"} · serial ${detail.file_serial ?? "-"} (적재 ${detail.loaded_serial ?? "-"})`
  );
}

function renderRaw(raw) {
  const editable = state.cache?.detail?.editable !== false;
  el("detail-actions").innerHTML =
    `<button type="button" id="btn-raw-save" class="primary">저장</button>` +
    `<button type="button" id="btn-raw-reset">되돌리기</button>` +
    `<span class="spacer"></span><span class="muted">${esc(raw.file)} · version ${esc(raw.version)}</span>`;
  const notice = raw.parse_error
    ? `<div class="notice error">${esc(raw.parse_error)} — 저장하면 named-checkzone 이 판정합니다.</div>`
    : "";
  const warn = raw.unsupported_directives?.length
    ? `<div class="notice warn">${esc(raw.unsupported_directives.join(", "))} 지시자가 있어 테이블 편집은 막혀 있습니다. 여기서만 편집하세요.</div>`
    : "";
  el("detail-body").innerHTML =
    notice + warn + `<textarea id="raw-editor" class="raw-editor" spellcheck="false">${esc(raw.text)}</textarea>`;
  setStatusbar(`${raw.name} · raw · 저장 시 SOA serial 이 자동 증가합니다`);
}

// 여러 줄 출력(named-checkconf, rndc status, zone 파일 등)은 한 줄로 뭉개지 않고
// 원문 형식 그대로 pre 블록에 보여준다. 명령 출력의 줄바꿈·들여쓰기가 곧 정보다.
function propValue(value, kind) {
  if (kind === "html") return `<dd class="full">${value}</dd>`;
  if (value === null || value === undefined || value === "") return `<dd class="muted">-</dd>`;
  const text = String(value);
  if (kind === "pre" || text.includes("\n")) {
    return `<dd class="full"><pre class="block${kind === "conf" ? " conf" : ""}">${
      kind === "conf" ? highlightConf(text) : esc(text)
    }</pre></dd>`;
  }
  return `<dd>${esc(text)}</dd>`;
}

function propsList(pairs) {
  return (
    `<dl class="props">` +
    pairs
      .map(([k, v, kind]) =>
        v === "__section__" ? `<dt class="section">${esc(k)}</dt>` : `<dt>${esc(k)}</dt>${propValue(v, kind)}`
      )
      .join("") +
    `</dl>`
  );
}

/* ---------------- 서식 있는 출력의 하이라이팅 ----------------
 * 외부 라이브러리 없이(폐쇄망) zone 파일과 named.conf 출력을 읽기 쉽게 만든다.
 * 입력은 반드시 먼저 이스케이프한 뒤 토큰만 감싼다.
 */

const RECORD_TYPES =
  /\b(SOA|NS|A|AAAA|CNAME|MX|PTR|SRV|TXT|CAA|DNAME|SSHFP|TLSA|NAPTR|SPF|URI|SVCB|HTTPS|DNSKEY|RRSIG|NSEC3?|DS|CDS|CDNSKEY)\b/g;

function highlightZone(text) {
  return esc(text)
    .split("\n")
    .map((line) => {
      const comment = line.indexOf(";");
      const code = comment >= 0 ? line.slice(0, comment) : line;
      const rest = comment >= 0 ? line.slice(comment) : "";
      const marked = code
        .replace(/^(\$[A-Z]+)/, '<span class="tok-dir">$1</span>')
        .replace(/&quot;[^&]*?&quot;/g, (m) => `<span class="tok-str">${m}</span>`)
        .replace(RECORD_TYPES, '<span class="tok-type">$&</span>')
        .replace(/\b(IN|CH|HS)\b/g, '<span class="tok-class">$1</span>');
      return marked + (rest ? `<span class="tok-comment">${rest}</span>` : "");
    })
    .join("\n");
}

function highlightConf(text) {
  return esc(text)
    .replace(/(\/\/.*$)/gm, '<span class="tok-comment">$1</span>')
    .replace(/&quot;[^&]*?&quot;/g, (m) => `<span class="tok-str">${m}</span>`)
    .replace(/\b(options|zone|view|type|file|masters|primaries|forwarders|forward|allow-update|allow-transfer|allow-query|also-notify|include|key|directory|listen-on)\b/g,
             '<span class="tok-kw">$1</span>');
}

// named.conf 에 실제로 저장된 모습 그대로를 보여준다(파일 기반 서버에서는 이게 진실).
function zoneConfPreview(detail) {
  const lines = [`zone "${detail.name}" IN {`, `    type ${detail.type};`];
  if (detail.file) lines.push(`    file "${detail.file.split("/").pop()}";`);
  if (detail.masters?.length) lines.push(`    masters { ${detail.masters.join("; ")}; };`);
  if (detail.forwarders?.length) lines.push(`    forwarders { ${detail.forwarders.join("; ")}; };`);
  if (detail.forward_policy) lines.push(`    forward ${detail.forward_policy};`);
  if (detail.allow_update?.length) lines.push(`    allow-update { ${detail.allow_update.join("; ")}; };`);
  if (detail.allow_transfer?.length) lines.push(`    allow-transfer { ${detail.allow_transfer.join("; ")}; };`);
  lines.push("};");
  return lines.join("\n");
}

function renderZoneProperties(detail) {
  const soa = state.cache?.soa ?? {};
  const list = (arr) => (arr && arr.length ? arr.join(", ") : "");
  const dynamicNote = detail.dynamic
    ? `<div class="notice warn">이 zone 은 동적 갱신(allow-update)이 열려 있습니다. 레코드 변경은 ` +
      `RFC 2136 으로 적용되고, raw 저장은 freeze → 저장 → thaw 순서로 처리됩니다.</div>`
    : "";

  el("detail-actions").innerHTML =
    `<button type="button" id="btn-props-save" class="primary">저장</button>` +
    `<button type="button" id="btn-new-delegation">New Delegation…</button>` +
    `<span class="spacer"></span><span class="muted">${esc(detail.file ?? "")}</span>`;

  el("detail-body").innerHTML =
    dynamicNote +
    `<h3 class="sub">General</h3>` +
    `<dl class="props">` +
    `<dt>Zone name</dt><dd>${esc(detail.name)}</dd>` +
    `<dt>Type</dt><dd>${esc(detail.type)}</dd>` +
    `<dt>View</dt><dd>${esc(detail.view ?? "(default)")}</dd>` +
    `<dt>Zone file</dt><dd>${esc(detail.file ?? "-")}</dd>` +
    `<dt>정의 위치</dt><dd>${
      detail.source_file
        ? esc(detail.source_file)
        : '<span class="err-text">출처 불명 — 앱이 읽는 설정 파일에서 이 zone 블록을 찾지 못했습니다. ' +
          'BIND 가 쓰는 named.conf 와 다를 수 있습니다(Settings 확인).</span>'
    }</dd>` +
    `<dt>DNSSEC</dt><dd>${detail.signed ? "signed (읽기 전용)" : "unsigned"}</dd>` +
    `<dt><label for="p-allow-update">Dynamic updates (allow-update)</label></dt>` +
    `<dd><input id="p-allow-update" class="cell-edit" value="${esc(list(detail.allow_update))}" placeholder="none"></dd>` +
    `</dl>` +

    `<h3 class="sub">Start of Authority (SOA)</h3>` +
    `<dl class="props">` +
    `<dt><label for="p-primary">Primary server</label></dt>` +
    `<dd><input id="p-primary" class="cell-edit" value="${esc(soa.primary ?? "")}"></dd>` +
    `<dt><label for="p-responsible">Responsible person</label></dt>` +
    `<dd><input id="p-responsible" class="cell-edit" value="${esc(soa.responsible ?? "")}" ` +
    `placeholder="admin@example.local 형식도 됩니다"></dd>` +
    `<dt>Serial number</dt><dd>${esc(soa.serial ?? detail.file_serial ?? "-")} ` +
    `<span class="muted">(저장할 때 자동으로 증가합니다)</span></dd>` +
    `<dt><label for="p-refresh">Refresh interval</label></dt>` +
    `<dd><input id="p-refresh" class="cell-edit" type="number" value="${esc(soa.refresh ?? "")}"></dd>` +
    `<dt><label for="p-retry">Retry interval</label></dt>` +
    `<dd><input id="p-retry" class="cell-edit" type="number" value="${esc(soa.retry ?? "")}"></dd>` +
    `<dt><label for="p-expire">Expires after</label></dt>` +
    `<dd><input id="p-expire" class="cell-edit" type="number" value="${esc(soa.expire ?? "")}"></dd>` +
    `<dt><label for="p-minimum">Minimum (default) TTL</label></dt>` +
    `<dd><input id="p-minimum" class="cell-edit" type="number" value="${esc(soa.minimum ?? "")}"></dd>` +
    `</dl>` +

    `<h3 class="sub">Name Servers</h3>` +
    `<dl class="props"><dt>apex NS 레코드</dt><dd>${
      (state.cache?.detail?.records ?? [])
        .filter((r) => r.type === "NS" && r.name === "@")
        .map((r) => esc(r.data))
        .join("<br>") || '<span class="muted">없음 — Records 탭에서 추가하세요.</span>'
    }</dd></dl>` +

    `<h3 class="sub">Zone Transfers</h3>` +
    `<dl class="props">` +
    `<dt><label for="p-allow-transfer">Allow zone transfers (allow-transfer)</label></dt>` +
    `<dd><input id="p-allow-transfer" class="cell-edit" value="${esc(list(detail.allow_transfer))}" placeholder="none"></dd>` +
    `<dt><label for="p-also-notify">also-notify</label></dt>` +
    `<dd><input id="p-also-notify" class="cell-edit" value="${esc(list(detail.also_notify ?? []))}"></dd>` +
    (detail.masters?.length || detail.type !== "master"
      ? `<dt><label for="p-masters">Masters / Primaries</label></dt>` +
        `<dd><input id="p-masters" class="cell-edit" value="${esc(list(detail.masters))}"></dd>`
      : "") +
    `</dl>` +

    `<h3 class="sub">named.conf 블록</h3>` +
    `<pre class="block conf">${highlightConf(zoneConfPreview(detail))}</pre>`;

  setStatusbar(`${detail.name} · properties`);
}

function parseList(value) {
  return (value || "").split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
}

async function saveProperties() {
  const detail = state.cache?.detail;
  if (!detail) return;
  const soaBody = {
    primary: el("p-primary")?.value.trim() || null,
    responsible: el("p-responsible")?.value.trim() || null,
    refresh: Number(el("p-refresh")?.value) || null,
    retry: Number(el("p-retry")?.value) || null,
    expire: Number(el("p-expire")?.value) || null,
    minimum: Number(el("p-minimum")?.value) || null,
  };
  const optionsBody = {
    allow_update: parseList(el("p-allow-update")?.value),
    allow_transfer: parseList(el("p-allow-transfer")?.value),
    also_notify: parseList(el("p-also-notify")?.value),
  };
  if (el("p-masters")) optionsBody.masters = parseList(el("p-masters").value);

  try {
    // 설정(named.conf)과 SOA(zone 파일)는 서로 다른 트랜잭션이다. 순서대로 적용하고 결과를 모은다.
    const options = await send(zonePath("/options"), "PUT", optionsBody);
    if (!options.ok) { showResult(options.result, false); return; }
    const soa = await send(zonePath("/soa"), "PUT", soaBody);
    if (!soa.ok) { showResult(soa.result, false); return; }
    banner(`${detail.name} 속성을 저장했습니다 (serial ${soa.result.serial_before} → ${soa.result.serial_after})`, "ok");
    // refresh() 안에서 loadDetail() 을 부른다. 여기서 또 부르면 두 번 그려지고,
    // 그 사이에 사용자가 입력한 값이 지워진다.
    await refresh();
  } catch (err) {
    banner(err.message, "error");
  }
}

/* ---- New Delegation — 하위 도메인 위임 ---- */

function openDelegation() {
  const zone = state.selection.zone;
  openModal({
    title: `New Delegation — ${zone}`,
    body:
      `<p class="muted">하위 도메인을 다른 네임서버로 위임합니다. 위임 구간 <strong>안쪽</strong> 이름을 ` +
      `네임서버로 쓰면 glue 주소가 필요합니다(없으면 해석이 끊깁니다).</p>` +
      `<div class="field"><label for="d-name">Delegated domain</label>` +
      `<input id="d-name" name="name" placeholder="south" required></div>` +
      `<div class="field"><label for="d-server">Name server (FQDN)</label>` +
      `<input id="d-server" name="server" placeholder="ns1.south.${esc(zone)}." required></div>` +
      `<div class="field"><label for="d-address">Glue 주소 (위임 구간 안쪽일 때만)</label>` +
      `<input id="d-address" name="address" placeholder="192.168.10.40"></div>`,
    buttons: [
      { label: "추가", submit: true, primary: true, action: "submit" },
      { label: "취소", action: "close" },
    ],
    focus: "d-name",
    onSubmit: async (data) => {
      const payload = {
        name: String(data.get("name") || "").trim(),
        servers: [
          {
            name: String(data.get("server") || "").trim(),
            address: String(data.get("address") || "").trim(),
          },
        ],
      };
      try {
        const outcome = await send(zonePath("/delegations"), "POST", payload);
        if (!outcome.ok) { showResult(outcome.result, false); return; }
        closeModal();
        await handleOutcome(outcome);
        await loadDetail();
      } catch (err) {
        modalError(err.message);
      }
    },
  });
}

function renderZoneList(category) {
  const zones = category ? state.zones.filter((z) => z.category === category) : state.zones;
  el("detail-actions").innerHTML =
    `<button type="button" id="btn-new-zone" ${state.zoneDraft ? "disabled" : ""}>New Zone</button>` +
    `<button type="button" id="btn-delete-zone" ${state.selectedZone ? "" : "disabled"}>Delete Zone</button>` +
    `<span class="spacer"></span><span>${zones.length} zones</span>`;
  const rows = zones
    .map(
      (z) =>
        `<tr data-zone="${esc(z.name)}" data-view="${esc(z.view ?? "")}">` +
        `<td>${esc(z.name)}</td><td class="type">${esc(z.type)}</td><td>${esc(z.category)}</td>` +
        `<td class="data">${esc(z.file ?? "-")}` +
        (z.conventional_file === false && z.expected_file_name
          ? ` <span class="badge warn" title="파일명 규칙(정방향 .zone / 역방향 .rev)과 다릅니다. 권장: ${esc(z.expected_file_name)}">규칙 외</span>`
          : "") +
        `</td><td class="ttl">${esc(z.file_serial ?? "-")}</td>` +
        `<td class="ttl">${esc(z.loaded_serial ?? "-")}</td><td>${esc(z.record_count ?? "-")}</td>` +
        `<td>${z.problem ? `<span class="badge err">${esc(z.problem)}</span>` : z.out_of_sync ? "reload 필요" : "ok"}</td></tr>`
    )
    .join("");
  const draft = state.zoneDraft ? zoneDraftRowHtml() : "";
  el("detail-body").innerHTML = zones.length || draft
    ? `<table class="grid"><thead><tr><th>Name</th><th>Type</th><th>Category</th><th>File</th>` +
      `<th>Serial(file)</th><th>Serial(loaded)</th><th>Records</th><th>Status</th></tr></thead>` +
      `<tbody>${rows}${draft}</tbody></table>`
    : `<div class="empty">zone 이 없습니다.</div>`;
  if (state.selectedZone) {
    document.querySelectorAll("tr[data-zone]").forEach((row) => {
      row.classList.toggle("selected", row.dataset.zone === state.selectedZone);
    });
  }
  if (state.zoneDraft) el("zdraft-name")?.focus();
  setStatusbar(`${zones.length} zones`);
}

function renderForwarders() {
  el("detail-actions").innerHTML =
    `<button type="button" id="btn-new-forwarder" ${state.fwdDraft ? "disabled" : ""}>New Conditional Forwarder</button>` +
    `<button type="button" id="btn-edit-server-fwd">기본 전달자 편집</button>` +
    `<span class="spacer"></span><span>조건부 ${state.forwarders.filter((f) => f.scope === "conditional").length}건</span>`;

  const rows = state.forwarders
    .map((f) => {
      const isServer = f.scope === "server";
      const key = isServer ? "__server__" : f.domain;
      if (state.fwdEdit === key) return forwarderEditRowHtml(f, isServer);
      const domain = isServer ? "Default forwarders (server-wide)" : f.domain;
      const ips = f.forwarders.length
        ? esc(f.forwarders.join(", "))
        : `<span class="muted">미설정 — options { forwarders } 없음</span>`;
      return (
        `<tr class="${isServer ? "server-row" : ""}" data-fwd="${esc(key)}"><td>${esc(domain)}</td>` +
        `<td class="type">${esc(f.scope)}</td>` +
        `<td class="data">${ips}</td>` +
        `<td>${esc(f.policy ?? (f.forwarders.length ? "first" : "-"))}</td>` +
        `<td class="fwd-actions"><button type="button" class="mini" data-fwd-edit="${esc(key)}">편집</button>` +
        (isServer
          ? ""
          : ` <button type="button" class="mini" data-fwd-del="${esc(f.domain)}">삭제</button>`) +
        `</td></tr>`
      );
    })
    .join("");

  const conditional = state.forwarders.filter((f) => f.scope === "conditional").length;
  const help =
    `<div class="notice">기본(전역) 전달자는 <code>named.conf</code> 의 <code>options { forwarders }</code> 에, ` +
    `도메인별(조건부) 전달자는 <code>type forward</code> zone 으로 저장됩니다. ` +
    `전역 전달자는 서버 노드 ▸ Server 탭에서도 확인할 수 있습니다. ` +
    `추가·편집은 P1 의 트랜잭션 엔진 적용 후 활성화됩니다.</div>`;

  el("detail-body").innerHTML =
    (state.forwarders.length || state.fwdDraft
      ? `<table class="grid"><thead><tr><th>Domain</th><th>Scope</th><th>Forwarder IPs</th><th>Policy</th><th></th></tr></thead>` +
        `<tbody>${rows}${state.fwdDraft ? forwarderDraftRowHtml() : ""}</tbody></table>`
      : `<div class="empty">설정된 전달자가 없습니다.</div>`) + help;
  const serverRow = state.forwarders.find((f) => f.scope === "server");
  setStatusbar(
    `기본 전달자: ${serverRow?.forwarders.length ? serverRow.forwarders.join(", ") : "미설정"} · ` +
    `조건부 ${conditional}건`
  );
}

function renderServerProperties() {
  const s = state.status ?? {};
  const blockers = s.external_blockers || [];
  el("detail-actions").innerHTML =
    `<button type="button" id="btn-access-save" class="primary">수신·질의 설정 저장</button>` +
    `<span class="spacer"></span>` +
    (blockers.length
      ? `<span class="err-text">외부 질의에 응답하지 않습니다 (${esc(blockers.join(", "))})</span>`
      : `<span class="ok-text">외부 질의에 응답합니다</span>`);

  const accessNotice = blockers.length
    ? `<div class="notice warn">이 서버는 <strong>외부에서 온 질의에 답하지 않습니다</strong>. ` +
      `막고 있는 설정: <code>${esc(blockers.join("</code>, <code>"))}</code>. ` +
      `zone 을 어느 파일에 정의했는지와는 무관합니다 — 아래 값을 고치세요. ` +
      `(<code>listen-on</code> 은 어느 주소로 받을지, <code>allow-query</code> 는 누구에게 답할지를 정합니다)</div>`
    : "";

  el("detail-body").innerHTML = accessNotice + propsList([
    ["Server", "__section__"],
    ["named 상태", s.running ? "running" : "stopped / 접근 불가"],
    ["Version", s.version],
    ["배포판 레이아웃", s.detected_family ? `${s.detected_family} 계열 (자동 감지)` : "(감지 실패 — 설정값 사용)"],
    ...(s.config_warnings?.length ? [["설정 경고", s.config_warnings.join("\n"), "pre"]] : []),
    ["named.conf", s.named_conf],
    ["options 블록 파일", s.options_file],
    ["zone 정의 파일", s.zones_conf],
    ["directory", s.directory],
    ["설정 파일", s.config_source ?? "(기본값)"],
    ["질의 수신·허용 (Interfaces / Advanced)", "__section__"],
    ["listen-on", s.listen_on?.length ? s.listen_on.join(", ") : "(미설정 — 모든 인터페이스)"],
    ["listen-on-v6", s.listen_on_v6?.length ? s.listen_on_v6.join(", ") : "(미설정 — 모든 인터페이스)"],
    ["allow-query", s.allow_query?.length ? s.allow_query.join(", ") : "(미설정 — 모두 허용)"],
    ["allow-recursion", s.allow_recursion?.length ? s.allow_recursion.join(", ") : "(기본값)"],
    ["recursion", s.recursion ?? "(기본값: yes)"],
    ["Forwarders", "__section__"],
    ["기본(전역) forwarders", s.server_forwarders?.length ? s.server_forwarders.join("\n") : "(미설정)"],
    ["forward policy", s.server_forward_policy ?? "(기본값: first)"],
    ["수신·질의 편집", "__section__"],
    ["listen-on (쉼표 구분, any 가능)", `<input id="a-listen-on" class="cell-edit" value="${esc((s.listen_on || []).join(", "))}" placeholder="any">`, "html"],
    ["allow-query", `<input id="a-allow-query" class="cell-edit" value="${esc((s.allow_query || []).join(", "))}" placeholder="any">`, "html"],
    ["allow-recursion", `<input id="a-allow-recursion" class="cell-edit" value="${esc((s.allow_recursion || []).join(", "))}" placeholder="(기본값)">`, "html"],
    ["recursion", `<select id="a-recursion" class="cell-edit"><option value="">(기본값)</option>` +
      ["yes", "no"].map((v) => `<option${v === s.recursion ? " selected" : ""}>${v}</option>`).join("") + `</select>`, "html"],
    ["Validation", "__section__"],
    ["named-checkconf", s.checkconf_ok ? "OK" : "실패"],
    ["오류 출력", s.checkconf_output || "(오류 없음)", s.checkconf_output ? "pre" : null],
    ["rndc status", s.raw || "(응답 없음)", "pre"],
    ["Configuration", "__section__"],
    ["named-checkconf -p (정규화된 설정 전문)", s.parsed_config || "(없음)", "conf"],
  ]);
  setStatusbar(s.running ? `named 실행 중 · ${s.zone_count} zones` : "named 상태를 확인할 수 없습니다");
}

/* ---------------- 상세 렌더링 ----------------
 * loadDetail(): 필요한 데이터를 가져와 캐시에 담고 그린다 (선택·탭·Advanced 변경 시)
 * drawDetail():  캐시만으로 다시 그린다 (필터·검색·정렬 — 재조회 없음)
 */

function detailError(message) {
  el("detail-body").innerHTML = `<div class="notice error">${esc(message)}</div>`;
  el("detail-actions").innerHTML = "";
}

function drawDetail() {
  const sel = state.selection;
  renderTabs();

  if (sel.kind === "zone") {
    el("detail-title").textContent = sel.zone;
    if (!state.cache) return;
    if (state.cache.error) { detailError(state.cache.error); return; }
    if (state.tab === "history") renderHistory();
    else if (state.tab === "raw") renderRaw(state.cache.raw);
    else if (state.tab === "properties") renderZoneProperties(state.cache.detail);
    else renderRecords(state.cache.detail);
    return;
  }
  if (state.tab === "history") {
    el("detail-title").textContent = "변경 이력";
    renderHistory();
    return;
  }
  if (state.tab === "files") {
    el("detail-title").textContent = "Files — 읽고 쓰는 파일";
    renderFiles();
    return;
  }
  if (state.tab === "settings") {
    el("detail-title").textContent = "Settings — BIND 경로";
    renderSettings();
    return;
  }
  if (sel.kind === "forwarders" || state.tab === "forwarders") {
    el("detail-title").textContent = "Conditional Forwarders";
    renderForwarders();
    return;
  }
  if (sel.kind === "category") {
    el("detail-title").textContent = sel.zone === "reverse" ? "Reverse Lookup Zones" : "Forward Lookup Zones";
    renderZoneList(sel.zone);
    return;
  }
  el("detail-title").textContent = state.status?.version ? "DNS server" : "DNS";
  if (state.tab === "properties") renderServerProperties();
  else renderZoneList(null);
}

async function loadDetail() {
  const sel = state.selection;

  if (state.tab === "history") {
    try {
      const target = sel.kind === "zone" ? `?target=${encodeURIComponent(sel.zone)}` : "";
      state.history = await api(`/api/history${target}`);
    } catch (err) {
      state.history = [];
      banner(err.message, "error");
    }
    drawDetail();
    return;
  }

  if (state.tab === "files") {
    try {
      state.files = await api("/api/files?limit=200");
    } catch (err) {
      banner(err.message, "error");
    }
    drawDetail();
    return;
  }

  if (state.tab === "settings") {
    try {
      state.settings = await api("/api/settings");
    } catch (err) {
      banner(err.message, "error");
    }
    drawDetail();
    return;
  }

  if (sel.kind !== "zone") { state.cache = null; drawDetail(); return; }

  try {
    if (state.tab === "raw") {
      const query = sel.view ? `?view=${encodeURIComponent(sel.view)}` : "";
      const [raw, detail] = await Promise.all([
        api(`/api/zones/${encodeURIComponent(sel.zone)}/raw${query}`),
        api(`/api/zones/${encodeURIComponent(sel.zone)}${query}`),
      ]);
      state.cache = { raw, detail };
    } else {
      const params = new URLSearchParams({ advanced: String(state.advanced) });
      if (sel.view) params.set("view", sel.view);
      const detail = await api(`/api/zones/${encodeURIComponent(sel.zone)}?${params}`);
      state.cache = { detail };
      if (state.tab === "properties" && detail.file) {
        try {
          state.cache.soa = await api(`/api/zones/${encodeURIComponent(sel.zone)}/soa`);
        } catch (_) { /* SOA 를 못 읽어도 나머지는 보여준다 */ }
      }
    }
  } catch (err) {
    state.cache = { error: err.message };
  }
  drawDetail();
}

/* ---------------- 데이터 로딩 ---------------- */

async function refresh() {
  try {
    const [status, zones, forwarders] = await Promise.all([
      api("/api/status"),
      api("/api/zones"),
      api("/api/forwarders"),
    ]);
    state.status = status;
    state.zones = zones;
    state.forwarders = forwarders;

    // 자동 종료가 켜져 있으면 남은 시간을 보여 준다. 작업 중에 갑자기 끊기면 당황스럽다.
    const idleNote = el("idle-note");
    if (status.shutdown_after_idle > 0 && status.idle_remaining !== null) {
      const minutes = Math.ceil(status.idle_remaining / 60);
      idleNote.textContent = `유휴 ${minutes}분 후 자동 종료`;
      idleNote.classList.remove("hidden");
      idleNote.title =
        "인증이 없는 도구라 작업 시간 동안만 띄우는 것이 전제입니다. " +
        "화면을 쓰면 시간이 다시 채워집니다.";
    } else {
      idleNote.classList.add("hidden");
    }

    el("server-state").innerHTML = status.running
      ? `<span class="up">named 실행 중</span> · ${esc(status.version ?? "")} · ${status.zone_count} zones`
      : `<span class="down">named 응답 없음</span>`;

    if (status.config_warnings?.length) {
      banner(
        `설정 경로 문제: ${status.config_warnings.join(" / ")} — Settings 탭에서 확인하세요.`,
        "error"
      );
    } else if (!status.running && !status.checkconf_ok) {
      banner(
        "BIND 설정을 읽지 못했습니다. 서버 노드 ▸ Settings 에서 named.conf 경로를 확인하세요.",
        "error"
      );
    } else if (!status.checkconf_ok) banner(`named-checkconf 실패: ${status.checkconf_output}`, "error");
    else {
      const stale = zones.filter((z) => z.out_of_sync).map((z) => z.name);
      const drifted = zones.filter((z) => z.file_changed_since_load).map((z) => z.name);
      const broken = zones.filter((z) => z.problem).map((z) => z.name);
      const orphan = zones.filter((z) => !z.source_file).map((z) => z.name);
      if (orphan.length) {
        banner(
          `출처를 찾을 수 없는 zone: ${orphan.join(", ")} — BIND 는 보고 있는데 앱이 읽는 ` +
            `설정 파일에는 없습니다. Settings 에서 named.conf 경로를 확인하세요.`,
          "error"
        );
      } else if (drifted.length) {
        banner(
          `파일이 바뀌었는데 적용되지 않은 zone: ${drifted.join(", ")} — 지금 서버가 답하는 내용은 ` +
            `파일과 다릅니다. 해당 zone 에서 Reload 를 누르세요.`,
          "error"
        );
      } else if (broken.length) banner(`읽을 수 없는 zone: ${broken.join(", ")}`, "error");
      else if (stale.length) banner(`파일과 적재 serial 이 다른 zone: ${stale.join(", ")} — reload 가 필요합니다.`);
      else banner(null);
    }

    renderTree();
    await loadDetail();
  } catch (err) {
    banner(`상태를 가져올 수 없습니다: ${err.message}`, "error");
    el("server-state").innerHTML = `<span class="down">오류</span>`;
  }
}

/* ---------------- 이벤트 ---------------- */

el("tree").addEventListener("click", (ev) => {
  const twisty = ev.target.closest("[data-twisty]");
  if (twisty) {
    const id = twisty.dataset.twisty;
    state.collapsed.has(id) ? state.collapsed.delete(id) : state.collapsed.add(id);
    renderTree();
    return;
  }
  const node = ev.target.closest(".node");
  if (!node) return;
  const id = node.dataset.id;
  if (id === "server") state.selection = { kind: "server", zone: null, view: null };
  else if (id === "forwarders") state.selection = { kind: "forwarders", zone: null, view: null };
  else if (id.startsWith("cat:")) state.selection = { kind: "category", zone: id.slice(4), view: null };
  else if (id.startsWith("zone:")) {
    const [name, view] = id.slice(5).split("|");
    state.selection = { kind: "zone", zone: name, view: view || null };
  }
  // 선택이 바뀌면 보기 상태를 초기화한다. 탭도 기본값(Records/Zones)으로 돌린다 —
  // 다른 zone 의 Properties 를 보던 상태가 새 선택에 끌려오면 혼란스럽다.
  state.typeFilter = "";
  state.search = "";
  state.sort = { key: "default", dir: 1 };
  state.tab = state.selection.kind === "zone" ? "records" : state.selection.kind === "forwarders" ? "forwarders" : "zones";
  state.cache = null;
  state.selected.clear();
  state.draft = null;
  state.zoneDraft = null;
  state.fwdDraft = null;
  state.fwdEdit = null;
  state.selectedZone = null;
  renderTree();
  loadDetail();
});

el("tabs").addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-tab]");
  if (!btn) return;
  state.tab = btn.dataset.tab;
  renderTabs();
  loadDetail();
});

el("detail-body").addEventListener("click", (ev) => {
  const th = ev.target.closest("th[data-sort]");
  if (th) {
    const key = th.dataset.sort;
    state.sort = state.sort.key === key ? { key, dir: -state.sort.dir } : { key, dir: 1 };
    drawDetail();
    return;
  }
  const tr = ev.target.closest("tr[data-zone]");
  if (tr) {
    if (ev.detail === 1 && !ev.target.closest("button")) {
      // 한 번 클릭은 선택(삭제 대상), 더블클릭하면 그 zone 을 연다.
      state.selectedZone = state.selectedZone === tr.dataset.zone ? null : tr.dataset.zone;
      drawDetail();
      return;
    }
  }
  const diffBtn = ev.target.closest("[data-diff]");
  if (diffBtn) { openDiff(Number(diffBtn.dataset.diff)); return; }
  const rollbackBtn = ev.target.closest("[data-rollback]");
  if (rollbackBtn) { confirmRollback(Number(rollbackBtn.dataset.rollback)); return; }

  const row = ev.target.closest("tr[data-record]");
  if (row && !row.classList.contains("draft")) {
    const id = row.dataset.record;
    if (ev.ctrlKey || ev.metaKey) {
      state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
    } else {
      state.selected.clear();
      state.selected.add(id);
    }
    applySelection();
  }
});

/** 선택 표시만 갱신한다. 테이블을 다시 그리지 않는 것이 중요하다. */
function applySelection() {
  document.querySelectorAll("table.grid tbody tr[data-record]").forEach((row) => {
    row.classList.toggle("selected", state.selected.has(row.dataset.record));
  });
  const count = state.selected.size;
  const del = el("btn-delete-record");
  const props = el("btn-properties");
  if (del) {
    del.disabled = count === 0 || !state.cache?.detail?.table_editable;
    del.textContent = `Delete${count > 1 ? ` (${count})` : ""}`;
  }
  if (props) props.disabled = count !== 1;
}

// 입력 행(draft) 처리
el("detail-body").addEventListener("click", async (ev) => {
  if (ev.target.closest("#draft-commit")) { await commitDraft(); return; }
  if (ev.target.closest("#draft-cancel")) { cancelDraft(); return; }

  if (ev.target.closest("#zdraft-commit")) { await previewZone(); return; }
  if (ev.target.closest("#zdraft-cancel")) { state.zoneDraft = null; drawDetail(); return; }

  if (ev.target.closest("#fdraft-commit")) { await commitForwarderDraft(); return; }
  if (ev.target.closest("#fdraft-cancel")) { state.fwdDraft = null; drawDetail(); return; }
  if (ev.target.closest("#fedit-commit")) { await commitForwarderEdit(); return; }
  if (ev.target.closest("#fedit-cancel")) { state.fwdEdit = null; drawDetail(); return; }

  const fwdEdit = ev.target.closest("[data-fwd-edit]");
  if (fwdEdit) { state.fwdEdit = fwdEdit.dataset.fwdEdit; state.fwdDraft = null; drawDetail(); return; }
  const fwdDel = ev.target.closest("[data-fwd-del]");
  if (fwdDel) { confirmDeleteForwarder(fwdDel.dataset.fwdDel); return; }
});

el("detail-body").addEventListener("change", (ev) => {
  if (ev.target.id === "zdraft-type" || ev.target.id === "zdraft-reverse") {
    state.zoneDraft = readZoneDraft();
    drawDetail();
    return;
  }
  if (ev.target.id === "draft-type") {
    // 타입이 바뀌면 PTR 옵션 노출과 Data 힌트를 갱신한다(입력값은 유지).
    state.draft = readDraft();
    const ptr = document.querySelector(".ptr-opt");
    if (ptr) ptr.classList.toggle("hidden", !["A", "AAAA"].includes(state.draft.type));
    const data = el("draft-data");
    if (data) data.placeholder = DATA_HINTS[state.draft.type] || "rdata";
  }
});

el("detail-body").addEventListener("keydown", async (ev) => {
  if (!ev.target.closest("tr.draft")) return;
  const enter = ev.key === "Enter";
  const escape = ev.key === "Escape";
  if (!enter && !escape) return;
  ev.preventDefault();

  if (state.draft) {
    enter ? await commitDraft() : cancelDraft();
  } else if (state.zoneDraft) {
    if (enter) await previewZone();
    else { state.zoneDraft = null; drawDetail(); }
  } else if (state.fwdDraft) {
    if (enter) await commitForwarderDraft();
    else { state.fwdDraft = null; drawDetail(); }
  } else if (state.fwdEdit) {
    if (enter) await commitForwarderEdit();
    else { state.fwdEdit = null; drawDetail(); }
  }
});

// 셀 더블클릭 → 인라인 편집 (수정은 Properties… 버튼으로도 가능)
el("detail-body").addEventListener("dblclick", (ev) => {
  const cell = ev.target.closest("td[data-edit]");
  if (cell) { ev.preventDefault(); startInlineEdit(cell); return; }

  const zoneRow = ev.target.closest("tr[data-zone]");
  if (zoneRow) {
    state.selection = { kind: "zone", zone: zoneRow.dataset.zone, view: zoneRow.dataset.view || null };
    state.tab = "records";
    state.cache = null;
    state.selectedZone = null;
    renderTree();
    loadDetail();
  }
});

el("detail-actions").addEventListener("click", async (ev) => {
  if (ev.target.closest("#btn-new-record")) {
    await loadRecordTypes();
    startDraft();
    return;
  }
  if (ev.target.closest("#btn-delete-record")) { confirmDelete(); return; }
  if (ev.target.closest("#btn-properties")) {
    const record = currentRecords().find((r) => state.selected.has(r.id));
    if (record) openRecordProperties(record);
    return;
  }
  if (ev.target.closest("#btn-reload-zone")) { await reloadZone(); return; }
  if (ev.target.closest("#btn-raw-save")) { await saveRaw(); return; }
  if (ev.target.closest("#btn-raw-reset")) { await loadDetail(); return; }

  if (ev.target.closest("#btn-new-zone")) {
    state.zoneDraft = { name: "", type: "master", reverse: false, extra: "", ttl: "3600" };
    drawDetail();
    return;
  }
  if (ev.target.closest("#btn-delete-zone")) { confirmDeleteZone(); return; }
  if (ev.target.closest("#btn-new-forwarder")) {
    state.fwdDraft = { domain: "", ips: "", policy: "only" };
    state.fwdEdit = null;
    drawDetail();
    return;
  }
  if (ev.target.closest("#btn-edit-server-fwd")) {
    state.fwdEdit = "__server__";
    state.fwdDraft = null;
    drawDetail();
    return;
  }
  if (ev.target.closest("#btn-props-save")) { await saveProperties(); return; }
  if (ev.target.closest("#btn-access-save")) { await saveServerAccess(); return; }
  if (ev.target.closest("#btn-new-delegation")) { openDelegation(); return; }
  if (ev.target.closest("#btn-files-list")) { state.filesView = "files"; drawDetail(); return; }
  if (ev.target.closest("#btn-files-events")) { state.filesView = "events"; drawDetail(); return; }
  if (ev.target.closest("#btn-files-refresh")) { await loadDetail(); return; }
  if (ev.target.closest("#btn-settings-save")) { await saveSettings(); return; }
  if (ev.target.closest("#btn-settings-detect")) { fillDetected(); return; }
});

el("detail-actions").addEventListener("change", (ev) => {
  if (ev.target.id === "type-filter") { state.typeFilter = ev.target.value; drawDetail(); }
});
el("detail-actions").addEventListener("input", (ev) => {
  if (ev.target.id === "record-search") { state.search = ev.target.value; drawDetail(); }
});

el("advanced-view").addEventListener("change", (ev) => {
  // Advanced 는 서버가 레코드를 걸러 주므로 재조회가 필요하다.
  state.advanced = ev.target.checked;
  loadDetail();
});
el("btn-refresh").addEventListener("click", refresh);

/* ---------------- 모달 ----------------
 * 네이티브 <dialog> 와 prompt() 는 쓰지 않는다 — 스타일 제어가 불가능하고
 * 브라우저마다 동작이 달라 콘솔의 다른 화면과 이질적이다.
 */

let modalSubmit = null;

function openModal({ title, body, buttons = [], onSubmit = null, focus = null }) {
  el("modal-title").textContent = title;
  el("modal-body").innerHTML = body;
  el("modal-foot").innerHTML = buttons
    .map((b) => `<button type="${b.submit ? "submit" : "button"}" class="${b.primary ? "primary" : ""}" data-action="${esc(b.action ?? "close")}">${esc(b.label)}</button>`)
    .join("");
  modalSubmit = onSubmit;
  el("modal-overlay").classList.remove("hidden");
  const target = focus ? el(focus) : el("modal-body").querySelector("input, select, button");
  target?.focus();
  if (target?.select) target.select();
}

function closeModal() {
  el("modal-overlay").classList.add("hidden");
  el("modal-body").innerHTML = "";
  el("modal-foot").innerHTML = "";
  modalSubmit = null;
}

function modalError(message) {
  const body = el("modal-body");
  const existing = body.querySelector(".modal-error");
  if (existing) existing.remove();
  body.insertAdjacentHTML("afterbegin", `<div class="modal-error">${esc(message)}</div>`);
}

el("modal-close").addEventListener("click", closeModal);
el("modal-overlay").addEventListener("mousedown", (ev) => {
  if (ev.target === el("modal-overlay")) closeModal();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !el("modal-overlay").classList.contains("hidden")) closeModal();
});
el("modal-foot").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (btn && btn.dataset.action === "close") closeModal();
});
el("modal-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (modalSubmit) await modalSubmit(new FormData(ev.target));
});

/* nslookup (dig) — Windows DNS Manager 의 Launch nslookup 대응 */

async function runLookup(name, type) {
  const result = el("lookup-result");
  result.innerHTML = `<pre class="block">질의 중…</pre>`;
  try {
    const res = await api(`/api/query?name=${encodeURIComponent(name)}&type=${encodeURIComponent(type)}`);
    result.innerHTML = `<pre class="block zone">${highlightZone(res.output || "(응답 없음)")}</pre>`;
  } catch (err) {
    result.innerHTML = `<pre class="block">${esc(err.message)}</pre>`;
  }
}

el("btn-lookup").addEventListener("click", () => {
  const sel = state.selection;
  const suggestion = sel.kind === "zone" ? sel.zone : "";
  const types = ["A", "AAAA", "CNAME", "MX", "NS", "PTR", "SRV", "TXT", "SOA", "CAA", "ANY"];
  openModal({
    title: "nslookup — 실제 응답 확인 (dig)",
    body:
      `<div class="field"><label for="lookup-name">이름</label>` +
      `<input id="lookup-name" name="name" value="${esc(suggestion)}" placeholder="www.example.local" required></div>` +
      `<div class="field"><label for="lookup-type">타입</label>` +
      `<select id="lookup-type" name="type">${types.map((t) => `<option${t === "A" ? " selected" : ""}>${t}</option>`).join("")}</select></div>` +
      `<div class="field"><label for="lookup-server">질의 대상</label>` +
      `<input id="lookup-server" name="server" value="127.0.0.1"></div>` +
      `<div id="lookup-result"></div>`,
    buttons: [
      { label: "질의", submit: true, primary: true, action: "submit" },
      { label: "닫기", action: "close" },
    ],
    focus: "lookup-name",
    onSubmit: async (form) => {
      const name = String(form.get("name") || "").trim();
      if (!name) { modalError("이름을 입력하세요."); return; }
      await runLookup(name, String(form.get("type") || "A"));
    },
  });
});

// 트리 pane 리사이즈
(() => {
  const splitter = document.querySelector(".splitter");
  const pane = document.querySelector(".tree-pane");
  let dragging = false;
  splitter.addEventListener("mousedown", () => { dragging = true; document.body.style.cursor = "col-resize"; });
  window.addEventListener("mouseup", () => { dragging = false; document.body.style.cursor = ""; });
  window.addEventListener("mousemove", (ev) => {
    if (!dragging) return;
    pane.style.width = `${Math.max(190, Math.min(ev.clientX, 560))}px`;
  });
})();

/* ---------------- 편집 동작 ----------------
 * 각 조작은 서버에서 하나의 완결된 트랜잭션이다(검증 → 원자적 교체 → reload → 실패 시 롤백).
 * 따라서 Windows DNS Manager 처럼 즉시 적용하고, 되돌리기는 History 탭에서 한다.
 * 변경분을 모아 두는 중간 상태를 만들지 않는다 — 부분 적용이라는 애매한 상태가 생기지 않는다.
 */

async function send(path, method, payload) {
  // 쓰기가 진행 중일 때는 조작을 막는다. 겹쳐 보내면 서버가 zone 락으로 거절하고
  // ("다른 작업이 … 변경하는 중입니다") 사용자는 영문을 모른 채 실패를 본다.
  state.busy = true;
  document.body.classList.add("busy");
  try {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(body.detail || res.statusText);
      err.status = res.status;
      throw err;
    }
    return body;
  } finally {
    state.busy = false;
    document.body.classList.remove("busy");
  }
}

/** 조작 결과를 해석해 배너에 띄우고 목록을 새로 고친다. */
async function handleOutcome(outcome, { onSuccess = null } = {}) {
  const result = outcome.result ?? outcome;
  if (!outcome.ok) {
    showResult(result, false);
    return false;
  }
  const extra = (outcome.notes ?? []).join(" / ");
  banner(
    `${result.summary} — 적용됨 (serial ${result.serial_before} → ${result.serial_after})` +
      (extra ? ` · ${extra}` : "") +
      (result.error ? ` · ${result.error}` : ""),
    result.error ? "warn" : "ok"
  );
  await refresh();
  // 선택을 통째로 비우지 않는다. 사라진 레코드만 뺀다 —
  // 여러 건을 연달아 지울 때 새로 고침이 사용자의 선택을 삼키면 안 된다.
  const alive = new Set(currentRecords().map((r) => r.id));
  for (const id of [...state.selected]) {
    if (!alive.has(id)) state.selected.delete(id);
  }
  applySelection();
  if (onSuccess) onSuccess();
  return true;
}

/** 실패 결과를 모달로 보여준다 — BIND 의 원문 출력이 가장 정확한 정보다. */
function showResult(result, ok) {
  openModal({
    title: ok ? "적용 결과" : "적용하지 못했습니다",
    body:
      `<div class="${ok ? "" : "modal-error"}">${esc(result.error || "검증에 실패했습니다.")}</div>` +
      (result.check_output ? `<h3 class="sub">named-checkzone 출력</h3><pre class="block">${esc(result.check_output)}</pre>` : "") +
      (result.reload_output ? `<h3 class="sub">rndc</h3><pre class="block">${esc(result.reload_output)}</pre>` : "") +
      (result.diff ? `<h3 class="sub">변경 내용</h3><pre class="block diff">${highlightDiff(result.diff)}</pre>` : "") +
      `<div class="muted">원본은 변경되지 않았습니다.</div>`,
    buttons: [{ label: "닫기", action: "close" }],
  });
}

function highlightDiff(text) {
  return esc(text)
    .split("\n")
    .map((line) => {
      if (line.startsWith("+++") || line.startsWith("---")) return `<span class="d-head">${line}</span>`;
      if (line.startsWith("@@")) return `<span class="d-hunk">${line}</span>`;
      if (line.startsWith("+")) return `<span class="d-add">${line}</span>`;
      if (line.startsWith("-")) return `<span class="d-del">${line}</span>`;
      return line;
    })
    .join("\n");
}

function zoneQuery() {
  return state.selection.view ? `?view=${encodeURIComponent(state.selection.view)}` : "";
}

function zonePath(suffix = "") {
  return `/api/zones/${encodeURIComponent(state.selection.zone)}${suffix}${zoneQuery()}`;
}

/* ---- 레코드 추가: 테이블 안의 입력 행 ----
 * Windows DNS Manager 는 대화상자를 띄우지만, 여기서는 보고 있는 표에 행을 하나 더해
 * 그 자리에서 입력한다. 표를 벗어나지 않으므로 기존 레코드와 비교하며 넣을 수 있다.
 */

const DRAFT_TYPES = ["A", "AAAA", "CNAME", "MX", "NS", "PTR", "SRV", "TXT", "CAA"];

const DATA_HINTS = {
  A: "192.168.10.50",
  AAAA: "fd00::1",
  CNAME: "target.example.local.",
  MX: "10 mail.example.local.",
  NS: "ns2.example.local.",
  PTR: "host.example.local.",
  SRV: "0 0 5060 sip.example.local.",
  TXT: '"v=spf1 mx -all"',
  CAA: '0 issue "letsencrypt.org"',
};

function draftRowHtml(cols) {
  const d = state.draft;
  const types = [...new Set([...DRAFT_TYPES, ...(recordTypes.other || [])])].filter((t) => t !== "SOA");
  const cell = (key) => {
    if (key === "display_name")
      return `<td class="name"><input id="draft-name" class="cell-edit" placeholder="(비우면 상위 도메인)" value="${esc(d.name)}"></td>`;
    if (key === "type")
      return `<td class="type"><select id="draft-type" class="cell-edit">` +
        types.map((t) => `<option${t === d.type ? " selected" : ""}>${t}</option>`).join("") +
        `</select></td>`;
    if (key === "data")
      return `<td class="data"><input id="draft-data" class="cell-edit" placeholder="${esc(DATA_HINTS[d.type] || "rdata")}" value="${esc(d.data)}"></td>`;
    if (key === "ttl")
      return `<td class="ttl"><input id="draft-ttl" class="cell-edit" type="number" min="0" placeholder="기본값" value="${esc(d.ttl)}"></td>`;
    // Status 열: PTR 체크박스와 확정/취소 버튼
    return (
      `<td class="status draft-actions">` +
      `<label class="ptr-opt${["A", "AAAA"].includes(d.type) ? "" : " hidden"}" title="Create associated pointer (PTR) record">` +
      `<input type="checkbox" id="draft-ptr" ${d.create_ptr ? "checked" : ""}> PTR</label>` +
      `<button type="button" id="draft-commit" class="mini primary" title="추가 (Enter)">추가</button>` +
      `<button type="button" id="draft-cancel" class="mini" title="취소 (Esc)">취소</button></td>`
    );
  };
  return `<tr class="draft">${cols.map((c) => cell(c.key)).join("")}</tr>`;
}

function startDraft() {
  state.draft = { name: "", type: "A", data: "", ttl: "", create_ptr: true };
  drawDetail();
}

function cancelDraft() {
  state.draft = null;
  drawDetail();
}

function readDraft() {
  return {
    name: el("draft-name")?.value.trim() ?? "",
    type: el("draft-type")?.value ?? "A",
    data: el("draft-data")?.value.trim() ?? "",
    ttl: el("draft-ttl")?.value.trim() ?? "",
    create_ptr: el("draft-ptr")?.checked ?? false,
  };
}

async function commitDraft() {
  const d = readDraft();
  state.draft = d;
  if (!d.data) {
    banner("Data 를 입력하세요.", "error");
    el("draft-data")?.focus();
    return;
  }
  const payload = {
    name: d.name || "@",
    type: d.type,
    data: d.data,
    create_ptr: d.create_ptr && ["A", "AAAA"].includes(d.type),
  };
  if (d.ttl) payload.ttl = Number(d.ttl);

  try {
    const outcome = await send(zonePath("/records"), "POST", payload);
    if (outcome.ok) {
      state.draft = null;
      await handleOutcome(outcome);
    } else {
      showResult(outcome.result, false);
    }
  } catch (err) {
    banner(err.message, "error");
  }
}



let recordTypes = { primary: [], other: [] };

async function loadRecordTypes() {
  if (!recordTypes.other.length) {
    try {
      recordTypes = await api("/api/record-types");
    } catch (_) { /* 목록을 못 가져와도 기본 타입으로 입력할 수 있다 */ }
  }
}

/* ---- Properties (레코드 편집) ---- */

function openRecordProperties(record) {
  openModal({
    title: `${record.type} — ${record.display_name}`,
    body:
      `<div class="field"><label for="f-name">Name</label>` +
      `<input id="f-name" name="name" value="${esc(record.name)}"></div>` +
      `<div class="field"><label for="f-type">Type</label>` +
      `<input id="f-type" name="type" value="${esc(record.type)}"></div>` +
      `<div class="field"><label for="f-data">Data</label>` +
      `<input id="f-data" name="data" value="${esc(record.data)}"></div>` +
      `<div class="field"><label for="f-ttl">TTL (초)</label>` +
      `<input id="f-ttl" name="ttl" type="number" value="${esc(record.ttl)}"></div>` +
      (record.fields && Object.keys(record.fields).length
        ? `<h3 class="sub">필드</h3>` +
          `<dl class="props compact">${Object.entries(record.fields)
            .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
            .join("")}</dl>`
        : ""),
    buttons: [
      { label: "저장", submit: true, primary: true, action: "submit" },
      { label: "취소", action: "close" },
    ],
    onSubmit: async (data) => {
      const values = Object.fromEntries(data.entries());
      try {
        const outcome = await send(zonePath(`/records/${record.id}`), "PUT", {
          name: values.name || "@",
          type: values.type,
          data: values.data,
          ttl: values.ttl ? Number(values.ttl) : null,
        });
        if (await handleOutcome(outcome)) closeModal();
      } catch (err) {
        modalError(err.message);
      }
    },
  });
}

/* ---- 인라인 셀 편집 ---- */

function currentRecords() {
  return state.cache?.detail?.records ?? [];
}

function startInlineEdit(cell) {
  if (cell.querySelector("input")) return;
  const row = cell.closest("tr[data-record]");
  const record = currentRecords().find((r) => r.id === row.dataset.record);
  if (!record) return;

  const field = cell.dataset.edit;
  const original = field === "display_name" ? record.name : field === "ttl" ? String(record.ttl) : record.data;
  cell.innerHTML = `<input class="cell-edit" value="${esc(original)}">`;
  const input = cell.querySelector("input");
  input.focus();
  input.select();

  let done = false;
  const cancel = () => {
    if (done) return;
    done = true;
    drawDetail();
  };
  const commit = async () => {
    if (done) return;
    const value = input.value.trim();
    if (value === original) return cancel();
    done = true;
    const payload = {
      name: field === "display_name" ? value || "@" : record.name,
      type: record.type,
      data: field === "data" ? value : record.data,
      ttl: field === "ttl" ? Number(value) : record.ttl,
    };
    try {
      const outcome = await send(zonePath(`/records/${record.id}`), "PUT", payload);
      if (!(await handleOutcome(outcome))) drawDetail();
    } catch (err) {
      banner(err.message, "error");
      drawDetail();
    }
  };

  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); commit(); }
    else if (ev.key === "Escape") { ev.preventDefault(); cancel(); }
  });
  input.addEventListener("blur", commit);
}

/* ---- 삭제 ---- */

function confirmDelete() {
  const targets = currentRecords().filter((r) => state.selected.has(r.id));
  if (!targets.length) return;
  const hostRecords = targets.filter((r) => ["A", "AAAA"].includes(r.type));

  openModal({
    title: `레코드 삭제 (${targets.length}건)`,
    body:
      `<p>다음 레코드를 삭제합니다. 삭제 전 백업이 만들어지며 History 에서 되돌릴 수 있습니다.</p>` +
      `<pre class="block">${esc(targets.map((r) => `${r.display_name}  ${r.type}  ${r.data}`).join("\n"))}</pre>` +
      (hostRecords.length
        ? `<label class="inline"><input type="checkbox" name="delete_ptr" checked> 연결된 PTR 레코드도 삭제</label>`
        : ""),
    buttons: [
      { label: "삭제", submit: true, primary: true, action: "submit" },
      { label: "취소", action: "close" },
    ],
    onSubmit: async (data) => {
      try {
        const outcome = await send(zonePath("/records/delete"), "POST", {
          ids: targets.map((r) => r.id),
          delete_ptr: data.get("delete_ptr") === "on",
        });
        if (await handleOutcome(outcome)) closeModal();
      } catch (err) {
        modalError(err.message);
      }
    },
  });
}

/* ---- raw 저장 ---- */

async function saveRaw() {
  const editor = el("raw-editor");
  if (!editor) return;
  try {
    const outcome = await send(zonePath("/raw"), "PUT", {
      text: editor.value,
      expected_version: state.cache?.raw?.version ?? null,
    });
    if (!outcome.ok) { showResult(outcome.result, false); return; }
    await handleOutcome(outcome);
  } catch (err) {
    if (err.status === 409) {
      banner(`${err.message} Raw 탭을 다시 열어 최신 내용을 확인하세요.`, "error");
    } else {
      banner(err.message, "error");
    }
  }
}

/* ---- zone reload ---- */

async function reloadZone() {
  try {
    const res = await send(zonePath("/reload"), "POST");
    banner(`rndc reload: ${res.output} (적재 serial ${res.loaded_serial})`, res.ok ? "ok" : "error");
    await refresh();
  } catch (err) {
    banner(err.message, "error");
  }
}

/* ---- History ---- */

function renderHistory() {
  const target = state.selection.kind === "zone" ? state.selection.zone : null;
  el("detail-actions").innerHTML =
    `<span>${target ? `${esc(target)} 변경 이력` : "전체 변경 이력"}</span>` +
    `<span class="spacer"></span><span>${state.history.length}건</span>`;

  if (!state.history.length) {
    el("detail-body").innerHTML = `<div class="empty">기록된 변경이 없습니다.</div>`;
    return;
  }
  const rows = state.history
    .map(
      (h) =>
        `<tr data-change="${h.id}">` +
        `<td class="ttl">${h.id}</td><td class="data">${esc(h.ts.replace("T", " ").replace("+00:00", "Z"))}</td>` +
        `<td>${esc(h.target)}</td><td>${esc(h.summary)}</td>` +
        `<td class="type ${h.status}">${esc(h.status)}</td>` +
        `<td class="ttl">${h.serial_before ?? "-"} → ${h.serial_after ?? "-"}</td>` +
        `<td>${esc(h.author ?? "-")}</td>` +
        `<td><button type="button" class="link" data-diff="${h.id}">diff</button>` +
        (h.has_backup && h.kind === "zone" && h.status === "applied"
          ? ` <button type="button" class="link" data-rollback="${h.id}">되돌리기</button>`
          : "") +
        `</td></tr>`
    )
    .join("");
  el("detail-body").innerHTML =
    `<table class="grid"><thead><tr><th>#</th><th>시각(UTC)</th><th>대상</th><th>내용</th>` +
    `<th>상태</th><th>serial</th><th>주체</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  setStatusbar(`${state.history.length} 건의 변경 이력`);
}

async function openDiff(changeId) {
  const change = await api(`/api/history/${changeId}`);
  openModal({
    title: `변경 #${change.id} — ${change.summary}`,
    body:
      `<dl class="props compact"><dt>시각</dt><dd>${esc(change.ts)}</dd>` +
      `<dt>대상</dt><dd>${esc(change.target)}</dd><dt>상태</dt><dd>${esc(change.status)}</dd>` +
      `<dt>주체</dt><dd>${esc(change.author ?? "-")}</dd></dl>` +
      (change.detail ? `<pre class="block">${esc(change.detail)}</pre>` : "") +
      `<pre class="block diff">${highlightDiff(change.diff || "(diff 없음)")}</pre>`,
    buttons: [{ label: "닫기", action: "close" }],
  });
}

function confirmRollback(changeId) {
  openModal({
    title: `변경 #${changeId} 되돌리기`,
    body:
      `<p>이 변경 직전 상태로 zone 을 되돌립니다. 되돌리기 자체도 검증을 거쳐 적용되며 ` +
      `새로운 이력으로 남습니다(serial 은 계속 증가합니다).</p>`,
    buttons: [
      { label: "되돌리기", submit: true, primary: true, action: "submit" },
      { label: "취소", action: "close" },
    ],
    onSubmit: async () => {
      try {
        const outcome = await send(`/api/history/${changeId}/rollback`, "POST");
        if (await handleOutcome(outcome)) closeModal();
      } catch (err) {
        modalError(err.message);
      }
    },
  });
}

el("btn-notice").addEventListener("click", async () => {
  openModal({
    title: "제3자 오픈소스 고지 (NOTICE)",
    body: `<pre class="block">불러오는 중…</pre>`,
    buttons: [{ label: "닫기", action: "close" }],
  });
  try {
    const res = await fetch("/api/notice");
    el("modal-body").innerHTML = `<pre class="block">${esc(await res.text())}</pre>`;
  } catch (err) {
    el("modal-body").innerHTML = `<pre class="block">${esc(err.message)}</pre>`;
  }
});

el("advanced-view").checked = state.advanced;
refresh();

/* ---------------- zone 추가/삭제 ----------------
 * 레코드와 같은 방식으로 표 안의 입력 행에서 만든다. 다만 zone 생성은 파일과 설정 두 곳을
 * 건드리므로, 기록 직전에 "만들어질 named.conf 블록 + zone 파일 전문" 을 확인시킨다.
 */

const ZONE_TYPES = ["master", "slave", "stub", "forward"];

function zoneDraftRowHtml() {
  const d = state.zoneDraft;
  const reverse = d.reverse;
  return (
    `<tr class="draft">` +
    `<td><input id="zdraft-name" class="cell-edit" value="${esc(d.name)}" placeholder="${
      reverse ? "Network ID — 192.168.10 또는 192.168.10.0/24" : "example.local"
    }"></td>` +
    `<td><select id="zdraft-type" class="cell-edit">${ZONE_TYPES.map(
      (t) => `<option${t === d.type ? " selected" : ""}>${t}</option>`
    ).join("")}</select></td>` +
    `<td><label class="inline-chk"><input type="checkbox" id="zdraft-reverse" ${reverse ? "checked" : ""}> 역방향</label></td>` +
    `<td colspan="3"><input id="zdraft-extra" class="cell-edit" value="${esc(d.extra)}" placeholder="${
      d.type === "forward" || d.type === "slave" || d.type === "stub"
        ? "서버 IP (쉼표로 구분)"
        : "(선택) 기본 네임서버 FQDN"
    }"></td>` +
    `<td class="ttl"><input id="zdraft-ttl" class="cell-edit" type="number" value="${esc(d.ttl)}" title="$TTL"></td>` +
    `<td class="draft-actions">` +
    `<button type="button" id="zdraft-commit" class="mini primary">미리보기</button>` +
    `<button type="button" id="zdraft-cancel" class="mini">취소</button></td></tr>`
  );
}

function readZoneDraft() {
  return {
    name: el("zdraft-name")?.value.trim() ?? "",
    type: el("zdraft-type")?.value ?? "master",
    reverse: el("zdraft-reverse")?.checked ?? false,
    extra: el("zdraft-extra")?.value.trim() ?? "",
    ttl: el("zdraft-ttl")?.value.trim() || "3600",
  };
}

function zoneSpecFrom(d) {
  const servers = d.extra ? d.extra.split(",").map((x) => x.trim()).filter(Boolean) : [];
  const spec = { type: d.type, ttl: Number(d.ttl) || 3600 };
  if (d.reverse) {
    spec.reverse = true;
    spec.network_id = d.name;
  } else {
    spec.name = d.name;
  }
  if (d.type === "slave" || d.type === "stub") spec.masters = servers;
  else if (d.type === "forward") { spec.forwarders = servers; spec.forward_policy = "only"; }
  else if (servers.length) spec.primary_ns = servers[0];
  return spec;
}

async function previewZone() {
  const d = readZoneDraft();
  state.zoneDraft = d;
  if (!d.name) {
    banner(d.reverse ? "Network ID 를 입력하세요." : "zone 이름을 입력하세요.", "error");
    return;
  }
  const spec = zoneSpecFrom(d);
  let view;
  try {
    view = await send("/api/zones/preview", "POST", spec);
  } catch (err) {
    banner(err.message, "error");
    return;
  }

  openModal({
    title: `New Zone — ${view.zone}`,
    body:
      `<p class="muted">아래 내용이 그대로 기록됩니다. 확인 후 만드세요.</p>` +
      `<h3 class="sub">named.conf 에 추가될 블록</h3>` +
      `<pre class="block conf">${highlightConf(view.conf_block)}</pre>` +
      (view.zone_file
        ? `<h3 class="sub">zone 파일 — ${esc(view.file_path)}</h3>` +
          `<pre class="block zone">${highlightZone(view.zone_file)}</pre>`
        : `<p class="muted">이 종류의 zone 은 zone 파일을 만들지 않습니다.</p>`) +
      (view.check_output
        ? `<h3 class="sub">named-checkzone 사전 검증</h3><pre class="block">${esc(view.check_output)}</pre>`
        : "") +
      (view.valid ? "" : `<div class="modal-error">검증에 실패했습니다. 만들 수 없습니다.</div>`),
    buttons: view.valid
      ? [
          { label: "만들기", submit: true, primary: true, action: "submit" },
          { label: "취소", action: "close" },
        ]
      : [{ label: "닫기", action: "close" }],
    onSubmit: async () => {
      try {
        const outcome = await send("/api/zones", "POST", spec);
        if (!outcome.ok) { showResult(outcome.result, false); return; }
        state.zoneDraft = null;
        closeModal();
        banner(`zone 생성: ${view.zone}`, "ok");
        await refresh();
      } catch (err) {
        modalError(err.message);
      }
    },
  });
}

function confirmDeleteZone() {
  const zone = state.selectedZone;
  if (!zone) return;
  const info = state.zones.find((z) => z.name === zone);
  openModal({
    title: `zone 삭제 — ${zone}`,
    body:
      `<p>named.conf 에서 이 zone 정의를 지웁니다.</p>` +
      (info?.file
        ? `<label class="inline"><input type="checkbox" name="delete_file"> zone 파일도 지우기 ` +
          `<span class="muted">(${esc(info.file)} → 백업 디렉터리로 이동)</span></label>` +
        `<p class="muted">선택하지 않으면 파일은 그대로 남습니다.</p>`
        : "") +
      `<p class="muted">변경은 이력에 남고 설정은 되돌릴 수 있습니다.</p>`,
    buttons: [
      { label: "삭제", submit: true, primary: true, action: "submit" },
      { label: "취소", action: "close" },
    ],
    onSubmit: async (data) => {
      try {
        const q = data.get("delete_file") === "on" ? "?delete_file=true" : "";
        const outcome = await send(`/api/zones/${encodeURIComponent(zone)}${q}`, "DELETE");
        if (!outcome.ok) { showResult(outcome.result, false); return; }
        state.selectedZone = null;
        closeModal();
        banner(
          `zone 삭제: ${zone}` + (outcome.result.error ? ` — ${outcome.result.error}` : ""),
          outcome.result.error ? "warn" : "ok"
        );
        await refresh();
      } catch (err) {
        modalError(err.message);
      }
    },
  });
}

/* ---------------- 전달자 추가/편집/삭제 ---------------- */

function forwarderDraftRowHtml() {
  const d = state.fwdDraft;
  return (
    `<tr class="draft">` +
    `<td><input id="fdraft-domain" class="cell-edit" value="${esc(d.domain)}" placeholder="partner.example"></td>` +
    `<td class="type">conditional</td>` +
    `<td><input id="fdraft-ips" class="cell-edit" value="${esc(d.ips)}" placeholder="10.9.8.7, 10.9.8.8"></td>` +
    `<td><select id="fdraft-policy" class="cell-edit">` +
    ["only", "first"].map((p) => `<option${p === d.policy ? " selected" : ""}>${p}</option>`).join("") +
    `</select></td>` +
    `<td class="draft-actions"><button type="button" id="fdraft-commit" class="mini primary">추가</button>` +
    `<button type="button" id="fdraft-cancel" class="mini">취소</button></td></tr>`
  );
}

function forwarderEditRowHtml(f, isServer) {
  return (
    `<tr class="draft">` +
    `<td>${esc(isServer ? "Default forwarders (server-wide)" : f.domain)}</td>` +
    `<td class="type">${esc(f.scope)}</td>` +
    `<td><input id="fedit-ips" class="cell-edit" value="${esc(f.forwarders.join(", "))}" placeholder="${
      isServer ? "비우면 기본 전달자 해제" : "10.9.8.7, 10.9.8.8"
    }"></td>` +
    `<td><select id="fedit-policy" class="cell-edit">` +
    ["", "only", "first"].map(
      (p) => `<option value="${p}"${p === (f.policy ?? "") ? " selected" : ""}>${p || "(기본)"}</option>`
    ).join("") +
    `</select></td>` +
    `<td class="draft-actions"><button type="button" id="fedit-commit" class="mini primary">저장</button>` +
    `<button type="button" id="fedit-cancel" class="mini">취소</button></td></tr>`
  );
}

function parseIps(text) {
  return text.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
}

async function commitForwarderDraft() {
  const domain = el("fdraft-domain")?.value.trim() ?? "";
  const ips = parseIps(el("fdraft-ips")?.value ?? "");
  const policy = el("fdraft-policy")?.value ?? "only";
  state.fwdDraft = { domain, ips: el("fdraft-ips")?.value ?? "", policy };

  if (!domain) { banner("도메인을 입력하세요.", "error"); return; }
  if (!ips.length) { banner("전달자 IP 를 입력하세요.", "error"); return; }
  try {
    const outcome = await send(`/api/forwarders/${encodeURIComponent(domain)}`, "PUT", { forwarders: ips, policy });
    if (!outcome.ok) { showResult(outcome.result, false); return; }
    state.fwdDraft = null;
    banner(`조건부 전달자 추가: ${domain} → ${ips.join(", ")}`, "ok");
    await refresh();
  } catch (err) {
    banner(err.message, "error");
  }
}

async function commitForwarderEdit() {
  const key = state.fwdEdit;
  const ips = parseIps(el("fedit-ips")?.value ?? "");
  const policy = el("fedit-policy")?.value || null;
  const isServer = key === "__server__";
  if (!isServer && !ips.length) { banner("전달자 IP 를 입력하세요. (삭제하려면 삭제 버튼을 쓰세요)", "error"); return; }

  try {
    const path = isServer ? "/api/forwarders/server" : `/api/forwarders/${encodeURIComponent(key)}`;
    const outcome = await send(path, "PUT", { forwarders: ips, policy });
    if (!outcome.ok) { showResult(outcome.result, false); return; }
    state.fwdEdit = null;
    banner(isServer
      ? (ips.length ? `기본 전달자: ${ips.join(", ")}` : "기본 전달자 해제")
      : `전달자 변경: ${key}`, "ok");
    await refresh();
  } catch (err) {
    banner(err.message, "error");
  }
}

function confirmDeleteForwarder(domain) {
  openModal({
    title: `조건부 전달자 삭제 — ${domain}`,
    body: `<p>이 도메인의 전달 설정(<code>type forward</code> zone)을 지웁니다.</p>`,
    buttons: [
      { label: "삭제", submit: true, primary: true, action: "submit" },
      { label: "취소", action: "close" },
    ],
    onSubmit: async () => {
      try {
        const outcome = await send(`/api/forwarders/${encodeURIComponent(domain)}`, "DELETE");
        if (!outcome.ok) { showResult(outcome.result, false); return; }
        closeModal();
        banner(`전달자 삭제: ${domain}`, "ok");
        await refresh();
      } catch (err) {
        modalError(err.message);
      }
    },
  });
}

/* ---------------- Settings — BIND 경로 입력 ----------------
 * 자동 감지는 배포판 기본 배치만 안다. 소스 설치·chroot·사내 커스텀 경로에서는
 * 운영자가 직접 넣어야 하므로, 무엇이 왜 부족한지와 함께 입력란을 제공한다.
 */

function renderSettings() {
  const s = state.settings;
  if (!s) { el("detail-body").innerHTML = `<div class="empty">불러오는 중…</div>`; return; }

  el("detail-actions").innerHTML =
    `<button type="button" id="btn-settings-save" class="primary">저장하고 적용</button>` +
    (Object.keys(s.detected || {}).length ? `<button type="button" id="btn-settings-detect">감지값으로 채우기</button>` : "") +
    `<span class="spacer"></span>` +
    `<span class="${s.ready ? "ok-text" : "err-text"}">${s.ready ? "설정 정상" : "설정 미비"}</span>`;

  const rows = s.paths
    .map(
      (p) =>
        `<tr><td>${esc(p.label)}${p.required ? ' <span class="req">*</span>' : ""}</td>` +
        `<td><input class="cell-edit" data-setting="${esc(p.key)}" value="${esc(p.value ?? "")}"></td>` +
        `<td class="${p.ok ? "ok-text" : "err-text"}">${p.ok ? "OK" : "확인 필요"}</td>` +
        `<td class="muted">${esc(p.note || p.description)}</td></tr>`
    )
    .join("");

  const tools = s.tools
    .map(
      (t) =>
        `<tr><td>${esc(t.label)}</td>` +
        `<td><input class="cell-edit" data-setting="${esc(t.key)}" value="${esc(t.value)}"></td>` +
        `<td class="${t.ok ? "ok-text" : "err-text"}">${t.ok ? "OK" : "없음"}</td>` +
        `<td class="muted">${esc(t.found ?? "PATH 에서 찾지 못했습니다. 절대 경로를 넣으세요.")}</td></tr>`
    )
    .join("");

  const detected = Object.keys(s.detected || {}).length
    ? `<h3 class="sub">자동 감지 결과 — ${esc(s.detected.family)} 계열</h3>` +
      `<dl class="props compact">` +
      Object.entries(s.detected)
        .filter(([k]) => k !== "family")
        .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v || "-")}</dd>`)
        .join("") +
      `</dl>`
    : `<div class="notice warn">알려진 배포판 배치(RedHat 계열 /etc/named.conf, Debian 계열 /etc/bind/named.conf)를
       찾지 못했습니다. 아래에 경로를 직접 입력하세요.</div>`;

  el("detail-body").innerHTML =
    (s.problems.length ? `<div class="notice error">${esc(s.problems.join(" / "))}</div>` : "") +
    detected +
    `<h3 class="sub">파일 경로</h3>` +
    `<table class="grid settings"><thead><tr><th>항목</th><th>경로</th><th>상태</th><th>비고</th></tr></thead><tbody>${rows}</tbody></table>` +
    `<h3 class="sub">BIND 도구</h3>` +
    `<table class="grid settings"><thead><tr><th>용도</th><th>명령</th><th>상태</th><th>찾은 위치</th></tr></thead><tbody>${tools}</tbody></table>` +
    `<h3 class="sub">파일명 규칙</h3>` +
    `<table class="grid settings"><tbody>` +
    `<tr><td>정방향 확장자</td><td><input class="cell-edit" data-setting="zone_suffix" value="${esc(s.zone_suffix)}"></td><td colspan="2" class="muted">새 zone 의 기본 파일명</td></tr>` +
    `<tr><td>역방향 확장자</td><td><input class="cell-edit" data-setting="reverse_suffix" value="${esc(s.reverse_suffix)}"></td><td colspan="2" class="muted"></td></tr>` +
    `</tbody></table>` +
    `<div class="notice">저장 위치: ${esc(s.config_path ?? "/etc/dns-manager/config.toml")}` +
    (s.config_writable ? "" : " — <strong>쓰기 권한이 없습니다.</strong> 실행 계정 권한을 확인하세요.") +
    `</div>`;

  setStatusbar(s.ready ? "설정 정상" : `설정 미비: ${s.problems.length}건`);
}

async function saveSettings() {
  const payload = {};
  document.querySelectorAll("[data-setting]").forEach((input) => {
    payload[input.dataset.setting] = input.value.trim();
  });
  try {
    state.settings = await send("/api/settings", "PUT", payload);
    banner(state.settings.ready ? "설정을 저장하고 적용했습니다." : "저장했지만 아직 미비한 항목이 있습니다.",
           state.settings.ready ? "ok" : "warn");
    drawDetail();
    await refresh();
  } catch (err) {
    banner(err.message, "error");
  }
}

async function saveServerAccess() {
  const payload = {
    listen_on: parseList(el("a-listen-on")?.value),
    allow_query: parseList(el("a-allow-query")?.value),
    allow_recursion: parseList(el("a-allow-recursion")?.value),
  };
  const recursion = el("a-recursion")?.value;
  if (recursion) payload.recursion = recursion;

  try {
    const outcome = await send("/api/server/access", "PUT", payload);
    if (!outcome.ok) { showResult(outcome.result, false); return; }
    banner(`수신·질의 설정을 저장했습니다. ${outcome.result.summary}`, "ok");
    await refresh();
  } catch (err) {
    banner(err.message, "error");
  }
}

function fillDetected() {
  const d = state.settings?.detected || {};
  for (const [key, value] of Object.entries(d)) {
    const input = document.querySelector(`[data-setting="${key}"]`);
    if (input && value) input.value = value;
  }
  banner("감지된 경로를 채웠습니다. 확인 후 저장하세요.", "warn");
}

/* ---------------- Files — 이 앱이 읽고 쓰는 파일 ----------------
 * 권한 문제나 "설정이 안 먹는" 상황에서 가장 먼저 보는 화면이다. 그래서 두 가지를 함께 둔다:
 *   - 대상 목록: 아직 건드리지 않은 파일까지 포함한 전체(존재·읽기·쓰기 권한)
 *   - 접근 기록: 실제로 언제 무엇을 읽고 썼는지
 * 키 파일은 경로와 역할만 보여준다. 내용은 서버도 보내지 않는다.
 */

function fileSize(bytes) {
  if (bytes === null || bytes === undefined) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function shortTime(value) {
  return value ? esc(String(value).replace("T", " ").replace("+00:00", "Z")) : "-";
}

function permBadge(file) {
  if (!file.exists) return `<span class="err-text">없음</span>`;
  if (!file.readable) return `<span class="err-text">읽기 불가</span>`;
  return file.writable ? `<span class="ok-text">읽기·쓰기</span>` : `<span class="muted">읽기 전용</span>`;
}

function renderFiles() {
  const data = state.files;
  if (!data) {
    el("detail-body").innerHTML = `<div class="empty">불러오는 중…</div>`;
    return;
  }
  const showEvents = state.filesView === "events";

  el("detail-actions").innerHTML =
    `<button type="button" id="btn-files-list" class="${showEvents ? "" : "primary"}">대상 파일 (${data.files.length})</button>` +
    `<button type="button" id="btn-files-events" class="${showEvents ? "primary" : ""}">접근 기록 (${data.events.length})</button>` +
    `<button type="button" id="btn-files-refresh">⟳ 새로 고침</button>` +
    `<span class="spacer"></span>` +
    `<span class="muted">실제로 접근한 파일 ${data.touched}개</span>`;

  if (showEvents) {
    const rows = data.events
      .map(
        (e) =>
          `<tr><td class="data">${shortTime(e.at)}</td>` +
          `<td class="type ${e.ok ? "" : "rejected"}">${esc(e.action)}</td>` +
          `<td>${esc(e.role_label)}</td>` +
          `<td class="data">${esc(e.path)}</td>` +
          `<td class="muted">${esc(e.detail || (e.ok ? "" : "실패"))}</td></tr>`
      )
      .join("");
    el("detail-body").innerHTML =
      (data.events.length
        ? `<table class="grid"><thead><tr><th>시각(UTC)</th><th>동작</th><th>역할</th><th>경로</th><th>비고</th></tr></thead>` +
          `<tbody>${rows}</tbody></table>`
        : `<div class="empty">아직 기록된 접근이 없습니다. 화면을 몇 번 열거나 변경을 적용해 보세요.</div>`) +
      `<div class="notice">접근 기록은 메모리에만 남습니다(최근 300건). 변경 이력은 History 탭의 DB 에 영구 보관됩니다. ` +
      `키 파일은 경로와 키 이름만 기록하며 비밀값은 어디에도 남기지 않습니다.</div>`;
    setStatusbar(`접근 기록 ${data.events.length}건`);
    return;
  }

  const rows = data.files
    .map((f) => {
      const touched = f.reads || f.writes;
      return (
        `<tr class="${touched ? "" : "untouched"}">` +
        `<td>${esc(f.role_label)}` +
        (f.secret ? ' <span class="badge warn" title="비밀값이 들어 있어 내용을 표시하지 않습니다">비밀</span>' : "") +
        (f.from_history ? ' <span class="badge" title="현재 관리 대상이 아니라 지난 접근 기록에만 있는 경로">기록</span>' : "") +
        `</td>` +
        `<td class="data">${esc(f.path)}</td>` +
        `<td>${permBadge(f)}</td>` +
        `<td class="ttl">${fileSize(f.size)}</td>` +
        `<td class="data">${shortTime(f.mtime)}</td>` +
        `<td class="ttl">${touched ? `읽기 ${f.reads} / 쓰기 ${f.writes}` : '<span class="muted">미접근</span>'}</td>` +
        `<td class="data">${shortTime(f.last_at)}</td>` +
        `<td class="muted">${esc(f.note)}</td></tr>`
      );
    })
    .join("");

  // 지난 기록에 남은 경로(이미 지운 zone 파일, 백업 등)는 "없음" 이 정상이다.
  const problems = data.files.filter((f) => !f.from_history && (!f.exists || !f.readable));
  const notice = problems.length
    ? `<div class="notice warn">읽을 수 없는 파일 ${problems.length}건: ` +
      `${problems.map((f) => esc(f.path)).join(", ")} — Settings 탭에서 경로와 권한을 확인하세요.</div>`
    : "";

  el("detail-body").innerHTML =
    notice +
    `<table class="grid"><thead><tr><th>역할</th><th>경로</th><th>권한</th><th>크기</th>` +
    `<th>수정 시각(UTC)</th><th>접근</th><th>마지막 접근</th><th>비고</th></tr></thead><tbody>${rows}</tbody></table>` +
    `<div class="notice">zone 파일·설정 파일·키 파일·백업·이력 DB 까지 이 앱이 건드리는 전부입니다. ` +
    `회색 행은 아직 접근하지 않은 파일, '기록' 배지는 지난 접근 기록에만 남은 경로(이미 지운 zone 파일 등)입니다.</div>`;
  setStatusbar(`대상 파일 ${data.files.length}개 · 접근 ${data.touched}개`);
}
