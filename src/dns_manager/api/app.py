# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""FastAPI 앱 구성."""

from __future__ import annotations

from pathlib import Path

import asyncio
import contextlib

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Config, load_config
from ..core.idle import IdleWatch, watch
from .routes import router
from .write_routes import router as write_router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    idle = IdleWatch(timeout=float(cfg.app.shutdown_after_idle))

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if idle.timeout > 0:
            task = asyncio.create_task(watch(idle))
        try:
            yield
        finally:
            idle.stopping = True
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        title="dns_manager",
        version=__version__,
        description="BIND 9 zone 편집기 — Windows Server DNS Manager 흐름 기반 콘솔",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.idle = idle

    # 살아 있는지 묻는 것은 '작업' 이 아니다. 이것까지 활동으로 세면
    # 상태 확인 루프나 모니터링이 자동 종료를 영원히 막는다(실제로 겪었다).
    IDLE_EXEMPT_PATHS = frozenset({"/healthz"})

    @app.middleware("http")
    async def mark_activity(request: Request, call_next):
        """요청이 오면 유휴 시계를 되돌린다. 작업 중에 종료되면 안 된다."""
        if request.url.path not in IDLE_EXEMPT_PATHS:
            idle.touch()
        return await call_next(request)
    app.include_router(router)
    app.include_router(write_router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

    @app.get("/", response_class=HTMLResponse)
    def console(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="console.html",
            context={
                "version": __version__,
                "advanced_default": cfg.app.advanced_view_default,
            },
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
