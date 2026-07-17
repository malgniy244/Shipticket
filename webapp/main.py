"""
main.py — Ship Ticket PDF Splitter Web App
FastAPI backend serving a single-page HTML/JS frontend.

Task 3: login, job creation (whitelist + upload), background processing, status polling.
Task 4: review screen (thumbnails, blocks, flags, reassign/split/merge, Confirm).
Task 5: splitter + ZIP + cleanup.
"""

import asyncio
import base64
import gc
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import struct
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from fastapi import (
    BackgroundTasks,
    Cookie,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader, PdfWriter

# ── Import our detection and grouping modules ─────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from detect import run_detection, run_detection_fast
from grouping import (
    group_detections, repool_from_boundaries, parse_whitelist, HARD_FLAGS,
    FLAG_SECOND_PASS, FLAG_INHERITED_UNMATCHED, FLAG_FUZZY_RESOLVED,
    FLAG_LOW_CONFIDENCE, FLAG_CORRECTION_OBSERVED, FLAG_NON_CONTIGUOUS,
    FLAG_CORRECTION_CONFLICT, FLAG_NOT_READ,
)
from pink_detect import detect_pink_stickers_batch, detect_pink_stickers_batch_debug

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config from environment ───────────────────────────────────────────────────
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
JOB_TTL_SECONDS = 4 * 3600  # 4 hours

# Job data root: persistent disk on Render (/data/sts_jobs), tmpfs locally.
# Set STS_DATA_DIR env var to override. Decision 17.
_default_data_dir = str(Path(tempfile.gettempdir()) / "sts_jobs")
JOBS_ROOT = Path(os.environ.get("STS_DATA_DIR", _default_data_dir))

# Permanent stores — siblings to JOBS_ROOT, never TTL'd.
# On Render: /data/snapshots/ and /data/fixtures/
# Locally: /tmp/snapshots/ and /tmp/fixtures/
_data_parent = JOBS_ROOT.parent
SNAPSHOTS_ROOT = _data_parent / "snapshots"  # confirmed_snapshot JSON per job
FIXTURES_ROOT = _data_parent / "fixtures"    # promoted fixture PDFs + snapshots
BATCHES_ROOT = _data_parent / "batches"      # bulk-mode batch state (permanent)

# ── Asset cache-busting ──────────────────────────────────────────────────────
# Compute a short content-hash for each versioned static asset at startup.
# The hash is injected into index.html as a query-string version parameter so
# browsers always fetch the latest JS/CSS after a deploy.
import hashlib as _hashlib

def _asset_hash(path: Path) -> str:
    """Return first 12 hex chars of the SHA-256 of a file's content."""
    try:
        return _hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except Exception:
        return "dev"

_static_dir = Path(__file__).parent / "static"
_BULK_JS_HASH = _asset_hash(_static_dir / "bulk_patch.js")

# ── FastAPI app ────────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Ship Ticket Splitter")

# ── In-memory job store ────────────────────────────────────────────────────────────────────────────────
# job_id → job dict
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# ── In-memory batch store (Bulk Mode) ─────────────────────────────────────────────
# batch_id → batch dict
batches: dict[str, dict] = {}
batches_lock = threading.Lock()

# ── Session store (HMAC-signed stateless tokens) ─────────────────────────────
# Tokens are self-contained: base64(expiry_u64 || hmac_sha256(secret, expiry_u64))
# No server-side dict — tokens survive process restarts as long as SESSION_SECRET
# is stable (it is set as a Render env var: SESSION_SECRET=sts-render-secret-2026-xk9m).

SESSION_COOKIE = "stsession"
SESSION_TTL = 8 * 3600  # 8 hours


def _session_secret_bytes() -> bytes:
    return SESSION_SECRET.encode()


def create_session() -> str:
    expiry = int(time.time()) + SESSION_TTL
    expiry_bytes = struct.pack(">Q", expiry)  # 8-byte big-endian uint64
    sig = hmac.new(_session_secret_bytes(), expiry_bytes, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(expiry_bytes + sig).decode()


def validate_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        if len(raw) != 8 + 32:  # 8 expiry bytes + 32 HMAC bytes
            return False
        expiry_bytes, sig = raw[:8], raw[8:]
        expected_sig = hmac.new(_session_secret_bytes(), expiry_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected_sig):
            return False
        expiry = struct.unpack(">Q", expiry_bytes)[0]
        return time.time() < expiry
    except Exception:
        return False


def require_session(stsession: Optional[str] = Cookie(default=None)):
    if not validate_session(stsession):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return stsession


# ── Job helpers ───────────────────────────────────────────────────────────────

def make_job(job_id: str, whitelist: list[str], pdf_path: str, total_pages: int, batch_type: str = "tib", fast_mode: bool = False) -> dict:
    return {
        "id": job_id,
        "whitelist": whitelist,
        "pdf_path": pdf_path,
        "total_pages": total_pages,
        "batch_type": batch_type,    # "tib" | "non_tib"
        "fast_mode": fast_mode,      # True = local pink detection + lazy identification
        "status": "queued",          # queued | detecting | grouping | ready | confirmed | error
        "progress_page": 0,
        "checkpoint_path": str(Path(pdf_path).parent / "checkpoint.json"),
        "detection_results": None,
        "grouping_result": None,
        "review_state": None,        # human-editable view of blocks
        "zip_path": None,
        "error": None,
        "created_at": time.time(),
        "thumbnail_dir": str(Path(pdf_path).parent / "thumbs"),
        # Diagnostics (populated during detection)
        "pink_diagnostics": None,    # list of per-page pink detector debug dicts (fast mode only)
        "fast_mode_metrics": None,   # dict of fast-mode API call counts and wall clock
        "confirmed_snapshot": None,  # frozen page→ticket map written at confirm time
        # Live retry/hang status (updated by detect_page callbacks)
        "retry_status": None,        # e.g. "retrying page 15 (attempt 2/5)"
        "last_heartbeat": None,      # epoch float updated each time a page completes
        "pre_boundaries": None,      # pink boundary pages (fast mode only)
        "fast_mode_not_read": 0,     # count of not_read pages (fast mode only)
        # Activity log: last 100 entries, each {ts, msg}
        "log_lines": [],
    }


# ── Job state persistence ────────────────────────────────────────────────────

# Fields that are large/transient and not needed across restarts.
# detection_results can be large (list of per-page dicts); we persist it because
# it is required for repool_job after a restart. All other fields are small.
_NON_SERIALISABLE_FIELDS: set[str] = set()  # nothing excluded — all fields are JSON-safe


def _state_path(job_id: str) -> Path:
    return JOBS_ROOT / job_id / "state.json"


def persist_job(job_id: str):
    """Write the job dict to {job_dir}/state.json atomically.
    
    Called after every mutation so a restart can reload from disk.
    Safe to call with jobs_lock held (does file I/O outside the lock).
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return
    # Shallow copy to avoid holding the lock during I/O
    snapshot = dict(job)
    state_file = _state_path(job_id)
    tmp_file = state_file.with_suffix(".tmp")
    # If detection_results was trimmed from memory (None), preserve the on-disk
    # value so repool can still reload it later.
    if snapshot.get("detection_results") is None and state_file.exists():
        try:
            with open(state_file) as f:
                existing = json.load(f)
            if existing.get("detection_results"):
                snapshot["detection_results"] = existing["detection_results"]
        except Exception:
            pass
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_file, "w") as f:
            json.dump(snapshot, f)
        tmp_file.rename(state_file)
    except Exception as exc:
        log.warning("Job %s: failed to persist state: %s", job_id, exc)


def job_log(job_id: str, msg: str, max_lines: int = 100):
    """Append a timestamped log entry to the job's activity log ring buffer.
    
    Thread-safe. Trims to max_lines. Does NOT call persist_job (too frequent);
    the log is persisted lazily on the next regular persist_job call.
    """
    entry = {"ts": datetime.utcnow().strftime("%H:%M:%S"), "msg": msg}
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            lines = job.setdefault("log_lines", [])
            lines.append(entry)
            if len(lines) > max_lines:
                job["log_lines"] = lines[-max_lines:]


def persist_snapshot(job_id: str, snapshot: dict):
    """Write confirmed_snapshot to the permanent snapshots store.

    This is called at confirm time AND before any cleanup, so snapshots
    survive TTL expiry and service restarts.  The snapshots store is never
    TTL'd — it is permanent.
    """
    try:
        SNAPSHOTS_ROOT.mkdir(parents=True, exist_ok=True)
        snap_path = SNAPSHOTS_ROOT / f"{job_id}.json"
        tmp_path = snap_path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f)
        tmp_path.rename(snap_path)
        log.info("Job %s: confirmed_snapshot persisted to %s", job_id, snap_path)
    except Exception as exc:
        log.warning("Job %s: failed to persist snapshot: %s", job_id, exc)


def load_snapshot(job_id: str) -> Optional[dict]:
    """Load a confirmed_snapshot from the permanent snapshots store, or None."""
    snap_path = SNAPSHOTS_ROOT / f"{job_id}.json"
    if snap_path.exists():
        try:
            with open(snap_path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def cleanup_job(job_id: str):
    """Delete heavy job files and remove from jobs dict.

    Confirmed snapshots are preserved in SNAPSHOTS_ROOT before deletion so
    they survive TTL expiry.  The per-job directory is then fully removed.
    """
    with jobs_lock:
        job = jobs.pop(job_id, None)
    if job:
        # Preserve confirmed_snapshot before deleting files
        snap = job.get("confirmed_snapshot")
        if snap:
            persist_snapshot(job_id, snap)
        pdf_dir = Path(job["pdf_path"]).parent
        if pdf_dir.exists():
            shutil.rmtree(pdf_dir, ignore_errors=True)
        log.info("Job %s cleaned up (snapshot preserved=%s)", job_id, snap is not None)


def schedule_cleanup(job_id: str, delay: float = JOB_TTL_SECONDS):
    """Schedule job cleanup after delay seconds (runs in a daemon thread)."""
    def _cleanup():
        time.sleep(delay)
        cleanup_job(job_id)
    t = threading.Thread(target=_cleanup, daemon=True)
    t.start()


# ── Batch helpers (Bulk Mode — Decision 19) ─────────────────────────────────────────────────────────

def make_batch(
    batch_id: str,
    whitelist: list[str],
    batch_type: str = "tib",
    fast_mode: bool = False,
) -> dict:
    """Create a new batch dict.  Persisted permanently in BATCHES_ROOT."""
    return {
        "id": batch_id,
        "whitelist": whitelist,
        "batch_type": batch_type,
        "fast_mode": fast_mode,
        "created_at": time.time(),
        # sub_jobs: list of sub-job summary dicts (id, status, expected_count, filename)
        "sub_jobs": [],
        # claimed_tickets: {ticket: sub_job_id} — populated only at sub-job confirm
        "claimed_tickets": {},
        # status: open | complete (complete when batch reconciliation passes)
        "status": "open",
        # archived: hidden from default list; set via POST /api/batches/{id}/archive
        "archived": False,
        # deleted: soft-deleted; hard-deleted from disk on DELETE /api/batches/{id}
        "deleted": False,
        # label: optional namespace tag (e.g. "test" for driver-created batches)
        "label": None,
    }


def _batch_state_path(batch_id: str) -> Path:
    return BATCHES_ROOT / batch_id / "state.json"


def persist_batch(batch_id: str):
    """Atomically write batch dict to BATCHES_ROOT/{batch_id}/state.json."""
    with batches_lock:
        batch = batches.get(batch_id)
    if not batch:
        return
    state_file = _batch_state_path(batch_id)
    tmp_file = state_file.with_suffix(".tmp")
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_file, "w") as f:
            json.dump(batch, f)
        tmp_file.rename(state_file)
    except Exception as exc:
        log.warning("Batch %s: failed to persist state: %s", batch_id, exc)


def batch_ledger_summary(batch: dict) -> dict:
    """Compute batch-level reconciliation summary."""
    wl = batch["whitelist"]
    claimed = batch["claimed_tickets"]  # {ticket: sub_job_id}
    claimed_set = set(claimed.keys())
    wl_set = set(wl)
    missing = sorted(wl_set - claimed_set)
    extra = sorted(claimed_set - wl_set)
    # Count confirmed sub-jobs
    confirmed_count = sum(
        1 for sj in batch["sub_jobs"]
        if sj.get("status") == "confirmed"
    )
    return {
        "total_expected": len(wl),
        "total_claimed": len(claimed_set),
        "missing": missing,
        "extra": extra,
        "confirmed_sub_jobs": confirmed_count,
        "reconciled": len(missing) == 0 and len(extra) == 0,
    }


# ── Background detection task ─────────────────────────────────────────────────

def run_detection_background(job_id: str):
    """Run detection + grouping for a job. Updates job dict in place."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return

    try:
        with jobs_lock:
            job["status"] = "detecting"
        persist_job(job_id)
        job_log(job_id, f"Job started — {total_pages if False else job.get('total_pages','?')} pages, {'fast' if job.get('fast_mode') else 'full'} mode")

        pdf_path = job["pdf_path"]
        whitelist = job["whitelist"]
        checkpoint_path = job["checkpoint_path"]
        total_pages = job["total_pages"]
        batch_type = job.get("batch_type", "tib")
        fast_mode = job.get("fast_mode", False)

        # Progress callback: update job["progress_page"] as pages complete.
        # In fast mode, not_read pages are never written to the checkpoint.
        # fast_mode_not_read is set on the job before API calls start so the
        # watcher can add it to the checkpoint count for an accurate display.
        # last_heartbeat is updated every time a checkpoint entry is written;
        # if it hasn't advanced for >90s the frontend shows a stale warning.
        def progress_watcher():
            last_checkpoint_count = 0
            while True:
                with jobs_lock:
                    j = jobs.get(job_id)
                    if not j or j["status"] not in ("detecting",):
                        break
                if Path(checkpoint_path).exists():
                    try:
                        with open(checkpoint_path) as f:
                            entries = json.load(f)
                        checkpoint_entries = len(entries)
                        with jobs_lock:
                            if jobs.get(job_id):
                                not_read_count = jobs[job_id].get("fast_mode_not_read", 0)
                                jobs[job_id]["progress_page"] = checkpoint_entries + not_read_count
                                # Update heartbeat whenever a new page completes
                                if checkpoint_entries > last_checkpoint_count:
                                    jobs[job_id]["last_heartbeat"] = time.time()
                                    jobs[job_id]["retry_status"] = None  # clear any stale retry msg
                                    last_checkpoint_count = checkpoint_entries
                    except Exception:
                        pass
                time.sleep(1)

        watcher = threading.Thread(target=progress_watcher, daemon=True)
        watcher.start()
        # Set initial heartbeat so the frontend can detect a hang from the start
        with jobs_lock:
            if jobs.get(job_id):
                jobs[job_id]["last_heartbeat"] = time.time()

        if fast_mode:
            # Fast mode: local pink detection for boundaries, then lazy identification
            log.info("Job %s: fast mode — running local pink detection for boundaries", job_id)
            doc = fitz.open(pdf_path)
            page_images = [
                doc[i].get_pixmap(matrix=fitz.Matrix(150 / 72, 150 / 72), colorspace=fitz.csRGB).tobytes("jpeg")
                for i in range(len(doc))
            ]
            doc.close()
            # Use debug variant to capture per-page scores
            pink_debug_results = detect_pink_stickers_batch_debug(page_images)
            pink_flags = [r["detected"] for r in pink_debug_results]
            # Annotate each debug result with its 1-indexed page number
            for i, dbg in enumerate(pink_debug_results):
                dbg["page"] = i + 1
            # pre_boundaries: 1-indexed page numbers where pink sticker detected
            pre_boundaries = [i + 1 for i, flag in enumerate(pink_flags) if flag]
            log.info("Job %s: pink boundaries detected on pages %s", job_id, pre_boundaries)
            if pre_boundaries:
                job_log(job_id, f"Pink stickers found on {len(pre_boundaries)} page(s): {pre_boundaries}")
            else:
                job_log(job_id, "No pink stickers detected — will read all pages")
            with jobs_lock:
                if jobs.get(job_id):
                    jobs[job_id]["pink_diagnostics"] = pink_debug_results
                    jobs[job_id]["pre_boundaries"] = pre_boundaries
            persist_job(job_id)
            def _set_not_read_count(count: int):
                with jobs_lock:
                    if jobs.get(job_id):
                        jobs[job_id]["fast_mode_not_read"] = count

            detection_results, fast_metrics = run_detection_fast(
                pdf_path=pdf_path,
                pre_boundaries=pre_boundaries,
                checkpoint_path=checkpoint_path,
                workers=5,
                whitelist=whitelist,
                progress_callback=_set_not_read_count,
            )
            with jobs_lock:
                if jobs.get(job_id):
                    jobs[job_id]["fast_mode_metrics"] = fast_metrics
            persist_job(job_id)
            api_total = fast_metrics.get('api_calls_total', '?')
            wall = fast_metrics.get('wall_clock_seconds', '?')
            not_read = fast_metrics.get('not_read_count', '?')
            job_log(job_id, f"Detection complete — {api_total} API calls, {not_read} pages skipped, {wall:.1f}s" if isinstance(wall, float) else f"Detection complete — {api_total} API calls, {not_read} pages skipped")
            log.info("Job %s: fast mode metrics: %s", job_id, fast_metrics)
        else:
            # Full mode: run detection on all pages
            def _set_retry_status(msg: str):
                with jobs_lock:
                    if jobs.get(job_id):
                        jobs[job_id]["retry_status"] = msg

            detection_results = run_detection(
                pdf_path=pdf_path,
                checkpoint_path=checkpoint_path,
                workers=5,
                whitelist=whitelist,
                retry_callback=_set_retry_status,
            )

        with jobs_lock:
            job["detection_results"] = detection_results
            job["progress_page"] = total_pages
            job["status"] = "grouping"
        persist_job(job_id)
        job_log(job_id, "Grouping pages into blocks…")

        # Run grouping
        # In fast mode, pass the pink-sticker boundaries as forced block starts
        # so Phase A shows the correct proposed boundaries (not identity-derived ones).
        with jobs_lock:
            _pre_boundaries = jobs.get(job_id, {}).get("pre_boundaries") if fast_mode else None
        grouping_result = group_detections(
            detection_results, whitelist,
            batch_type=batch_type,
            pre_boundaries=_pre_boundaries,
        )

        # Build review_state from grouping result (with per_page decisions for filmstrip)
        review_state = build_review_state(grouping_result, whitelist, detection_results)

        with jobs_lock:
            job["grouping_result"] = {
                "missing_tickets": grouping_result.missing_tickets,
                "unmatched_values": grouping_result.unmatched_values,
                "total_pages": grouping_result.total_pages,
            }
            job["review_state"] = review_state
            job["status"] = "ready"
        persist_job(job_id)

        n_blocks = len(review_state["blocks"])
        missing = review_state.get("missing_tickets", [])
        job_log(job_id, f"Ready — {n_blocks} block(s) found" + (f", {len(missing)} ticket(s) missing" if missing else ", all tickets matched"))
        log.info("Job %s: detection+grouping complete, %d blocks", job_id, n_blocks)

        # Trim large intermediate data from in-memory job to reduce RSS.
        # detection_results is already persisted to state.json and reloaded on demand.
        with jobs_lock:
            if jobs.get(job_id):
                jobs[job_id]["detection_results"] = None
        gc.collect()

        # Generate thumbnails in background (after detection closes its doc)
        generate_thumbnails(job_id, pdf_path, job["thumbnail_dir"])

    except Exception as exc:
        log.exception("Job %s failed: %s", job_id, exc)
        job_log(job_id, f"Error: {exc}")
        with jobs_lock:
            if jobs.get(job_id):
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = str(exc)
        persist_job(job_id)


def build_review_state(
    grouping_result,
    whitelist: list[str],
    detection_results: list[dict] | None = None,
) -> dict:
    """Convert a GroupingResult into a JSON-serialisable review state dict.
    
    If detection_results is provided, also builds per_page_decisions for the
    filmstrip view (red/black classification, source, confidence per page).
    """
    blocks = []
    for i, b in enumerate(grouping_result.blocks):
        blocks.append({
            "id": i,
            "ticket": b.ticket,
            "pages": b.pages,
            "flags": b.flags,
            "detection_sources": b.detection_sources,
            "max_confidence": b.max_confidence,
            "unmatched_raw": b.unmatched_raw,
            "corrected_from": b.corrected_from,
            "suggestion": b.suggestion,
            "neighbor_suggestion": b.neighbor_suggestion,
            "page_range": _page_range_str(b.pages),
            # Hard flag check
            "has_hard_flag": any(f in HARD_FLAGS for f in b.flags),
        })

    # Build per_page_decisions for filmstrip red/black classification.
    # A page is RED (decision point) if:
    #   - it is the first page of its block (block start), OR
    #   - it has at least one detection candidate, OR
    #   - it carries any flag (hard or soft active-decision flags)
    # A page is BLACK (inherited) if it has no candidates and is not a block start.
    SOFT_RED_FLAGS = {
        FLAG_SECOND_PASS, FLAG_INHERITED_UNMATCHED, FLAG_FUZZY_RESOLVED,
        FLAG_LOW_CONFIDENCE, FLAG_CORRECTION_OBSERVED, FLAG_NON_CONTIGUOUS,
    }
    per_page: dict[int, dict] = {}
    if detection_results:
        # Build a lookup: page_num → raw detection result
        det_by_page = {r["page"]: r for r in detection_results}
        # Build a lookup: page_num → block
        page_to_block: dict[int, dict] = {}
        for b in blocks:
            for p in b["pages"]:
                page_to_block[p] = b
        # Determine block starts
        block_starts: set[int] = set()
        for b in blocks:
            if b["pages"]:
                block_starts.add(min(b["pages"]))
        total = grouping_result.total_pages
        for page_num in range(1, total + 1):
            det = det_by_page.get(page_num, {"candidates": []})
            candidates = det.get("candidates", [])
            has_candidates = bool(candidates)
            is_block_start = page_num in block_starts
            blk = page_to_block.get(page_num)
            blk_flags = blk["flags"] if blk else []
            # A page is red if it is a block start, has candidates, or has any active flag
            has_active_flag = any(f in HARD_FLAGS or f in SOFT_RED_FLAGS for f in blk_flags)
            is_red = is_block_start or has_candidates or has_active_flag
            # Source and confidence from the best candidate on this page
            # Check if this page was not_read (fast mode)
            is_not_read = det.get("not_read", False)
            if candidates:
                best = max(candidates, key=lambda c: c.get("confidence", 0))
                source = best.get("source", "")
                confidence = best.get("confidence", 0.0)
                is_second_pass = best.get("second_pass", False)
                source_display = "second-pass" if is_second_pass else source
            elif is_not_read:
                source_display = "not_read"
                confidence = 0.0
            elif is_block_start and blk:
                # Block start with no direct detection — inherited
                source_display = "inherited"
                confidence = 0.0
            else:
                source_display = "inherited"
                confidence = 0.0
            per_page[page_num] = {
                "is_red": is_red,
                "source": source_display,
                "confidence": confidence,
                "is_block_start": is_block_start,
                "is_not_read": is_not_read,
            }

    return {
        "blocks": blocks,
        "whitelist": whitelist,
        "missing_tickets": grouping_result.missing_tickets,
        "unmatched_values": grouping_result.unmatched_values,
        "total_pages": grouping_result.total_pages,
        "per_page": per_page,
    }


def generate_thumbnails(job_id: str, pdf_path: str, thumb_dir: str):
    """Render all pages as small JPEG thumbnails (72 DPI) and full-res images (150 DPI).

    Thumbnails go to {thumb_dir}/p{n}.jpg (72 DPI, used in Phase A strip and reconcile).
    Full-res images go to {thumb_dir}/../pages/p{n}.jpg (150 DPI, used in Phase B viewer).
    Pre-generating full-res images eliminates on-demand render latency and prevents
    blank images when the PDF is temporarily unavailable (e.g. during a restart).
    """
    try:
        thumb_path = Path(thumb_dir)
        thumb_path.mkdir(parents=True, exist_ok=True)
        pages_path = thumb_path.parent / "pages"
        pages_path.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(pdf_path)
        n = len(doc)
        for i in range(n):
            pg = doc[i]
            # 72 DPI thumbnail
            mat_thumb = fitz.Matrix(72 / 72, 72 / 72)
            pix = pg.get_pixmap(matrix=mat_thumb, colorspace=fitz.csRGB)
            pix.save(str(thumb_path / f"p{i+1}.jpg"))
            # 150 DPI full-res
            mat_full = fitz.Matrix(150 / 72, 150 / 72)
            pix_full = pg.get_pixmap(matrix=mat_full, colorspace=fitz.csRGB)
            pix_full.save(str(pages_path / f"p{i+1}.jpg"))
        doc.close()
        log.info("Job %s: pre-generated %d thumbnails + %d full-res images", job_id, n, n)
    except Exception as exc:
        log.warning("Job %s: image pre-generation failed: %s", job_id, exc)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve index.html with cache-busted asset URLs injected."""
    html = Path(__file__).parent / "static" / "index.html"
    content = html.read_text()
    # Replace the placeholder hash with the actual content hash computed at startup
    content = content.replace("__BULK_JS_HASH__", _BULK_JS_HASH)
    return HTMLResponse(content=content, status_code=200)


@app.post("/api/login")
async def login(response: Response, password: str = Form(...)):
    if password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")
    token = create_session()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL,
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response, _=Depends(require_session)):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(stsession: Optional[str] = Cookie(default=None)):
    return {"authenticated": validate_session(stsession)}


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    whitelist_raw: str = Form(...),
    file: UploadFile = File(...),
    batch_type: str = Form(default="tib"),
    fast_mode: str = Form(default="off"),
    _=Depends(require_session),
):
    """Create a new job: parse whitelist, save PDF, start background detection."""
    # Validate batch_type
    if batch_type not in ("tib", "non_tib"):
        batch_type = "tib"  # default gracefully
    # Parse fast_mode (checkbox sends "on" when checked, absent otherwise)
    fast_mode_bool = fast_mode.lower() in ("on", "true", "1", "yes")
    # Parse whitelist
    try:
        whitelist = parse_whitelist(whitelist_raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not whitelist:
        raise HTTPException(status_code=422, detail="Whitelist is empty")

    # Save uploaded PDF to the persistent job directory
    job_id = str(uuid.uuid4())
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = str(job_dir / "input.pdf")

    # Stream upload to disk
    with open(pdf_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1 MB chunks
            f.write(chunk)

    # Get page count
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=f"Could not open PDF: {e}")

    # Create job
    job = make_job(job_id, whitelist, pdf_path, total_pages, batch_type=batch_type, fast_mode=fast_mode_bool)
    with jobs_lock:
        jobs[job_id] = job
    persist_job(job_id)  # write initial state to disk immediately

    # Schedule cleanup after 24h
    schedule_cleanup(job_id, JOB_TTL_SECONDS)

    # Start background detection
    background_tasks.add_task(run_detection_background, job_id)

    return {"job_id": job_id, "total_pages": total_pages, "whitelist": whitelist, "batch_type": batch_type, "fast_mode": fast_mode_bool}


@app.get("/api/jobs/{job_id}/status")
async def job_status(job_id: str, _=Depends(require_session)):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job["status"],
        "progress_page": min(job["progress_page"], job["total_pages"]),
        "total_pages": job["total_pages"],
        "error": job["error"],
        "retry_status": job.get("retry_status"),       # e.g. "retrying page 15 (attempt 2/5)"
        "last_heartbeat": job.get("last_heartbeat"),   # epoch float; None if not started yet
        "fast_mode": job.get("fast_mode", False),
        "log_lines": job.get("log_lines", []),         # activity log ring buffer
    }


@app.get("/api/jobs/{job_id}/review")
async def get_review(job_id: str, _=Depends(require_session)):
    """Return the current review state (blocks + flags)."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("ready", "confirmed"):
        raise HTTPException(status_code=409, detail=f"Job not ready (status={job['status']})")
    return job["review_state"]


@app.get("/api/jobs/{job_id}/thumbnail/{page}")
async def get_thumbnail(job_id: str, page: int, _=Depends(require_session)):
    """Return a thumbnail JPEG for a specific page."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    thumb_path = Path(job["thumbnail_dir"]) / f"p{page}.jpg"
    if not thumb_path.exists():
        # Generate on demand if not ready yet
        try:
            doc = fitz.open(job["pdf_path"])
            pg = doc[page - 1]
            mat = fitz.Matrix(72 / 72, 72 / 72)
            pix = pg.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            Path(job["thumbnail_dir"]).mkdir(parents=True, exist_ok=True)
            pix.save(str(thumb_path))
            doc.close()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Thumbnail error: {e}")
    return FileResponse(str(thumb_path), media_type="image/jpeg")


@app.get("/api/jobs/{job_id}/page/{page}/image")
async def get_page_image(job_id: str, page: int, _=Depends(require_session)):
    """Return a full-size JPEG for a specific page (Phase B viewer).

    Serves from the pre-generated pages/ cache if available (fast, no PDF needed).
    Falls back to on-demand render from the PDF if the cache file is missing.
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Try pre-generated cache first
    cached = Path(job["thumbnail_dir"]).parent / "pages" / f"p{page}.jpg"
    if cached.exists():
        return FileResponse(str(cached), media_type="image/jpeg")
    # Fall back to on-demand render
    try:
        doc = fitz.open(job["pdf_path"])
        pg = doc[page - 1]
        mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI
        pix = pg.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        jpeg_bytes = pix.tobytes("jpeg")
        doc.close()
        # Save to cache for future requests
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(jpeg_bytes)
        except Exception:
            pass
        return Response(content=jpeg_bytes, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Page render error: {e}")


@app.post("/api/jobs/{job_id}/page/{page}/second-pass")
async def on_demand_second_pass(job_id: str, page: int, _=Depends(require_session)):
    """
    Run the whitelist-constrained vision query for a specific page on demand.
    Returns a candidate dict if a whitelist member is found, or {matched: null}.
    This is a suggestion only — the human still confirms any assignment.
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("ready",):
        raise HTTPException(status_code=409, detail="Job not ready")

    whitelist = job["whitelist"]
    pdf_path = job["pdf_path"]

    # Import here to avoid circular at module level
    from detect import render_page_to_jpeg, detect_page_second_pass, DEFAULT_MODEL
    from openai import OpenAI

    try:
        doc = fitz.open(pdf_path)
        jpeg_bytes = render_page_to_jpeg(doc, page - 1, dpi=150)
        doc.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Render error: {e}")

    client = OpenAI()
    candidate = detect_page_second_pass(client, page, jpeg_bytes, whitelist, DEFAULT_MODEL)

    if candidate:
        return {"matched": candidate["value"], "source": candidate["source"], "confidence": candidate["confidence"]}
    return {"matched": None}


@app.patch("/api/jobs/{job_id}/review")
async def update_review(
    job_id: str,
    action: str = Form(...),
    block_id: Optional[int] = Form(default=None),
    ticket: Optional[str] = Form(default=None),
    split_after_page: Optional[int] = Form(default=None),
    block_id_a: Optional[int] = Form(default=None),
    block_id_b: Optional[int] = Form(default=None),
    block_id_from: Optional[int] = Form(default=None),
    block_id_to: Optional[int] = Form(default=None),
    page: Optional[int] = Form(default=None),
    _=Depends(require_session),
):
    """
    Apply a human edit to the review state.
    Form fields: action, block_id, ticket, split_after_page, block_id_a, block_id_b, etc.
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("ready",):
        raise HTTPException(status_code=409, detail="Job not in editable state")
    review = job["review_state"]
    if action == "reassign":
        if block_id is None:
            raise HTTPException(status_code=422, detail="block_id required")
        new_ticket = ticket if ticket else None
        # Validate ticket is in whitelist or None
        if new_ticket is not None and new_ticket not in review["whitelist"]:
            raise HTTPException(status_code=422, detail="Ticket not in whitelist")
        for b in review["blocks"]:
            if b["id"] == block_id:
                b["ticket"] = new_ticket
                # Clear hard flags that are now resolved
                b["flags"] = [f for f in b["flags"] if f not in HARD_FLAGS]
                b["has_hard_flag"] = False
                break
        else:
            raise HTTPException(status_code=404, detail="Block not found")
    elif action == "split":
        if block_id is None or split_after_page is None:
            raise HTTPException(status_code=422, detail="block_id and split_after_page required")
        _apply_split(review, block_id, split_after_page)
    elif action == "merge":
        if block_id_a is None or block_id_b is None:
            raise HTTPException(status_code=422, detail="block_id_a and block_id_b required")
        _apply_merge(review, block_id_a, block_id_b)
    elif action == "move_page":
        if block_id_from is None or block_id_to is None or page is None:
            raise HTTPException(status_code=422, detail="block_id_from, block_id_to, and page required")
        _apply_move_page(review, block_id_from, block_id_to, page)
    else:
        raise HTTPException(status_code=422, detail=f"Unknown action: {action}")
    # Recompute has_hard_flag for all blocks
    for b in review["blocks"]:
        b["has_hard_flag"] = any(f in HARD_FLAGS for f in b["flags"])
    # Recompute missing_tickets (any whitelist ticket with no assigned block)
    wl = review.get("whitelist", [])
    assigned = {b["ticket"] for b in review["blocks"] if b["ticket"]}
    review["missing_tickets"] = [t for t in wl if t not in assigned]
    persist_job(job_id)
    return review


def _apply_split(review: dict, block_id: int, split_after_page: int):
    """Split a block at split_after_page: pages ≤ split_after_page stay, rest become new block."""
    blocks = review["blocks"]
    idx = next((i for i, b in enumerate(blocks) if b["id"] == block_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Block not found")
    b = blocks[idx]
    pages = sorted(b["pages"])
    if split_after_page not in pages or split_after_page == pages[-1]:
        raise HTTPException(status_code=422, detail="Invalid split point")
    split_idx = pages.index(split_after_page)
    pages_a = pages[:split_idx + 1]
    pages_b = pages[split_idx + 1:]
    # New block id
    new_id = max(bb["id"] for bb in blocks) + 1
    b["pages"] = pages_a
    b["page_range"] = _page_range_str(pages_a)
    new_block = {
        "id": new_id,
        "ticket": b["ticket"],
        "pages": pages_b,
        "flags": [],
        "detection_sources": [],
        "max_confidence": 0.0,
        "unmatched_raw": [],
        "corrected_from": None,
        "suggestion": None,
        "neighbor_suggestion": None,
        "page_range": _page_range_str(pages_b),
        "has_hard_flag": False,
    }
    blocks.insert(idx + 1, new_block)


def _apply_merge(review: dict, block_id_a: int, block_id_b: int):
    """Merge two blocks into one (block_a absorbs block_b)."""
    blocks = review["blocks"]
    idx_a = next((i for i, b in enumerate(blocks) if b["id"] == block_id_a), None)
    idx_b = next((i for i, b in enumerate(blocks) if b["id"] == block_id_b), None)
    if idx_a is None or idx_b is None:
        raise HTTPException(status_code=404, detail="Block not found")
    ba = blocks[idx_a]
    bb = blocks[idx_b]
    merged_pages = sorted(set(ba["pages"]) | set(bb["pages"]))
    ba["pages"] = merged_pages
    ba["page_range"] = _page_range_str(merged_pages)
    ba["flags"] = list(set(ba["flags"]) | set(bb["flags"]))
    blocks.pop(idx_b)


def _apply_move_page(review: dict, block_id_from: int, block_id_to: int, page_num: int):
    """Move a single page from one block to an adjacent block."""
    blocks = review["blocks"]
    bf = next((b for b in blocks if b["id"] == block_id_from), None)
    bt = next((b for b in blocks if b["id"] == block_id_to), None)
    if bf is None or bt is None:
        raise HTTPException(status_code=404, detail="Block not found")
    if page_num not in bf["pages"]:
        raise HTTPException(status_code=422, detail="Page not in source block")
    bf["pages"] = sorted(p for p in bf["pages"] if p != page_num)
    bf["page_range"] = _page_range_str(bf["pages"])
    bt["pages"] = sorted(bt["pages"] + [page_num])
    bt["page_range"] = _page_range_str(bt["pages"])
    # Remove empty blocks
    review["blocks"] = [b for b in blocks if b["pages"]]


def _page_range_str(pages: list[int]) -> str:
    """Format a list of page numbers as a human-readable range string.
    Contiguous runs are shown as 'X–Y', non-contiguous runs are comma-separated.
    Examples: [1,2,3] -> '1–3', [1,2,4,5] -> '1–2, 4–5', [7] -> '7'
    """
    if not pages:
        return ""
    pages = sorted(set(pages))
    if len(pages) == 1:
        return str(pages[0])
    # Group into contiguous runs
    runs = []
    run_start = pages[0]
    run_end = pages[0]
    for p in pages[1:]:
        if p == run_end + 1:
            run_end = p
        else:
            runs.append((run_start, run_end))
            run_start = run_end = p
    runs.append((run_start, run_end))
    parts = []
    for s, e in runs:
        parts.append(str(s) if s == e else f"{s}–{e}")
    return ", ".join(parts)


@app.post("/api/jobs/{job_id}/repool")
async def repool_job(job_id: str, boundaries_str: str = Form(..., alias="b"), _=Depends(require_session)):
    """
    Phase A → Phase B: Given confirmed block boundaries, re-pool all detections
    within each block and return updated block identities + flags.

    Body (form): b=1,5,9,12,15  (comma-separated block start pages, 1-indexed)

    This is the same code path used by Phase B and the final Confirm.
    The client fires this on boundary drop (debounced) and on "Confirm boundaries".
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("ready", "confirmed"):
        raise HTTPException(status_code=409, detail=f"Job not ready (status={job['status']})")

    try:
        boundaries = [int(x.strip()) for x in boundaries_str.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="boundaries must be comma-separated integers")
    if not boundaries:
        raise HTTPException(status_code=422, detail="boundaries list is required")

    detection_results = job["detection_results"]
    whitelist = job["whitelist"]
    batch_type = job.get("batch_type", "tib")

    if not detection_results:
        # detection_results was trimmed from memory after grouping to save RAM.
        # Reload from the persisted state.json on disk.
        try:
            state_path = _state_path(job_id)
            with open(state_path) as f:
                saved = json.load(f)
            detection_results = saved.get("detection_results")
        except Exception:
            pass
    if not detection_results:
        raise HTTPException(status_code=409, detail="Detection results not available")

    # Run the same grouping engine code path as the initial grouping
    grouping_result = repool_from_boundaries(detection_results, whitelist, boundaries, batch_type=batch_type)

    # Build review state using the same build_review_state function
    review_state = build_review_state(grouping_result, whitelist, detection_results)

    # Store the updated review state
    with jobs_lock:
        if jobs.get(job_id):
            jobs[job_id]["review_state"] = review_state
            jobs[job_id]["grouping_result"] = {
                "missing_tickets": grouping_result.missing_tickets,
                "unmatched_values": grouping_result.unmatched_values,
                "total_pages": grouping_result.total_pages,
            }
    persist_job(job_id)

    return review_state


@app.post("/api/jobs/{job_id}/confirm")
async def confirm_job(job_id: str, _=Depends(require_session)):
    """
    Validate that all hard flags are resolved, then split the PDF and build the ZIP.
    Returns the ZIP as a streaming download.
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("ready",):
        raise HTTPException(status_code=409, detail="Job not ready to confirm")

    review = job["review_state"]
    blocks = review["blocks"]
    wl: list[str] = review.get("whitelist", job.get("whitelist", []))

    # ── Server-side reconciliation checks (mirrors the client reconciliation screen) ──
    # Check 1: no unresolved hard flags / unassigned blocks
    unresolved = [b for b in blocks if b["has_hard_flag"] or b["ticket"] is None]
    if unresolved:
        raise HTTPException(
            status_code=422,
            detail=f"{len(unresolved)} block(s) still have unresolved flags or no ticket assigned",
        )

    # Check 2: no missing tickets (every whitelist number has at least one block)
    assigned_tickets: set[str] = {b["ticket"] for b in blocks if b["ticket"]}
    missing = [t for t in wl if t not in assigned_tickets]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing tickets not assigned to any block: {', '.join(missing)}",
        )

    # Check 3: no duplicates (no whitelist ticket assigned to more than one block)
    from collections import Counter
    ticket_counts = Counter(b["ticket"] for b in blocks if b["ticket"])
    duplicates = [t for t, c in ticket_counts.items() if c > 1 and t in set(wl)]
    if duplicates:
        raise HTTPException(
            status_code=422,
            detail=f"Tickets assigned to multiple blocks (merge or reassign): {', '.join(duplicates)}",
        )

    # Check 4: no extras (no block carries a ticket outside the whitelist)
    extras = [b["ticket"] for b in blocks if b["ticket"] and b["ticket"] not in set(wl)]
    if extras:
        raise HTTPException(
            status_code=422,
            detail=f"Blocks carry ticket numbers not in whitelist: {', '.join(set(extras))}",
        )

    # Build ZIP
    try:
        zip_path = _build_zip(job, review)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ZIP build failed: {e}")

    # Build confirmed_snapshot: frozen page→ticket mapping for fixture ground truth
    confirmed_snapshot = {
        "job_id": job_id,
        "confirmed_at": datetime.now().isoformat(),
        "whitelist": wl,
        "batch_type": job.get("batch_type", "tib"),
        "fast_mode": job.get("fast_mode", False),
        "total_pages": job.get("total_pages"),
        # page_map: {"1": "301532", "2": "301532", ...}
        # per_page is a dict[int, dict] keyed by page number
        "page_map": {
            str(page_num): next(
                (b["ticket"] for b in blocks if page_num in b.get("pages", [])),
                None,
            )
            for page_num in review.get("per_page", {})
        },
        # blocks: [{ticket, pages, flags}, ...]
        "blocks": [
            {
                "ticket": b["ticket"],
                "pages": b.get("pages", []),
                "flags": b.get("flags", []),
            }
            for b in blocks
        ],
    }

    with jobs_lock:
        job["zip_path"] = zip_path
        job["status"] = "confirmed"
        job["confirmed_snapshot"] = confirmed_snapshot
    persist_job(job_id)

    # Persist snapshot to permanent store immediately — before any TTL cleanup
    # can fire.  This ensures the snapshot survives even if the 30-second
    # post-download cleanup runs before the user navigates away.
    persist_snapshot(job_id, confirmed_snapshot)

    # Schedule cleanup after download (give 30 seconds to download)
    schedule_cleanup(job_id, delay=30)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"tickets_{timestamp}.zip"

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_zip(job: dict, review: dict) -> str:
    """Split PDF by blocks and pack into a ZIP. Returns ZIP path.
    
    Blocks with the same ticket number are merged into a single PDF
    (pages in page-number order) before writing to the ZIP.
    Filename contract: {ticketnumber}.pdf — LOCKED.
    """
    pdf_path = job["pdf_path"]
    job_dir = Path(pdf_path).parent
    zip_path = str(job_dir / "tickets.zip")

    reader = PdfReader(pdf_path)

    # Collect all pages per ticket (merging blocks with the same ticket)
    ticket_pages: dict[str, list[int]] = {}
    for block in review["blocks"]:
        ticket = block["ticket"]
        if not ticket:
            continue  # skip unassigned (should not happen post-confirm)
        if ticket not in ticket_pages:
            ticket_pages[ticket] = []
        ticket_pages[ticket].extend(block["pages"])

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for ticket, pages in ticket_pages.items():
            pages = sorted(set(pages))  # deduplicate and sort

            writer = PdfWriter()
            for page_num in pages:
                writer.add_page(reader.pages[page_num - 1])  # 0-indexed

            pdf_bytes = io.BytesIO()
            writer.write(pdf_bytes)
            pdf_bytes.seek(0)

            # Filename contract: {ticketnumber}.pdf — LOCKED
            filename = f"{ticket}.pdf"
            zf.writestr(filename, pdf_bytes.read())
            log.info("ZIP: added %s (%d pages: %s)", filename, len(pages), pages)

    return zip_path


# ── Diagnostics endpoint ───────────────────────────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/diagnostics")
async def get_diagnostics(job_id: str, _=Depends(require_session)):
    """
    Return pink detector diagnostics, fast-mode metrics, and confirmed snapshot for a job.

    Available after detection completes (status=ready or confirmed).
    pink_diagnostics: list of per-page dicts with blob area, fill ratio, hue range, etc.
    fast_mode_metrics: API call counts, block ranges, wall clock (fast mode only).
    confirmed_snapshot: frozen page→ticket mapping (available after confirm).
    """
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("ready", "confirmed"):
        raise HTTPException(status_code=409, detail=f"Job not ready (status={job['status']})")
    return {
        "job_id": job_id,
        "status": job["status"],
        "fast_mode": job.get("fast_mode", False),
        "pink_diagnostics": job.get("pink_diagnostics"),
        "fast_mode_metrics": job.get("fast_mode_metrics"),
        "confirmed_snapshot": job.get("confirmed_snapshot"),
    }


# ── Bulk Mode routes (Decision 19) ───────────────────────────────────────────
from webapp.batch_routes import _register_batch_routes
_register_batch_routes(
    app,
    jobs=jobs,
    jobs_lock=jobs_lock,
    batches=batches,
    batches_lock=batches_lock,
    JOBS_ROOT=JOBS_ROOT,
    BATCHES_ROOT=BATCHES_ROOT,
    require_session=require_session,
    make_job=make_job,
    persist_job=persist_job,
    persist_batch=persist_batch,
    batch_ledger_summary=batch_ledger_summary,
    schedule_cleanup=schedule_cleanup,
    JOB_TTL_SECONDS=JOB_TTL_SECONDS,
    parse_whitelist=parse_whitelist,
    run_detection_background=run_detection_background,
    persist_snapshot=persist_snapshot,
    build_zip=_build_zip,
)

# ── Static files (HTML/JS/CSS) ────────────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Startup / shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    log.info("Ship Ticket Splitter starting up — JOBS_ROOT=%s", JOBS_ROOT)
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_ROOT.mkdir(parents=True, exist_ok=True)
    FIXTURES_ROOT.mkdir(parents=True, exist_ok=True)
    BATCHES_ROOT.mkdir(parents=True, exist_ok=True)
    log.info("Permanent stores: snapshots=%s fixtures=%s batches=%s", SNAPSHOTS_ROOT, FIXTURES_ROOT, BATCHES_ROOT)

    # Reload persisted jobs from disk.
    # Skip jobs that have expired (created_at + JOB_TTL_SECONDS < now).
    # Skip jobs whose PDF is missing (file was deleted externally).
    # Jobs that were mid-detection when the service restarted are set to
    # status='error' with a message so the user knows to re-submit.
    now = time.time()
    reloaded = 0
    expired = 0
    for state_file in JOBS_ROOT.glob("*/state.json"):
        try:
            with open(state_file) as f:
                job = json.load(f)
            job_id = job.get("id")
            if not job_id:
                continue
            # Expire check
            created_at = job.get("created_at", 0)
            if now - created_at > JOB_TTL_SECONDS:
                # Preserve confirmed_snapshot before deleting the directory
                snap = job.get("confirmed_snapshot")
                if snap:
                    persist_snapshot(job_id, snap)
                log.info("Startup: expiring old job %s (snapshot preserved=%s)", job_id, snap is not None)
                shutil.rmtree(state_file.parent, ignore_errors=True)
                expired += 1
                continue
            # PDF existence check
            if not Path(job.get("pdf_path", "")).exists():
                log.warning("Startup: job %s PDF missing, skipping", job_id)
                continue
            # Jobs that were mid-detection are unrecoverable — mark error
            if job.get("status") in ("queued", "detecting", "grouping"):
                job["status"] = "error"
                job["error"] = "Service restarted during detection — please re-submit this job."
                # Persist the updated error state
                tmp = state_file.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(job, f)
                tmp.rename(state_file)
            # Strip detection_results from the in-memory copy to keep RSS low.
            # It stays intact on disk (state.json) and is reloaded on demand by repool_job.
            job["detection_results"] = None
            with jobs_lock:
                jobs[job_id] = job
            reloaded += 1
            log.info("Startup: reloaded job %s (status=%s)", job_id, job.get("status"))
        except Exception as exc:
            log.warning("Startup: failed to reload %s: %s", state_file, exc)

    log.info("Startup complete: %d jobs reloaded, %d expired", reloaded, expired)

    # Reload persisted batches from BATCHES_ROOT (permanent, never TTL'd).
    batch_reloaded = 0
    for batch_state_file in BATCHES_ROOT.glob("*/state.json"):
        try:
            with open(batch_state_file) as f:
                batch = json.load(f)
            batch_id = batch.get("id")
            if not batch_id:
                continue
            # Skip hard-deleted batches (should not exist on disk, but defensive)
            if batch.get("deleted"):
                log.info("Startup: skipping deleted batch %s", batch_id)
                continue
            # Back-fill new fields for batches created before schema update
            batch.setdefault("archived", False)
            batch.setdefault("deleted", False)
            batch.setdefault("label", None)
            with batches_lock:
                batches[batch_id] = batch
            batch_reloaded += 1
            log.info("Startup: reloaded batch %s (status=%s, sub_jobs=%d, archived=%s)",
                     batch_id, batch.get("status"), len(batch.get("sub_jobs", [])),
                     batch.get("archived"))
        except Exception as exc:
            log.warning("Startup: failed to reload batch %s: %s", batch_state_file, exc)
    log.info("Startup complete: %d batches reloaded", batch_reloaded)


@app.get("/api/admin/jobs-list")
async def admin_list_jobs(_=Depends(require_session)):
    """List all job directories on disk plus all permanent snapshots."""
    result = []
    # Live jobs from JOBS_ROOT
    if JOBS_ROOT.exists():
        for job_dir in sorted(JOBS_ROOT.iterdir()):
            state_path = job_dir / "state.json"
            if state_path.exists():
                try:
                    data = json.loads(state_path.read_text())
                    snap = data.get("confirmed_snapshot")
                    result.append({
                        "source": "jobs",
                        "job_id": job_dir.name,
                        "status": data.get("status"),
                        "total_pages": data.get("total_pages"),
                        "whitelist": data.get("whitelist", []),
                        "has_confirmed_snapshot": snap is not None,
                        "fast_mode": data.get("fast_mode"),
                        "created_at": data.get("created_at"),
                    })
                except Exception as e:
                    result.append({"source": "jobs", "job_id": job_dir.name, "error": str(e)})
    # Permanent snapshots from SNAPSHOTS_ROOT
    if SNAPSHOTS_ROOT.exists():
        for snap_file in sorted(SNAPSHOTS_ROOT.glob("*.json")):
            job_id = snap_file.stem
            try:
                snap = json.loads(snap_file.read_text())
                result.append({
                    "source": "snapshots",
                    "job_id": job_id,
                    "status": "confirmed",
                    "total_pages": snap.get("total_pages"),
                    "whitelist": snap.get("whitelist", []),
                    "has_confirmed_snapshot": True,
                    "fast_mode": snap.get("fast_mode"),
                    "confirmed_at": snap.get("confirmed_at"),
                })
            except Exception as e:
                result.append({"source": "snapshots", "job_id": job_id, "error": str(e)})
    return result


@app.get("/api/admin/job-state/{job_id}")
async def admin_read_job_state(job_id: str, _=Depends(require_session)):
    """Read state.json from disk for any job, or confirmed_snapshot from the permanent store."""
    if not re.match(r'^[0-9a-f-]{36}$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    # Try live job state first
    state_path = JOBS_ROOT / job_id / "state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    # Fall back to permanent snapshot store
    snap = load_snapshot(job_id)
    if snap:
        return {"source": "snapshots", "confirmed_snapshot": snap, "job_id": job_id}
    raise HTTPException(status_code=404, detail=f"No state.json or snapshot found for job {job_id}")


@app.post("/api/admin/promote-fixture/{job_id}")
async def admin_promote_fixture(job_id: str, _=Depends(require_session)):
    """Promote a confirmed job to the permanent fixtures directory.

    Copies the confirmed_snapshot JSON and the original PDF (input.pdf) into
    FIXTURES_ROOT/{job_id}/.  The PDF copy is required so the fixture can be
    re-run through the detection engine for regression testing.

    If the job directory has already been cleaned up, the snapshot is loaded
    from the permanent snapshots store and the PDF copy is skipped (the caller
    must supply the PDF separately if needed).
    """
    if not re.match(r'^[0-9a-f-]{36}$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")

    # Resolve snapshot: live job > snapshots store
    with jobs_lock:
        live_job = jobs.get(job_id)
    snap = None
    pdf_src: Optional[Path] = None

    if live_job:
        snap = live_job.get("confirmed_snapshot")
        if live_job.get("pdf_path"):
            pdf_src = Path(live_job["pdf_path"])
    if snap is None:
        # Try state.json on disk
        state_path = JOBS_ROOT / job_id / "state.json"
        if state_path.exists():
            data = json.loads(state_path.read_text())
            snap = data.get("confirmed_snapshot")
            if data.get("pdf_path"):
                pdf_src = Path(data["pdf_path"])
    if snap is None:
        snap = load_snapshot(job_id)

    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=f"No confirmed_snapshot found for job {job_id} in live jobs, state.json, or snapshots store",
        )

    # Write to fixtures directory
    fixture_dir = FIXTURES_ROOT / job_id
    fixture_dir.mkdir(parents=True, exist_ok=True)

    snap_dest = fixture_dir / "snapshot.json"
    tmp = snap_dest.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=2)
    tmp.rename(snap_dest)

    pdf_copied = False
    pdf_dest = fixture_dir / "input.pdf"
    if pdf_src and pdf_src.exists():
        shutil.copy2(pdf_src, pdf_dest)
        pdf_copied = True

    log.info("Fixture promoted: job=%s pdf_copied=%s dest=%s", job_id, pdf_copied, fixture_dir)
    return {
        "ok": True,
        "job_id": job_id,
        "fixture_dir": str(fixture_dir),
        "snapshot_written": str(snap_dest),
        "pdf_copied": pdf_copied,
        "pdf_dest": str(pdf_dest) if pdf_copied else None,
        "snapshot_summary": {
            "total_pages": snap.get("total_pages"),
            "whitelist": snap.get("whitelist", []),
            "block_count": len(snap.get("blocks", [])),
            "confirmed_at": snap.get("confirmed_at"),
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
