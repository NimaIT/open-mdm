"""Serves the single-page application shell.

The UI is a React SPA that talks to the same /api/v1 endpoints as any other
client. React is vendored under app/static/vendor so the application has no
build step and no runtime CDN dependency — which matters for the locked-down,
AD-joined networks this tool is designed for.
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter(include_in_schema=False)

STATIC = Path(__file__).parent / "static"


@router.get("/", response_class=HTMLResponse)
@router.get("/login", response_class=HTMLResponse)
@router.get("/inbox", response_class=HTMLResponse)
@router.get("/inbox/{rest:path}", response_class=HTMLResponse)
@router.get("/models", response_class=HTMLResponse)
@router.get("/models/{rest:path}", response_class=HTMLResponse)
@router.get("/review", response_class=HTMLResponse)
@router.get("/review/{rest:path}", response_class=HTMLResponse)
@router.get("/records", response_class=HTMLResponse)
@router.get("/records/{rest:path}", response_class=HTMLResponse)
@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/{rest:path}", response_class=HTMLResponse)
def spa(rest: str = ""):
    """All UI routes return the same shell; routing happens client-side."""
    return FileResponse(STATIC / "index.html")
