#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
#
# dns_manager 단일 파일 인스톨러(.run) 생성.
#
#   deploy/make-installer.sh [--no-wheels] [--out DIR]
#
# 결과물: dist/dns-manager-<버전>.run — 소스 + 의존성 wheel + 설치 스크립트를 담은
# 자체 추출 실행 파일. 폐쇄망 호스트에 이 파일 하나만 옮기면 설치가 끝난다.
#
# wheel 은 **대상과 같은 환경에서** 받아야 한다(C 확장 포함). 그래서 기본은 빌드 서버에서
# 실행하는 것을 전제로 하며, 파이썬 버전과 플랫폼을 결과물 이름에 적어 둔다.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${SRC}/dist"
WITH_WHEELS=1
BUNDLE_PYTHON=1
PYTHON="${PYTHON:-}"
# 설치본에 담을 portable CPython (python-build-standalone).
# 대상 호스트에 파이썬이 없거나 버전이 달라도 설치·실행된다.
PBS_VERSION="${PBS_VERSION:-3.12.15}"
PBS_RELEASE="${PBS_RELEASE:-20261001}"
PBS_TARGET="${PBS_TARGET:-x86_64-unknown-linux-gnu}"

IN_DOCKER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --in-docker) shift; IN_DOCKER="$1" ;;
    --no-wheels) WITH_WHEELS=0 ;;
    --no-python) BUNDLE_PYTHON=0 ;;
    --python-version) shift; PBS_VERSION="$1" ;;
    --out) shift; OUT_DIR="$1" ;;
    --python) shift; PYTHON="$1" ;;
    -h|--help)
      sed -n '4,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
  shift
done

# 동봉 wheel 은 빌드 파이썬의 ABI 에 묶이므로, 대상과 같은 환경에서 만들어야 한다.
# 다른 배포판용 설치 파일은 그 배포판 컨테이너 안에서 만든다.
if [[ -n "${IN_DOCKER}" ]]; then
  echo "== ${IN_DOCKER} 안에서 빌드"
  mkdir -p "${OUT_DIR}"
  exec docker run --rm -v "${SRC}:/src:ro" -v "${OUT_DIR}:/out" "${IN_DOCKER}" bash -lc '
    set -e
    if command -v apt-get >/dev/null; then
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq >/dev/null && apt-get install -y -qq python3-venv python3-pip >/dev/null
      PY=python3
    else
      dnf -y install python3.12 python3.12-pip >/dev/null 2>&1
      PY=python3.12
    fi
    cp -r /src /build && cd /build && rm -rf .venv dist
    PYTHON=$PY ./deploy/make-installer.sh --out /out
  '
fi

# --- portable 파이썬 내려받기 ---
# 대상 호스트의 파이썬에 기대지 않는다. 배포판마다 있는 버전이 다르고,
# 폐쇄망에서는 "python3.12 를 설치하세요" 라는 안내 자체가 성립하지 않는다.
PBS_DIR=""
if [[ "${BUNDLE_PYTHON}" -eq 1 ]]; then
  # stripped 배포본: 디버그 심볼이 빠져 절반 이하다. 실행에는 차이가 없다.
  PBS_NAME="cpython-${PBS_VERSION}+${PBS_RELEASE}-${PBS_TARGET}-install_only_stripped.tar.gz"
  PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_NAME}"
  PBS_CACHE="${PBS_CACHE:-${SRC}/.cache/python}"
  mkdir -p "${PBS_CACHE}"
  if [[ ! -f "${PBS_CACHE}/${PBS_NAME}" ]]; then
    echo "== portable 파이썬 내려받기 (${PBS_VERSION})"
    curl -fsSL -o "${PBS_CACHE}/${PBS_NAME}.part" "${PBS_URL}" \
      || { echo "내려받기에 실패했습니다: ${PBS_URL}" >&2; exit 1; }
    mv "${PBS_CACHE}/${PBS_NAME}.part" "${PBS_CACHE}/${PBS_NAME}"
  fi
  PBS_DIR="$(mktemp -d)"
  tar -C "${PBS_DIR}" -xzf "${PBS_CACHE}/${PBS_NAME}"
  PYTHON="${PBS_DIR}/python/bin/python3"
  [[ -x "${PYTHON}" ]] || { echo "portable 파이썬 구성이 예상과 다릅니다." >&2; exit 1; }
  echo "   $("${PYTHON}" -V) — 설치본에 함께 담습니다"
fi

# 담을 파이썬이 없으면 시스템 파이썬을 쓴다(--no-python).
if [[ -z "${PYTHON}" ]]; then
  for candidate in python3.12 python3.13 python3.11 python3; do
    command -v "${candidate}" >/dev/null && { PYTHON="${candidate}"; break; }
  done
fi
command -v "${PYTHON}" >/dev/null || [[ -x "${PYTHON}" ]] || { echo "쓸 파이썬이 없습니다." >&2; exit 1; }

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "${SRC}/pyproject.toml" | head -1)"
[[ -n "${VERSION}" ]] || { echo "pyproject.toml 에서 버전을 읽지 못했습니다." >&2; exit 1; }

PY_TAG="$("${PYTHON}" -c 'import sys; print(f"py{sys.version_info[0]}{sys.version_info[1]}")')"
ARCH="$(uname -m)"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}" "${PBS_DIR:-}"' EXIT

echo "== 페이로드 구성 (${VERSION})"
# 설치에 필요한 것만 담는다. .git/.venv/캐시/임시물은 제외한다.
tar -C "${SRC}" \
    --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='temp' --exclude='dist' --exclude='.pytest_cache' --exclude='*.egg-info' \
    --exclude='vendor' \
    -cf - \
    pyproject.toml README.md LICENSE NOTICE.md src deploy docs tests \
    | tar -C "${STAGE}" -xf -

if [[ "${WITH_WHEELS}" -eq 1 ]]; then
  echo "== 의존성 wheel 내려받기 (${PY_TAG}, ${ARCH})"
  mkdir -p "${STAGE}/vendor/wheels"
  "${PYTHON}" -m pip download --quiet --dest "${STAGE}/vendor/wheels" "${SRC}" \
    || { echo "wheel 수집에 실패했습니다. 네트워크가 되는 빌드 서버에서 실행하세요." >&2; exit 1; }
  # 프로젝트 자체도 미리 wheel 로 만들어 담는다. 대상 호스트에서 소스를 빌드하면
  # pip 가 빌드 의존성(setuptools 등)을 외부에서 받으려 해 폐쇄망에서 실패한다.
  "${PYTHON}" -m pip wheel --quiet --no-deps --wheel-dir "${STAGE}/vendor/wheels" "${SRC}" \
    || { echo "프로젝트 wheel 생성에 실패했습니다." >&2; exit 1; }
  echo "   $(ls -1 "${STAGE}/vendor/wheels" | wc -l) 개 수집 (프로젝트 wheel 포함)"
fi

if [[ -n "${PBS_DIR}" ]]; then
  echo "== portable 파이썬 담기"
  mkdir -p "${STAGE}/vendor"
  cp -a "${PBS_DIR}/python" "${STAGE}/vendor/python"
  # 설치본 크기를 줄인다. 이 앱이 쓰지 않는 것만 덜어낸다:
  #   test/idlelib/tkinter/turtledemo — 실행에 필요 없다
  #   libpython*.a, config-* , include  — C 확장을 **빌드**할 때만 쓴다. 우리는 wheel 만 설치한다
  #   __pycache__ — 설치 후 다시 생긴다
  rm -rf "${STAGE}/vendor/python/lib/python"*/test \
         "${STAGE}/vendor/python/lib/python"*/idlelib \
         "${STAGE}/vendor/python/lib/python"*/tkinter \
         "${STAGE}/vendor/python/lib/python"*/turtledemo \
         "${STAGE}/vendor/python/lib/python"*/config-* \
         "${STAGE}/vendor/python/include" \
         "${STAGE}/vendor/python/share"
  rm -f "${STAGE}/vendor/python/lib/libpython"*.a
  find "${STAGE}/vendor/python" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "${STAGE}/vendor/python" -name '*.pyc' -delete 2>/dev/null || true
  echo "   $(du -sh "${STAGE}/vendor/python" | cut -f1)"
fi

mkdir -p "${OUT_DIR}"
if [[ -n "${PBS_DIR}" ]]; then
  NAME="dns-manager-${VERSION}-portable-${ARCH}.run"
else
  NAME="dns-manager-${VERSION}-${PY_TAG}-${ARCH}.run"
fi
TARGET="${OUT_DIR}/${NAME}"
PAYLOAD="${STAGE}.tar.gz"

echo "== 압축"
tar -C "${STAGE}" -czf "${PAYLOAD}" .
PAYLOAD_SHA="$(sha256sum "${PAYLOAD}" | cut -d' ' -f1)"
PAYLOAD_SIZE="$(stat -c%s "${PAYLOAD}")"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "== 스텁 결합"
STUB="${SRC}/deploy/installer/stub.sh"
# 페이로드 시작 줄 번호는 스텁 줄 수 + 1. 치환 전후로 줄 수가 바뀌지 않도록 한 줄 치환만 쓴다.
STUB_LINES="$(wc -l < "${STUB}")"
sed -e "s|@PRODUCT@|dns_manager|g" \
    -e "s|@PYTHON_TAG@|${PY_TAG}|g" \
    -e "s|@PYTHON_CMD@|$([[ -n "${PBS_DIR}" ]] && echo "(bundled)" || echo "${PYTHON}")|g" \
    -e "s|@BUNDLED_PYTHON@|$([[ -n "${PBS_DIR}" ]] && echo 1 || echo 0)|g" \
    -e "s|@ARCH@|${ARCH}|g" \
    -e "s|@VERSION@|${VERSION}|g" \
    -e "s|@BUILT_AT@|${BUILT_AT}|g" \
    -e "s|@PAYLOAD_SHA256@|${PAYLOAD_SHA}|g" \
    -e "s|@PAYLOAD_SIZE@|${PAYLOAD_SIZE}|g" \
    -e "s|@PAYLOAD_LINE@|$((STUB_LINES + 1))|g" \
    "${STUB}" > "${TARGET}"
cat "${PAYLOAD}" >> "${TARGET}"
chmod +x "${TARGET}"
rm -f "${PAYLOAD}"

# 파일 이름만 적는다. 빌드 서버의 절대 경로를 적으면 받는 쪽에서 `sha256sum -c` 가
# "그런 파일 없음" 으로 실패한다 — 배포물은 받는 사람의 디렉터리에서 검증돼야 한다.
(cd "$(dirname "${TARGET}")" && sha256sum "$(basename "${TARGET}")" > "$(basename "${TARGET}").sha256")

echo
echo "완성: ${TARGET}"
echo "크기: $(du -h "${TARGET}" | cut -f1)"
echo "검증: ${TARGET}.sha256"
echo
[[ -n "${PBS_DIR}" ]] && echo "파이썬: ${PBS_VERSION} 동봉 (대상 호스트에 파이썬이 없어도 됩니다)"
echo
echo "대상 호스트에서:"
echo "  ./${NAME} --dir /opt/dns-manager   # 설치 (쓰기 권한 있으면 root 불필요)"
echo "  /opt/dns-manager/dns_manager start --host 0.0.0.0 --port 8100"
echo "  ./${NAME} --check                  # 무결성만 확인"
echo "  ./${NAME} --extract /tmp/x         # 풀어서 내용 확인"
