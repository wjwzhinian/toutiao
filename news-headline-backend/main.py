from contextlib import asynccontextmanager
import asyncio
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config.cache_conf import redis_client
from config.db_confing import AsyncSessionLocal
from config.settings import get_settings, validate_settings
from db.migrations import ensure_all_schema
from routers import admin, agent, auth, favorite, history, news, users
from services.auto_pipeline import auto_pipeline_scheduler
from utils.exception_handlers import register_exception_handlers
from utils.metrics import metrics


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings(settings)
    if settings.app_env in {"local", "development"}:
        await ensure_all_schema()
    scheduler_task = None
    if settings.auto_pipeline_enabled:
        scheduler_task = asyncio.create_task(
            auto_pipeline_scheduler(), name="news-auto-pipeline"
        )
    try:
        yield
    finally:
        if scheduler_task:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task


app = FastAPI(
    title="News Headline Agent API",
    version="1.2.0",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
register_exception_handlers(app)


@app.middleware("http")
async def metrics_middleware(request, call_next):
    """记录请求总量和状态码，标签保持低基数以便 Prometheus 长期运行。"""
    response = await call_next(request)
    # 使用 FastAPI 路由模板而非原始 URL，避免把新闻 ID 等写成高基数标签。
    route = request.scope.get("route")
    normalized = getattr(route, "path", request.url.path)
    metrics.inc(
        "news_http_requests_total",
        method=request.method,
        path=normalized,
        status=str(response.status_code),
    )
    return response


@app.get("/")
async def root():
    return {"service": "news-headline-backend", "status": "ok"}


@app.get("/health/live", tags=["health"])
async def health_live():
    return {"status": "ok", "service": "news-headline-backend"}


@app.get("/health/ready", tags=["health"])
async def health_ready():
    checks: dict[str, str] = {}
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["mysql"] = "ok"
    except Exception:
        checks["mysql"] = "error"
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"

    settings = get_settings()
    if settings.qdrant_enabled:
        try:
            from services.vector_store import QdrantVectorStore

            await QdrantVectorStore(settings).ping()
            checks["qdrant"] = "ok"
        except Exception:
            # 语义检索降级时 MySQL 证据检索仍可用，因此不让可选服务阻断 API 健康状态。
            checks["qdrant"] = "degraded"

    ready = checks.get("mysql") == "ok" and checks.get("redis") == "ok"
    fully_ready = ready and all(value == "ok" for value in checks.values())
    body = {
        "status": "ok" if fully_ready else "degraded",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """Prometheus 抓取入口；生产网关应额外限制该路径的网络访问。"""
    return PlainTextResponse(await metrics.render_with_shared(), media_type="text/plain; version=0.0.4")


app.include_router(news.router)
app.include_router(users.router)
app.include_router(auth.router)
app.include_router(favorite.router)
app.include_router(history.router)
app.include_router(agent.router)
app.include_router(admin.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.frontend_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
    max_age=600,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
