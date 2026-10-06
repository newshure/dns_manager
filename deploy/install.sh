#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
# dns_manager 설치 (Rocky Linux 9, vanilla/systemd)
# 폐쇄망: 의존성 wheel 을 미리 받아 두고 --no-index 로 설치할 수 있다.
#   준비:  pip download -r requirements.lock -d vendor/wheels
#   설치:  PIP_ARGS="--no-index --find-links=/opt/dns-manager/vendor/wheels" ./install.sh
set -euo pipefail

PREFIX="${PREFIX:-/opt/dns-manager}"
CONFIG_DIR="${CONFIG_DIR:-/etc/dns-manager}"
STATE_DIR="${STATE_DIR:-/var/lib/dns-manager}"
SERVICE_USER="${SERVICE_USER:-dnsmgr}"
PYTHON="${PYTHON:-python3.12}"
PIP_ARGS="${PIP_ARGS:-}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "root 권한이 필요합니다." >&2; exit 1; }

command -v "${PYTHON}" >/dev/null || { echo "${PYTHON} 가 없습니다. dnf install python3.12" >&2; exit 1; }
command -v named-checkzone >/dev/null || echo "경고: bind 패키지(named-checkzone)가 없습니다." >&2

# BIND 가 쓰는 그룹과 디렉터리는 배포판마다 다르다. 파일 위치로 판단한다
# (RedHat 계열: named / /etc/named / /var/named, Debian 계열: bind / /etc/bind / /var/cache/bind).
if getent group named >/dev/null; then
  BIND_GROUP=named
elif getent group bind >/dev/null; then
  BIND_GROUP=bind
else
  echo "BIND 그룹(named 또는 bind)이 없습니다. bind 패키지를 먼저 설치하세요." >&2
  exit 1
fi

if [[ -f /etc/named.conf ]]; then
  BIND_CONF_DIR=/etc/named
  BIND_ZONE_DIR=/var/named
  NAMED_CONF_DETECTED=/etc/named.conf
  ZONES_CONF_DETECTED=/etc/named/zones.conf
elif [[ -f /etc/bind/named.conf ]]; then
  BIND_CONF_DIR=/etc/bind
  BIND_ZONE_DIR=/var/cache/bind
  NAMED_CONF_DETECTED=/etc/bind/named.conf
  # Debian 은 배포판이 제공하는 사용자 zone 파일을 그대로 쓴다
  ZONES_CONF_DETECTED=/etc/bind/named.conf.local
else
  echo "경고: named.conf 를 찾지 못했습니다. 설치 후 Settings 에서 경로를 지정하세요." >&2
  BIND_CONF_DIR=/etc/named
  BIND_ZONE_DIR=/var/named
  NAMED_CONF_DETECTED=""
  ZONES_CONF_DETECTED=""
fi
echo "BIND 레이아웃: 그룹=${BIND_GROUP}, 설정=${BIND_CONF_DIR}, zone=${BIND_ZONE_DIR}"

# 전용 계정 (쉘 없음, 홈 없음). BIND 그룹으로 zone 파일 쓰기.
id -u "${SERVICE_USER}" >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin -g "${BIND_GROUP}" "${SERVICE_USER}"

install -d -m 0755 "${PREFIX}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${BIND_GROUP}" "${STATE_DIR}" "${STATE_DIR}/backups"
# 서비스 계정이 설정을 읽을 수 있어야 한다
install -d -m 0750 -g "${BIND_GROUP}" "${CONFIG_DIR}"

# 코드 배치 (venv·캐시·빌드 산출물 제외)
tar -C "${SRC_DIR}" \
    --exclude='.venv' --exclude='__pycache__' --exclude='.git' --exclude='temp' \
    --exclude='build' --exclude='dist' --exclude='*.egg-info' \
    -cf - . | tar -C "${PREFIX}" -xf -

# 이전 설치가 남긴 잔재 제거.
# 특히 ${PREFIX}/dns_manager 는 치명적이다: 서비스의 작업 디렉터리가 ${PREFIX} 라
# 파이썬이 그것을 먼저 집어 **설치된 패키지를 가리고 옛 코드를 서비스한다**.
rm -rf "${PREFIX}/dns_manager" "${PREFIX}/build" "${PREFIX}"/*.egg-info
rm -rf "${PREFIX}/unit" "${PREFIX}/smoke" "${PREFIX}/integration" "${PREFIX}/fixtures" "${PREFIX}/conftest.py"

# 설정이 없으면 예제를 깔되, **감지한 경로를 실제 값으로 적어 둔다**.
# 예제에 특정 배포판 경로가 박혀 있으면 다른 배포판에서 자동 감지가 무력화된다.
if [[ ! -f "${CONFIG_DIR}/config.toml" ]]; then
  install -m 0640 -g "${BIND_GROUP}" "${SRC_DIR}/deploy/config.example.toml" "${CONFIG_DIR}/config.toml"
  if [[ -f "${NAMED_CONF_DETECTED}" ]]; then
    sed -i \
      -e "s|^# named_conf = .*|named_conf = \"${NAMED_CONF_DETECTED}\"|" \
      -e "s|^# zones_conf = .*|zones_conf = \"${ZONES_CONF_DETECTED}\"|" \
      -e "s|^# zone_dir = .*|zone_dir = \"${BIND_ZONE_DIR}\"|" \
      "${CONFIG_DIR}/config.toml"
    echo "설정에 감지한 경로를 기록했습니다: ${NAMED_CONF_DETECTED}"
  fi
fi

"${PYTHON}" -m venv "${PREFIX}/.venv"

OFFLINE=0
[[ "${PIP_ARGS}" == *--no-index* ]] && OFFLINE=1

if [[ "${OFFLINE}" -eq 0 ]]; then
  "${PREFIX}/.venv/bin/pip" install --upgrade pip >/dev/null
fi

# 폐쇄망에서는 소스를 빌드하지 않는다 — pip 가 빌드 의존성을 외부에서 받으려다 실패한다.
# 동봉된 wheel 에서 이름으로 설치한다.
if [[ "${OFFLINE}" -eq 1 ]]; then
  # shellcheck disable=SC2086
  "${PREFIX}/.venv/bin/pip" install ${PIP_ARGS} dns-manager
else
  # 설치 경로에서 직접 빌드하면 pip/setuptools 가 build/·egg-info 를 그 안에 만든다.
  # 그 잔재가 작업 디렉터리를 통해 설치된 패키지를 가리므로, 임시 복사본에서 빌드한다.
  BUILD_TMP="$(mktemp -d)"
  trap 'rm -rf "${BUILD_TMP}"' EXIT
  tar -C "${PREFIX}" --exclude='.venv' --exclude='__pycache__' -cf - . | tar -C "${BUILD_TMP}" -xf -
  # shellcheck disable=SC2086
  "${PREFIX}/.venv/bin/pip" install ${PIP_ARGS} "${BUILD_TMP}"
fi

# 설치 후 확인: 서비스가 실제로 집을 모듈이 설치본인지.
INSTALLED="$(cd "${PREFIX}" && "${PREFIX}/.venv/bin/python" -P -c 'import dns_manager; print(dns_manager.__file__)' 2>/dev/null || true)"
if [[ "${INSTALLED}" != "${PREFIX}/.venv/"* ]]; then
  echo "경고: 설치된 패키지가 아닌 다른 경로를 집습니다: ${INSTALLED}" >&2
fi

# systemd 가 없는 환경(컨테이너, 최소 이미지)에서는 유닛 설치를 건너뛴다.
# 설치 자체를 실패로 끝내면 컨테이너로 쓰는 길이 막힌다.
if command -v systemctl >/dev/null 2>&1 && [[ -d /etc/systemd/system ]]; then
  # 유닛의 그룹·쓰기 경로를 감지한 레이아웃으로 맞춘다.
  sed -e "s|^Group=.*|Group=${BIND_GROUP}|" \
      -e "s|^User=.*|User=${SERVICE_USER}|" \
      -e "s|^WorkingDirectory=.*|WorkingDirectory=${PREFIX}|" \
      -e "s|^Environment=DNS_MANAGER_CONFIG=.*|Environment=DNS_MANAGER_CONFIG=${CONFIG_DIR}/config.toml|" \
      -e "s|^ExecStart=.*|ExecStart=${PREFIX}/.venv/bin/python -m dns_manager|" \
      -e "s|^ReadWritePaths=.*|ReadWritePaths=${BIND_ZONE_DIR} ${BIND_CONF_DIR} ${STATE_DIR}|" \
      "${SRC_DIR}/deploy/systemd/dns-manager.service" > /etc/systemd/system/dns-manager.service
  chmod 0644 /etc/systemd/system/dns-manager.service
  systemctl daemon-reload
  HAVE_SYSTEMD=1
else
  HAVE_SYSTEMD=0
  echo "systemd 가 없어 서비스 유닛을 설치하지 않았습니다. 직접 실행하세요:" >&2
  echo "  DNS_MANAGER_CONFIG=${CONFIG_DIR}/config.toml ${PREFIX}/.venv/bin/python -m dns_manager" >&2
fi

# 원자적 교체(임시파일 생성 → rename)에는 "디렉터리" 쓰기 권한이 필요하다.
# 파일만 쓰기 가능하면 적용 단계에서 Permission denied 로 실패한다.
for dir in "${BIND_ZONE_DIR}" "${BIND_CONF_DIR}"; do
  [[ -d "$dir" ]] || continue
  if ! sudo -u "${SERVICE_USER}" test -w "$dir"; then
    echo "경고: ${SERVICE_USER} 가 ${dir} 에 쓸 수 없습니다. 다음을 실행하세요:" >&2
    echo "       chgrp ${BIND_GROUP} ${dir} && chmod g+w ${dir}" >&2
  fi
done

# --- 전역 전달자 관리용 include (선택) ---
# named.conf 는 보통 root 전용이고 서비스는 최소 권한으로 돈다(ProtectSystem=strict 아래에서
# /etc 는 읽기 전용). options 블록 안에 include 를 두면 named.conf 를 건드리지 않고도
# 전역 전달자를 관리할 수 있다.
NAMED_CONF="${NAMED_CONF_DETECTED:-/etc/named.conf}"
FWD_INCLUDE="${BIND_CONF_DIR}/options-forwarders.conf"

if ! grep -q "options-forwarders.conf" "${NAMED_CONF}" 2>/dev/null; then
  cat >&2 <<HINT

[선택] 전역(기본) 전달자를 앱에서 관리하려면 ${NAMED_CONF} 의 options 블록 안에 다음을 넣고,
       그 파일을 named 그룹이 쓸 수 있게 두세요(앱이 named.conf 자체를 고치지 않아도 됩니다):

         options {
             include "${FWD_INCLUDE}";
             ...
         };

         touch ${FWD_INCLUDE} && chgrp ${BIND_GROUP} ${FWD_INCLUDE} && chmod 664 ${FWD_INCLUDE}

HINT
fi

if [[ "${HAVE_SYSTEMD}" -eq 1 ]]; then
  START_HINT="systemctl start dns-manager   # 상시 구동 아님 — 필요할 때만"
else
  START_HINT="DNS_MANAGER_CONFIG=${CONFIG_DIR}/config.toml ${PREFIX}/.venv/bin/python -m dns_manager"
fi

cat <<MSG

설치 완료: ${PREFIX}
설정 파일: ${CONFIG_DIR}/config.toml  (bind.zones_conf 가 named.conf 에서 include 되어 있는지 확인)
기동:      ${START_HINT}
확인:      curl -s http://127.0.0.1:8100/healthz
MSG
