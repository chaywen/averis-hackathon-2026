"""SDOC Shipping Document Verification — Backend API.

FastAPI entry point. Run locally:

    cd backend
    uvicorn app.main:app --reload --port 8000

Interactive docs: http://localhost:8000/docs
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db, init_db
from app.routers import emails, frontend_compat, reports, reviews


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Email inbox → classification → SI/BL attachment extraction → "
        "deterministic 7-field comparison → discrepancy report with "
        "human-in-the-loop review."
    ),
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(emails.router)
app.include_router(reports.router)
app.include_router(reviews.router)
app.include_router(frontend_compat.router)


# Frontend (P1) UI — Review Desk, served from /ui/
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

if WEB_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount(
        "/ui",
        StaticFiles(directory=str(WEB_DIR), html=True),
        name="ui",
    )


@app.on_event("startup")
def on_startup() -> None:
    """
    Initialize the database only.

    Heavy inbox processing is intentionally skipped during startup.

    Previously, the application processed the entire inbox during startup.
    This could cause the Render Free instance to exceed its 512 MB memory
    limit before the API finished starting.

    Document processing remains available through the existing API endpoints.
    """
    init_db()

    logging.info(
        "Database initialized. "
        "Skipping inbox pre-processing during startup."
    )


@app.get("/", tags=["meta"])
def root():
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/ui/")


@app.get("/meta", tags=["meta"])
def meta():
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "endpoints": [
            "GET  /emails",
            "GET  /emails/{email_id}",
            "POST /emails/{email_id}/process",
            "POST /emails/process-all",
            "GET  /reports",
            "GET  /reports/{report_id_or_email_id}",
            "GET  /reports/summary/stats",
            "GET  /reports/submission/json",
            "POST /reviews/{email_id}",
            "GET  /health",
            "GET  /ui/  (P1 frontend: Review Desk)",
            "GET  /api/summary, /api/emails, /api/emails/{id}, "
            "/api/emails/{id}/review, /api/review-queue, "
            "/api/attachments/{path}",
        ],
    }


@app.get("/health", tags=["meta"])
def health(db: Session = Depends(get_db)):
    from app.models import EmailRecord, ReportRecord

    return {
        "status": "ok",
        "app_name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "database": settings.database_url.split("://")[0],
        "data_source": settings.data_source,
        "ai_provider": settings.ai_provider,
        "emails_cached": db.query(EmailRecord).count(),
        "reports_stored": db.query(ReportRecord).count(),
    }
