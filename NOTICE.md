# NOTICE

dns_manager
Copyright (c) 2026 haedong (theknowledges.net)
Licensed under the MIT License — see [LICENSE](LICENSE).

이 제품은 아래의 제3자 오픈소스를 포함하거나 함께 사용합니다. **각 라이선스는 저작권 고지의
보존을 의무로 요구하므로, 이 파일은 배포본에 반드시 동봉되어야 합니다.**
각 구성요소는 아래 우선순위에 따라 고른 것입니다: 무제약(CC0/Unlicense/0BSD) → MIT/BSD/Apache-2.0/ISC →
약한 copyleft(사전 승인) 순이며, 상업적 사용이 제한되는 라이선스는 쓰지 않습니다.

---

## 1. 실행 시 포함되는 라이브러리 (Python 패키지)

| 구성요소 | 버전 | 라이선스 | 저작권 고지 |
|---|---|---|---|
| dnspython | 2.8.0 | ISC | Copyright (C) Dnspython Contributors |
| FastAPI | 0.142.2 | MIT | Copyright (c) 2018 Sebastián Ramírez |
| Starlette | 1.7.0 | BSD-3-Clause | Copyright © 2018, Encode OSS Ltd |
| Uvicorn | 0.54.0 | BSD-3-Clause | Copyright © 2017-present, Encode OSS Ltd |
| Jinja2 | 3.1.6 | BSD-3-Clause | Copyright 2007 Pallets |
| MarkupSafe | 3.0.3 | BSD-3-Clause | Copyright 2010 Pallets |
| Pydantic | 2.13.5 | MIT | Copyright (c) 2017 to present Pydantic Services Inc. and individual contributors |
| pydantic-core | 2.46.5 | MIT | 동상 |
| annotated-types | 0.8.0 | MIT | Copyright (c) 2022 the contributors |
| anyio | 4.15.1 | MIT | Copyright (c) 2018 Alex Grönholm |
| click | 8.5.0 | BSD-3-Clause | Copyright 2014 Pallets |
| h11 | 0.16.0 | MIT | Copyright (c) 2016 Nathaniel J. Smith and other contributors |
| idna | 3.20 | BSD-3-Clause | Copyright (c) 2013-2024, Kim Davies and contributors |
| python-multipart | 0.0.32 | Apache-2.0 | Copyright (c) 2012-2013, Andrew Dunham |
| python-dotenv | 1.2.3 | BSD-3-Clause | Copyright (c) 2014, Saurabh Kumar |
| PyYAML | 6.0.3 | MIT | Copyright (c) 2017-2021 Ingy döt Net; Copyright (c) 2006-2016 Kirill Simonov |
| httptools | 0.8.0 | MIT | Copyright (c) 2015 MagicStack Inc. |
| uvloop | 0.22.1 | MIT | Copyright (c) 2015-present MagicStack Inc. |
| watchfiles | 1.3.0 | MIT | Copyright (c) 2017, 2018, 2019, 2020, 2021 Samuel Colvin |
| websockets | 17.1 | BSD-3-Clause | Copyright (c) Aymeric Augustin and contributors |
| typing-extensions | 4.16.0 | PSF-2.0 | Copyright (c) Python Software Foundation |
| typing-inspection | 0.4.4 | MIT | Copyright (c) 2025 Pydantic Services Inc. |
| annotated-doc | 0.0.5 | MIT | Copyright (c) 2025 Sebastián Ramírez |
| opentelemetry-api | 1.45.0 | Apache-2.0 | Copyright The OpenTelemetry Authors |

> 각 라이선스 전문은 설치된 패키지의 배포 메타데이터(`.venv/lib/python3.*/site-packages/<패키지>-*.dist-info/`)에 포함되어 있으며, 배포 시 해당 메타데이터를 함께 전달합니다.

## 1-2. 설치본에 함께 담기는 런타임

| 구성요소 | 버전 | 라이선스 | 저작권 고지 |
|---|---|---|---|
| CPython (python-build-standalone 빌드) | 3.12.x | PSF-2.0 | Copyright © 2001-2026 Python Software Foundation. All rights reserved. |
| python-build-standalone (빌드 도구·배포본) | — | MPL-2.0 / BSD-3-Clause / PSF-2.0 | Copyright (c) 2018 Gregory Szorc; Copyright (c) Astral Software Inc. |

> `.run` 설치 파일에는 **대상 호스트의 파이썬에 기대지 않기 위해** CPython 런타임이 함께 담깁니다
> (`vendor/python/`). 배포판마다 있는 파이썬 버전이 다르고, 폐쇄망에서는 "파이썬을 설치하세요" 라는
> 안내가 성립하지 않기 때문입니다. CPython 과 그 번들 구성요소의 라이선스 전문은
> 설치본의 `vendor/python/` 아래(`LICENSE`, `lib/python3.12/LICENSE.txt` 등)에 포함되어 있습니다.
> 파이썬을 담지 않은 설치본은 `deploy/make-installer.sh --no-python` 으로 만들 수 있습니다.

## 2. 개발·시험 전용 (배포본에 포함되지 않음)

| 구성요소 | 라이선스 | 용도 |
|---|---|---|
| pytest, pluggy, iniconfig | MIT | 단위·통합 시험 |
| httpx, httpcore | BSD-3-Clause | 시험용 HTTP 클라이언트 |
| Playwright | Apache-2.0 | 브라우저 UI 점검 |
| pyee | MIT | Playwright 의존성 |
| Pygments | BSD-2-Clause | 시험 출력 서식 |
| certifi | MPL-2.0 | 시험용 TLS 신뢰 저장소 (수정 없이 사용) |

## 3. 외부 실행 파일로 호출하는 소프트웨어 (링크 아님, 포함 아님)

| 구성요소 | 라이선스 | 호출 방식 |
|---|---|---|
| BIND 9 (`named`, `named-checkzone`, `named-checkconf`, `rndc`, `nsupdate`, `dig`) | MPL-2.0 | 별도 프로세스 실행(subprocess). 코드를 링크하거나 포함하지 않으므로 copyleft 전파 대상이 아닙니다. OS 패키지(`bind`, `bind-utils`)로 설치된 것을 사용합니다. |

## 4. 설계 참조 (코드 미포함)

| 대상 | 라이선스 | 참조 범위 |
|---|---|---|
| Windows Server DNS Manager 공식 문서 (Microsoft Learn) | Microsoft 문서 | UI 조작 흐름·용어만 참조. Microsoft 의 코드·리소스·상표는 사용하지 않습니다. 제품명은 식별 목적의 명목적 사용입니다. |
| PowerDNS-Admin | MIT | 레코드 테이블 구성과 입력 검증 규칙 참고 |
| Webmin (BIND DNS Server 모듈) | BSD-3-Clause | named.conf 처리 순서 참고 |

---

© 2026 haedong · 출처: theknowledges.net · MIT License
