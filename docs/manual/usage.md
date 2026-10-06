# dns_manager 사용 설명서

BIND 9 zone 파일 편집기. 레코드를 표에서 보고 고치고, zone·전달자를 추가합니다.
Windows Server의 DNS Manager 조작 흐름을 따릅니다.

---

## 1. 설치

대상 호스트에 설치 파일(`.run`) 하나만 옮기면 됩니다. 파이썬이 없어도 됩니다 — 설치 파일이
CPython 런타임을 함께 담고 있습니다.

```bash
# 무결성 확인 (선택)
./dns-manager-<버전>-portable-x86_64.run --check

# 설치 — 경로를 물어봅니다 (그냥 Enter 면 /opt/dns-manager)
./dns-manager-<버전>-portable-x86_64.run

# 경로를 지정해 설치
./dns-manager-<버전>-portable-x86_64.run --dir /srv/dns-manager

# 묻지 않고 기본 경로로
./dns-manager-<버전>-portable-x86_64.run -y
```

설치와 실행은 root 권한으로 합니다. 일반 계정으로 실행하면 sudo로 자동 전환합니다.
BIND가 쓰는 계정·그룹이 서버마다 달라(named/bind/기타) 권한을 맞추려 들면 서버마다 다른
문제가 생기기 때문입니다.

### 설치되는 것

```
/opt/dns-manager/
├── dns_manager        실행 제어 스크립트 (유일한 실행 파일)
├── config.toml        설정 — 설치 시 감지한 BIND 경로가 기록됨
├── vendor/python/     동봉된 파이썬
├── .venv/             파이썬 환경
├── var/               변경 이력 DB · 백업
├── run/  log/         PID · 로그
└── src/ deploy/ tests/
```

지울 때는 `rm -rf /opt/dns-manager` 하나면 됩니다. 재설치해도 `config.toml`은 보존됩니다.

### 설치 파일 다시 만들기

네트워크가 되는 빌드 서버에서:

```bash
./deploy/make-installer.sh                        # 현재 호스트 기준
./deploy/make-installer.sh --in-docker debian:13  # Debian 대상용
./deploy/make-installer.sh --no-python            # 시스템 파이썬을 쓰는 가벼운 변형
```

---

## 2. 실행

```bash
/opt/dns-manager/dns_manager start --host 0.0.0.0 --port 8100
/opt/dns-manager/dns_manager status
/opt/dns-manager/dns_manager restart     # 인자 없으면 직전 host/port 사용
/opt/dns-manager/dns_manager stop
/opt/dns-manager/dns_manager log -f
/opt/dns-manager/dns_manager run         # 전면 실행 (Ctrl+C 로 종료)
/opt/dns-manager/dns_manager doctor      # 진단
```

host/port를 주지 않으면 `config.toml`의 값을 씁니다(기본 `0.0.0.0:8100`).
상시 구동을 전제하지 않습니다 — 필요할 때 띄우고 끝나면 내리면 됩니다.

브라우저로 `http://<서버>:8100` 에 접속합니다. 앱 자체에는 인증이 없습니다. 폐쇄망 내부
사용을 전제로 하며, 외부에 노출해야 한다면 방화벽이나 앞단 역방향 프록시로 통제하세요.

---

## 3. 화면

왼쪽은 콘솔 트리, 오른쪽은 선택한 대상의 상세입니다.

```
DNS
└── BIND (/etc/named.conf)
    ├── Forward Lookup Zones
    ├── Reverse Lookup Zones
    └── Conditional Forwarders
```

### 서버 노드

| 탭 | 내용 |
|---|---|
| **Zones** | zone 목록. 추가·삭제 |
| **Forwarders** | 기본(전역) 전달자와 도메인별(조건부) 전달자 |
| **Server** | 수신·질의 설정, 전달자, 검증 결과, 설정 전문 |
| **Files** | 이 앱이 읽고 쓰는 파일 목록과 실제 접근 기록 |
| **Settings** | BIND 경로 확인·입력 |
| **History** | 변경 이력 · diff · 되돌리기 |

### zone 노드

| 탭 | 내용 |
|---|---|
| **Records** | 레코드 표. 추가·수정·삭제 |
| **Raw** | zone 파일 직접 편집 |
| **Properties** | General · SOA · Name Servers · Zone Transfers |
| **History** | 이 zone의 변경 이력 |

`View ▸ Advanced` 를 켜면 SOA·TTL·DNSSEC 레코드가 함께 보입니다(평소에는 접어 둡니다).

---

## 4. 레코드 다루기

### 추가

`New Record` 를 누르면 **표 맨 아래에 입력 행**이 생깁니다. 이름·타입·데이터·TTL을 넣고
Enter 또는 `추가` 를 누릅니다. Esc로 취소합니다.

타입이 A·AAAA면 `PTR` 체크박스가 나타납니다. 켜 두면 해당 IP를 담는 역방향 zone에
PTR 레코드를 함께 만듭니다(역방향 zone이 있을 때만).

| 타입 | 데이터 예 |
|---|---|
| A / AAAA | `192.168.10.50` / `fd00::1` |
| CNAME | `target.example.local.` |
| MX | `10 mail.example.local.` |
| SRV | `0 0 5060 sip.example.local.` |
| TXT | `"v=spf1 mx -all"` |
| CAA | `0 issue "letsencrypt.org"` |

이름에 `*` 를 넣으면 와일드카드 레코드가 됩니다.

### 수정

- **셀을 더블클릭**하면 그 자리에서 고칩니다(이름·TTL·데이터). Enter로 적용, Esc로 취소.
- 행을 고르고 `Properties…` 를 누르면 타입까지 포함해 고칩니다.

### 삭제

행을 고르고 `Delete`. Ctrl+클릭으로 여러 건을 고르면 한 번에 지웁니다.
A·AAAA 레코드를 지울 때는 짝이 되는 PTR도 함께 지울지 물어봅니다.

### 적용 방식

**각 조작은 즉시 적용됩니다.** 변경분을 모아 두는 단계가 없습니다. 조작 하나하나가
다음 순서를 모두 거치기 때문입니다:

```
잠금 → 외부 변경 검사 → SOA serial 증가 → 임시 파일 기록 → named-checkzone 검증
     → 원자적 교체 → rndc reload → 적재 확인 → 이력 기록
실패하면 → 백업으로 복구 → reload → BIND의 오류 메시지를 그대로 표시
```

검증에 실패하면 **원본은 한 글자도 바뀌지 않습니다.** 되돌리기는 History 탭에서 합니다.

---

## 5. zone 다루기

### 추가

서버 노드 ▸ Zones ▸ `New Zone` 을 누르면 입력 행이 생깁니다.

| 항목 | 설명 |
|---|---|
| 이름 | 정방향이면 `example.local`. 역방향이면 Network ID를 **정방향 순서**로 (`192.168.10` 또는 `192.168.10.0/24`) |
| 종류 | master / slave / stub / forward |
| 역방향 | 체크하면 이름 칸이 Network ID가 됩니다 |
| 추가 정보 | slave·stub은 master 서버 IP, forward는 전달자 IP, master는 기본 네임서버(선택) |

`미리보기` 를 누르면 **실제로 기록될 named.conf 블록과 zone 파일 전문**을 보여주고,
`named-checkzone` 사전 검증 결과까지 함께 표시합니다. 확인한 뒤 `만들기` 를 누릅니다.

파일명은 정방향 `.zone`, 역방향 `.rev` 규칙을 따릅니다.

### 삭제

zone 행을 고르고 `Delete Zone`. zone 파일은 기본적으로 남깁니다(실수 복구를 쉽게 하려고).
체크하면 백업 디렉터리로 옮깁니다.

### 위임 (New Delegation)

zone ▸ Properties ▸ `New Delegation…` 에서 하위 도메인을 다른 네임서버로 위임합니다.
네임서버 이름이 위임 구간 **안쪽**이면 glue 주소가 필요합니다(없으면 해석이 끊깁니다).
위임 구간 **밖** 이름의 주소는 상위 zone에 넣을 수 없으므로 거부합니다.

---

## 6. 전달자

Conditional Forwarders 노드에서 두 가지를 모두 다룹니다.

| 구분 | 저장 위치 |
|---|---|
| **기본(전역) 전달자** | `options { forwarders ... }` |
| **조건부 전달자** | `type forward` zone |

기본 전달자는 설정되지 않았어도 행이 표시됩니다("설정이 없음"과 "화면에 없음"을 구분하려고).

> `named.conf` 자체에 쓰기 권한이 없는 환경이라면, `options` 블록 안에 쓰기 가능한 파일을
> include해 두면 앱이 그 파일을 씁니다:
> ```
> options {
>     include "/etc/named/options-forwarders.conf";
>     ...
> };
> ```

---

## 7. 동적 갱신(RFC 2136) zone

`allow-update` 가 걸린 zone은 named가 변경을 journal(`.jnl`)에 쌓습니다. 이런 zone의
파일을 직접 고치면 journal과 어긋나므로, 앱이 **자동으로 프로토콜 경로(RFC 2136)** 를 씁니다.
TSIG 키는 `named.conf`가 include하는 파일들에서 찾습니다.

- 레코드 추가·수정·삭제 → 동적 갱신으로 적용
- Raw 저장 → `rndc freeze` → 저장 → `rndc thaw`
- 조회 시 적재 serial이 파일보다 앞서 있으면 `rndc sync`로 먼저 내려 씁니다
  (그러지 않으면 동적으로 넣은 레코드가 화면에 안 보입니다)

---

## 8. 문제 해결

### 진단부터

```bash
/opt/dns-manager/dns_manager doctor
```

한 장에 다음이 나옵니다: 앱이 실제로 읽는 설정 파일, 레이아웃 감지 결과, include 목록,
`named-checkconf` 출력, **zone별 정의 위치와 적재 상태**, 도구 존재 여부, 점검 요약.

### 자주 겪는 것

**"zone 파일에는 있는데 질의하면 응답이 없다"**

응답 코드로 원인이 갈립니다.

| 응답 | 의미 | 해결 |
|---|---|---|
| SERVFAIL | zone이 적재되지 않음 (파일 권한·문법 오류 등) | named 로그 확인 |
| NXDOMAIN | zone은 적재됨. 서버가 들고 있는 내용에 그 이름이 없음 | `rndc reload <zone>` |
| NOERROR·IP 없음 | 이름은 있으나 A 레코드가 없음 | 레코드 확인 |

파일을 고치면서 serial을 올리지 않으면 serial 비교로는 아무 문제가 없어 보입니다.
앱은 **파일 수정 시각과 적재 시각**을 함께 비교해 "파일 변경됨 — reload 필요" 로 알립니다.

**"외부에서 질의가 안 된다"**

zone을 어느 파일에 정의했는지와는 **무관합니다**. `options` 의 두 설정이 좌우합니다.

| 설정 | 뜻 |
|---|---|
| `listen-on` | 어느 주소로 질의를 받을지 |
| `allow-query` | 누구에게 답할지 |

RedHat 계열 기본값은 `listen-on { 127.0.0.1; }` + `allow-query { localhost; }` 이라
외부에서 전혀 쓸 수 없습니다. Debian 계열 기본값은 제한이 없습니다.
서버 노드 ▸ Server 탭에서 확인하고 고칠 수 있습니다. 외부 질의를 막는 설정이 있으면
붉은 글씨로 짚어 줍니다.

**"설정이 안 먹는다 / 권한 문제 같다"**

Files 탭을 보세요. 이 앱이 다루는 파일 전부와 각각의 존재·읽기·쓰기 권한, 실제 접근
기록이 나옵니다. 쓰기 권한은 **디렉터리까지** 확인합니다(원자적 교체에 필요합니다).

**"경로가 예전 것을 가리킨다"**

서버를 옮기거나 BIND를 재설치하면 설정의 경로가 실제와 어긋날 수 있습니다. 없는 경로는
설정이 아니라 고장으로 보고, 앱이 레이아웃을 다시 감지해 살아 있는 경로로 되돌리며
상단 배너로 알립니다. 자동으로 고칠 수 없으면 Settings 탭에서 직접 입력하세요.

### 되돌리기

History 탭에서 변경 이력과 diff를 보고 특정 시점으로 되돌립니다. 되돌리기도 같은 검증을
거쳐 적용되고 새로운 이력으로 남습니다(serial은 계속 증가합니다).

---

## 9. 다루지 않는 것

- **DNSSEC 서명 zone**: 읽기 전용으로 표시하고 편집을 막습니다
- `$INCLUDE` · `$GENERATE` 가 있는 zone: 표 편집을 막고 Raw 편집만 허용합니다
  (재직렬화하면 원본 구조가 깨지기 때문)
- 여러 BIND 서버 동시 관리: 한 서버만 다룹니다

---

## 10. 전제 조건

- BIND 9 와 `named-checkzone` · `named-checkconf` · `rndc` (`bind`, `bind-utils` 또는
  `bind9`, `bind9-utils` 패키지)
- `named.conf` 에 zone 정의용 include
  - RedHat 계열: `include "/etc/named/zones.conf";` 를 추가하고 그 파일을 만듭니다
  - Debian 계열: 배포판이 제공하는 `named.conf.local` 을 그대로 씁니다
