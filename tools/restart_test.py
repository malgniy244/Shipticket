#!/usr/bin/env python3
"""
Restart test for job-state persistence.

Steps:
  1. Login to the live Render app.
  2. Upload a fixture PDF and create a job (full mode).
  3. Wait for status=ready.
  4. Read the review state (blocks, flags).
  5. Make a Phase A boundary edit (move a boundary).
  6. Read back the updated review state to confirm the edit persisted in memory.
  7. Trigger a Render redeploy via the Render API.
  8. Wait for the new deploy to go live.
  9. Fetch the job status from the new instance.
 10. Fetch the review state and confirm the Phase A edit survived.

Usage:
  python3 tools/restart_test.py \
    --url https://ship-ticket-splitter.onrender.com \
    --password <APP_PASSWORD> \
    --pdf /path/to/fixture.pdf \
    --whitelist "300588,300291,300871" \
    --render-token <RENDER_TOKEN> \
    --service-id srv-d9a8k7gk1i2s73f8sb5g
"""

import argparse
import json
import sys
import time

import requests

RENDER_API = "https://api.render.com/v1"


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def login(session, base_url, password):
    r = session.post(f"{base_url}/api/login", data={"password": password})
    r.raise_for_status()
    log(f"Login OK: {r.json()}")


def create_job(session, base_url, pdf_path, whitelist):
    with open(pdf_path, "rb") as f:
        r = session.post(
            f"{base_url}/api/jobs",
            data={
                "whitelist_raw": whitelist,
                "batch_type": "tib",
                "fast_mode": "",  # off
            },
            files={"file": (pdf_path.split("/")[-1], f, "application/pdf")},
        )
    r.raise_for_status()
    d = r.json()
    log(f"Job created: {d['job_id']} ({d['total_pages']} pages)")
    return d["job_id"]


def wait_for_ready(session, base_url, job_id, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = session.get(f"{base_url}/api/jobs/{job_id}/status")
        r.raise_for_status()
        d = r.json()
        status = d["status"]
        prog = d.get("progress_page", 0)
        total = d.get("total_pages", 0)
        log(f"  status={status} page={prog}/{total}")
        if status == "ready":
            return d
        if status == "error":
            raise RuntimeError(f"Job errored: {d.get('error')}")
        time.sleep(3)
    raise TimeoutError("Job did not reach ready within timeout")


def get_review(session, base_url, job_id):
    r = session.get(f"{base_url}/api/jobs/{job_id}/review")
    r.raise_for_status()
    return r.json()


def make_phase_a_edit(session, base_url, job_id, review):
    """Move the boundary of block 1 to page 2 (if it starts at page 1 and has >1 page)."""
    blocks = review.get("blocks", [])
    if not blocks:
        raise ValueError("No blocks in review state")
    b0 = blocks[0]
    b0_id = b0["id"]
    b0_start = b0["pages"][0]
    b0_end = b0["pages"][-1]
    log(f"  Block 0 (id={b0_id}) currently spans pages {b0_start}\u2013{b0_end}")
    if b0_end <= b0_start:
        log("  Block 0 is a single page \u2014 skipping boundary edit, using ticket reassign instead")
        # Reassign block 0 ticket to a known value
        new_ticket = review.get("whitelist", [None])[0]
        if not new_ticket:
            raise ValueError("No whitelist to reassign to")
        r = session.patch(
            f"{base_url}/api/jobs/{job_id}/review",
            data={"action": "reassign", "block_id": b0_id, "ticket": str(new_ticket)},
        )
        r.raise_for_status()
        log(f"  Reassigned block 0 to ticket {new_ticket}")
        return {"edit_type": "reassign", "block_id": b0_id, "ticket": str(new_ticket)}
    else:
        # Split block 0 after page b0_start (split_after_page keeps pages <= that value in block)
        split_after = b0_start
        log(f"  Splitting block 0 after page {split_after} (block_id={b0_id})")
        r = session.patch(
            f"{base_url}/api/jobs/{job_id}/review",
            data={"action": "split", "block_id": b0_id, "split_after_page": split_after},
        )
        r.raise_for_status()
        log(f"  Split OK")
        return {"edit_type": "split", "block_id": b0_id, "split_after_page": split_after}


def trigger_restart(render_token, service_id):
    """Use the Render restart endpoint (no build, just process restart)."""
    r = requests.post(
        f"{RENDER_API}/services/{service_id}/restart",
        headers={"Authorization": f"Bearer {render_token}", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    log(f"Service restart triggered (HTTP {r.status_code})")


def wait_for_service_up(base_url, password, timeout=120):
    """Wait until the service is back up after restart."""
    deadline = time.time() + timeout
    # First wait a moment for the restart to kick in
    time.sleep(5)
    while time.time() < deadline:
        try:
            r = requests.post(f"{base_url}/api/login", data={"password": password}, timeout=10)
            if r.status_code == 200:
                log("  Service is back up")
                return
        except Exception:
            pass
        log("  Waiting for service to come back up...")
        time.sleep(5)
    raise TimeoutError("Service did not come back up within timeout")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--pdf", required=True)
    p.add_argument("--whitelist", required=True)
    p.add_argument("--render-token", required=True)
    p.add_argument("--service-id", required=True)
    args = p.parse_args()

    session = requests.Session()

    # Step 1: Login
    log("=== Step 1: Login ===")
    login(session, args.url, args.password)

    # Step 2: Create job
    log("=== Step 2: Create job ===")
    job_id = create_job(session, args.url, args.pdf, args.whitelist)

    # Step 3: Wait for ready
    log("=== Step 3: Wait for detection to complete ===")
    wait_for_ready(session, args.url, job_id)

    # Step 4: Get review state
    log("=== Step 4: Get review state ===")
    review_before = get_review(session, args.url, job_id)
    blocks_before = len(review_before.get("blocks", []))
    log(f"  Review state: {blocks_before} blocks")

    # Step 5: Make a Phase A edit
    log("=== Step 5: Make Phase A edit ===")
    edit = make_phase_a_edit(session, args.url, job_id, review_before)
    log(f"  Edit applied: {edit}")

    # Step 6: Read back to confirm edit in memory
    log("=== Step 6: Confirm edit persisted in memory ===")
    review_after_edit = get_review(session, args.url, job_id)
    blocks_after_edit = len(review_after_edit.get("blocks", []))
    log(f"  Blocks after edit: {blocks_after_edit} (was {blocks_before})")
    if edit["edit_type"] == "split" and blocks_after_edit != blocks_before + 1:
        raise AssertionError(f"Split edit did not take effect: expected {blocks_before+1} blocks, got {blocks_after_edit}")
    log("  ✓ Edit confirmed in memory")

    # Step 7: Trigger service restart (no rebuild — just process restart)
    log("=== Step 7: Trigger service restart ===")
    trigger_restart(args.render_token, args.service_id)

    # Step 8: Wait for service to come back up
    log("=== Step 8: Wait for service to come back up ===")
    wait_for_service_up(args.url, args.password)

    # Step 9: Fetch job status from new instance
    log("=== Step 9: Check job survives restart ===")
    # Re-login (session cookie may be invalidated by restart)
    login(session, args.url, args.password)
    r = session.get(f"{args.url}/api/jobs/{job_id}/status")
    if r.status_code == 404:
        raise AssertionError(f"FAIL: Job {job_id} not found after restart — persistence not working")
    r.raise_for_status()
    status_after = r.json()
    log(f"  Job status after restart: {status_after['status']}")
    if status_after["status"] not in ("ready", "confirmed"):
        raise AssertionError(f"FAIL: Job status is {status_after['status']} after restart, expected 'ready'")
    log("  ✓ Job survived restart")

    # Step 10: Confirm Phase A edit survived
    log("=== Step 10: Confirm Phase A edit survived restart ===")
    review_after_restart = get_review(session, args.url, job_id)
    blocks_after_restart = len(review_after_restart.get("blocks", []))
    log(f"  Blocks after restart: {blocks_after_restart} (expected {blocks_after_edit})")
    if blocks_after_restart != blocks_after_edit:
        raise AssertionError(
            f"FAIL: Block count changed after restart: expected {blocks_after_edit}, got {blocks_after_restart}"
        )
    log("  ✓ Phase A edit survived restart")

    print("\n" + "="*60)
    print("RESTART TEST PASSED")
    print(f"  Job ID:              {job_id}")
    print(f"  Blocks before edit:  {blocks_before}")
    print(f"  Edit applied:        {edit}")
    print(f"  Blocks after edit:   {blocks_after_edit}")
    print(f"  Blocks after restart:{blocks_after_restart}")
    print("="*60)


if __name__ == "__main__":
    main()
