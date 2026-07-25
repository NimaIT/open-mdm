"""MDM Platform — application entrypoint."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import check_connection, session_scope
from app.services.auth import ensure_local_admin

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("mdm")

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
    yield


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
          admin.router):
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
