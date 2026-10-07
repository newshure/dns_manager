#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
#
# dns_manager 실행 제어 스크립트.
# 설치 디렉터리 안에서 모든 것이 끝난다 — systemd 등록도, 시스템 경로 오염도 없다.
#
#   dns_manager start [--host 0.0.0.0] [--port 8100]
#   dns_manager stop | restart | status | run | log
set -uo pipefail

APP_DIR="@APP_DIR@"
PYTHON="${APP_DIR}/.venv/bin/python"
CONFIG="${DNS_MANAGER_CONFIG:-${APP_DIR}/config.toml}"
RUN_DIR="${APP_DIR}/run"
LOG_FILE="${APP_DIR}/log/dns_manager.log"
PID_FILE="${RUN_DIR}/dns_manager.pid"
ARGS_FILE="${RUN_DIR}/last-args"

HOST=""
PORT=""
SHUTDOWN_AFTER=""
WAIT_SECONDS="${DNS_MANAGER_WAIT:-20}"
ORIGINAL_ARGS=("$@")

usage() {
  cat <<USAGE
dns_manager — BIND 9 zone 편집기

사용법:
  dns_manager start   [--host 0.0.0.0] [--port 8100] [--shutdown-after 10m]
  dns_manager stop                                     정지
                                                       백그라운드로 기동
  dns_manager restart [--host ...] [--port ...]        재기동 (인자 없으면 직전 값 사용)
  dns_manager status                                   상태 확인
  dns_manager run     [--host ...] [--port ...]        전면 실행 (Ctrl+C 로 종료)
  dns_manager log     [-f]                             로그 보기
  dns_manager doctor                                   진단 (설정·zone 출처·권한 한눈에)

root 로 동작합니다. 일반 계정으로 실행하면 sudo 로 자동 전환합니다.

설치 위치 : ${APP_DIR}
설정 파일 : ${CONFIG}
로그      : ${LOG_FILE}

host/port 를 주지 않으면 설정 파일의 값을 씁니다(기본 0.0.0.0:8100).

이 도구에는 인증이 없습니다. 서비스로 등록하지 않고, 작업하는 동안만 띄우는 것이 맞습니다.
기본값으로 유휴 10분이 지나면 스스로 종료합니다. 화면 오른쪽 위 '⏻ 종료' 로 바로 내릴 수도 있습니다.
  --shutdown-after 30m   유휴 기준을 바꾼다 (30m, 2h, 90s 형식)
  --shutdown-after off   자동 종료를 끈다 (권하지 않음)
USAGE
}

die() { printf 'dns_manager: %s\n' "$*" >&2; exit 1; }

# root 로 실행한다. zone 파일·named.conf 쓰기와 rndc 호출에 필요하고,
# BIND 가 쓰는 계정·그룹은 서버마다 달라(named/bind/기타) 맞추려 들면 서버마다 문제가 생긴다.
ensure_root() {
  [[ "$(id -u)" == "0" ]] && return 0
  if command -v sudo >/dev/null 2>&1; then
    exec sudo -- "${BASH_SOURCE[0]}" "${ORIGINAL_ARGS[@]}"
  fi
  die "root 권한이 필요합니다. root 로 실행하거나 sudo 를 설치하세요."
}

parse_options() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host) shift; HOST="${1:-}" ;;
      --port) shift; PORT="${1:-}" ;;
      --host=*) HOST="${1#*=}" ;;
      --port=*) PORT="${1#*=}" ;;
      --config) shift; CONFIG="${1:-}" ;;
      --shutdown-after) shift; SHUTDOWN_AFTER="${1:-}" ;;
      --shutdown-after=*) SHUTDOWN_AFTER="${1#*=}" ;;
      -h|--help) usage; exit 0 ;;
      *) die "알 수 없는 옵션: $1" ;;
    esac
    shift || true
  done
}

app_args() {
  local args=()
  [[ -f "${CONFIG}" ]] && args+=(--config "${CONFIG}")
  [[ -n "${HOST}" ]] && args+=(--host "${HOST}")
  [[ -n "${PORT}" ]] && args+=(--port "${PORT}")
  [[ -n "${SHUTDOWN_AFTER}" ]] && args+=(--shutdown-after "${SHUTDOWN_AFTER}")
  printf '%s\n' "${args[@]:-}"
}

running_pid() {
  [[ -f "${PID_FILE}" ]] || return 1
  local pid
  pid="$(cat "${PID_FILE}" 2>/dev/null)"
  [[ -n "${pid}" ]] || return 1
  # PID 재사용에 속지 않도록 명령줄까지 확인한다.
  if kill -0 "${pid}" 2>/dev/null && tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -q dns_manager; then
    printf '%s' "${pid}"
    return 0
  fi
  return 1
}

endpoint() {
  "${PYTHON}" - "$@" <<'PY' 2>/dev/null
import sys, tomllib
from pathlib import Path

config = Path(sys.argv[1]) if len(sys.argv) > 1 else None
host, port = "0.0.0.0", 8100
if config and config.is_file():
    data = tomllib.loads(config.read_text(encoding="utf-8")).get("app", {})
    host = data.get("host", host)
    port = data.get("port", port)
print(f"{host} {port}")
PY
}

health_url() {
  local host="$1" port="$2"
  # 0.0.0.0 으로 리슨해도 확인은 루프백으로 한다.
  [[ "${host}" == "0.0.0.0" || "${host}" == "::" ]] && host="127.0.0.1"
  printf 'http://%s:%s/healthz' "${host}" "${port}"
}

# 서버가 실제로 쓰는 유휴 종료 값(분). 0 이면 꺼짐, 빈 값이면 알 수 없음.
# 설정 파일·기본값·--shutdown-after 가 섞이므로 추측하지 않고 서버에 묻는다.
idle_minutes() {
  "${PYTHON}" - "$1" <<'IDLE'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
        print(int(json.load(response)["shutdown_after_idle"]) // 60)
except Exception:
    pass
IDLE
}

probe() {
  "${PYTHON}" - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

resolve_endpoint() {
  local from_config
  from_config="$(endpoint "${CONFIG}")"
  [[ -n "${HOST}" ]] || HOST="$(awk '{print $1}' <<<"${from_config}")"
  [[ -n "${PORT}" ]] || PORT="$(awk '{print $2}' <<<"${from_config}")"
}

cmd_start() {
  local pid
  if pid="$(running_pid)"; then
    echo "이미 실행 중입니다 (pid ${pid}). 재기동하려면 restart 를 쓰세요."
    return 0
  fi
  [[ -x "${PYTHON}" ]] || die "파이썬 환경이 없습니다: ${PYTHON} (설치가 끝나지 않았습니다)"

  resolve_endpoint
  mkdir -p "${RUN_DIR}" "$(dirname "${LOG_FILE}")"
  printf 'HOST=%s\nPORT=%s\nCONFIG=%s\nSHUTDOWN_AFTER=%s\n' \
    "${HOST}" "${PORT}" "${CONFIG}" "${SHUTDOWN_AFTER}" > "${ARGS_FILE}"

  local args=()
  mapfile -t args < <(app_args)
  # 터미널을 닫아도 살아 있도록 세션에서 떼어 낸다.
  DNS_MANAGER_CONFIG="${CONFIG}" PYTHONSAFEPATH=1 \
    setsid nohup "${PYTHON}" -m dns_manager "${args[@]}" >>"${LOG_FILE}" 2>&1 &
  local pid=$!
  echo "${pid}" > "${PID_FILE}"

  local url
  url="$(health_url "${HOST}" "${PORT}")"
  local waited=0
  while (( waited < WAIT_SECONDS * 2 )); do
    if probe "${url}"; then
      echo "기동했습니다: ${HOST}:${PORT} (pid ${pid})"
      echo "  로그: ${LOG_FILE}"
      local idle
      idle="$(idle_minutes "${url%/healthz}/api/status" 2>/dev/null)"
      if [[ -n "${idle}" && "${idle}" -gt 0 ]]; then
        echo "  유휴 ${idle}분이 지나면 스스로 종료합니다 (화면에서 작업하면 연장)."
        echo "  바로 내리려면 화면 오른쪽 위 '⏻ 종료' 또는 dns_manager stop."
      else
        echo "  자동 종료가 꺼져 있습니다. 인증이 없는 도구이므로 작업이 끝나면"
        echo "  dns_manager stop 으로 내려 주세요."
      fi
      return 0
    fi
    kill -0 "${pid}" 2>/dev/null || break
    sleep 0.5
    (( waited++ ))
  done

  echo "기동에 실패했습니다. 로그 마지막 20줄:" >&2
  tail -20 "${LOG_FILE}" >&2 2>/dev/null
  rm -f "${PID_FILE}"
  return 1
}

cmd_stop() {
  local pid
  if ! pid="$(running_pid)"; then
    echo "실행 중이 아닙니다."
    rm -f "${PID_FILE}"
    return 0
  fi
  kill -TERM "${pid}" 2>/dev/null
  local waited=0
  while (( waited < 20 )) && kill -0 "${pid}" 2>/dev/null; do
    sleep 0.5
    (( waited++ ))
  done
  if kill -0 "${pid}" 2>/dev/null; then
    echo "정상 종료에 응답하지 않아 강제 종료합니다 (pid ${pid})." >&2
    kill -KILL "${pid}" 2>/dev/null
    sleep 0.5
  fi
  rm -f "${PID_FILE}"
  echo "정지했습니다."
}

cmd_restart() {
  # 인자를 주지 않으면 직전 기동 때 쓴 값을 그대로 쓴다.
  if [[ -z "${HOST}${PORT}" && -f "${ARGS_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ARGS_FILE}"
  fi
  cmd_stop
  cmd_start
}

cmd_status() {
  local pid
  if pid="$(running_pid)"; then
    [[ -f "${ARGS_FILE}" ]] && source "${ARGS_FILE}"
    local url
    url="$(health_url "${HOST:-0.0.0.0}" "${PORT:-8100}")"
    if probe "${url}"; then
      echo "실행 중 (pid ${pid}) — ${HOST:-0.0.0.0}:${PORT:-8100}"
    else
      echo "프로세스는 있으나 응답하지 않습니다 (pid ${pid}). 로그를 확인하세요: ${LOG_FILE}"
      return 1
    fi
  else
    # 스스로 종료했으면 PID 파일만 남는다. 치워 둔다.
    [[ -f "${PID_FILE}" ]] && rm -f "${PID_FILE}"
    echo "실행 중이 아닙니다."
    return 1
  fi
}

cmd_run() {
  [[ -x "${PYTHON}" ]] || die "파이썬 환경이 없습니다: ${PYTHON}"
  resolve_endpoint
  local args=()
  mapfile -t args < <(app_args)
  echo "전면 실행 (${HOST}:${PORT}) — Ctrl+C 로 종료합니다."
  exec env DNS_MANAGER_CONFIG="${CONFIG}" PYTHONSAFEPATH=1 "${PYTHON}" -m dns_manager "${args[@]}"
}

cmd_doctor() {
  [[ -x "${PYTHON}" ]] || die "파이썬 환경이 없습니다: ${PYTHON}"
  DNS_MANAGER_CONFIG="${CONFIG}" PYTHONSAFEPATH=1 "${PYTHON}" -m dns_manager --config "${CONFIG}" --doctor
}

cmd_log() {
  [[ -f "${LOG_FILE}" ]] || die "로그가 없습니다: ${LOG_FILE}"
  if [[ "${1:-}" == "-f" ]]; then
    tail -f "${LOG_FILE}"
  else
    tail -50 "${LOG_FILE}"
  fi
}

COMMAND="${1:-}"
[[ $# -gt 0 ]] && shift

case "${COMMAND}" in
  start)   ensure_root; parse_options "$@"; cmd_start ;;
  stop)    ensure_root; cmd_stop ;;
  restart) ensure_root; parse_options "$@"; cmd_restart ;;
  status)  ensure_root; cmd_status ;;
  run)     ensure_root; parse_options "$@"; cmd_run ;;
  log|logs) ensure_root; cmd_log "${1:-}" ;;
  doctor)  ensure_root; cmd_doctor ;;
  ""|-h|--help|help) usage ;;
  *) die "알 수 없는 명령: ${COMMAND} (--help 를 보세요)" ;;
esac
