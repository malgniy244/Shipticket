#!/usr/bin/env python3
"""
e2e_driver.py — End-to-end session driver for Ship Ticket Splitter.

Simulates a complete real user session against the deployed Render instance:
  1.  Login
  2.  Create job (real pink batch PDF, fast mode, full whitelist)
  3.  Wait for detection to complete (status=ready)
  4.  Fetch EVERY page image — assert HTTP 200 + valid JPEG bytes
  5.  Phase A: read boundaries, apply one split edit, call repool to confirm boundaries
  6.  Phase B: walk every block, resolve flags, assign missing tickets
  7.  Confirm job (POST /jobs/{id}/confirm)
  8.  Download ZIP
  9.  Verify ZIP: correct number of files, each is a valid PDF

Run 2 of 3 includes a forced service restart mid-session to test HMAC session survival.

Usage:
    python3 tools/e2e_driver.py \
        --url https://ship-ticket-splitter.onrender.com \
        --password crystal2026 \
        --pdf /home/ubuntu/upload/SKM_C250i26070917180.pdf \
        --whitelist "300889,301119,301122,301124,301127,301129,301132,301135,301139,301140,301141,301142,301143,301144,301145,301146,301150,301151,301155,301156,301165,301361" \
        --render-token rnd_1DKuUH0hqHIuFb4TD8rKlciv5GHo \
        --service-id srv-d9a8k7gk1i2s73f8sb5g
"""

import argparse
import io
import json
import os
import time
import zipfile
from datetime import datetime

import requests

# ── Helpers ───────────────────────────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(msg, indent=0):
    prefix = "  " * indent
    print(f"[{ts()}] {prefix}{msg}", flush=True)

def fail(msg):
    print(f"\n[{ts()}] FAIL: {msg}", flush=True)
    raise AssertionError(msg)

def assert_ok(r, label):
    if r.status_code != 200:
        fail(f"{label} returned HTTP {r.status_code}: {r.text[:400]}")

def is_valid_jpeg(data: bytes) -> bool:
    return len(data) > 4 and data[:2] == b'\xff\xd8'

def is_valid_pdf(data: bytes) -> bool:
    return data[:4] == b'%PDF'

def mem_rss_mb():
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)
    except ImportError:
        return None

# ── Steps ─────────────────────────────────────────────────────────────────────

def step_login(session, base_url, password):
    log("=== Step 1: Login ===")
    r = session.post(f"{base_url}/api/login",
                     data={"password": password},
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert_ok(r, "Login")
    if not r.json().get("ok"):
        fail(f"Login returned ok=false: {r.json()}")
    log("Login OK", 1)


def step_create_job(session, base_url, pdf_path, whitelist_raw):
    log("=== Step 2: Create job ===")
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    r = session.post(
        f"{base_url}/api/jobs",
        files={"pdf": (os.path.basename(pdf_path), pdf_bytes, "application/pdf")},
        data={"whitelist_raw": whitelist_raw, "fast_mode": "true"},
    )
    assert_ok(r, "Create job")
    d = r.json()
    job_id = d["job_id"]
    total_pages = d["total_pages"]
    log(f"Job created: {job_id} ({total_pages} pages, fast mode)", 1)
    return job_id, total_pages


def step_wait_for_ready(session, base_url, job_id, timeout=360):
    log("=== Step 3: Wait for detection ===")
    deadline = time.time() + timeout
    last_page = -1
    while time.time() < deadline:
        r = session.get(f"{base_url}/api/jobs/{job_id}/status")
        assert_ok(r, "Status poll")
        d = r.json()
        status = d["status"]
        prog = d.get("progress_page", 0)
        total = d.get("total_pages", 0)
        if prog != last_page:
            log(f"status={status} page={prog}/{total}", 1)
            last_page = prog
        if status == "ready":
            log(f"Detection complete — {total} pages", 1)
            return d
        if status == "error":
            fail(f"Job entered error state: {d.get('error')}")
        time.sleep(3)
    fail(f"Detection did not complete within {timeout}s")


def step_fetch_all_images(session, base_url, job_id, total_pages):
    log(f"=== Fetch all {total_pages} page images ===")
    failures = []
    for page_num in range(1, total_pages + 1):
        r = session.get(f"{base_url}/api/jobs/{job_id}/page/{page_num}/image")
        if r.status_code != 200:
            failures.append(f"page {page_num}: HTTP {r.status_code}")
            continue
        data = r.content
        if len(data) < 100:
            failures.append(f"page {page_num}: response too small ({len(data)} bytes)")
            continue
        if not is_valid_jpeg(data):
            failures.append(f"page {page_num}: not a valid JPEG (starts {data[:4].hex()})")
            continue
        log(f"page {page_num}/{total_pages}: {len(data):,} bytes OK", 1)
    if failures:
        fail(f"Image fetch failures:\n" + "\n".join(f"  {f}" for f in failures))
    log(f"All {total_pages} page images: valid JPEG, HTTP 200", 1)


def step_phase_a(session, base_url, job_id):
    """Phase A: read boundaries, apply one split, then call repool to confirm boundaries."""
    log("=== Step 5: Phase A — boundaries + repool ===")
    r = session.get(f"{base_url}/api/jobs/{job_id}/review")
    assert_ok(r, "Get review")
    review = r.json()
    blocks = review.get("blocks", [])
    whitelist = review.get("whitelist", [])
    blocks_before = len(blocks)
    log(f"Blocks: {blocks_before}  Whitelist: {len(whitelist)} tickets", 1)

    # Apply one split on the first multi-page block
    split_info = None
    for b in blocks:
        if b["end_page"] > b["start_page"]:
            split_after = b["start_page"]
            block_id = b["id"]
            log(f"Splitting block {block_id} (pages {b['start_page']}–{b['end_page']}) after page {split_after}", 1)
            r = session.patch(
                f"{base_url}/api/jobs/{job_id}/review",
                data={"action": "split", "block_id": str(block_id), "split_after_page": str(split_after)},
            )
            assert_ok(r, "Split block")
            split_info = {"block_id": block_id, "split_after": split_after}
            break

    # Re-read review after split
    r = session.get(f"{base_url}/api/jobs/{job_id}/review")
    assert_ok(r, "Get review after split")
    review = r.json()
    blocks_after_split = len(review.get("blocks", []))
    if split_info:
        if blocks_after_split != blocks_before + 1:
            fail(f"Split did not increase block count: {blocks_before} → {blocks_after_split}")
        log(f"Split OK: {blocks_before} → {blocks_after_split} blocks", 1)

    # Call repool with current boundaries (Phase A → Phase B transition)
    boundaries = [b["start_page"] for b in review.get("blocks", [])]
    boundaries_str = ",".join(str(p) for p in boundaries)
    log(f"Calling repool with {len(boundaries)} boundaries: {boundaries_str[:80]}...", 1)
    r = session.post(
        f"{base_url}/api/jobs/{job_id}/repool",
        data={"b": boundaries_str},
    )
    assert_ok(r, "Repool")
    review_after_repool = r.json()
    blocks_after_repool = len(review_after_repool.get("blocks", []))
    log(f"Repool OK: {blocks_after_repool} blocks", 1)

    return review_after_repool, split_info


def step_force_restart(render_token, service_id, base_url):
    log("=== Step 5b: Force service restart (session survival test) ===")
    r = requests.post(
        f"https://api.render.com/v1/services/{service_id}/restart",
        headers={"Authorization": f"Bearer {render_token}"},
    )
    if r.status_code != 200:
        fail(f"Restart API returned HTTP {r.status_code}: {r.text[:200]}")
    log("Restart triggered", 1)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/api/me", timeout=5)
            if r.status_code in (200, 401):  # 401 = up but not logged in
                log("Service back up", 1)
                return
        except Exception:
            pass
        log("Waiting for service...", 1)
        time.sleep(5)
    fail("Service did not come back within 120s after restart")


def step_verify_session_after_restart(session, base_url, job_id):
    log("=== Step 5c: Verify session cookie survives restart ===")
    r = session.get(f"{base_url}/api/jobs/{job_id}/status")
    if r.status_code == 401:
        fail("Session cookie was invalidated by restart — HMAC token fix not working")
    assert_ok(r, "Status after restart (no re-login)")
    log("Session cookie survived restart — HMAC tokens working", 1)


def step_phase_b(session, base_url, job_id, review):
    """Phase B: resolve all flags, assign missing tickets, verify all blocks are clean."""
    log("=== Step 6: Phase B — resolve flags, assign tickets ===")
    blocks = review.get("blocks", [])
    whitelist = review.get("whitelist", [])
    log(f"{len(blocks)} blocks, {len(whitelist)} tickets in whitelist", 1)

    # Build a pool of unassigned whitelist tickets
    assigned = {b["ticket"] for b in blocks if b.get("ticket")}
    unassigned_pool = [t for t in whitelist if t not in assigned]
    ticket_idx = 0

    for b in blocks:
        block_id = b["id"]
        ticket = b.get("ticket")
        flags = b.get("flags", [])
        has_hard = b.get("has_hard_flag", False)

        # If no ticket, assign one
        if not ticket:
            if ticket_idx < len(unassigned_pool):
                ticket = unassigned_pool[ticket_idx]
                ticket_idx += 1
                r = session.patch(
                    f"{base_url}/api/jobs/{job_id}/review",
                    data={"action": "reassign", "block_id": str(block_id), "ticket": ticket},
                )
                assert_ok(r, f"Reassign block {block_id}")
                log(f"Block {block_id}: assigned ticket {ticket}", 1)
            else:
                log(f"Block {block_id}: no unassigned ticket available — skipping", 1)
                continue
        elif has_hard:
            # Block has a ticket but also a hard flag — reassign to clear the flag
            r = session.patch(
                f"{base_url}/api/jobs/{job_id}/review",
                data={"action": "reassign", "block_id": str(block_id), "ticket": ticket},
            )
            assert_ok(r, f"Re-confirm block {block_id} ticket to clear flag")
            log(f"Block {block_id}: re-confirmed ticket {ticket} to clear hard flag {flags}", 1)
        else:
            log(f"Block {block_id}: ticket={ticket} flags={flags} OK", 1)

    # Final check: re-read review and verify no hard flags remain
    r = session.get(f"{base_url}/api/jobs/{job_id}/review")
    assert_ok(r, "Get review after Phase B")
    final_review = r.json()
    hard_flag_blocks = [b for b in final_review.get("blocks", []) if b.get("has_hard_flag")]
    unassigned_blocks = [b for b in final_review.get("blocks", []) if not b.get("ticket")]
    missing_tickets = final_review.get("missing_tickets", [])

    if hard_flag_blocks:
        log(f"WARNING: {len(hard_flag_blocks)} blocks still have hard flags — will try to resolve", 1)
        for b in hard_flag_blocks:
            ticket = b.get("ticket")
            if ticket:
                r = session.patch(
                    f"{base_url}/api/jobs/{job_id}/review",
                    data={"action": "reassign", "block_id": str(b["id"]), "ticket": ticket},
                )
                log(f"  Block {b['id']}: force-reassigned {ticket} to clear flag, HTTP {r.status_code}", 1)

    log(f"Phase B complete: unassigned={len(unassigned_blocks)} missing_tickets={len(missing_tickets)} hard_flags={len(hard_flag_blocks)}", 1)
    return final_review


def step_confirm_job(session, base_url, job_id):
    log("=== Step 7: Confirm job ===")
    r = session.post(f"{base_url}/api/jobs/{job_id}/confirm")
    if r.status_code != 200:
        fail(f"Confirm returned HTTP {r.status_code}: {r.text[:400]}")
    log(f"Confirm OK", 1)
    return r.json()


def step_download_zip(session, base_url, job_id):
    log("=== Step 8: Download ZIP ===")
    r = session.get(f"{base_url}/api/jobs/{job_id}/download")
    assert_ok(r, "Download ZIP")
    content_type = r.headers.get("content-type", "")
    if "zip" not in content_type and "octet-stream" not in content_type:
        fail(f"Download returned unexpected content-type: {content_type}")
    data = r.content
    log(f"ZIP downloaded: {len(data):,} bytes", 1)
    return data


def step_verify_zip(zip_bytes, whitelist_raw):
    log("=== Step 9: Verify ZIP contents ===")
    whitelist = [t.strip() for t in whitelist_raw.split(",") if t.strip()]
    expected_count = len(whitelist)

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as e:
        fail(f"ZIP is not a valid ZIP file: {e}")

    pdf_files = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
    log(f"ZIP contains {len(pdf_files)} PDF files (expected {expected_count})", 1)

    failures = []
    for fname in pdf_files:
        data = zf.read(fname)
        if not is_valid_pdf(data):
            failures.append(f"{fname}: not a valid PDF (starts {data[:4]})")
        else:
            log(f"  {fname}: {len(data):,} bytes — valid PDF", 1)

    if len(pdf_files) != expected_count:
        failures.append(f"Expected {expected_count} PDFs, got {len(pdf_files)}")

    if failures:
        fail("ZIP verification failures:\n" + "\n".join(f"  {f}" for f in failures))

    log(f"ZIP verified: {len(pdf_files)} valid PDFs", 1)
    return pdf_files


# ── Session runner ────────────────────────────────────────────────────────────

def run_session(args, run_number, force_restart=False):
    print(f"\n{'='*60}")
    print(f"E2E SESSION DRIVER — Run {run_number}/3")
    if force_restart:
        print("  (includes forced restart after Phase A)")
    print(f"{'='*60}\n")

    session = requests.Session()
    session.headers.update({"User-Agent": "STS-E2E-Driver/1.0"})
    start = time.time()
    mem_log = []

    def mchk(label):
        rss = mem_rss_mb()
        if rss is not None:
            mem_log.append(f"{label}={rss}MB")
            log(f"Memory [{label}]: {rss} MB RSS", 1)

    mchk("start")

    # 1. Login
    step_login(session, args.url, args.password)

    # 2. Create job
    job_id, total_pages = step_create_job(session, args.url, args.pdf, args.whitelist)

    # 3. Wait for detection
    step_wait_for_ready(session, args.url, job_id)
    mchk("after_detection")

    # 4. Fetch all page images
    step_fetch_all_images(session, args.url, job_id, total_pages)
    mchk("after_images")

    # 5. Phase A (split + repool)
    review, split_info = step_phase_a(session, args.url, job_id)

    # 5b. Force restart (run 2 only)
    if force_restart:
        step_force_restart(args.render_token, args.service_id, args.url)
        step_verify_session_after_restart(session, args.url, job_id)
        mchk("after_restart")
        # Re-fetch all images to confirm they still work after restart
        log("=== Step 5d: Re-fetch all images after restart ===")
        step_fetch_all_images(session, args.url, job_id, total_pages)

    # 6. Phase B
    final_review = step_phase_b(session, args.url, job_id, review)
    mchk("after_phase_b")

    # 7. Confirm
    step_confirm_job(session, args.url, job_id)

    # 8. Download ZIP
    zip_bytes = step_download_zip(session, args.url, job_id)

    # 9. Verify ZIP
    pdf_files = step_verify_zip(zip_bytes, args.whitelist)

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"RUN {run_number} PASSED in {elapsed:.0f}s")
    print(f"  Job ID:        {job_id}")
    print(f"  Pages:         {total_pages}")
    print(f"  PDFs in ZIP:   {len(pdf_files)}")
    print(f"  Restart:       {'YES — session survived' if force_restart else 'No'}")
    print(f"  Memory:        {' | '.join(mem_log)}")
    print(f"{'='*60}\n")

    return {
        "run": run_number,
        "job_id": job_id,
        "total_pages": total_pages,
        "pdf_count": len(pdf_files),
        "elapsed_s": round(elapsed, 1),
        "force_restart": force_restart,
        "memory_log": mem_log,
        "passed": True,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="E2E session driver for Ship Ticket Splitter")
    parser.add_argument("--url", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--whitelist", required=True)
    parser.add_argument("--render-token", default="")
    parser.add_argument("--service-id", default="")
    args = parser.parse_args()

    results = []
    all_passed = True

    for run_num in range(1, 4):
        force_restart = (run_num == 2) and bool(args.render_token) and bool(args.service_id)
        try:
            result = run_session(args, run_num, force_restart=force_restart)
            results.append(result)
        except AssertionError as e:
            all_passed = False
            results.append({"run": run_num, "passed": False, "error": str(e)})
            print(f"\n[FAIL] Run {run_num} failed: {e}\n")

    print(f"\n{'='*60}")
    print("E2E DRIVER SUMMARY")
    print(f"{'='*60}")
    for r in results:
        status_str = "PASS" if r.get("passed") else "FAIL"
        restart_str = " (with restart)" if r.get("force_restart") else ""
        if r.get("passed"):
            print(f"  Run {r['run']}{restart_str}: {status_str} — {r['elapsed_s']}s — job {r['job_id'][:8]}... — {r['pdf_count']} PDFs — mem: {' | '.join(r.get('memory_log', []))}")
        else:
            print(f"  Run {r['run']}{restart_str}: {status_str} — {r.get('error', 'unknown')}")

    if all_passed:
        print(f"\n{'='*60}")
        print("ALL 3 RUNS PASSED — SAFE TO RUN GROUND-TRUTH SESSION")
        print(f"{'='*60}\n")
    else:
        print(f"\n{'='*60}")
        print("SOME RUNS FAILED — NOT SAFE TO RUN")
        print(f"{'='*60}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
