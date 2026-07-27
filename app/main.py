"""MDM Platform — application entrypoint."""
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import check_connection, session_scope
from app.services.auth import ensure_local_admin
from app.services.logging_config import (
    configure_logging,
    reset_request_context,
    set_request_context,
    stream_logger,
)

# Structured, stream-aware logging (AO-1). Idempotent; text or JSON per config.
configure_logging()
log = logging.getLogger("mdm")
access_log = stream_logger("platform")

DESCRIPTION = """
Metadata-driven **Master Data Management** platform.

### The write path
`POST` / `PUT` / `PATCH` / `DELETE` never modify golden records directly. Every
change is captured in the **landing** tier, validated and typed into
**staging**, reviewed by a human **data steward**, and only then applied to the
**live** golden tables — with the prior version written to **history**.

    API write  ->  landing  ->  staging  ->  [steward review]  ->  live -> history

### Roles
| Role | Capability |
|------|------------|
| `admin` | Model design, DDL publishing, users, API keys, settings |
| `steward` | Review, edit, approve or reject staged data |
| `reader` | Read-only access to golden records |
| `service` | Machine writes into landing only — cannot approve |

Authentication is via LDAP / Active Directory, with a local break-glass admin
and API keys for service accounts.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = check_connection()
    if conn.get("connected"):
        log.info(
            "Connected to PostgreSQL %s as %s on database %s",
            conn.get("server_version"), conn.get("user"), conn.get("database"),
        )
        try:
            with session_scope() as db:
                created = ensure_local_admin(db)
            if created and created.get("created"):
                pw = created.get("generated_password")
                log.warning(
                    "Created break-glass admin '%s'%s",
                    created["username"],
                    f" with generated password: {pw}" if pw else "",
                )
        except Exception as exc:
            log.warning("Could not ensure local admin (run bootstrap first): %s", exc)
    else:
        log.error("Database unavailable at startup: %s", conn.get("error"))

    # Extensibility (EX-1): import operator hook/transform modules named in
    # HOOK_MODULES so their registrations take effect. Never fatal.
    if settings.HOOK_MODULES:
        try:
            from app.services import hooks as hooks_svc

            loaded = hooks_svc.load_hook_modules()
            log.info("Hook modules: loaded=%s failed=%s",
                     loaded["loaded"], loaded["failed"])
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not load hook modules: %s", exc)

    # Background scheduler (W5) — opt-in. Must never block startup or leak the
    # thread: start guards against a double-start, stop joins with a timeout.
    scheduler_started = False
    if settings.SCHEDULER_ENABLED:
        try:
            from app.services import scheduler as scheduler_svc

            scheduler_started = scheduler_svc.start_scheduler()
            log.info("Background scheduler started: %s", scheduler_started)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not start background scheduler: %s", exc)

    yield

    if scheduler_started:
        try:
            from app.services import scheduler as scheduler_svc

            joined = scheduler_svc.stop_scheduler(timeout=5.0)
            log.info("Background scheduler stopped cleanly: %s", joined)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Error stopping background scheduler: %s", exc)


app = FastAPI(
    title=settings.APP_NAME,
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """Assign a per-request id (AO-1), carry method/path in the log context, and
    emit one structured access-log line per request on the ``platform`` stream.

    The ``request_id`` is echoed as ``X-Request-ID`` so a client can correlate a
    response with its server-side logs (ELK). Never alters the body or status.
    """
    request_id = str(uuid.uuid4())
    # actor is seeded to None here (a token is taken) so it is reset cleanly even
    # though it is filled in later by auth via bind_actor().
    tokens = set_request_context(
        request_id=request_id,
        actor=None,
        method=request.method,
        path=request.url.path,
    )
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        try:
            access_log.info(
                "%s %s -> %s (%sms)",
                request.method, request.url.path, status_code, duration_ms,
                extra={"status": status_code, "duration_ms": duration_ms},
            )
        except Exception:  # pragma: no cover - logging must never break a request
            pass
        reset_request_context(tokens)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error.",
            "error": str(exc) if settings.DEBUG else None,
        },
    )


# ------------------------------------------------------------------- routers
from app.api.v1 import admin, auth, data, models_api, stewardship  # noqa: E402

for r in (auth.router, models_api.router, data.router, stewardship.router,
          admin.router, admin.domain_router):
    app.include_router(r, prefix=settings.API_PREFIX)

from app.ui import router as ui_router  # noqa: E402

app.include_router(ui_router)

try:
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
except Exception:  # pragma: no cover
    log.warning("Static directory not mounted")


# -------------------------------------------------------------------- health
@app.get("/health", tags=["operations"])
def health():
    """Liveness probe — is the process up?"""
    return {"status": "ok", "app": settings.APP_NAME, "version": "1.0.0"}


@app.get("/ready", tags=["operations"])
def ready():
    """Readiness probe — can we actually serve traffic?"""
    conn = check_connection()
    if not conn.get("connected"):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "database": conn},
        )
    return {"status": "ready", "database": conn}
