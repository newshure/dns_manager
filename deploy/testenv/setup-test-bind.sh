#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
# 테스트용 BIND 환경 구성 (Rocky Linux 9)
# - 127.0.0.1:53 에서만 수신하는 로컬 전용 권한 서버
# - zone 정의는 /etc/named/zones.conf (named.conf 에서 include)
# - 파일명 규칙: 정방향 <zone>.zone / 역방향 <zone>.rev
# - 전역(기본) 전달자와 조건부 전달자를 모두 구성
# - dns_manager 가 zone 파일과 include 파일을 쓸 수 있도록 그룹 쓰기 권한 부여
# 멱등: 여러 번 실행해도 안전
set -euo pipefail

# 배포판별 BIND 레이아웃 (파일 위치로 판단)
if [[ -f /etc/bind/named.conf ]]; then
  ZONE_DIR=/var/cache/bind
  CONF_DIR=/etc/bind
  ZONES_CONF="${CONF_DIR}/named.conf.local"
  NAMED_CONF=/etc/bind/named.conf
  BIND_GROUP=bind
else
  ZONE_DIR=/var/named
  CONF_DIR=/etc/named
  ZONES_CONF="${CONF_DIR}/zones.conf"
  NAMED_CONF=/etc/named.conf
  BIND_GROUP=named
fi
MGR_USER="${MGR_USER:-$(id -un)}"

need_root() { [[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"; }
need_root "$@"

command -v named >/dev/null || { command -v dnf >/dev/null && dnf -y install bind || apt-get install -y bind9; }

# 원자적 교체는 같은 디렉터리에 임시파일을 만든다 → 디렉터리 그룹 쓰기 권한이 필요하다
install -d -m 0770 -o root -g "${BIND_GROUP}" "${CONF_DIR}"

# --- named.conf: include 추가 (중복 방지) ---
if ! grep -qF "include \"${ZONES_CONF}\";" "${NAMED_CONF}"; then
  cp -a "${NAMED_CONF}" "${NAMED_CONF}.bak.$(date -u +%Y%m%d%H%M%S)"
  printf '\ninclude "%s";\n' "${ZONES_CONF}" >> "${NAMED_CONF}"
fi

# --- 테스트 zone 정의 ---
# 블록별로 "없으면 덧붙인다". 파일 통째로 덮어쓰면 배포판이 이미 제공하는 내용
# (Debian 의 named.conf.local 등)을 잃는다.
[[ -f "${ZONES_CONF}" ]] || { : > "${ZONES_CONF}"; }

if ! grep -q 'zone "example.local"' "${ZONES_CONF}"; then
  cat >> "${ZONES_CONF}" <<'CONF'

// dns_manager 테스트용 정방향 zone
zone "example.local" IN {
    type master;
    file "example.local.zone";
    allow-update { none; };
    allow-transfer { none; };
};
CONF
fi

if ! grep -q 'zone "10.168.192.in-addr.arpa"' "${ZONES_CONF}"; then
  cat >> "${ZONES_CONF}" <<'CONF'

// dns_manager 테스트용 역방향 zone
zone "10.168.192.in-addr.arpa" IN {
    type master;
    file "10.168.192.in-addr.arpa.rev";
    allow-update { none; };
    allow-transfer { none; };
};
CONF
fi

# --- 동적 갱신(RFC 2136) 검증용 zone ---
# TSIG 키로 allow-update 를 건 zone. 이런 zone 은 journal 때문에 파일 직접 편집이 위험하므로
# 앱이 nsupdate 경로로 전환하는지 확인하는 데 쓴다.
DDNS_KEY="${CONF_DIR}/ddns.key"
if [[ ! -f "${DDNS_KEY}" ]]; then
  SECRET="$(dd if=/dev/urandom bs=32 count=1 2>/dev/null | base64 -w0)"
  cat > "${DDNS_KEY}" <<CONF
key "ddns-key" {
    algorithm hmac-sha256;
    secret "${SECRET}";
};
CONF
fi
chgrp "${BIND_GROUP}" "${DDNS_KEY}"
chmod 0640 "${DDNS_KEY}"

if ! grep -q 'ddns.key' "${ZONES_CONF}"; then
  cat >> "${ZONES_CONF}" <<CONF

include "${DDNS_KEY}";

// 동적 갱신(RFC 2136) 검증용
zone "dynamic.local" IN {
    type master;
    file "dynamic.local.zone";
    allow-update { key "ddns-key"; };
};
CONF
fi

if [[ ! -f "${ZONE_DIR}/dynamic.local.zone" ]]; then
  cat > "${ZONE_DIR}/dynamic.local.zone" <<'ZONE'
; dynamic.local - RFC 2136 동적 갱신 검증용
$TTL 3600
@       IN  SOA ns1.example.local. admin.example.local. (
                2026100101  ; serial
                3600        ; refresh
                600         ; retry
                604800      ; expire
                3600 )      ; minimum
@           IN  NS      ns1.example.local.
static      IN  A       192.168.10.200
ZONE
fi

# 기존 zones.conf 에 조건부 전달자가 없으면 덧붙인다(멱등).
if ! grep -q 'zone "partner.example"' "${ZONES_CONF}"; then
  cat >> "${ZONES_CONF}" <<'CONF'

// 조건부 전달자 (Windows DNS Manager 의 Conditional Forwarders 에 대응)
zone "partner.example" IN {
    type forward;
    forward only;
    forwarders { 192.168.2.1; 192.168.2.2; };
};
CONF
fi

# --- 정방향 zone ---
if [[ ! -f "${ZONE_DIR}/example.local.zone" ]]; then
  cat > "${ZONE_DIR}/example.local.zone" <<'ZONE'
; example.local - dns_manager 테스트용 정방향 zone
$TTL 3600
@       IN  SOA ns1.example.local. admin.example.local. (
                2026100101  ; serial
                3600        ; refresh
                600         ; retry
                604800      ; expire
                3600 )      ; minimum
@           IN  NS      ns1.example.local.
@           IN  MX  20  mail.example.local.
ns1         IN  A       192.168.10.10
www         IN  A       192.168.10.21
www         IN  AAAA    fd00:dead:beef::21
mail        IN  A       192.168.10.25
mail2       IN  CNAME   mail.example.local.
api     300 IN  A       192.168.10.31
@           IN  TXT     "v=spf1 mx -all"
_sip._tcp   IN  SRV 0 0 5060 sipserver1.example.local.
@           IN  CAA 0 issue "letsencrypt.org"
ZONE
fi

# --- 역방향 zone ---
if [[ ! -f "${ZONE_DIR}/10.168.192.in-addr.arpa.rev" ]]; then
  cat > "${ZONE_DIR}/10.168.192.in-addr.arpa.rev" <<'ZONE'
; 192.168.10.0/24 - dns_manager 테스트용 역방향 zone
$TTL 3600
@       IN  SOA ns1.example.local. admin.example.local. (
                2026100101  ; serial
                3600        ; refresh
                600         ; retry
                604800      ; expire
                3600 )      ; minimum
@       IN  NS  ns1.example.local.
10      IN  PTR ns1.example.local.
21      IN  PTR www.example.local.
25      IN  PTR mail.example.local.
ZONE
fi

# --- 파일명 규칙 마이그레이션: 역방향 zone 은 .rev 를 쓴다 ---
if [[ -f "${ZONE_DIR}/10.168.192.in-addr.arpa.zone" && ! -f "${ZONE_DIR}/10.168.192.in-addr.arpa.rev" ]]; then
  mv "${ZONE_DIR}/10.168.192.in-addr.arpa.zone" "${ZONE_DIR}/10.168.192.in-addr.arpa.rev"
fi
sed -i 's|"10\.168\.192\.in-addr\.arpa\.zone"|"10.168.192.in-addr.arpa.rev"|' "${ZONES_CONF}"

# --- 전역(기본) 전달자 ---
# named.conf 자체는 root 전용으로 두고(서비스는 최소 권한으로 돈다),
# options 블록 안에 include 한 전용 파일에서 관리한다.
FWD_CONF="${CONF_DIR}/options-forwarders.conf"
if [[ ! -f "${FWD_CONF}" ]]; then
  cat > "${FWD_CONF}" <<'CONF'
// dns_manager 가 관리하는 전역 전달자 설정 (options 블록 안에서 include)
forwarders { 168.126.63.1; 8.8.8.8; };
forward first;
CONF
fi
chgrp "${BIND_GROUP}" "${FWD_CONF}"
chmod 0664 "${FWD_CONF}"

if ! grep -q 'options-forwarders.conf' "${NAMED_CONF}"; then
  cp -a "${NAMED_CONF}" "${NAMED_CONF}.bak.$(date -u +%Y%m%d%H%M%S)"
  # 기존에 직접 넣어 둔 forwarders 문이 있으면 제거(include 와 중복 방지)
  sed -i '/^\s*forwarders {/d; /^\s*forward first;/d' "${NAMED_CONF}"
  sed -i "0,/^options {/s||options {\n\tinclude \"${FWD_CONF}\";|" "${NAMED_CONF}"
fi

# --- rndc 키 ---
[[ -f /etc/rndc.key ]] || rndc-confgen -a -b 512

# --- 권한: named 그룹에 쓰기 허용, 관리 사용자를 named 그룹에 추가 ---
chgrp "${BIND_GROUP}" "${ZONES_CONF}" "${ZONE_DIR}"/*.zone "${ZONE_DIR}"/*.rev
chmod 0664 "${ZONES_CONF}" "${ZONE_DIR}"/*.zone "${ZONE_DIR}"/*.rev
chmod 0775 "${ZONE_DIR}"
chown "${BIND_GROUP}" "${ZONE_DIR}/dynamic.local.zone" 2>/dev/null || true
chmod 0770 "${CONF_DIR}"
chmod 0640 /etc/rndc.key && chgrp "${BIND_GROUP}" /etc/rndc.key 2>/dev/null || true
id -nG "${MGR_USER}" | tr ' ' '\n' | grep -qx "${BIND_GROUP}" || usermod -aG "${BIND_GROUP}" "${MGR_USER}"

# --- 검증 후 기동 ---
named-checkconf
named-checkzone example.local "${ZONE_DIR}/example.local.zone"
named-checkzone 10.168.192.in-addr.arpa "${ZONE_DIR}/10.168.192.in-addr.arpa.rev"
named-checkzone dynamic.local "${ZONE_DIR}/dynamic.local.zone"

systemctl enable --now named
systemctl is-active named
rndc status | head -5

echo
echo "완료. ${MGR_USER} 는 새 로그인 세션부터 named 그룹 권한이 적용된다."
