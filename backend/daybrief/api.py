import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .adapters import MockModel, MockSearch, Nemotron, ProviderError, Tavily
from .config import Settings
from .models import Brief, GenerateRequest
from .pipeline import run_pipeline
from .storage import Storage
from .video import Jobs
from .video_api import video_router
from .visual_api import visual_router


def create_app(settings=None):
    s = settings or Settings()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daybrief-video")

    @asynccontextmanager
    async def lifespan(app):
        Jobs(s.database_url).recover()
        yield
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="DayBrief", lifespan=lifespan)
    app.include_router(video_router(s, pool))
    app.include_router(visual_router(s))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins.split(","),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError):
        return JSONResponse(
            status_code=502,
            content={"error": {"code": "provider_error", "message": str(exc)}},
        )

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error):
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "storage_error",
                    "message": "Brief storage is unavailable",
                }
            },
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": s.daybrief_mode}

    @app.get("/api/topics")
    def topics():
        return [
            {"id": i, "label": label}
            for i, label in [
                ("general", "General"),
                ("ai_technology", "AI & Technology"),
                ("science", "Science"),
                ("markets", "Macro & Policy"),
            ]
        ]

    @app.post("/api/briefs/generate", response_model=Brief)
    async def generate(body: GenerateRequest):
        try:
            s.validate_real()
        except ValueError as exc:
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "configuration_error", "message": str(exc)}},
            )
        search, model = (
            (MockSearch(), MockModel())
            if s.daybrief_mode == "mock"
            else (Tavily(s), Nemotron(s))
        )
        brief = await run_pipeline(
            body.topic,
            body.lookback_hours or s.news_lookback_hours,
            datetime.now(UTC),
            search,
            model,
            s.daybrief_mode,
            enrichment_max_candidates=s.enrichment_max_candidates,
            enrichment_queries_per_candidate=s.enrichment_queries_per_candidate,
            fallback_max_candidates=s.fallback_max_candidates,
            fallback_max_articles=s.fallback_max_articles,
        )
        Storage(s.database_url).save(brief)
        return brief

    @app.get("/api/briefs/{brief_id}", response_model=Brief)
    def retrieve(brief_id: str):
        brief = Storage(s.database_url).get(brief_id)
        return (
            brief
            if brief
            else JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": "Brief not found"}},
            )
        )

    return app


app = create_app()
