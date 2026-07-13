"""
fixture5_fast_mode_runner.py — Run fixture #5 through run_detection_fast() and compare
against full-mode ground truth.

Fixture #5: SKM_C250i26070916020.pdf (17 pages, TIB batch)
Ground truth blocks:
  247799: pages 1–4
  248256: pages 5–8
  248258: pages 9–11
  248259: pages 12–14
  248260: pages 15–17

Pink sticker pages (from calibration): 1, 4, 8, 11, 14
  → These are the block boundaries in non-TIB mode.
  → In TIB mode (fixture #5), fast mode still uses pink boundaries.

Usage:
    python3 tools/fixture5_fast_mode_runner.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from detect import run_detection_fast, render_page_to_jpeg, DEFAULT_MODEL
from pink_detect import detect_pink_sticker
import fitz

PDF_PATH = "/home/ubuntu/upload/SKM_C250i26070916020.pdf"
WHITELIST = ["247799", "248256", "248258", "248259", "248260"]

# Ground truth: page → expected ticket
GROUND_TRUTH = {}
for p in range(1, 5):   GROUND_TRUTH[p] = "247799"
for p in range(5, 9):   GROUND_TRUTH[p] = "248256"
for p in range(9, 12):  GROUND_TRUTH[p] = "248258"
for p in range(12, 15): GROUND_TRUTH[p] = "248259"
for p in range(15, 18): GROUND_TRUTH[p] = "248260"

# Full-mode results for comparison (from fixture_suite_runner.py run above)
FULL_MODE_RESULTS = {
    1:  {"detected": "247799", "source": "print", "conf": 1.00, "status": "CORRECT"},
    2:  {"detected": "247799", "source": "print", "conf": 1.00, "status": "CORRECT"},
    3:  {"detected": "247798", "source": "handw", "conf": 0.95, "status": "FUZZY_CORRECT"},
    4:  {"detected": "247798", "source": "handw", "conf": 0.95, "status": "FUZZY_CORRECT"},
    5:  {"detected": "248256", "source": "print", "conf": 1.00, "status": "CORRECT"},
    6:  {"detected": "248256", "source": "print", "conf": 1.00, "status": "CORRECT"},
    7:  {"detected": "248286", "source": "handw", "conf": 0.95, "status": "FUZZY_CORRECT"},
    8:  {"detected": "248256", "source": "handw", "conf": 0.95, "status": "CORRECT"},
    9:  {"detected": "248258", "source": "print", "conf": 1.00, "status": "CORRECT"},
    10: {"detected": "248258", "source": "print", "conf": 1.00, "status": "CORRECT"},
    11: {"detected": "248258", "source": "handw", "conf": 1.00, "status": "CORRECT"},
    12: {"detected": "248259", "source": "print", "conf": 1.00, "status": "CORRECT"},
    13: {"detected": "248259", "source": "print", "conf": 1.00, "status": "CORRECT"},
    14: {"detected": "248259", "source": "handw", "conf": 0.95, "status": "CORRECT"},
    15: {"detected": "248260", "source": "print", "conf": 1.00, "status": "CORRECT"},
    16: {"detected": "248260", "source": "print", "conf": 1.00, "status": "CORRECT"},
    17: {"detected": "224826", "source": "handw", "conf": 0.95, "status": "UNMATCHED"},
}


def main():
    print("=" * 70)
    print("Fixture #5 — Fast mode run")
    print(f"PDF: {PDF_PATH}")
    print(f"Model: {DEFAULT_MODEL}")
    print(f"Whitelist: {WHITELIST}")
    print("=" * 70)

    # Compute pink boundaries locally (same as main.py does in fast mode)
    print("Computing pink sticker boundaries locally...")
    doc = fitz.open(PDF_PATH)
    total_pages_local = len(doc)
    pre_boundaries = [1]  # page 1 always starts a block
    for i in range(total_pages_local):
        pn = i + 1
        jpeg = render_page_to_jpeg(doc, i, dpi=150)
        has_pink = detect_pink_sticker(jpeg)
        if has_pink and pn not in pre_boundaries:
            pre_boundaries.append(pn)
    doc.close()
    print(f"Pink boundaries detected: {pre_boundaries}")

    t0 = time.time()
    results, fast_metrics = run_detection_fast(
        pdf_path=PDF_PATH,
        pre_boundaries=pre_boundaries,
        whitelist=WHITELIST,
        workers=5,
        model=DEFAULT_MODEL,
    )
    elapsed = time.time() - t0
    print(f"Fast mode metrics: {fast_metrics}")

    # Build result dict by page
    by_page = {r["page"]: r for r in results}
    total_pages = len(results)
    api_calls = sum(1 for r in results if not r.get("not_read"))
    not_read_count = sum(1 for r in results if r.get("not_read"))

    print(f"\nFast mode complete in {elapsed:.1f}s")
    print(f"Total pages: {total_pages}")
    print(f"API calls made: {api_calls} (not_read: {not_read_count})")
    print(f"Avg latency per API call: {elapsed/api_calls:.1f}s" if api_calls else "N/A")

    print(f"\nFast mode vs Full mode — Per-page comparison:")
    print(f"{'Page':>4}  {'Expected':>10}  {'Fast Detected':>14}  {'Fast Src':>8}  {'Fast Conf':>9}  {'Not Read':>8}  {'Fast Status':>14}  {'Full Status':>14}  {'Match?'}")
    print("-" * 110)

    fast_correct = 0
    fast_wrong = 0
    fast_errors = 0
    full_correct = 0
    full_wrong = 0

    whitelist_set = set(WHITELIST)

    for pn in range(1, total_pages + 1):
        r = by_page.get(pn, {})
        expected = GROUND_TRUTH.get(pn, "?")
        not_read = r.get("not_read", False)
        candidates = r.get("candidates", [])
        error = r.get("error")

        best = None
        for c in candidates:
            if not c.get("crossed_out"):
                if best is None or c["confidence"] > best["confidence"]:
                    best = c

        det = best["value"] if best else None
        src = (best["source"] or "")[:5] if best else "—"
        conf = f"{best['confidence']:.2f}" if best else "—"

        # Determine fast status
        if not_read:
            fast_status = "NOT_READ"
        elif error:
            fast_status = "ERROR"
            fast_errors += 1
        elif det is None:
            fast_status = "EMPTY"
        elif det in whitelist_set:
            if det == expected:
                fast_status = "CORRECT"
                fast_correct += 1
            else:
                fast_status = "WRONG"
                fast_wrong += 1
        else:
            from grouping import digit_edit_distance
            best_match = min(WHITELIST, key=lambda w: digit_edit_distance(det, w))
            dist = digit_edit_distance(det, best_match)
            if dist <= 1 and best_match == expected:
                fast_status = "FUZZY_CORRECT"
                fast_correct += 1
            elif dist <= 1:
                fast_status = "FUZZY_WRONG"
                fast_wrong += 1
            else:
                fast_status = "UNMATCHED"

        # Full mode status
        full_r = FULL_MODE_RESULTS.get(pn, {})
        full_status = full_r.get("status", "—")
        if full_status in ("CORRECT", "FUZZY_CORRECT"):
            full_correct += 1
        elif full_status in ("WRONG", "FUZZY_WRONG", "UNMATCHED", "MISS"):
            full_wrong += 1

        # Agreement check
        if not_read:
            match_str = "SKIP"
        elif fast_status == full_status:
            match_str = "="
        elif fast_status in ("CORRECT", "FUZZY_CORRECT") and full_status in ("CORRECT", "FUZZY_CORRECT"):
            match_str = "≈"
        elif fast_status == "EMPTY" and full_status in ("CORRECT", "FUZZY_CORRECT"):
            match_str = "MISS"
        else:
            match_str = "DIFF"

        nr_str = "YES" if not_read else ""
        det_str = det or "—"
        print(
            f"{pn:>4}  {expected:>10}  {det_str:>14}  {src:>8}  {conf:>9}  {nr_str:>8}  {fast_status:>14}  {full_status:>14}  {match_str}"
        )

    print()
    print(f"Fast mode:  {fast_correct} correct, {fast_wrong} wrong, {fast_errors} errors (not_read pages excluded)")
    print(f"Full mode:  {full_correct} correct, {full_wrong} wrong (all 17 pages)")
    print(f"API calls saved: {not_read_count} of {total_pages} pages skipped")


if __name__ == "__main__":
    main()
