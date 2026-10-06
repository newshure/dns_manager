# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""실행 진입점.

기본 수신 주소는 0.0.0.0 이다. 앱 자체에 인증이 없으므로 접근 통제는
폐쇄망 + 방화벽, 또는 앞단 역방향 프록시(인증·TLS)가 담당한다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .api.app import create_app
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dns-manager", description="BIND 9 zone editor")
    parser.add_argument("-c", "--config", type=Path, default=None, help="설정 TOML 경로")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="개발용 자동 재시작")
    parser.add_argument(
        "--shutdown-after",
        default=None,
        metavar="시간",
        help="요청이 없을 때 스스로 종료 (예: 30m, 2h, 90s). 인증이 없으므로 작업 후에는 내리는 것이 맞다",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="진단 보고서를 출력하고 끝낸다 (어떤 설정 파일을 읽는지, zone 이 어디에 정의됐는지)",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    if args.doctor:
        from .doctor import report

        print(report(cfg))
        return 0

    host = args.host or cfg.app.host
    port = args.port or cfg.app.port

    if args.shutdown_after is not None:
        from dataclasses import replace

        from .core.idle import parse_duration

        try:
            seconds = parse_duration(args.shutdown_after)
        except ValueError as exc:
            parser.error(str(exc))
        cfg = replace(cfg, app=replace(cfg.app, shutdown_after_idle=seconds))

    if cfg.app.shutdown_after_idle > 0:
        print(
            f"유휴 {cfg.app.shutdown_after_idle // 60}분이 지나면 스스로 종료합니다.",
            flush=True,
        )

    if args.reload:
        uvicorn.run("dns_manager.api.app:create_app", factory=True, host=host, port=port, reload=True)
    else:
        uvicorn.run(create_app(cfg), host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
