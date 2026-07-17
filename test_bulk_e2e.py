# ruff: noqa: E402
"""
test_bulk_e2e.py — End-to-end integration tests for Bulk Mode (Decision 19).

Uses the fixture #6 PDF and ground truth to exercise the full batch flow
without hitting the live Render service or the real OCR/vision API.

The detection step is stubbed: run_detection_background is patched to inject
the fixture #6 ground truth review_state directly, making tests fast and
deterministic (no API calls, no 3-minute waits).

Coverage:
  1. POST /api/batches — create batch
  2. GET  /api/batches — list batches
  3. GET  /api/batches/{id} — batch status + ledger
  4. POST /api/batches/{id}/sub-jobs — upload PDF → sub-job created
  5. GET  /api/jobs/{sub_job_id}/status — sub-job reaches 'ready'
  6. GET  /api/jobs/{sub_job_id}/review — review state
  7. POST /api/batches/{id}/sub-jobs/{sj_id}/confirm — confirm sub-job
  8. GET  /api/batches/{id} — ledger updated after confirm
  9. GET  /api/batches/{id}/download — batch ZIP (gated on reconciliation)
  10. POST /api/batches/{id}/sub-jobs/{sj_id}/unconfirm — release tickets
  11. POST /api/batches/{id}/sub-jobs/{sj_id}/abandon — abandon sub-job
  12. Cross-file duplicate detection (two sub-jobs claiming same ticket)
"""

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ── Env setup (must happen before importing main) ──────────────────────────────
os.environ.setdefault("APP_PASSWORD", "test_password")
os.environ.setdefault("OPENAI_API_KEY", "test_key")
os.environ.setdefault("OPENAI_API_BASE", "http://localhost:11111")

sys.path.insert(0, str(Path(__file__).parent))

# ── Fixtures ──────────────────────────────────────────────────────────────────
FIXTURE6_DIR = Path(__file__).parent / "tests" / "fixtures" / "fixture6"
FIXTURE6_PDF = FIXTURE6_DIR / "input.pdf"
FIXTURE6_GT  = FIXTURE6_DIR / "ground_truth.json"

with open(FIXTURE6_GT) as _f:
    GT = json.load(_f)

WHITELIST_RAW = ", ".join(GT["whitelist"])
BATCH_TYPE    = GT["batch_type"]
FAST_MODE     = GT.get("fast_mode", True)


# ── App import (deferred so env is set first) ─────────────────────────────────
import webapp.main as _main_module
import webapp.batch_routes as _batch_routes_module
from webapp.main import app, jobs, jobs_lock  # noqa: E402

client = TestClient(app, raise_server_exceptions=True)


# ── Detection stub ─────────────────────────────────────────────────────────────
def _stub_detection(job_id: str) -> None:
    """
    Bypass real OCR/vision detection.
    Injects fixture #6 ground truth blocks directly into the job's review_state,
    then marks the job 'ready'.  Runs synchronously (no background thread needed).
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return

    # Build a review_state that matches fixture #6 ground truth exactly.
    # Each block must have: id, ticket, pages, page_range, flags, has_hard_flag,
    # source, confidence, unmatched_raw, per_page (optional).
    blocks = []
    for i, b in enumerate(GT["blocks"]):
        pages = b["pages"]
        flags = b["flags"]
        hard_flags = {"UNMATCHED_NUMBER", "AMBIGUOUS_SUFFIX", "AMBIGUOUS_MATCH",
                      "ORPHAN_LEADING_PAGES", "CORRECTION_CONFLICT", "DETECTION_FAILED"}
        has_hard = any(f in hard_flags for f in flags)
        blocks.append({
            "id": i,
            "ticket": b["ticket"],
            "pages": pages,
            "page_range": f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0]),
            "flags": flags,
            "has_hard_flag": has_hard,
            "source": "sticker",
            "confidence": 0.95,
            "unmatched_raw": None,
        })

    review_state = {
        "blocks": blocks,
        "whitelist": GT["whitelist"],
        "missing_tickets": [],
        "unmatched_values": [],
        "per_page": {str(p): GT["page_map"][str(p)] for p in range(1, GT["total_pages"] + 1)},
    }

    with jobs_lock:
        if jobs.get(job_id):
            jobs[job_id]["review_state"] = review_state
            jobs[job_id]["detection_results"] = []
            jobs[job_id]["progress_page"] = GT["total_pages"]
            jobs[job_id]["status"] = "ready"

    from webapp.main import persist_job
    persist_job(job_id)


# ── Auth helper ────────────────────────────────────────────────────────────────
def login():
    r = client.post("/api/login", data={"password": "test_password"})
    assert r.status_code == 200, f"Login failed: {r.text}"


# ── Sub-job helpers ────────────────────────────────────────────────────────────
def upload_sub_job(batch_id: str, expected_count: int = None) -> dict:
    if expected_count is None:
        expected_count = len(GT["whitelist"])
    with open(FIXTURE6_PDF, "rb") as f:
        pdf_bytes = f.read()
    # Install the stub via the module-level hook so the BackgroundTask picks it up.
    _batch_routes_module._detection_fn_override = _stub_detection
    try:
        r = client.post(
            f"/api/batches/{batch_id}/sub-jobs",
            files={"file": ("fixture6.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
            data={"expected_count": str(expected_count)},
        )
    finally:
        _batch_routes_module._detection_fn_override = None
    assert r.status_code == 200, f"sub-job upload failed: {r.text}"
    return r.json()


def confirm_sub_job(batch_id: str, sub_job_id: str) -> bytes:
    r = client.post(
        f"/api/batches/{batch_id}/sub-jobs/{sub_job_id}/confirm",
    )
    assert r.status_code == 200, f"confirm failed ({r.status_code}): {r.text}"
    return r.json()


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestBulkBatchCreation:
    def setup_method(self):
        login()

    def test_create_batch_returns_batch_id(self):
        r = client.post("/api/batches", data={
            "whitelist_raw": WHITELIST_RAW,
            "batch_type": BATCH_TYPE,
            "fast_mode": "on" if FAST_MODE else "off",
        })
        assert r.status_code == 200
        d = r.json()
        assert "batch_id" in d
        assert d["whitelist_count"] == len(GT["whitelist"])
        assert d["batch_type"] == BATCH_TYPE

    def test_list_batches_includes_new_batch(self):
        r1 = client.post("/api/batches", data={
            "whitelist_raw": WHITELIST_RAW,
            "batch_type": BATCH_TYPE,
            "fast_mode": "on",
        })
        batch_id = r1.json()["batch_id"]
        r2 = client.get("/api/batches")
        assert r2.status_code == 200
        ids = [b["batch_id"] for b in r2.json()]
        assert batch_id in ids

    def test_get_batch_status(self):
        r1 = client.post("/api/batches", data={
            "whitelist_raw": WHITELIST_RAW,
            "batch_type": BATCH_TYPE,
            "fast_mode": "on",
        })
        batch_id = r1.json()["batch_id"]
        r2 = client.get(f"/api/batches/{batch_id}")
        assert r2.status_code == 200
        d = r2.json()
        assert d["batch_id"] == batch_id
        assert d["status"] == "open"
        assert d["whitelist_count"] == len(GT["whitelist"])
        assert "ledger" in d
        assert d["ledger"]["total_expected"] == len(GT["whitelist"])
        assert d["ledger"]["total_claimed"] == 0

    def test_create_batch_invalid_whitelist(self):
        r = client.post("/api/batches", data={
            "whitelist_raw": "not-a-number, also-bad",
            "batch_type": BATCH_TYPE,
            "fast_mode": "on",
        })
        assert r.status_code == 422

    def test_create_batch_missing_type(self):
        r = client.post("/api/batches", data={
            "whitelist_raw": WHITELIST_RAW,
            "fast_mode": "on",
        })
        assert r.status_code == 422


class TestBulkSubJobUpload:
    def setup_method(self):
        login()
        r = client.post("/api/batches", data={
            "whitelist_raw": WHITELIST_RAW,
            "batch_type": BATCH_TYPE,
            "fast_mode": "on" if FAST_MODE else "off",
        })
        self.batch_id = r.json()["batch_id"]

    def test_upload_sub_job_creates_job(self):
        d = upload_sub_job(self.batch_id)
        assert "sub_job_id" in d
        assert d["total_pages"] == GT["total_pages"]
        assert d["batch_type"] == BATCH_TYPE

    def test_sub_job_appears_in_batch_status(self):
        d = upload_sub_job(self.batch_id)
        sj_id = d["sub_job_id"]
        r = client.get(f"/api/batches/{self.batch_id}")
        sj_ids = [sj["id"] for sj in r.json()["sub_jobs"]]
        assert sj_id in sj_ids

    def test_sub_job_is_ready_after_upload(self):
        d = upload_sub_job(self.batch_id)
        sj_id = d["sub_job_id"]
        r = client.get(f"/api/jobs/{sj_id}/status")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"

    def test_sub_job_review_state_has_blocks(self):
        d = upload_sub_job(self.batch_id)
        sj_id = d["sub_job_id"]
        r = client.get(f"/api/jobs/{sj_id}/review")
        assert r.status_code == 200
        rv = r.json()
        assert len(rv["blocks"]) == len(GT["blocks"])
        assert rv["whitelist"] == GT["whitelist"]


class TestBulkSubJobConfirm:
    def setup_method(self):
        login()
        r = client.post("/api/batches", data={
            "whitelist_raw": WHITELIST_RAW,
            "batch_type": BATCH_TYPE,
            "fast_mode": "on" if FAST_MODE else "off",
        })
        self.batch_id = r.json()["batch_id"]
        d = upload_sub_job(self.batch_id)
        self.sj_id = d["sub_job_id"]

    def test_confirm_sub_job_returns_json_ledger(self):
        # Confirm now returns JSON (ledger update), not a ZIP.
        # The ZIP is built internally and served only via the batch download endpoint.
        data = confirm_sub_job(self.batch_id, self.sj_id)
        assert data["status"] == "confirmed"
        assert "tickets_claimed" in data
        assert len(data["tickets_claimed"]) == len(GT["whitelist"])
        assert "ledger" in data
        assert data["ledger"]["total_claimed"] == len(GT["whitelist"])

    def test_confirm_updates_batch_ledger(self):
        confirm_sub_job(self.batch_id, self.sj_id)
        r = client.get(f"/api/batches/{self.batch_id}")
        ledger = r.json()["ledger"]
        assert ledger["confirmed_sub_jobs"] == 1
        assert ledger["total_claimed"] == len(GT["whitelist"])

    def test_confirm_marks_sub_job_confirmed(self):
        confirm_sub_job(self.batch_id, self.sj_id)
        r = client.get(f"/api/batches/{self.batch_id}")
        sj = next(s for s in r.json()["sub_jobs"] if s["id"] == self.sj_id)
        assert sj["status"] == "confirmed"

    def test_unconfirm_releases_tickets(self):
        confirm_sub_job(self.batch_id, self.sj_id)
        r = client.post(f"/api/batches/{self.batch_id}/sub-jobs/{self.sj_id}/unconfirm")
        assert r.status_code == 200
        r2 = client.get(f"/api/batches/{self.batch_id}")
        ledger = r2.json()["ledger"]
        assert ledger["total_claimed"] == 0
        sj = next(s for s in r2.json()["sub_jobs"] if s["id"] == self.sj_id)
        assert sj["status"] == "ready"

    def test_abandon_marks_sub_job_abandoned(self):
        r = client.post(f"/api/batches/{self.batch_id}/sub-jobs/{self.sj_id}/abandon")
        assert r.status_code == 200
        r2 = client.get(f"/api/batches/{self.batch_id}")
        sj = next(s for s in r2.json()["sub_jobs"] if s["id"] == self.sj_id)
        assert sj["status"] == "abandoned"


class TestBulkBatchDownload:
    def setup_method(self):
        login()
        r = client.post("/api/batches", data={
            "whitelist_raw": WHITELIST_RAW,
            "batch_type": BATCH_TYPE,
            "fast_mode": "on" if FAST_MODE else "off",
        })
        self.batch_id = r.json()["batch_id"]

    def test_download_blocked_before_reconciliation(self):
        r = client.get(f"/api/batches/{self.batch_id}/download")
        assert r.status_code in (400, 409, 422)

    def test_download_succeeds_after_full_reconciliation(self):
        d = upload_sub_job(self.batch_id)
        sj_id = d["sub_job_id"]
        confirm_sub_job(self.batch_id, sj_id)
        # Ledger should be reconciled (all 6 tickets claimed by 1 sub-job)
        r = client.get(f"/api/batches/{self.batch_id}")
        ledger = r.json()["ledger"]
        assert ledger["reconciled"], f"Batch not reconciled: {ledger}"
        r2 = client.get(f"/api/batches/{self.batch_id}/download")
        assert r2.status_code == 200
        assert r2.headers.get("content-type", "").startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(r2.content)) as zf:
            assert len(zf.namelist()) > 0


class TestBulkCrossFileDuplicateDetection:
    """Two sub-jobs in the same batch cannot claim the same ticket."""

    def setup_method(self):
        login()
        # Use a 2-ticket whitelist so we can test overlap easily.
        # We use only the first 2 tickets from fixture #6 and set expected_count=1
        # for each sub-job so each file claims exactly 1 ticket.
        # But the stub always injects all 6 blocks — so we use the full whitelist
        # and expected_count=6 for both, then check that the second confirm is
        # rejected because all 6 tickets are already claimed.
        r = client.post("/api/batches", data={
            "whitelist_raw": WHITELIST_RAW,
            "batch_type": BATCH_TYPE,
            "fast_mode": "on",
        })
        self.batch_id = r.json()["batch_id"]

    def test_second_confirm_rejects_duplicate_ticket(self):
        # Sub-job 1: confirm all 6 tickets
        d1 = upload_sub_job(self.batch_id, expected_count=len(GT["whitelist"]))
        sj1 = d1["sub_job_id"]
        confirm_sub_job(self.batch_id, sj1)

        # Sub-job 2: same PDF, same 6 tickets — all already claimed
        d2 = upload_sub_job(self.batch_id, expected_count=len(GT["whitelist"]))
        sj2 = d2["sub_job_id"]
        r = client.post(
            f"/api/batches/{self.batch_id}/sub-jobs/{sj2}/confirm",
            data={},
        )
        # Must be rejected: all tickets already claimed by sj1
        assert r.status_code in (409, 422), (
            f"Expected 409/422 for duplicate ticket claim, got {r.status_code}: {r.text}"
        )
