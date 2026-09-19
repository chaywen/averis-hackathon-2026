#!/usr/bin/env python3
"""SDOC Review Desk -- FastAPI service around the sdoc engine.

Endpoints:
    GET  /                     review desk UI (static)
    GET  /api/summary          dashboard counts
    GET  /api/emails           list w/ filters: category, status, q, limit, offset
    GET  /api/emails/{id}      full detail incl. SI-vs-BL field comparison
    POST /api/emails/{id}/review   human review (confirm / override)
    GET  /api/submission       merged submission.json (machine + human verdicts)
    GET  /api/attachments/{path}   serve an attachment file (path-traversal safe)

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# make the sibling `sdoc` package importable when running from app/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdoc import Engine  # noqa: E402
from sdoc.classifier import Classifier  # noqa: E402

DATA_ROOT = os.environ.get("SDOC_DATA_ROOT",
                           str(Path(__file__).resolve().parent.parent / "sdoc-hackathon-bundle"))
REVIEWS_PATH = os.environ.get("SDOC_REVIEWS",
                              str(Path(__file__).resolve().parent / "data" / "reviews.json"))
PORT = int(os.environ.get("PORT", "8000"))

engine = Engine(DATA_ROOT, review_store_path=REVIEWS_PATH)

RESULTS_CACHE = os.environ.get("SDOC_RESULTS_CACHE",
                                str(Path(__file__).resolve().parent / "data" / "results_cache.json"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.load()
    if not engine.load_results_cache(RESULTS_CACHE):
        engine.process_all()
        engine.dump_results_cache(RESULTS_CACHE)
    print(f"engine ready: {engine.summary()}")
    yield

app = FastAPI(title="SDOC Review Desk", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/summary")
def summary():
    return engine.summary()


@app.get("/api/emails")
def emails(category: str | None = None, status: str | None = None,
           q: str | None = None,
           limit: int = Query(50, le=500), offset: int = 0):
    hits = []
    for eid in sorted(engine.results):
        r = engine.results[eid]
        if category and r.category != category:
            continue
        if status and r.status != status:
            continue
        if q:
            email = engine.emails[eid]
            hay = f"{eid} {email.get('subject','')} {email.get('from','')}".lower()
            if q.lower() not in hay:
                continue
        email = engine.emails[eid]
        hits.append({
            "email_id": eid,
            "from": email.get("from"),
            "subject": email.get("subject"),
            "n_attachments": len(email.get("attachments", [])),
            "category": r.category,
            "status": r.status,
            "has_defect": r.has_defect,
            "defect_fields": r.defect_fields,
            "review_reason": r.review_reason,
            "decided_by": r.decided_by,
        })
    return {"total": len(hits), "items": hits[offset:offset + limit]}


@app.get("/api/emails/{email_id}")
def email_detail(email_id: str):
    if email_id not in engine.results:
        raise HTTPException(404, "unknown email id")
    r = engine.results[email_id]
    email = engine.emails[email_id]
    detail = {
        "email": {
            "email_id": email_id,
            "from": email.get("from"),
            "subject": email.get("subject"),
            "body": email.get("body"),
            "attachments": email.get("attachments", []),
        },
        "result": {
            "category": r.category, "status": r.status,
            "review_reason": r.review_reason,
            "has_defect": r.has_defect, "defect_fields": r.defect_fields,
            "decided_by": r.decided_by, "rule": r.rule,
        },
        "comparisons": [c.__dict__ for c in r.comparisons] if r.comparisons else None,
        "human_review": engine.reviews.get(email_id),
    }
    return detail


class ReviewInput(BaseModel):
    action: str = Field("confirm", pattern="^(confirm|override)$")
    status: str | None = Field(None, pattern="^(OK|MISMATCH|NEEDS_REVIEW)$")
    defect_fields: list[str] | None = None
    note: str | None = ""


@app.post("/api/emails/{email_id}/review")
def review(email_id: str, payload: ReviewInput):
    if email_id not in engine.results:
        raise HTTPException(404, "unknown email id")
    if payload.action == "override" and not payload.status:
        raise HTTPException(422, "override requires a status")
    result = engine.apply_review(email_id, payload.model_dump())
    return {"ok": True, "result": {**result.to_submission(), "decided_by": result.decided_by}}


@app.get("/api/submission")
def submission():
    return JSONResponse(content=engine.submission(), headers={
        "Content-Disposition": "attachment; filename=submission.json"})


@app.get("/api/attachments/{rel_path:path}")
def attachment(rel_path: str):
    base = Path(engine.data_root).resolve()
    full = (base / rel_path).resolve()
    if not str(full).startswith(str(base)) or not full.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(full)


# ---------------------------------------------------------------------------
# static UI
# ---------------------------------------------------------------------------
static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
