"""SDOC Shipping Document Verification — Backend API.

FastAPI entry point. Run locally:

    cd backend
    uvicorn app.main:app --reload --port 8000

Interactive docs: http://localhost:8000/docs
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db, init_db
from app.models import EmailRecord
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


def _parse_received_at(value):
    """Convert common JSON datetime formats into a Python datetime."""
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    if not isinstance(value, str):
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def seed_email_records() -> None:
    """
    Import the static inbox JSON files into EmailRecord.

    This intentionally does NOT process attachments, run OCR, classify emails,
    or generate reports. It only makes the inbox available to the Review Desk.

    This is lightweight enough to run during application startup on Render's
    Free instance.
    """
    inbox_dir = Path(settings.data_source) / "inbox"

    if not inbox_dir.is_dir():
        logging.warning(
            "Inbox directory not found: %s. "
            "Skipping email database seeding.",
            inbox_dir,
        )
        return

    email_files = sorted(inbox_dir.glob("email_*.json"))

    if not email_files:
        logging.warning(
            "No email JSON files found in %s.",
            inbox_dir,
        )
        return

    db = SessionLocal()

    try:
        existing_ids = {
            row.email_id
            for row in db.query(EmailRecord.email_id).all()
        }

        added = 0

        for path in email_files:
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                logging.warning(
                    "Could not read inbox file %s: %s",
                    path,
                    exc,
                )
                continue

            email_id = (
                data.get("email_id")
                or data.get("id")
                or data.get("message_id")
                or path.stem
            )

            if not email_id:
                logging.warning(
                    "Skipping %s because no email ID was found.",
                    path,
                )
                continue

            email_id = str(email_id)

            if email_id in existing_ids:
                continue

            sender = (
                data.get("sender")
                or data.get("from")
                or data.get("email")
                or ""
            )

            subject = data.get("subject") or ""

            body = (
                data.get("body")
                or data.get("text")
                or data.get("content")
                or ""
            )

            attachments = (
                data.get("attachments")
                or data.get("files")
                or []
            )

            if not isinstance(attachments, list):
                attachments = [str(attachments)]

            received_at = _parse_received_at(
                data.get("received_at")
                or data.get("timestamp")
                or data.get("date")
            )

            record = EmailRecord(
                email_id=email_id,
                sender=str(sender),
                subject=str(subject),
                body=str(body),
                attachments=attachments,
                received_at=received_at,
            )

            db.add(record)
            existing_ids.add(email_id)
            added += 1

        if added:
            db.commit()

        logging.info(
            "Inbox database seeding complete: %s new emails imported, "
            "%s JSON files found.",
            added,
            len(email_files),
        )

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


@app.on_event("startup")
def on_startup() -> None:
    """
    Initialize the database and import lightweight inbox metadata.

    Heavy document processing is intentionally skipped during startup.
    This prevents the Render Free instance from exceeding its 512 MB
    memory limit.
    """
    init_db()

    try:
        seed_email_records()
    except Exception as exc:
        logging.warning(
            "Inbox database seeding failed: %s",
            exc,
        )

    logging.info(
        "Database initialized. "
        "Heavy inbox processing skipped during startup."
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
