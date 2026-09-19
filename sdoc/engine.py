"""Orchestration: inbox -> per-email results -> submission.json.

The Engine is format- and strategy-agnostic: classifiers and extractors are
pluggable, human reviews overlay machine results, and the submission is
derived on demand.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict

from .classifier import Classifier
from .comparator import FieldComparison, compare_fields, defect_fields
from .extractor import (COMPARE_FIELDS, doc_title, load_document, parse_fields)

REVIEW_REASONS = ("wrong_doc_type", "missing_attachment", "unreadable", "missing_value")


@dataclass
class EmailResult:
    email_id: str
    category: str
    status: str                      # OK | MISMATCH | NEEDS_REVIEW
    review_reason: str | None = None
    has_defect: bool = False
    defect_fields: list[str] = field(default_factory=list)
    rule: str | None = None
    decided_by: str = "rule"         # rule | human
    comparisons: list[FieldComparison] | None = None

    def to_submission(self) -> dict:
        return {
            "category": self.category,
            "status": self.status,
            "review_reason": self.review_reason,
            "has_defect": self.has_defect,
            "defect_fields": list(self.defect_fields),
        }


class Engine:
    def __init__(self, data_root: str, review_store_path: str | None = None,
                 classifier: Classifier | None = None):
        self.data_root = data_root
        self.inbox_dir = os.path.join(data_root, "inbox")
        self.classifier = classifier or Classifier()
        self.emails: dict[str, dict] = {}
        self.results: dict[str, EmailResult] = {}
        self.review_store_path = review_store_path
        self.reviews: dict[str, dict] = {}
        if review_store_path and os.path.exists(review_store_path):
            with open(review_store_path, encoding="utf-8") as f:
                self.reviews = json.load(f)

    # ------------------------------------------------------------------
    # loading / processing
    # ------------------------------------------------------------------
    def load(self):
        for fname in sorted(os.listdir(self.inbox_dir)):
            if fname.endswith(".json"):
                email = json.load(open(os.path.join(self.inbox_dir, fname), encoding="utf-8"))
                self.emails[email["email_id"]] = email
        return self

    def process_all(self):
        for eid, email in self.emails.items():
            self.results[eid] = self.process(email)
        return self

    def process(self, email: dict) -> EmailResult:
        cls = self.classifier.classify(email)
        result = EmailResult(email_id=email["email_id"], category=cls.category,
                             status="OK", rule=cls.rule, decided_by=cls.decided_by)
        if cls.category == "BL_COMPARISON":
            self._compare_documents(email, result)
        return result

    # ------------------------------------------------------------------
    # document comparison pipeline
    # ------------------------------------------------------------------
    def _compare_documents(self, email: dict, result: EmailResult):
        atts = email.get("attachments", [])
        si_rel = next((a for a in atts if "_SI." in a), None)
        bl_rel = next((a for a in atts if "_BL." in a), None)

        # -- missing attachment ------------------------------------------
        if len(atts) == 1:
            result.status, result.review_reason = "NEEDS_REVIEW", "missing_attachment"
            return
        if len(atts) == 0:
            # regular 'please send the draft BL' requests carry NO docs and
            # stay OK; the edge variant claims the docs should be there.
            if re.search(r"compare the SI and (draft )?BL", email.get("body", ""), re.I):
                result.status, result.review_reason = "NEEDS_REVIEW", "missing_attachment"
            return
        if si_rel is None or bl_rel is None:
            result.status, result.review_reason = "NEEDS_REVIEW", "missing_attachment"
            return

        si_status, si_text = load_document(self.data_root, si_rel)
        bl_status, bl_text = load_document(self.data_root, bl_rel)

        # -- unreadable ----------------------------------------------------
        if si_status == "unreadable" or bl_status == "unreadable":
            result.status, result.review_reason = "NEEDS_REVIEW", "unreadable"
            return

        # -- wrong doc type (title check, .txt only: xlsx/docx extracted
        #    text starts with data rows, not a title) -----------------------
        for rel, text in ((si_rel, si_text), (bl_rel, bl_text)):
            if not rel.lower().endswith(".txt"):
                continue
            title = doc_title(text)
            if title and "SHIPPING INSTRUCTION" not in title and "BILL OF LADING" not in title:
                result.status, result.review_reason = "NEEDS_REVIEW", "wrong_doc_type"
                return

        si_fields = parse_fields(si_text)
        bl_fields = parse_fields(bl_text)
        comparisons = compare_fields(si_fields, bl_fields, COMPARE_FIELDS)
        result.comparisons = comparisons

        # -- missing value --------------------------------------------------
        if any(c.missing for c in comparisons):
            result.status, result.review_reason = "NEEDS_REVIEW", "missing_value"
            return

        # -- verdict ---------------------------------------------------------
        defects = defect_fields(comparisons)
        result.defect_fields = defects
        result.has_defect = bool(defects)
        result.status = "MISMATCH" if defects else "OK"

    # ------------------------------------------------------------------
    # human-in-the-loop overlay
    # ------------------------------------------------------------------
    def apply_review(self, email_id: str, review: dict) -> EmailResult:
        """review: {action: 'confirm'|'override', status?, defect_fields?, note?}."""
        if email_id not in self.results:
            raise KeyError(email_id)
        entry = {"action": review.get("action", "confirm"),
                 "status": review.get("status"),
                 "defect_fields": review.get("defect_fields"),
                 "note": review.get("note", "")}
        self.reviews[email_id] = entry
        self._persist_reviews()
        self.results[email_id] = self._merge_review(self.results[email_id], entry)
        return self.results[email_id]

    def _merge_review(self, base: EmailResult, entry: dict) -> EmailResult:
        merged = EmailResult(**{**asdict(base), "comparisons": base.comparisons})
        merged.decided_by = "human"
        if entry["action"] == "confirm":
            return merged
        if entry["status"] in ("OK", "MISMATCH", "NEEDS_REVIEW"):
            merged.status = entry["status"]
        merged.review_reason = None
        if entry["status"] == "NEEDS_REVIEW" and entry.get("note"):
            merged.review_reason = entry["note"]
        if entry["status"] == "MISMATCH":
            merged.has_defect = True
            merged.defect_fields = list(entry.get("defect_fields") or [])
        else:
            merged.has_defect = False
            merged.defect_fields = []
        return merged

    def _persist_reviews(self):
        if not self.review_store_path:
            return
        os.makedirs(os.path.dirname(self.review_store_path), exist_ok=True)
        with open(self.review_store_path, "w", encoding="utf-8") as f:
            json.dump(self.reviews, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # outputs
    # ------------------------------------------------------------------

    def dump_results_cache(self, path: str):
        """Serialize processed results to disk so a later startup can skip
        reprocessing (esp. OCR-heavy PDFs)."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = {eid: {**asdict(r), "comparisons": [asdict(c) for c in (r.comparisons or [])] or None}
            for eid, r in self.results.items()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)

    def load_results_cache(self, path: str) -> bool:
            """Load pre-baked results if present. Returns True if it loaded."""
            if not path or not os.path.exists(path):
                return False
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            for eid, r in raw.items():
                comps = [FieldComparison(**c) for c in (r.pop("comparisons") or [])] or None
                self.results[eid] = EmailResult(comparisons=comps, **r)
            return True

    def submission(self) -> dict[str, dict]:
        return {eid: r.to_submission() for eid, r in self.results.items()}

    def summary(self) -> dict:
        by_cat: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for r in self.results.values():
            by_cat[r.category] = by_cat.get(r.category, 0) + 1
            by_status[r.status] = by_status.get(r.status, 0) + 1
        return {
            "total": len(self.results),
            "by_category": by_cat,
            "by_status": by_status,
            "reviewed": len(self.reviews),
        }
