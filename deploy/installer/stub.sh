#!/bin/sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
#
# dns_manager 자체 추출 인스톨러 스텁.
# 이 파일 아래에 tar.gz 페이로드가 이어 붙는다. 외부 자체추출 도구(makeself 등)를 쓰지 않는 이유:
# 생성물에 그 도구의 스텁 코드가 섞여 들어가 라이선스가 따라붙는다. 150줄이면 직접 쓰는 편이 깔끔하다.
#
# POSIX sh 로만 쓴다 — 대상 호스트에 bash 가 없을 수도 있고, 부트스트랩 단계에서 의존성을 늘릴 이유가 없다.
set -eu

PRODUCT="@PRODUCT@"
VERSION="@VERSION@"
BUILT_AT="@BUILT_AT@"
PYTHON_TAG="@PYTHON_TAG@"
PYTHON_CMD="@PYTHON_CMD@"
BUNDLED_PYTHON=@BUNDLED_PYTHON@
ARCH="@ARCH@"
PAYLOAD_SHA256="@PAYLOAD_SHA256@"
PAYLOAD_SIZE="@PAYLOAD_SIZE@"
PAYLOAD_LINE=@PAYLOAD_LINE@
DEFAULT_DIR="${DNS_MANAGER_DIR:-/opt/dns-manager}"

SELF="$0"
case "$SELF" in
  /*) : ;;
  *) SELF="$(pwd)/$SELF" ;;
esac

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- root 권한 확보 ---
# DNS 설정을 다루는 작업이고, BIND 가 쓰는 계정·그룹은 서버마다 달라(named/bind/기타)
# 권한을 맞추려 들면 서버마다 다른 문제가 생긴다. root 로 실행하는 쪽이 단순하고 확실하다.
ensure_root() {
  [ "$(id -u)" = "0" ] && return 0
  if command -v sudo >/dev/null 2>&1; then
    say "root 권한이 필요합니다. sudo 로 다시 실행합니다."
    exec sudo -- "$@"
  fi
  die "root 권한이 필요합니다. root 로 실행하거나 sudo 를 설치하세요."
}


usage() {
  cat <<USAGE
${PRODUCT} ${VERSION} 설치 파일 (${PYTHON_TAG}, ${ARCH}, 빌드 ${BUILT_AT})

지정한 디렉터리 하나에 모두 풀고, 그 안의 실행 스크립트로 쓰는 방식입니다.
systemd 등록도, 시스템 계정 생성도 하지 않습니다. 지울 때는 그 디렉터리만 지우면 됩니다.
설치와 실행 모두 root 로 합니다(일반 계정으로 실행하면 sudo 로 자동 전환).

사용법:
  $(basename "$SELF") [--dir 설치경로] [옵션]

옵션:
  --dir DIR         설치 디렉터리 (기본 ${DEFAULT_DIR}). 주지 않으면 설치 중에 물어본다
  -y, --yes         묻지 않고 기본 경로로 설치
  --python CMD      쓸 파이썬 (파이썬을 동봉한 설치 파일에서는 무시된다)
  --check           무결성만 확인하고 끝낸다
  --extract DIR     설치하지 않고 내용만 푼다
  --list            담긴 파일 목록
  --version         버전
  --help            이 도움말

설치 후:
  <설치경로>/dns_manager start --host 0.0.0.0 --port 8100
  <설치경로>/dns_manager status | stop | restart | log -f

예:
  ./$(basename "$SELF")                           # 경로를 물어본 뒤 설치
  ./$(basename "$SELF") -y                        # 묻지 않고 ${DEFAULT_DIR} 에 설치
  ./$(basename "$SELF") --dir /srv/dns-manager    # 지정한 경로에 설치
USAGE
}

payload() {
  tail -n +"${PAYLOAD_LINE}" "$SELF"
}

verify() {
  # 다운로드·복사 중 손상된 파일로 설치를 시작하지 않는다.
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(payload | sha256sum | cut -d' ' -f1)"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(payload | shasum -a 256 | cut -d' ' -f1)"
  else
    say "경고: sha256 도구가 없어 무결성 확인을 건너뜁니다."
    return 0
  fi
  [ "$actual" = "$PAYLOAD_SHA256" ] || die "페이로드가 손상되었습니다 (기대 ${PAYLOAD_SHA256}, 실제 ${actual})"
}

EXTRACT_ONLY=""
TARGET_DIR="${DEFAULT_DIR}"
PYTHON_OVERRIDE=""
DIR_GIVEN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --version) say "${PRODUCT} ${VERSION} (${PYTHON_TAG}, ${ARCH}, 빌드 ${BUILT_AT})"; exit 0 ;;
    --check) verify; say "무결성 확인 완료: ${PRODUCT} ${VERSION} (${PAYLOAD_SIZE} bytes)"; exit 0 ;;
    --list) verify; payload | tar tzf -; exit 0 ;;
    --extract) shift; [ $# -gt 0 ] || die "--extract 에 디렉터리가 필요합니다"; EXTRACT_ONLY="$1" ;;
    --dir) shift; [ $# -gt 0 ] || die "--dir 에 디렉터리가 필요합니다"; TARGET_DIR="$1"; DIR_GIVEN=1 ;;
    --dir=*) TARGET_DIR="${1#*=}"; DIR_GIVEN=1 ;;
    -y|--yes) DIR_GIVEN=1 ;;
    --python) shift; PYTHON_OVERRIDE="$1" ;;
    *) die "알 수 없는 옵션: $1 (--help 를 보세요)" ;;
  esac
  shift || true
done

verify

if [ -n "$EXTRACT_ONLY" ]; then
  mkdir -p "$EXTRACT_ONLY"
  payload | tar xzf - -C "$EXTRACT_ONLY"
  say "풀었습니다: $EXTRACT_ONLY"
  exit 0
fi

ensure_root "$SELF" "$@"

# --dir 를 주지 않았고 사람이 보고 있으면 설치 경로를 물어본다.
# 자동화(파이프·스크립트)에서는 묻지 않고 기본값을 쓴다.
if [ "${DIR_GIVEN}" = "0" ] && [ -t 0 ] && [ -t 1 ]; then
  printf '설치 경로를 입력하세요 [%s]: ' "${TARGET_DIR}"
  read -r ANSWER || ANSWER=""
  [ -n "${ANSWER}" ] && TARGET_DIR="${ANSWER}"
fi

# 상대 경로도 받아들인다
case "${TARGET_DIR}" in
  /*) : ;;
  *) TARGET_DIR="$(pwd)/${TARGET_DIR}" ;;
esac

if [ -d "${TARGET_DIR}" ] && [ -f "${TARGET_DIR}/dns_manager" ]; then
  say "기존 설치를 갱신합니다: ${TARGET_DIR} (설정 config.toml 은 그대로 둡니다)"
fi

mkdir -p "${TARGET_DIR}" || die "설치 디렉터리를 만들 수 없습니다: ${TARGET_DIR}"

# 파이썬을 함께 담았으면 대상 호스트의 파이썬을 따지지 않는다.
# 담지 않은 경우에만 ABI 를 확인한다 — 동봉 wheel 은 C 확장을 포함해 빌드 파이썬에 묶인다.
TARGET_PYTHON=""
if [ "${BUNDLED_PYTHON}" = "1" ]; then
  say "파이썬 ${PYTHON_TAG} 를 함께 담았습니다 (대상 호스트의 파이썬을 쓰지 않습니다)."
else
  TARGET_PYTHON="${PYTHON_OVERRIDE:-${PYTHON:-${PYTHON_CMD}}}"
  if ! command -v "${TARGET_PYTHON}" >/dev/null 2>&1; then
    die "$(printf '%s\n' \
      "이 설치 파일은 ${PYTHON_TAG} (${ARCH}) 용입니다. '${TARGET_PYTHON}' 를 찾을 수 없습니다." \
      "  - 파이썬을 함께 담은 설치 파일을 쓰거나(deploy/make-installer.sh 기본 동작)," \
      "  - 대상과 같은 버전으로 설치 파일을 다시 만드세요.")"
  fi
  HAVE_TAG="$("${TARGET_PYTHON}" -c 'import sys; print(f"py{sys.version_info[0]}{sys.version_info[1]}")' 2>/dev/null || echo "?")"
  if [ "${HAVE_TAG}" != "${PYTHON_TAG}" ]; then
    die "파이썬 버전이 맞지 않습니다: 설치 파일 ${PYTHON_TAG}, 대상 ${HAVE_TAG}."
  fi
fi

PYTHON="${TARGET_PYTHON}"
export PYTHON

say "${PRODUCT} ${VERSION} 을 ${TARGET_DIR} 에 설치합니다."

# 페이로드를 설치 디렉터리에 바로 푼다 — 임시 공간을 거칠 이유가 없다.
payload | tar xzf - -C "${TARGET_DIR}"

[ -x "${TARGET_DIR}/deploy/setup.sh" ] || die "설치 스크립트를 찾을 수 없습니다 (페이로드 구성 오류)"

# 폐쇄망: 동봉한 wheel 만으로 설치한다. 외부 저장소를 보지 않는다.
if [ -d "${TARGET_DIR}/vendor/wheels" ]; then
  PIP_ARGS="--no-index --find-links=${TARGET_DIR}/vendor/wheels"
  export PIP_ARGS
  say "동봉된 의존성으로 설치합니다 (외부 네트워크를 쓰지 않습니다)."
fi

if [ -n "${TARGET_PYTHON}" ]; then
  PYTHON="${TARGET_PYTHON}"
  export PYTHON
  "${TARGET_DIR}/deploy/setup.sh" --dir "${TARGET_DIR}" --python "${TARGET_PYTHON}"
else
  "${TARGET_DIR}/deploy/setup.sh" --dir "${TARGET_DIR}"
fi

exit 0

__PAYLOAD_BELOW__
