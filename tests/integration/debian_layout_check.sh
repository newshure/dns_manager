#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
#
# Debian 계열(bind9 패키지) 레이아웃 감지 검증.
# 실제 Debian 컨테이너에 bind9 를 설치해 레이아웃을 감지하고 zone 을 만들어 본다.
# 사용법: tests/integration/debian_layout_check.sh [이미지]
set -euo pipefail

IMAGE="${1:-debian:13}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

docker run --rm -v "${SRC}:/src:ro" "${IMAGE}" bash -lc '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq bind9 bind9-utils python3-venv python3-pip >/dev/null 2>&1

cp -r /src /app && cd /app && rm -rf .venv
python3 -m venv .venv >/dev/null
.venv/bin/pip install -q -e . >/dev/null

echo "=== 설치된 파일 배치"
ls -l /etc/bind/named.conf /etc/bind/named.conf.options /etc/bind/named.conf.local 2>&1 | sed "s|^|  |"
ls -d /var/cache/bind | sed "s|^|  |"

echo "=== 감지 결과"
.venv/bin/python - <<PY
from dns_manager.config import load_config
from dns_manager.core import detect, settings

profile = detect.detect()
assert profile is not None, "레이아웃을 감지하지 못했다"
print(f"  family            : {profile.family}")
print(f"  named.conf        : {profile.named_conf}")
print(f"  zone 정의 파일     : {profile.zones_conf} (include={profile.zones_conf_included})")
print(f"  zone 디렉터리      : {profile.zone_dir}")
print(f"  rndc key          : {profile.rndc_key}")
assert profile.family == "debian", profile.family
assert str(profile.named_conf) == "/etc/bind/named.conf"
assert str(profile.zones_conf) == "/etc/bind/named.conf.local"
assert str(profile.zone_dir) == "/var/cache/bind"
assert profile.zones_conf_included, "named.conf.local 이 include 되어 있어야 한다"

options = detect.find_options_file(profile.named_conf)
print(f"  options 블록 파일  : {options}")
assert str(options) == "/etc/bind/named.conf.options", "Debian 은 options 가 별도 파일에 있다"

cfg = load_config()
print(f"  설정 반영          : {cfg.bind.named_conf} / {cfg.bind.zones_conf} / {cfg.bind.zone_dir}")
assert str(cfg.bind.named_conf) == "/etc/bind/named.conf"
assert str(cfg.bind.zones_conf) == "/etc/bind/named.conf.local"

report = settings.report(cfg)
print(f"  점검               : ready={report.ready} problems={report.problems}")
assert report.ready, report.problems
PY

echo "=== named 기동"
rndc-confgen -a 2>/dev/null || true
mkdir -p /run/named && chown bind:bind /run/named
/usr/sbin/named -u bind
for i in $(seq 1 20); do rndc status >/dev/null 2>&1 && break; sleep 0.5; done
rndc status | head -2 | sed "s|^|  |"

echo "=== 실제 zone 생성 (Debian 경로)"
named-checkconf && echo "  named-checkconf OK"
.venv/bin/python - <<PY
from dns_manager.config import load_config
from dns_manager.core import zoneadmin, service

cfg = load_config()
spec = zoneadmin.ZoneSpec(name="debian.test", zone_type="master")
view, result = zoneadmin.create_zone(cfg, spec, author="integration")
print(f"  생성: ok={result.ok} status={result.status}")
print(f"  파일: {view.file_path}")
assert result.ok, result.error or result.check_output
assert service.find_zone(cfg, "debian.test") is not None

r = zoneadmin.set_server_forwarders(cfg, ["8.8.8.8"], "first", author="integration")
print(f"  전역 전달자: ok={r.ok} (대상 파일 {zoneadmin.options_file(cfg)})")
assert r.ok, r.error or r.check_output
assert "8.8.8.8" in open("/etc/bind/named.conf.options").read(), "named.conf.options 가 수정되어야 한다"

r = zoneadmin.delete_zone(cfg, "debian.test", delete_file=True, author="integration")
print(f"  삭제: ok={r.ok}")
assert r.ok
PY
named-checkconf && echo "  최종 named-checkconf OK"
rndc status >/dev/null && echo "  named 정상 동작 중"
echo "=== Debian 레이아웃 검증 완료"
'
