#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
#
# .run 인스톨러 검증. 깨끗한 컨테이너에 **네트워크를 끊고** 설치해
# 폐쇄망에서 동작하는지 확인한다.
#   tests/integration/installer_check.sh <run 파일> [이미지]
set -euo pipefail

RUN_FILE="${1:?.run 파일 경로를 주세요}"
IMAGE="${2:-rockylinux/rockylinux:9}"
RUN_NAME="$(basename "${RUN_FILE}")"

echo "== 이미지: ${IMAGE} / 파일: ${RUN_NAME}"

# 1단계: BIND·파이썬이 있는 기준 이미지를 만든다(여기까지는 네트워크 필요).
BASE_TAG="dns-manager-test-base:$(echo "${IMAGE}" | tr '/:' '--')"
if ! docker image inspect "${BASE_TAG}" >/dev/null 2>&1; then
  echo "== 기준 이미지 준비 (BIND + python3.12)"
  if [[ "${IMAGE}" == *debian* || "${IMAGE}" == *ubuntu* ]]; then
    PREP='export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq bind9 bind9-utils python3-venv python3-pip >/dev/null'
  else
    PREP='dnf -y install bind python3.12 python3.12-pip >/dev/null 2>&1'
  fi
  docker run --name dm-prep "${IMAGE}" bash -lc "${PREP}" >/dev/null
  docker commit dm-prep "${BASE_TAG}" >/dev/null
  docker rm dm-prep >/dev/null
fi

# 2단계: 네트워크를 끊고 .run 하나만 들고 설치한다.
echo "== 네트워크 차단 상태로 설치"
docker run --rm --network none -v "$(realpath "${RUN_FILE}")":/tmp/"${RUN_NAME}":ro \
  -v "$(realpath "$(dirname "${BASH_SOURCE[0]}")")/installer_probe.py":/tmp/probe.py:ro "${BASE_TAG}" bash -lc '
set -e
cd /tmp
RUN="'"${RUN_NAME}"'"
APP=/opt/dns-manager

echo "--- 무결성 확인"
./"$RUN" --check

echo "--- 버전"
./"$RUN" --version

echo "--- 네트워크 차단 확인"
(timeout 3 getent hosts pypi.org >/dev/null 2>&1 && echo "경고: 외부 조회가 됩니다") || echo "    외부 조회 불가 (의도대로)"

echo "--- 설치 (디렉터리 하나로)"
./"$RUN" --dir "$APP" 2>&1 | tail -8

echo "--- 비대화형에서는 묻지 않고 기본 경로를 쓴다"
echo "" | ./"$RUN" 2>&1 | grep -q "/opt/dns-manager" && echo "    기본 경로 사용 확인" || true

echo "--- 설치 결과"
test -x "$APP/dns_manager" || { echo "제어 스크립트 없음"; exit 1; }
if [ -d "$APP/vendor/python" ]; then
  BASE="$("$APP/.venv/bin/python" -c "import sys; print(sys.base_prefix)")"
  case "$BASE" in
    "$APP"/*) echo "    동봉 파이썬 사용: $("$APP/.venv/bin/python" -V 2>&1)" ;;
    *) echo "동봉 파이썬이 있는데 시스템 파이썬을 쓴다: $BASE"; exit 1 ;;
  esac
fi
test -x "$APP/.venv/bin/python" || { echo "venv 없음"; exit 1; }
test -f "$APP/config.toml" || { echo "설정 없음"; exit 1; }
test -d "$APP/var/backups" || { echo "상태 디렉터리 없음"; exit 1; }
test ! -f /etc/systemd/system/dns-manager.service || { echo "systemd 유닛을 만들면 안 된다"; exit 1; }
id dnsmgr >/dev/null 2>&1 && { echo "시스템 계정을 만들면 안 된다"; exit 1; }
echo "    systemd 유닛·시스템 계정 없음 (의도대로)"

echo "--- 일반 계정 처리"
useradd -m tester 2>/dev/null || true
OUT="$(su tester -c "$APP/dns_manager status" 2>&1 || true)"
if command -v sudo >/dev/null 2>&1; then
  # sudo 가 있으면 전환을 시도한다(암호 없는 환경이 아니면 sudo 가 거부한다)
  echo "    sudo 전환 시도: $(printf "%s" "$OUT" | head -1)"
else
  printf "%s" "$OUT" | grep -q "root 권한이 필요" \
    && echo "    sudo 없음 → 한국어 안내 표시" \
    || { echo "일반 계정 처리가 잘못됐다: $OUT"; exit 1; }
fi

echo "--- BIND 준비"
"$APP/deploy/testenv/setup-test-bind.sh" >/dev/null 2>&1 || true
rndc-confgen -a 2>/dev/null || true
mkdir -p /run/named && chown named:named /run/named 2>/dev/null || true
named -u named 2>/dev/null || /usr/sbin/named -u bind 2>/dev/null || true
for i in $(seq 1 20); do rndc status >/dev/null 2>&1 && break; sleep 0.5; done

echo "--- 기동 (start)"
"$APP/dns_manager" start --host 0.0.0.0 --port 8100

echo "--- 상태 (status)"
"$APP/dns_manager" status

echo "--- 동작 확인"
"$APP/.venv/bin/python" /tmp/probe.py

echo "--- 파일 소유권 (root 로 돌아도 named 가 읽을 수 있어야 한다)"
# zone 디렉터리는 배포판마다 다르다. 앱이 감지한 값을 그대로 쓴다.
ZDIR="$("$APP/.venv/bin/python" -c "
import json, urllib.request
with urllib.request.urlopen(\"http://127.0.0.1:8100/api/status\", timeout=5) as r:
    print(json.load(r)[\"directory\"])
")"
echo "    zone 디렉터리: $ZDIR"
BEFORE_OWNER="$(stat -c "%U:%G %a" "$ZDIR/example.local.zone")"
"$APP/.venv/bin/python" - <<PYOWN
import json, urllib.request
req = urllib.request.Request("http://127.0.0.1:8100/api/zones/example.local/records", method="POST",
    data=json.dumps({"name": "ownercheck", "type": "A", "data": "10.3.3.3"}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    assert json.loads(r.read())["ok"]
PYOWN
AFTER_OWNER="$(stat -c "%U:%G %a" "$ZDIR/example.local.zone")"
[ "$BEFORE_OWNER" = "$AFTER_OWNER" ] \
  && echo "    기존 zone 파일 소유권 보존: $AFTER_OWNER" \
  || { echo "소유권이 바뀌었다: $BEFORE_OWNER → $AFTER_OWNER"; exit 1; }

DIR_GROUP="$(stat -c "%G" "$ZDIR")"
"$APP/.venv/bin/python" - <<PYNEW
import json, urllib.request
req = urllib.request.Request("http://127.0.0.1:8100/api/zones", method="POST",
    data=json.dumps({"name": "ownnew.test", "type": "master", "primary_ns": "ns1.example.local."}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    assert json.loads(r.read())["ok"]
PYNEW
NEW_OWNER="$(stat -c "%G %a" "$ZDIR/ownnew.test.zone")"
case "$NEW_OWNER" in
  "$DIR_GROUP 66"*) echo "    새 zone 파일이 디렉터리 관례를 따름: $NEW_OWNER" ;;
  *) echo "새 zone 파일 소유권이 관례와 다르다: $NEW_OWNER (디렉터리 그룹 $DIR_GROUP)"; exit 1 ;;
esac

named-checkzone ownnew.test "$ZDIR/ownnew.test.zone" >/dev/null \
  && echo "    새 zone 파일 검증 통과" || { echo "새 zone 파일이 깨졌다"; exit 1; }

echo "--- 재기동 (restart)"
"$APP/dns_manager" restart
"$APP/dns_manager" status

echo "--- 정지 (stop)"
"$APP/dns_manager" stop
"$APP/dns_manager" status && { echo "정지 후에도 실행 중으로 나온다"; exit 1; } || echo "    정지 확인"

echo "--- 재설치(업그레이드) 후 설정 보존"
echo "# 사용자가 적은 메모" >> "$APP/config.toml"
./"$RUN" --dir "$APP" >/dev/null 2>&1
grep -q "사용자가 적은 메모" "$APP/config.toml" || { echo "재설치가 설정을 덮어썼다"; exit 1; }
echo "    설정 보존 확인"

echo "== 폐쇄망 설치 검증 완료"
'
