from __future__ import annotations

import logging
import sys

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import EmailRecord, ReportRecord
from app.services import workflow


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("precompute_reports")


def seed_email_records() -> int:
    """
    Import the static inbox JSON files into EmailRecord.

    This is intentionally kept lightweight. Document processing is handled
    separately by workflow.process_all().
    """
    import json
    from datetime import datetime
    from pathlib import Path

    settings = get_settings()
    inbox_dir = Path(settings.data_source) / "inbox"

    if not inbox_dir.is_dir():
        raise RuntimeError(
            f"Inbox directory not found: {inbox_dir}"
        )

    email_files = sorted(inbox_dir.glob("email_*.json"))

    if not email_files:
        raise RuntimeError(
            f"No email JSON files found in {inbox_dir}"
        )

    def parse_received_at(value):
        if not value:
            return None

        if isinstance(value, datetime):
            return value

        if not isinstance(value, str):
            return None

        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            return None

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
                logger.warning(
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

            received_at = parse_received_at(
                data.get("received_at")
                or data.get("timestamp")
                or data.get("date")
            )

            db.add(
                EmailRecord(
                    email_id=email_id,
                    sender=str(sender),
                    subject=str(subject),
                    body=str(body),
                    attachments=attachments,
                    received_at=received_at,
                )
            )

            existing_ids.add(email_id)
            added += 1

        db.commit()

        logger.info(
            "Seeded %s new emails from %s JSON files.",
            added,
            len(email_files),
        )

        return added

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def main() -> int:
    settings = get_settings()

    logger.info("Starting build-time report precomputation.")
    logger.info("DATA_SOURCE=%s", settings.data_source)
    logger.info("AI_PROVIDER=%s", settings.ai_provider)
    logger.info("OCR_ENABLED=%s", settings.ocr_enabled)

    # Create all database tables.
    init_db()

    # Make sure the 520 inbox records exist before workflow processing.
    seed_email_records()

    db = SessionLocal()

    try:
        existing_reports = db.query(ReportRecord).count()
        email_count = db.query(EmailRecord).count()

        logger.info(
            "Database currently contains %s emails and %s reports.",
            email_count,
            existing_reports,
        )

    finally:
        db.close()

    # Process every email using the existing production workflow.
    logger.info(
        "Running workflow.process_all() for %s emails.",
        email_count,
    )

    db = SessionLocal()

    try:
        result = workflow.process_all(
            db,
            limit=0,
        )

        logger.info(
            "workflow.process_all() completed: %s",
            result,
        )

        db.expire_all()

        final_email_count = db.query(EmailRecord).count()
        final_report_count = db.query(ReportRecord).count()

        logger.info(
            "Final database state: emails=%s reports=%s",
            final_email_count,
            final_report_count,
        )

        if final_report_count == 0:
            raise RuntimeError(
                "Precomputation produced zero reports."
            )

        if final_report_count < final_email_count:
            raise RuntimeError(
                "Precomputation incomplete: "
                f"{final_report_count} reports for "
                f"{final_email_count} emails."
            )

    finally:
        db.close()

    logger.info("Build-time report precomputation completed successfully.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
