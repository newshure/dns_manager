# SPDX-License-Identifier: MIT
# Copyright (c) 2026 haedong (theknowledges.net)
"""FastAPI 앱 구성."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Config, load_config
from .routes import router
from .write_routes import router as write_router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(
        title="dns_manager",
        version=__version__,
        description="BIND 9 zone 편집기 — Windows Server DNS Manager 흐름 기반 콘솔",
    )
    app.state.config = cfg
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
