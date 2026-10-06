# dns_manager

BIND 9 zone 파일 편집기 겸 configurator. **Windows Server DNS Manager(MMC) 의 조작 흐름**을 웹으로 옮겨, 레코드를 테이블에서 보고 편집하고 zone·전달자를 추가한다.

- 대상: BIND 9 호스트 (vanilla, systemd). **RedHat 계열(Rocky/RHEL/CentOS)과 Debian 계열(Debian/Ubuntu)의 설정 배치를 자동 감지**하며, 다른 배치는 Settings 에서 직접 입력한다
- 제어 경로: zone 파일 직접 편집 + `rndc`. **동적 갱신(`allow-update`)이 걸린 zone 은 RFC 2136 으로 자동 전환** — 이런 zone 의 파일을 직접 고치면 journal 과 어긋난다
- 사용 설명서: [docs/manual/usage.md](docs/manual/usage.md)

## 현재 상태 — 계획한 범위 전부 완료 (P0~P4)

| 기능 | 상태 |
|---|---|
| named.conf 탐색, zone 목록, 콘솔 트리 | 완료 |
| 레코드 테이블 (Name/Type/Data/TTL/Status), 타입 필터·검색·정렬 | 완료 |
| View ▸ Advanced (SOA·DNSSEC 노출) | 완료 |
| Raw zone 파일 보기 | 완료 (편집은 P1) |
| zone Properties (General/SOA/Name Servers/Zone Transfers) | 조회 완료 |
| 전달자 조회 — 전역(`options forwarders`) + 조건부(`type forward` zone) | 완료 |
| 파일 serial ↔ 적재 serial 비교(reload 필요 감지) | 완료 |
| dig 질의 (Launch nslookup) | 완료 |
| 변경 트랜잭션 엔진 (검증→원자적 교체→reload→자동 롤백) | 완료 |
| 변경 이력·diff·시점 롤백 (SQLite) | 완료 |
| SOA serial 자동 증가 (YYYYMMDDnn, 단조 증가 보장) | 완료 |
| zone 단위 쓰기 락, 외부 편집 충돌 감지 | 완료 |
| 레코드 CRUD — 인라인 편집 · Properties 대화상자 · New Record 메뉴 | 완료 |
| Create associated pointer (PTR) record / 삭제 시 PTR 동반 삭제 | 완료 |
| raw zone 편집기 저장 (검증·충돌 감지) | 완료 |
| History 탭 — 변경 이력 · diff · 시점 되돌리기 | 완료 |
| zone 추가 — 입력 행 + 생성될 named.conf 블록/zone 파일 전문 미리보기 | 완료 |
| zone 삭제 (파일 보존 또는 백업 이동) | 완료 |
| 전달자 추가·편집·삭제 — 전역(`options forwarders`) + 조건부(`type forward`) | 완료 |
| **배포판 레이아웃 자동 감지** — RedHat 계열 / Debian 계열 | 완료 |
| Settings — 경로 직접 입력·검증 (감지가 맞지 않는 배치) | 완료 |
| **Files — 이 앱이 읽고 쓰는 파일 목록과 실제 접근 기록** | 완료 |
| 수신·질의 설정 보기·편집 (`listen-on` / `allow-query` / `recursion`) | 완료 |
| "파일이 바뀌었는데 적용 안 됨" 감지 (serial 이 같아도 잡는다) | 완료 |
| zone Properties 편집 — General · SOA · Name Servers · Zone Transfers | 완료 |
| New Delegation — 하위 도메인 위임 (NS + glue 검사) | 완료 |
| **RFC 2136 동적 갱신** — `allow-update` zone 은 자동으로 프로토콜 경로 사용 | 완료 |
| 동적 zone: 조회 시 journal 반영, raw 저장은 freeze → 저장 → thaw | 완료 |

각 편집은 **즉시 적용**된다. 서버에서 조작 하나가 이미 완결된 트랜잭션이기 때문이다. 되돌리기는 History 탭에서 한다.

모든 쓰기는 단일 경로를 지난다:

```
lock → 외부 변경 검사 → serial 증가 → 임시파일 → named-checkzone
     → 원자적 교체 → rndc reload → 적재 확인 → 이력 기록
실패 → 백업 복구 → reload → BIND 의 원문 오류를 그대로 반환
```

## 설치

### 단일 파일 인스톨러 (.run)

의존성까지 한 파일에 담은 자체 추출 설치 파일. 대상 호스트에 **그 파일 하나만** 옮깁니다.
**디렉터리 하나에 모두 풀고, 그 안의 실행 스크립트로 씁니다** — systemd 등록도, 시스템 계정 생성도 하지 않습니다.

```bash
# 네트워크가 되는 빌드 서버에서
./deploy/make-installer.sh                        # dist/dns-manager-<버전>-py312-x86_64.run
./deploy/make-installer.sh --in-docker debian:13  # Debian 대상(py313)용을 컨테이너에서 빌드

# 대상 호스트에서 (외부 네트워크 불필요)
./dns-manager-0.1.0-py312-x86_64.run --check        # 무결성 확인
./dns-manager-0.1.0-py312-x86_64.run --list         # 담긴 파일 보기
./dns-manager-0.1.0-py312-x86_64.run                # 설치 경로를 물어본 뒤 설치
./dns-manager-0.1.0-py312-x86_64.run -y             # 묻지 않고 /opt/dns-manager 에 설치
./dns-manager-0.1.0-py312-x86_64.run --dir /srv/dm  # 지정한 경로에 설치
```

- **대상 호스트에 파이썬이 없어도 됩니다.** 설치 파일이 CPython 런타임을 함께 담습니다(`portable` 표기). 배포판마다 있는 파이썬 버전이 다르고, 폐쇄망에서는 "파이썬을 설치하세요" 가 성립하지 않기 때문입니다. 파이썬이 아예 없는 호스트에서 설치·기동까지 검증합니다.
- 설치와 실행 모두 **root** 로 합니다. 일반 계정으로 실행하면 sudo 로 자동 전환합니다. BIND 가 쓰는 계정·그룹이 서버마다 달라(named/bind/기타) 권한을 맞추려 들면 서버마다 다른 문제가 생기기 때문입니다.
- 시스템 파이썬을 쓰는 가벼운 설치 파일이 필요하면 `--no-python` 으로 만듭니다(그때는 파일명에 `py312` 처럼 ABI 태그가 붙고, 대상 파이썬이 다르면 설치 전에 멈춥니다).
- `--dir` 를 주지 않으면 설치 중에 경로를 물어봅니다(그냥 Enter 면 기본값). 파이프·스크립트처럼 사람이 없는 환경에서는 묻지 않고 기본값을 씁니다.
- 같은 호스트에 여러 벌을 서로 다른 경로·포트로 둘 수 있습니다.
- `--extract DIR` 로 설치 없이 내용만 꺼낼 수 있습니다.

### 실행

```bash
/opt/dns-manager/dns_manager start --host 0.0.0.0 --port 8100
/opt/dns-manager/dns_manager status
/opt/dns-manager/dns_manager restart          # 인자 없으면 직전 값 사용
/opt/dns-manager/dns_manager stop
/opt/dns-manager/dns_manager log -f
/opt/dns-manager/dns_manager run              # 전면 실행 (Ctrl+C 로 종료)
/opt/dns-manager/dns_manager doctor           # 진단 — 설정·zone 출처·권한을 한눈에
```

host/port 를 주지 않으면 설정 파일의 값을 씁니다(기본 `0.0.0.0:8100`).

### 설치 디렉터리 구조

```
/opt/dns-manager/
├── dns_manager        실행 제어 스크립트 (여기 있는 유일한 실행 파일)
├── config.toml        설정 — 설치 시 감지한 BIND 경로가 기록된다
├── .venv/             파이썬 환경
├── var/               이력 DB·백업
├── run/ log/          PID·로그
└── src/ deploy/ docs/ tests/
```

지울 때는 `rm -rf /opt/dns-manager` 하나면 됩니다. 재설치해도 `config.toml` 은 보존됩니다.

### 설정 경로가 사라진 경우

서버를 옮기거나 BIND 를 재설치하면 `config.toml` 의 경로가 실제와 어긋날 수 있습니다.
**없는 경로는 설정이 아니라 고장으로 봅니다**: 앱이 레이아웃을 다시 감지해 살아 있는 경로로
되돌리고, 그 사실을 상단 배너와 Settings·Server 탭에 남깁니다. 감지로도 고칠 수 없으면
무엇이 없는지 분명히 알립니다(조용히 빈 화면을 보여주지 않습니다).

### 파일 소유권

root 로 zone 파일을 고치지만 **named 가 읽지 못하게 되는 일은 없습니다**:
- 기존 파일을 고칠 때는 원본의 소유자·권한을 그대로 승계합니다(`root:named 0664` → 그대로).
- 새로 만드는 zone 파일은 zone 디렉터리의 그룹과 이웃 zone 파일의 권한을 따릅니다. 그래야 나중에 그 zone 에 동적 갱신을 걸어도 named 가 journal 을 쓸 수 있습니다.

폐쇄망 컨테이너 검증에서 소유권 보존과 동적 갱신 동작을 매번 확인합니다.

### systemd 로 돌리고 싶다면

`deploy/systemd/dns-manager.service` 가 들어 있습니다(설치 스크립트는 등록하지 않습니다). 경로만 맞춰 복사해 쓰세요.

## 운영 방식

필요할 때만 띄우는 **온디맨드 도구**다. 수신은 `0.0.0.0:8100`, 폐쇄망 내부 사용 전제로 앱 자체 인증은 두지 않는다.

## 개발

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests/unit -q          # 단위 테스트 (BIND 불필요)
.venv/bin/python -m dns_manager --port 8100       # 실행
```

테스트용 BIND 환경(Rocky 9):

```bash
sudo ./deploy/testenv/setup-test-bind.sh          # 샘플 zone + 조건부 전달자 구성, named 기동
BASE=http://127.0.0.1:8100 ./tests/smoke/smoke.sh # 스모크 테스트
DNS_MANAGER_LIVE=1 .venv/bin/python -m pytest tests/integration -q   # 실제 zone 을 변경한다
BASE=http://127.0.0.1:8100 .venv/bin/python tests/smoke/ui_check.py --shots temp/shots
```

## 라이선스·재사용

zone 파싱은 dnspython(ISC), 검증·적용은 BIND 자신(`named-checkzone`/`named-checkconf`/`rndc`)에 위임한다. 동봉·사용하는 제3자 구성요소와 그 라이선스는 [NOTICE.md](NOTICE.md)에 모두 적혀 있다 — MIT/BSD/ISC 는 저작권 고지 보존을 의무로 요구하므로 배포본에 함께 둔다.

---

© 2026 haedong · 출처: theknowledges.net · [MIT License](LICENSE) · 제3자 고지: [NOTICE.md](NOTICE.md)
