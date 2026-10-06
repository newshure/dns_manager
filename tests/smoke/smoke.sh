#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
# 스모크 테스트 — 실제 BIND 가 동작하는 호스트에서 실행한다.
# 사용법: BASE=http://127.0.0.1:8100 tests/smoke/smoke.sh
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8100}"
ZONE="${ZONE:-example.local}"
REVERSE_ZONE="${REVERSE_ZONE:-10.168.192.in-addr.arpa}"
pass=0; fail=0

check() { # 설명, jq 식, 기대값, 경로
  local desc="$1" filter="$2" expect="$3" path="$4" got
  got="$(curl -fsS "${BASE}${path}" 2>/dev/null | python3 -c "
import json,sys
doc=json.load(sys.stdin)
try:
    print(eval(sys.argv[1], {'d': doc}))
except Exception as exc:
    print(f'ERR:{exc}')
" "$filter" 2>/dev/null)"
  if [[ "$got" == "$expect" ]]; then
    printf '  ok   %s\n' "$desc"; pass=$((pass+1))
  else
    printf '  FAIL %s (기대 %s, 실제 %s) %s\n' "$desc" "$expect" "$got" "$path"; fail=$((fail+1))
  fi
}

http_code() { curl -s -o /dev/null -w '%{http_code}' "${BASE}$1"; }

echo "dns_manager 스모크 테스트 — ${BASE}"

code="$(http_code /healthz)"
[[ "$code" == "200" ]] && { echo "  ok   /healthz"; pass=$((pass+1)); } || { echo "  FAIL /healthz ($code)"; fail=$((fail+1)); }

code="$(http_code /)"
[[ "$code" == "200" ]] && { echo "  ok   콘솔 페이지"; pass=$((pass+1)); } || { echo "  FAIL 콘솔 페이지 ($code)"; fail=$((fail+1)); }

check "named 실행 중"            "d['running']"        "True"  "/api/status"
check "named-checkconf OK"       "d['checkconf_ok']"   "True"  "/api/status"
check "zone 디렉터리 탐색"        "bool(d['directory'])" "True" "/api/status"
check "zone 목록 비어있지 않음"    "len(d) > 0"          "True"  "/api/zones"
check "동적 zone 인식"            "any(z['dynamic'] for z in d)" "True" "/api/zones"
check "정방향 zone 존재"          "any(z['name']=='${ZONE}' for z in d)" "True" "/api/zones"
check "역방향 zone 분류"          "[z['category'] for z in d if z['name']=='${REVERSE_ZONE}'] == ['reverse']" "True" "/api/zones"
check "zone 편집 가능 판정"        "[z['editable'] for z in d if z['name']=='${ZONE}'] == [True]" "True" "/api/zones"
check "파일/적재 serial 일치"      "[z['out_of_sync'] for z in d if z['name']=='${ZONE}'] == [False]" "True" "/api/zones"
check "레코드 조회"               "len(d['records']) > 0" "True" "/api/zones/${ZONE}"
check "기본 보기에서 SOA 숨김"     "all(r['type']!='SOA' for r in d['records'])" "True" "/api/zones/${ZONE}"
check "Advanced 보기에서 SOA 노출" "any(r['type']=='SOA' for r in d['records'])" "True" "/api/zones/${ZONE}?advanced=true"
check "apex 표기"                 "any(r['display_name']=='(same as parent folder)' for r in d['records'])" "True" "/api/zones/${ZONE}?advanced=true"
check "테이블 편집 가능"           "d['table_editable']" "True" "/api/zones/${ZONE}"
check "raw 텍스트 조회"           "'SOA' in d['text']"  "True"  "/api/zones/${ZONE}/raw"
check "전달자 엔드포인트"          "isinstance(d, list)" "True"  "/api/forwarders"
check "dig 질의 동작"             "'ANSWER' in d['output'] or d['returncode']==0" "True" "/api/query?name=www.${ZONE}&type=A"
check "레코드 타입 목록"           "'A' in d['primary']" "True"  "/api/record-types"

code="$(http_code /api/zones/does-not-exist.invalid)"
[[ "$code" == "404" ]] && { echo "  ok   없는 zone 404"; pass=$((pass+1)); } || { echo "  FAIL 없는 zone ($code)"; fail=$((fail+1)); }

echo
echo "결과: ${pass} 통과, ${fail} 실패"
[[ "$fail" -eq 0 ]]
