# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""설치본이 실제로 동작하는지 확인한다 (설치 검증에서 컨테이너 안에서 실행).

curl 이 없는 최소 이미지가 있어 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8100"


def get(path: str, raw: bool = False):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        body = response.read().decode()
    return body if raw else json.loads(body)


def main() -> int:
    for _ in range(60):
        try:
            get("/healthz")
            break
        except Exception:  # noqa: BLE001 - 기동 대기
            time.sleep(0.5)
    else:
        print("    앱이 뜨지 않았습니다")
        try:
            print(open("/tmp/app.log").read()[-1000:])
        except OSError:
            pass
        return 1

    print("    healthz:", get("/healthz"))

    status = get("/api/status")
    family = status["detected_family"]
    print(f"    named={status['running']} | 레이아웃={family} | zones={status['zone_count']}")
    assert family in ("redhat", "debian"), f"레이아웃을 감지하지 못했습니다: {family}"
    assert status["options_file"], "options 블록 파일을 찾아야 합니다"

    settings = get("/api/settings")
    print(f"    설정 점검: ready={settings['ready']} 문제={settings['problems']}")
    assert settings["ready"], settings["problems"]

    notice = get("/api/notice", raw=True)
    assert "dnspython" in notice and "ISC" in notice, "제3자 고지가 제공되어야 합니다"
    print(f"    제3자 고지 {notice.count(chr(10))}줄 제공")

    zones = [z["name"] for z in get("/api/zones")]
    print("    zone:", zones)
    assert zones, "테스트 zone 이 보여야 합니다"

    # 쓰기 경로까지 확인한다 — 설치만 되고 적용이 안 되면 의미가 없다.
    target = "example.local"
    if target in zones:
        req = urllib.request.Request(
            f"{BASE}/api/zones/{target}/records",
            method="POST",
            data=json.dumps({"name": "installcheck", "type": "A", "data": "10.77.77.77"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            outcome = json.loads(response.read())
        print(f"    레코드 추가: ok={outcome['ok']} {outcome['result']['summary']}")
        assert outcome["ok"], outcome["result"].get("error") or outcome["result"].get("check_output")

    print("== 설치본 동작 확인 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
