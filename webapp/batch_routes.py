"""
batch_routes.py — Bulk Mode API endpoints (Decision 19).

Registered into the main FastAPI app via include_router.
All endpoints are additive — single-file TIB/non-TIB flows are untouched.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import time
import uuid
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import fitz
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse
from pypdf import PdfReader, PdfWriter

log = logging.getLogger(__name__)

# These are imported from main at registration time (see main.py include_router call).
# They are injected via module-level assignment in _register_batch_routes().
_jobs: dict = {}
_jobs_lock = None
_batches: dict = {}
_batches_lock = None
_JOBS_ROOT: Path = None
_BATCHES_ROOT: Path = None
_require_session = None
_make_job = None
_persist_job = None
_persist_batch = None
_batch_ledger_summary = None
_schedule_cleanup = None
_JOB_TTL_SECONDS: float = 4 * 3600
_parse_whitelist = None
_run_detection_background = None
_persist_snapshot = None
_build_zip = None

# Test hook: if set, this callable is used instead of run_detection_background.
# Set this in tests to inject deterministic review state without real API calls.
_detection_fn_override = None


router = APIRouter(prefix="/api/batches", tags=["bulk"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_batch(batch_id: str) -> dict:
    with _batches_lock:
        batch = _batches.get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
    return batch


def _get_sub_job(batch: dict, sub_job_id: str) -> dict:
    """Return the sub-job summary entry from the batch's sub_jobs list."""
    for sj in batch["sub_jobs"]:
        if sj["id"] == sub_job_id:
            return sj
    raise HTTPException(status_code=404, detail=f"Sub-job {sub_job_id} not in batch {batch['id']}")


def _sub_job_summary(job: dict) -> dict:
    """Build the sub-job summary dict stored in batch['sub_jobs']."""
    return {
        "id": job["id"],
        "status": job["status"],
        "expected_count": job.get("expected_count", 0),
        "filename": job.get("original_filename", ""),
        "total_pages": job.get("total_pages"),
        "batch_id": job.get("batch_id"),
    }


def _update_sub_job_summary(batch: dict, job: dict):
    """Refresh the sub-job summary in batch['sub_jobs'] from the live job dict."""
    new_summary = _sub_job_summary(job)
    for i, sj in enumerate(batch["sub_jobs"]):
        if sj["id"] == job["id"]:
            batch["sub_jobs"][i] = new_summary
            return
    batch["sub_jobs"].append(new_summary)


def _batch_response(batch: dict) -> dict:
    """Build the full batch status response payload."""
    ledger = _batch_ledger_summary(batch)
    return {
        "batch_id": batch["id"],
        "status": batch["status"],
        "batch_type": batch["batch_type"],
        "fast_mode": batch["fast_mode"],
        "whitelist": batch["whitelist"],
        "created_at": batch["created_at"],
        "sub_jobs": batch["sub_jobs"],
        "ledger": ledger,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("")
async def create_batch(
    whitelist_raw: str = Form(...),
    batch_type: str = Form(default="tib"),
    fast_mode: str = Form(default="off"),
    _=Depends(lambda: None),  # replaced at registration
):
    """Create a new batch with the full whitelist."""
    raise NotImplementedError("replaced at registration")


@router.get("/{batch_id}")
async def get_batch(batch_id: str, _=Depends(lambda: None)):
    """Return batch status, sub-job list, and ledger summary."""
    raise NotImplementedError("replaced at registration")


@router.post("/{batch_id}/sub-jobs")
async def add_sub_job(
    batch_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    expected_count: int = Form(...),
    _=Depends(lambda: None),
):
    """Upload a PDF into a batch, creating a new sub-job."""
    raise NotImplementedError("replaced at registration")


@router.post("/{batch_id}/sub-jobs/{sub_job_id}/confirm")
async def confirm_sub_job(batch_id: str, sub_job_id: str, _=Depends(lambda: None)):
    """Confirm a sub-job: run per-file reconciliation + cross-file duplicate check."""
    raise NotImplementedError("replaced at registration")


@router.post("/{batch_id}/sub-jobs/{sub_job_id}/abandon")
async def abandon_sub_job(batch_id: str, sub_job_id: str, _=Depends(lambda: None)):
    """Mark a sub-job as abandoned (no ticket release needed — none were claimed)."""
    raise NotImplementedError("replaced at registration")


@router.post("/{batch_id}/sub-jobs/{sub_job_id}/unconfirm")
async def unconfirm_sub_job(batch_id: str, sub_job_id: str, _=Depends(lambda: None)):
    """Un-confirm a sub-job: release its tickets back to the batch pool."""
    raise NotImplementedError("replaced at registration")


@router.get("/{batch_id}/download")
async def download_batch(batch_id: str, _=Depends(lambda: None)):
    """Download the whole-batch ZIP (gates on batch-level reconciliation)."""
    raise NotImplementedError("replaced at registration")


# ── Real implementations injected at registration ─────────────────────────────

def _register_batch_routes(app, *, jobs, jobs_lock, batches, batches_lock,
                            JOBS_ROOT, BATCHES_ROOT, require_session,
                            make_job, persist_job, persist_batch,
                            batch_ledger_summary, schedule_cleanup,
                            JOB_TTL_SECONDS, parse_whitelist,
                            run_detection_background, persist_snapshot,
                            build_zip, load_snapshot=None):
    """
    Register all batch routes onto the FastAPI app with real implementations.
    Called from main.py after all helpers are defined.
    """
    from fastapi import APIRouter as _APIRouter

    r = _APIRouter(prefix="/api/batches", tags=["bulk"])

    def _rs():
        return require_session

    # ── POST /api/batches ─────────────────────────────────────────────────────

    @r.post("")
    async def _create_batch(
        whitelist_raw: str = Form(...),
        batch_type: str = Form(...),
        fast_mode: str = Form(default="off"),
        label: Optional[str] = Form(default=None),
        _=Depends(require_session),
    ):
        if batch_type not in ("tib", "non_tib"):
            raise HTTPException(status_code=422, detail="batch_type must be 'tib' or 'non_tib'")
        fast_mode_bool = fast_mode.lower() in ("on", "true", "1", "yes")
        try:
            whitelist = parse_whitelist(whitelist_raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if not whitelist:
            raise HTTPException(status_code=422, detail="Whitelist is empty")

        batch_id = str(uuid.uuid4())
        batch = {
            "id": batch_id,
            "whitelist": whitelist,
            "batch_type": batch_type,
            "fast_mode": fast_mode_bool,
            "created_at": time.time(),
            "sub_jobs": [],
            "claimed_tickets": {},
            "status": "open",
            "archived": False,
            "deleted": False,
            "label": label or None,
        }
        with batches_lock:
            batches[batch_id] = batch
        persist_batch(batch_id)
        log.info("Batch %s created (whitelist=%d tickets, label=%s)", batch_id, len(whitelist), label)
        return {
            "batch_id": batch_id,
            "whitelist": whitelist,
            "whitelist_count": len(whitelist),
            "batch_type": batch_type,
            "fast_mode": fast_mode_bool,
            "label": label or None,
        }

    # ── GET /api/batches ──────────────────────────────────────────────────────

    @r.get("")
    async def _list_batches(
        show_archived: str = "0",
        show_test: str = "0",
        _=Depends(require_session),
    ):
        with batches_lock:
            batch_list = list(batches.values())
        include_archived = show_archived in ("1", "true", "yes")
        include_test = show_test in ("1", "true", "yes")
        result = []
        for b in batch_list:
            if b.get("deleted"):
                continue
            if b.get("archived") and not include_archived:
                continue
            if b.get("label") == "test" and not include_test:
                continue
            result.append({
                "batch_id": b["id"],
                "status": b["status"],
                "batch_type": b["batch_type"],
                "whitelist_count": len(b["whitelist"]),
                "sub_job_count": len(b["sub_jobs"]),
                "sub_jobs": b["sub_jobs"],
                "created_at": b["created_at"],
                "archived": b.get("archived", False),
                "label": b.get("label"),
                "ledger": batch_ledger_summary(b),
            })
        return result

    # ── GET /api/batches/{batch_id} ───────────────────────────────────────────

    @r.get("/{batch_id}")
    async def _get_batch(batch_id: str, _=Depends(require_session)):
        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
        ledger = batch_ledger_summary(batch)
        return {
            "batch_id": batch["id"],
            "status": batch["status"],
            "batch_type": batch["batch_type"],
            "fast_mode": batch["fast_mode"],
            "whitelist": batch["whitelist"],
            "whitelist_count": len(batch["whitelist"]),
            "sub_job_count": len(batch["sub_jobs"]),
            "created_at": batch["created_at"],
            "sub_jobs": batch["sub_jobs"],
            "ledger": ledger,
        }

    # ── POST /api/batches/{batch_id}/sub-jobs ─────────────────────────────────

    @r.post("/{batch_id}/sub-jobs")
    async def _add_sub_job(
        batch_id: str,
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        expected_count: int = Form(...),
        _=Depends(require_session),
    ):
        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
        if batch["status"] == "complete":
            raise HTTPException(status_code=409, detail="Batch is already complete")

        if expected_count < 1:
            raise HTTPException(status_code=422, detail="expected_count must be >= 1")

        # Save PDF
        sub_job_id = str(uuid.uuid4())
        job_dir = JOBS_ROOT / sub_job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = str(job_dir / "input.pdf")
        with open(pdf_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)

        # Page count
        try:
            doc = fitz.open(pdf_path)
            total_pages = len(doc)
            doc.close()
        except Exception as e:
            import shutil as _shutil
            _shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=422, detail=f"Could not open PDF: {e}")

        # Create sub-job (normal job + batch_id + expected_count fields)
        job = make_job(
            sub_job_id,
            batch["whitelist"],
            pdf_path,
            total_pages,
            batch_type=batch["batch_type"],
            fast_mode=batch["fast_mode"],
        )
        job["batch_id"] = batch_id
        job["expected_count"] = expected_count
        job["original_filename"] = file.filename or "input.pdf"

        with jobs_lock:
            jobs[sub_job_id] = job
        persist_job(sub_job_id)
        schedule_cleanup(sub_job_id, JOB_TTL_SECONDS)

        # Register sub-job in batch
        sj_summary = {
            "id": sub_job_id,
            "status": "queued",
            "expected_count": expected_count,
            "filename": job["original_filename"],
            "total_pages": total_pages,
            "batch_id": batch_id,
        }
        with batches_lock:
            batch["sub_jobs"].append(sj_summary)
        persist_batch(batch_id)

        import webapp.batch_routes as _br
        _detect_fn = _br._detection_fn_override if _br._detection_fn_override is not None else run_detection_background
        background_tasks.add_task(_detect_fn, sub_job_id)
        log.info("Sub-job %s added to batch %s (expected=%d pages=%d)",
                 sub_job_id, batch_id, expected_count, total_pages)
        return {
            "sub_job_id": sub_job_id,
            "batch_id": batch_id,
            "total_pages": total_pages,
            "expected_count": expected_count,
            "whitelist": batch["whitelist"],
            "batch_type": batch["batch_type"],
            "fast_mode": batch["fast_mode"],
        }

    # ── POST /api/batches/{batch_id}/sub-jobs/{sub_job_id}/confirm ────────────

    @r.post("/{batch_id}/sub-jobs/{sub_job_id}/confirm")
    async def _confirm_sub_job(
        batch_id: str,
        sub_job_id: str,
        _=Depends(require_session),
    ):
        """
        Confirm a sub-job.

        Per-file reconciliation checks (mirrors single-file confirm):
          1. No unresolved hard flags / unassigned blocks
          2. No missing tickets (relative to this file's assigned set)
          3. No within-file duplicates
          4. No extras outside whitelist
          5. Expected-count check: confirmed ticket count == expected_count

        Cross-file duplicate check:
          6. None of this file's tickets are already claimed by another confirmed sub-job.

        On success: tickets are claimed in the batch ledger, sub-job status → confirmed,
        per-file ZIP is built, confirmed_snapshot is persisted.
        """
        from collections import Counter as _Counter

        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

        with jobs_lock:
            job = jobs.get(sub_job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Sub-job {sub_job_id} not found")
        if job.get("batch_id") != batch_id:
            raise HTTPException(status_code=409, detail="Sub-job does not belong to this batch")
        if job["status"] not in ("ready",):
            raise HTTPException(status_code=409, detail=f"Sub-job not ready to confirm (status={job['status']})")

        review = job["review_state"]
        blocks = review["blocks"]
        wl: list[str] = batch["whitelist"]

        # Check 1: no unresolved hard flags / unassigned blocks
        unresolved = [b for b in blocks if b["has_hard_flag"] or b["ticket"] is None]
        if unresolved:
            raise HTTPException(
                status_code=422,
                detail=f"{len(unresolved)} block(s) still have unresolved flags or no ticket assigned",
            )

        # Check 2: no extras outside whitelist
        wl_set = set(wl)
        extras = [b["ticket"] for b in blocks if b["ticket"] and b["ticket"] not in wl_set]
        if extras:
            raise HTTPException(
                status_code=422,
                detail=f"Blocks carry ticket numbers not in whitelist: {', '.join(set(extras))}",
            )

        # Check 3: no within-file duplicates
        ticket_counts = _Counter(b["ticket"] for b in blocks if b["ticket"])
        duplicates = [t for t, c in ticket_counts.items() if c > 1 and t in wl_set]
        if duplicates:
            raise HTTPException(
                status_code=422,
                detail=f"Tickets assigned to multiple blocks within this file: {', '.join(duplicates)}",
            )

        # Check 4: expected-count check
        assigned_tickets = {b["ticket"] for b in blocks if b["ticket"]}
        expected_count = job.get("expected_count", 0)
        if len(assigned_tickets) != expected_count:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Expected {expected_count} ticket(s) but confirmed {len(assigned_tickets)}. "
                    f"Adjust blocks or update expected count before confirming."
                ),
            )

        # Check 5: cross-file duplicate check (against batch ledger)
        with batches_lock:
            claimed = batch["claimed_tickets"]  # {ticket: sub_job_id}
        cross_dupes = []
        for t in assigned_tickets:
            owner = claimed.get(t)
            if owner and owner != sub_job_id:
                cross_dupes.append((t, owner))
        if cross_dupes:
            detail_parts = [f"{t} (owned by sub-job {owner[:8]}…)" for t, owner in cross_dupes]
            raise HTTPException(
                status_code=422,
                detail=f"Cross-file duplicate tickets: {', '.join(detail_parts)}",
            )

        # Build per-file ZIP
        try:
            zip_path = build_zip(job, review)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"ZIP build failed: {e}")

        # Build confirmed_snapshot
        confirmed_snapshot = {
            "job_id": sub_job_id,
            "batch_id": batch_id,
            "confirmed_at": datetime.now().isoformat(),
            "whitelist": list(assigned_tickets),
            "batch_type": job.get("batch_type", "tib"),
            "fast_mode": job.get("fast_mode", False),
            "total_pages": job.get("total_pages"),
            "expected_count": expected_count,
            "page_map": {
                str(page_num): next(
                    (b["ticket"] for b in blocks if page_num in b.get("pages", [])),
                    None,
                )
                for page_num in review.get("per_page", {})
            },
            "blocks": [
                {"ticket": b["ticket"], "pages": b.get("pages", []), "flags": b.get("flags", [])}
                for b in blocks
            ],
        }

        # Claim tickets in batch ledger + update sub-job status
        with batches_lock:
            for t in assigned_tickets:
                batch["claimed_tickets"][t] = sub_job_id
            # Update sub-job summary in batch
            for i, sj in enumerate(batch["sub_jobs"]):
                if sj["id"] == sub_job_id:
                    batch["sub_jobs"][i]["status"] = "confirmed"
                    break

        with jobs_lock:
            job["zip_path"] = zip_path
            job["status"] = "confirmed"
            job["confirmed_snapshot"] = confirmed_snapshot

        persist_job(sub_job_id)
        persist_snapshot(sub_job_id, confirmed_snapshot)
        persist_batch(batch_id)

        # Copy the per-sub-job ZIP into the batch directory so it survives job cleanup.
        # The batch download endpoint reads from BATCHES_ROOT/{batch_id}/{sub_job_id}.zip.
        batch_sj_zip_path = BATCHES_ROOT / batch_id / f"{sub_job_id}.zip"
        try:
            batch_sj_zip_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(zip_path, batch_sj_zip_path)
            with jobs_lock:
                job["batch_zip_path"] = str(batch_sj_zip_path)
        except Exception as exc:
            log.warning("Sub-job %s: failed to copy ZIP to batch dir: %s", sub_job_id, exc)

        # Schedule cleanup of heavy files (snapshot already persisted).
        # Use a long TTL (24 h) so the original ZIP is still available if the
        # batch-level copy fails for any reason.
        schedule_cleanup(sub_job_id, delay=86400)

        log.info("Sub-job %s confirmed in batch %s (tickets=%s)",
                 sub_job_id, batch_id, sorted(assigned_tickets))

        # Return JSON — the ZIP is stored at job["zip_path"] and served by the batch download endpoint.
        # Per-file download is not available for bulk sub-jobs.
        return {
            "status": "confirmed",
            "sub_job_id": sub_job_id,
            "batch_id": batch_id,
            "tickets_claimed": sorted(assigned_tickets),
            "ledger": batch_ledger_summary(batch),
        }

    # ── POST /api/batches/{batch_id}/sub-jobs/{sub_job_id}/abandon ────────────

    @r.post("/{batch_id}/sub-jobs/{sub_job_id}/abandon")
    async def _abandon_sub_job(
        batch_id: str,
        sub_job_id: str,
        _=Depends(require_session),
    ):
        """
        Mark a sub-job as abandoned.
        No ticket release needed — tickets are only claimed at confirm time.
        """
        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

        with jobs_lock:
            job = jobs.get(sub_job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Sub-job {sub_job_id} not found")
        if job.get("batch_id") != batch_id:
            raise HTTPException(status_code=409, detail="Sub-job does not belong to this batch")
        if job["status"] == "confirmed":
            raise HTTPException(
                status_code=409,
                detail="Cannot abandon a confirmed sub-job — use unconfirm first",
            )

        with jobs_lock:
            job["status"] = "abandoned"
        with batches_lock:
            for i, sj in enumerate(batch["sub_jobs"]):
                if sj["id"] == sub_job_id:
                    batch["sub_jobs"][i]["status"] = "abandoned"
                    break

        persist_job(sub_job_id)
        persist_batch(batch_id)
        log.info("Sub-job %s abandoned in batch %s", sub_job_id, batch_id)
        return {"ok": True, "sub_job_id": sub_job_id, "status": "abandoned"}

    # ── POST /api/batches/{batch_id}/sub-jobs/{sub_job_id}/unconfirm ──────────

    @r.post("/{batch_id}/sub-jobs/{sub_job_id}/unconfirm")
    async def _unconfirm_sub_job(
        batch_id: str,
        sub_job_id: str,
        _=Depends(require_session),
    ):
        """
        Un-confirm a sub-job: release its tickets back to the batch pool.
        Returns the sub-job to 'ready' state for re-review.
        This is the only reversal operation in the design.
        """
        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

        # Verify this sub-job belongs to this batch (check batch sub_jobs list)
        sj_summary = next((sj for sj in batch.get("sub_jobs", []) if sj["id"] == sub_job_id), None)
        if not sj_summary:
            raise HTTPException(status_code=404, detail=f"Sub-job {sub_job_id} not found in batch {batch_id}")
        if sj_summary.get("status") != "confirmed":
            raise HTTPException(
                status_code=409,
                detail=f"Sub-job is not confirmed (status={sj_summary.get('status')})",
            )

        # Get the ticket list from in-memory job or persisted snapshot
        with jobs_lock:
            job = jobs.get(sub_job_id)
        snap = None
        if job:
            snap = job.get("confirmed_snapshot") or {}
        if not snap and load_snapshot:
            snap = load_snapshot(sub_job_id) or {}
        released_tickets = list(snap.get("whitelist", []))

        # Release tickets from ledger
        with batches_lock:
            for t in released_tickets:
                if batch["claimed_tickets"].get(t) == sub_job_id:
                    del batch["claimed_tickets"][t]
            for i, sj in enumerate(batch["sub_jobs"]):
                if sj["id"] == sub_job_id:
                    # If job is still in memory it can be re-reviewed; otherwise mark abandoned
                    # so the user knows they need to re-upload
                    new_status = "ready" if job else "abandoned"
                    batch["sub_jobs"][i]["status"] = new_status
                    break
            # If batch was complete, reopen it
            if batch["status"] == "complete":
                batch["status"] = "open"

        # Also delete the batch-level ZIP copy so it isn't included in future downloads
        batch_sj_zip = BATCHES_ROOT / batch_id / f"{sub_job_id}.zip"
        if batch_sj_zip.exists():
            try:
                batch_sj_zip.unlink()
            except Exception:
                pass

        if job:
            with jobs_lock:
                job["status"] = "ready"
                job["confirmed_snapshot"] = None
                job["zip_path"] = None
            persist_job(sub_job_id)

        persist_batch(batch_id)
        final_status = "ready" if job else "abandoned"
        log.info("Sub-job %s un-confirmed in batch %s (released tickets: %s, new_status: %s)",
                 sub_job_id, batch_id, released_tickets, final_status)
        return {
            "ok": True,
            "sub_job_id": sub_job_id,
            "status": final_status,
            "released_tickets": released_tickets,
        }

    # ── DELETE /api/batches/{batch_id} ───────────────────────────────────────

    @r.delete("/{batch_id}")
    async def _delete_batch(batch_id: str, _=Depends(require_session)):
        """
        Hard-delete a batch.  Only allowed if the batch has zero confirmed sub-jobs.
        Removes from memory and from disk (BATCHES_ROOT/{batch_id}/).
        """
        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

        confirmed_count = sum(
            1 for sj in batch["sub_jobs"] if sj.get("status") == "confirmed"
        )
        if confirmed_count > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete batch with {confirmed_count} confirmed sub-job(s). "
                    "Un-confirm or abandon all confirmed sub-jobs first."
                ),
            )

        # Remove from memory
        with batches_lock:
            batches.pop(batch_id, None)

        # Remove from disk
        batch_dir = BATCHES_ROOT / batch_id
        if batch_dir.exists():
            shutil.rmtree(batch_dir, ignore_errors=True)

        log.info("Batch %s hard-deleted", batch_id)
        return {"ok": True, "batch_id": batch_id, "deleted": True}

    # ── POST /api/batches/{batch_id}/archive ──────────────────────────────────

    @r.post("/{batch_id}/archive")
    async def _archive_batch(batch_id: str, _=Depends(require_session)):
        """
        Archive a batch: hidden from the default list but not deleted.
        Also removes the per-sub-job ZIP copies from the batch directory to
        free disk space (the batch state.json is preserved for audit).
        Can be reversed via POST /api/batches/{id}/unarchive.
        """
        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
        with batches_lock:
            batches[batch_id]["archived"] = True
        persist_batch(batch_id)

        # Delete per-sub-job ZIP copies to free disk space
        batch_dir = BATCHES_ROOT / batch_id
        freed_bytes = 0
        for sj in batch.get("sub_jobs", []):
            sj_zip = batch_dir / f"{sj['id']}.zip"
            if sj_zip.exists():
                try:
                    freed_bytes += sj_zip.stat().st_size
                    sj_zip.unlink()
                except Exception as exc:
                    log.warning("Batch %s: failed to delete sub-job ZIP %s: %s", batch_id, sj_zip, exc)
        # Also remove the merged batch_tickets.zip if present
        merged_zip = batch_dir / "batch_tickets.zip"
        if merged_zip.exists():
            try:
                freed_bytes += merged_zip.stat().st_size
                merged_zip.unlink()
            except Exception:
                pass
        log.info("Batch %s archived (freed ~%d KB of ZIP files)", batch_id, freed_bytes // 1024)
        return {"ok": True, "batch_id": batch_id, "archived": True}

    @r.post("/{batch_id}/unarchive")
    async def _unarchive_batch(batch_id: str, _=Depends(require_session)):
        """Un-archive a batch: makes it visible in the default list again."""
        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
        with batches_lock:
            batches[batch_id]["archived"] = False
        persist_batch(batch_id)
        log.info("Batch %s un-archived", batch_id)
        return {"ok": True, "batch_id": batch_id, "archived": False}

    # ── GET /api/batches/{batch_id}/download ──────────────────────────────────

    @r.get("/{batch_id}/download")
    async def _download_batch(batch_id: str, _=Depends(require_session)):
        """
        Download the whole-batch ZIP.
        Gates on batch-level reconciliation: every whitelist ticket must be
        assigned exactly once across all confirmed sub-jobs, none missing,
        none duplicated.
        """
        with batches_lock:
            batch = batches.get(batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

        ledger = batch_ledger_summary(batch)
        if not ledger["reconciled"]:
            missing = ledger["missing"]
            extra = ledger["extra"]
            detail = "Batch not reconciled."
            if missing:
                detail += f" Missing tickets: {', '.join(missing[:10])}{'…' if len(missing) > 10 else ''}."
            if extra:
                detail += f" Extra tickets: {', '.join(extra[:10])}{'…' if len(extra) > 10 else ''}."
            raise HTTPException(status_code=422, detail=detail)

        # Build whole-batch ZIP: collect per-sub-job ZIPs and merge all ticket PDFs
        batch_dir = BATCHES_ROOT / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_zip_path = str(batch_dir / "batch_tickets.zip")

        with zipfile.ZipFile(batch_zip_path, "w", zipfile.ZIP_DEFLATED) as batch_zf:
            for sj in batch["sub_jobs"]:
                if sj.get("status") != "confirmed":
                    continue
                sj_id = sj["id"]
                # Prefer the copy stored in the batch directory (survives job cleanup).
                # Fall back to the live job's zip_path if the batch copy isn't there yet.
                candidate_paths = [
                    str(batch_dir / f"{sj_id}.zip"),  # batch-level copy (primary)
                ]
                with jobs_lock:
                    job = jobs.get(sj_id)
                if job:
                    live_zip = job.get("zip_path") or job.get("batch_zip_path")
                    if live_zip:
                        candidate_paths.append(live_zip)
                zip_path = next((p for p in candidate_paths if Path(p).exists()), None)
                if not zip_path:
                    log.warning("Batch %s: no ZIP found for confirmed sub-job %s (searched: %s)",
                                batch_id, sj_id, candidate_paths)
                    continue
                # Add all PDFs from this sub-job's ZIP into the batch ZIP
                try:
                    with zipfile.ZipFile(zip_path, "r") as sub_zf:
                        for name in sub_zf.namelist():
                            batch_zf.writestr(name, sub_zf.read(name))
                except Exception as exc:
                    log.warning("Batch %s: failed to read sub-job %s ZIP: %s",
                                batch_id, sj_id, exc)

        # Mark batch complete
        with batches_lock:
            batch["status"] = "complete"
        persist_batch(batch_id)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"batch_{batch_id[:8]}_{timestamp}.zip"
        return FileResponse(
            batch_zip_path,
            media_type="application/zip",
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    app.include_router(r)
    log.info("Bulk Mode batch routes registered (/api/batches/…)")
