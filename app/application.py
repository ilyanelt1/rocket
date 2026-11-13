import os

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware

from .routes.admin import router as admin_router
from .routes.auth import router as auth_router
from .routes.dashboard import router as dashboard_router
from .routes.history import router as history_router
from .routes.operations import router as operations_router
from .routes.system import router as system_router
from .services import StockError
from .templating import templates


def _get_session_secret() -> str:
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET is required; configure a stable secret.")
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")
    return secret


def create_app() -> FastAPI:
    application = FastAPI(title="CheckStock")
    application.add_middleware(
        SessionMiddleware,
        secret_key=_get_session_secret(),
        https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        same_site="lax",
    )

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response

    @application.exception_handler(StockError)
    async def stock_error_handler(request: Request, exc: StockError):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"error": str(exc)},
            status_code=409,
        )

    application.include_router(system_router)
    application.include_router(auth_router)
    application.include_router(dashboard_router)
    application.include_router(admin_router)
    application.include_router(operations_router)
    application.include_router(history_router)
    return application
