#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
#
# 디렉터리 하나로 끝나는 설치.
#   - systemd 등록 없음, 시스템 계정 생성 없음, /etc 오염 없음
#   - 설치 디렉터리 안에 venv·설정·상태·로그·제어 스크립트가 모두 들어간다
#   - 지우려면 그 디렉터리만 지우면 된다
#
#   deploy/setup.sh [--dir /opt/dns-manager] [--python python3.12]
set -euo pipefail

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# root 로 설치한다. BIND 가 쓰는 계정·그룹은 서버마다 달라 권한을 맞추려 들면
# 서버마다 다른 문제가 생긴다.
if [[ "$(id -u)" != "0" ]]; then
  if command -v sudo >/dev/null 2>&1; then
    say "root 권한이 필요합니다. sudo 로 다시 실행합니다."
    exec sudo -- "${BASH_SOURCE[0]}" "$@"
  fi
  die "root 권한이 필요합니다. root 로 실행하거나 sudo 를 설치하세요."
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-/opt/dns-manager}"
PYTHON="${PYTHON:-}"
PIP_ARGS="${PIP_ARGS:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) shift; APP_DIR="$1" ;;
    --python) shift; PYTHON="$1" ;;
    -h|--help) sed -n '4,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
  shift
done

# 파이썬 선택
#   1) 설치본에 담아 온 portable 파이썬 (대상 호스트에 파이썬이 없어도 된다)
#   2) --python 으로 지정한 것
#   3) 시스템에 있는 3.11 이상
# 배포판마다 있는 파이썬 버전이 다르고, 폐쇄망에서는 "설치하세요" 가 성립하지 않는다.
# 그래서 동봉 파이썬을 가장 먼저 본다.
BUNDLED_PY="${SRC_DIR}/vendor/python/bin/python3"
if [[ -z "${PYTHON}" && -x "${BUNDLED_PY}" ]]; then
  PYTHON="${BUNDLED_PY}"
  USE_BUNDLED=1
fi
if [[ -z "${PYTHON}" ]]; then
  for candidate in python3.12 python3.13 python3.11 python3; do
    command -v "${candidate}" >/dev/null && { PYTHON="${candidate}"; break; }
  done
fi
[[ -n "${PYTHON}" ]] || die "$(printf '%s\n' \
  "쓸 파이썬이 없습니다." \
  "파이썬을 동봉한 설치 파일을 쓰거나(deploy/make-installer.sh 기본 동작)," \
  "대상 호스트에 파이썬 3.11 이상을 설치하세요.")"
"${PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "파이썬 3.11 이상이 필요합니다 (현재: $("${PYTHON}" -V 2>&1))."

command -v named-checkzone >/dev/null || echo "경고: named-checkzone 이 없습니다. bind-utils(bind9-utils)를 설치하세요." >&2

APP_DIR="$(mkdir -p "${APP_DIR}" && cd "${APP_DIR}" && pwd)"
echo "== 설치 위치: ${APP_DIR}"

# 코드 배치.
# .run 인스톨러는 페이로드를 설치 디렉터리에 바로 푼다 — 그때는 이미 제자리에 있으므로
# 복사하지 않는다(자기 자신을 복사하면 tar 가 깨진다).
if [[ "${SRC_DIR}" == "${APP_DIR}" ]]; then
  echo "   (코드는 이미 제자리에 있습니다)"
else
  rm -rf "${APP_DIR}/src" "${APP_DIR}/deploy" "${APP_DIR}/docs" "${APP_DIR}/tests"
  tar -C "${SRC_DIR}" \
      --exclude='.venv' --exclude='__pycache__' --exclude='.git' --exclude='temp' \
      --exclude='build' --exclude='dist' --exclude='*.egg-info' --exclude='run' --exclude='log' \
      -cf - . | tar -C "${APP_DIR}" -xf -
fi

# 이전 설치의 잔재는 어느 경우든 지운다 — 특히 최상위 dns_manager/ 는
# 작업 디렉터리를 통해 설치된 패키지를 가린다.
rm -rf "${APP_DIR}/dns_manager.egg-info" "${APP_DIR}/build" "${APP_DIR}"/*.egg-info
[[ -d "${APP_DIR}/dns_manager" ]] && rm -rf "${APP_DIR}/dns_manager"

# 상태·실행·로그는 root 전용, 설치 디렉터리와 제어 스크립트는 모두가 볼 수 있게 둔다.
# 일반 계정이 `dns_manager start` 를 치면 스크립트가 sudo 로 전환하는 흐름이라,
# 디렉터리를 막아 두면 셸의 "Permission denied" 만 보이고 안내가 닿지 않는다.
install -d -m 0750 "${APP_DIR}/var" "${APP_DIR}/var/backups" "${APP_DIR}/run" "${APP_DIR}/log"
chmod 0755 "${APP_DIR}"

# 설정: 처음 한 번만 만든다. 상태·백업도 설치 디렉터리 안에 둔다(디렉터리 하나로 끝나도록).
if [[ ! -f "${APP_DIR}/config.toml" ]]; then
  cp "${SRC_DIR}/deploy/config.example.toml" "${APP_DIR}/config.toml"
  sed -i -e "s|^state_dir = .*|state_dir = \"${APP_DIR}/var\"|" \
         -e "s|^backup_dir = .*|backup_dir = \"${APP_DIR}/var/backups\"|" \
         "${APP_DIR}/config.toml"

  # BIND 레이아웃을 감지해 적어 둔다(파일 위치 기준).
  if [[ -f /etc/named.conf ]]; then
    NAMED_CONF=/etc/named.conf; ZONES_CONF=/etc/named/zones.conf; ZONE_DIR=/var/named
  elif [[ -f /etc/bind/named.conf ]]; then
    NAMED_CONF=/etc/bind/named.conf; ZONES_CONF=/etc/bind/named.conf.local; ZONE_DIR=/var/cache/bind
  else
    NAMED_CONF=""; echo "경고: named.conf 를 찾지 못했습니다. 실행 후 Settings 에서 경로를 지정하세요." >&2
  fi
  if [[ -n "${NAMED_CONF}" ]]; then
    sed -i -e "s|^# named_conf = .*|named_conf = \"${NAMED_CONF}\"|" \
           -e "s|^# zones_conf = .*|zones_conf = \"${ZONES_CONF}\"|" \
           -e "s|^# zone_dir = .*|zone_dir = \"${ZONE_DIR}\"|" \
           "${APP_DIR}/config.toml"
    echo "   BIND 레이아웃: ${NAMED_CONF}"
  fi
fi

# 동봉 파이썬은 설치 디렉터리로 옮겨 둔다 — 설치 후에도 그 인터프리터로 돌아야 한다.
if [[ "${USE_BUNDLED:-0}" == "1" ]]; then
  if [[ "${SRC_DIR}" != "${APP_DIR}" ]]; then
    rm -rf "${APP_DIR}/vendor/python"
    mkdir -p "${APP_DIR}/vendor"
    cp -a "${SRC_DIR}/vendor/python" "${APP_DIR}/vendor/python"
  fi
  PYTHON="${APP_DIR}/vendor/python/bin/python3"
  echo "== 파이썬 환경 (동봉 $("${PYTHON}" -V 2>&1 | awk '{print $2}'))"
else
  echo "== 파이썬 환경 (${PYTHON})"
fi

# 파이썬이 바뀌었으면 venv 를 다시 만든다(인터프리터 경로가 박혀 있어 섞이면 깨진다).
if [[ -x "${APP_DIR}/.venv/bin/python" ]]; then
  CURRENT_BASE="$("${APP_DIR}/.venv/bin/python" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
  WANTED_BASE="$("${PYTHON}" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
  if [[ "${CURRENT_BASE}" != "${WANTED_BASE}" ]]; then
    echo "   (파이썬이 바뀌어 가상환경을 다시 만듭니다)"
    rm -rf "${APP_DIR}/.venv"
  fi
fi
[[ -x "${APP_DIR}/.venv/bin/python" ]] || "${PYTHON}" -m venv "${APP_DIR}/.venv"

OFFLINE=0
[[ "${PIP_ARGS}" == *--no-index* ]] && OFFLINE=1
[[ "${OFFLINE}" -eq 1 ]] || "${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip

if [[ "${OFFLINE}" -eq 1 ]]; then
  # 폐쇄망: 동봉한 wheel 에서 이름으로 설치한다(소스를 빌드하면 빌드 의존성을 받으러 나간다).
  # shellcheck disable=SC2086
  "${APP_DIR}/.venv/bin/pip" install --quiet ${PIP_ARGS} dns-manager
else
  # 설치 디렉터리에서 직접 빌드하면 build/·egg-info 잔재가 남아 패키지를 가린다.
  BUILD_TMP="$(mktemp -d)"
  trap 'rm -rf "${BUILD_TMP}"' EXIT
  tar -C "${APP_DIR}" --exclude='.venv' --exclude='__pycache__' --exclude='var' \
      --exclude='run' --exclude='log' --exclude='vendor' -cf - . | tar -C "${BUILD_TMP}" -xf -
  "${APP_DIR}/.venv/bin/pip" install --quiet "${BUILD_TMP}"
fi

# 제어 스크립트 — 설치 디렉터리에 남는 유일한 실행 파일
sed "s|@APP_DIR@|${APP_DIR}|g" "${SRC_DIR}/deploy/dns_manager.sh" > "${APP_DIR}/dns_manager"
chmod 0755 "${APP_DIR}/dns_manager"
chmod 0640 "${APP_DIR}/config.toml" 2>/dev/null || true

# 설치본이 제대로 집히는지 확인(작업 디렉터리가 패키지를 가리지 않는지)
INSTALLED="$(cd "${APP_DIR}" && "${APP_DIR}/.venv/bin/python" -P -c 'import dns_manager; print(dns_manager.__file__)' 2>/dev/null || true)"
[[ "${INSTALLED}" == "${APP_DIR}/.venv/"* ]] || echo "경고: 설치본이 아닌 경로를 집습니다: ${INSTALLED}" >&2

cat <<MSG

설치 완료: ${APP_DIR}

  실행    ${APP_DIR}/dns_manager start --host 0.0.0.0 --port 8100
  진단    ${APP_DIR}/dns_manager doctor
  상태    ${APP_DIR}/dns_manager status
  정지    ${APP_DIR}/dns_manager stop
  로그    ${APP_DIR}/dns_manager log -f

  설정    ${APP_DIR}/config.toml
  상태/백업 ${APP_DIR}/var
  지우기   rm -rf ${APP_DIR}

파이썬: $("${APP_DIR}/.venv/bin/python" -V 2>&1)$([[ "${USE_BUNDLED:-0}" == "1" ]] && echo " (동봉)" || echo " (시스템)")

root 로 실행됩니다. BIND 계정·그룹이 서버마다 다른 문제를 피하기 위한 선택입니다.
일반 계정으로 dns_manager 를 실행하면 sudo 로 자동 전환합니다.
MSG
