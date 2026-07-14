"""
Memory measurement script for Ship Ticket Splitter.
Measures RSS at key points: baseline, after imports, after loading N jobs,
after running detection on a 16-page PDF, and after gc.collect().
"""
import json
import os
import sys
import gc
import resource
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "webapp"))

def rss_mb():
    """Return current RSS in MB using /proc/self/status (Linux)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    # Fallback: resource module (returns max RSS on Linux, not current)
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

print("=== Memory Measurement: Ship Ticket Splitter ===\n")

# Step 1: baseline before any imports
baseline = rss_mb()
print(f"[0] Baseline (before app imports): {baseline:.1f} MB")

# Step 2: import the app modules
import fitz
import numpy as np
import cv2
from detect import run_detection_fast
from grouping import group_detections, parse_whitelist
from pink_detect import detect_pink_stickers_batch_debug

after_imports = rss_mb()
print(f"[1] After all imports (fitz, numpy, cv2, detect, grouping, pink_detect): {after_imports:.1f} MB")
print(f"    Import overhead: {after_imports - baseline:.1f} MB")

# Step 3: simulate loading 5 jobs from disk WITHOUT detection_results (new behavior)
jobs = {}
for i in range(5):
    jobs[f"job-{i}"] = {
        "id": f"job-{i}",
        "status": "ready",
        "detection_results": None,  # stripped on startup
        "review_state": {
            "blocks": [{"pages": list(range(1+j*3, 4+j*3)), "ticket": str(100000+j), "flags": []} for j in range(6)]
        },
        "whitelist": [str(x) for x in range(100000, 100024)],
        "pdf_path": "/data/sts_jobs/job-0/input.pdf",
        "thumbnail_dir": "/data/sts_jobs/job-0/thumbs",
    }

after_5_jobs = rss_mb()
print(f"\n[2] After loading 5 jobs (detection_results=None): {after_5_jobs:.1f} MB")
print(f"    Per-job overhead: {(after_5_jobs - after_imports)/5:.1f} MB")

# Step 4: simulate detection on a real 16-page PDF
fixture_pdf = Path("/home/ubuntu/upload/testingfile.pdf")
if not fixture_pdf.exists():
    fixture_pdf = Path(__file__).parent / "fixtures" / "testingfile.pdf"

if fixture_pdf.exists():
    print(f"\n[3] Running detection on {fixture_pdf.name} ({fixture_pdf.stat().st_size//1024} KB)...")
    # Use same render path as the app: 150 DPI, JPEG bytes
    import io
    from detect import render_page_to_jpeg as _render_jpeg
    doc = fitz.open(str(fixture_pdf))
    page_images = {}  # page_num (1-indexed) -> jpeg bytes
    for page_num in range(len(doc)):
        page_images[page_num + 1] = _render_jpeg(doc, page_num, dpi=150)
    doc.close()

    after_render = rss_mb()
    print(f"    After rendering {len(page_images)} pages to JPEG bytes (150 DPI): {after_render:.1f} MB")
    print(f"    Render overhead: {after_render - after_5_jobs:.1f} MB")
    print(f"    Per-page render cost: {(after_render - after_5_jobs)/len(page_images):.1f} MB")

    # Run pink detection
    jpeg_bytes_list = [page_images[p] for p in sorted(page_images)]
    pink_results = detect_pink_stickers_batch_debug(jpeg_bytes_list)
    after_pink = rss_mb()
    print(f"    After pink detection: {after_pink:.1f} MB")
    print(f"    Pink detection overhead: {after_pink - after_render:.1f} MB")

    # Simulate detection_results (list of per-page dicts)
    detection_results = [
        {"page": i+1, "value": "12345", "confidence": 0.95, "source": "printed",
         "raw_texts": ["12345"], "pink_flag": pink_results[i]["detected"]}
        for i in range(len(page_images))
    ]
    after_det = rss_mb()
    print(f"    After building detection_results ({len(detection_results)} pages): {after_det:.1f} MB")

    # Trim detection_results and gc.collect (as the app does post-grouping)
    detection_results = None
    page_images = None
    gc.collect()
    after_trim = rss_mb()
    print(f"    After trim + gc.collect(): {after_trim:.1f} MB")
    print(f"    Memory returned to OS: {after_det - after_trim:.1f} MB")

    peak = after_det
    print(f"\n=== SUMMARY ===")
    print(f"  Startup RSS (5 old jobs, no detection_results): {after_5_jobs:.1f} MB")
    print(f"  Peak during 16-page detection: {peak:.1f} MB")
    print(f"  Post-job RSS after trim+gc: {after_trim:.1f} MB")
    print(f"  Render Starter plan limit: 512 MB")
    print(f"  Headroom after one job: {512 - after_trim:.1f} MB")
    print(f"  Headroom at peak: {512 - peak:.1f} MB")

    # Extrapolate for 300-page batch
    pages_per_job = len(page_images) if page_images else 16
    render_mb_per_page = (after_render - after_5_jobs) / max(1, len(page_images) if page_images else 16)
    peak_300 = after_5_jobs + render_mb_per_page * 300
    print(f"\n  Extrapolated peak for 300-page batch: {peak_300:.0f} MB")
    if peak_300 > 512:
        print(f"  WARNING: 300-page batch would exceed 512 MB limit by {peak_300-512:.0f} MB")
        print(f"  RECOMMENDATION: Upgrade to Render Standard ($25/mo, 2 GB RAM)")
    else:
        print(f"  300-page batch fits within 512 MB limit with {512-peak_300:.0f} MB margin")
else:
    print(f"\n[3] No fixture PDF found, skipping detection measurement")
    print(f"    Searched: {fixture_pdf}")
