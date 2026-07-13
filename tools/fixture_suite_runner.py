"""
fixture_suite_runner.py — Full fixture suite runner for gemini-3-flash-preview validation.

Runs all pages of fixtures #1–#5 through detect.py's detect_page() function
and compares results against known ground truth. Outputs per-page tables.

Usage:
    python3 tools/fixture_suite_runner.py [--model gemini-3-flash-preview]
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from detect import detect_page, detect_page_sticker_retry, detect_page_second_pass, render_page_to_jpeg, DEFAULT_MODEL
import fitz
from openai import OpenAI

# ── Ground truth ──────────────────────────────────────────────────────────────
# Each fixture: {page_num: expected_ticket_or_None}
# None = photo/content page that inherits from previous (no direct detection expected)
# "EMPTY_INHERIT" = page expected to be empty and inherit

FIXTURE_GROUND_TRUTH = {
    1: {  # testingfile.pdf — 16 pages, TIB batch
        "pdf": "/home/ubuntu/upload/testingfile.pdf",
        "batch_type": "tib",
        "whitelist": ["301532", "257535", "253983", "258066", "257086"],
        "blocks": {
            "301532": list(range(1, 4)),    # pages 1–3
            "257535": [4],                   # page 4
            "253983": list(range(5, 11)),    # pages 5–10
            "258066": list(range(11, 14)),   # pages 11–13
            "257086": list(range(14, 17)),   # pages 14–16
        },
        # Pages where direct detection is expected (first page of each block)
        "expected_detections": {
            1: "301532",   # printed cover
            4: "257535",   # printed cover
            5: "253983",   # printed cover
            11: "258066",  # printed cover
            14: "257086",  # printed cover
        },
        # Pages that are handwritten/photo — detection may be empty or fuzzy
        "handwritten_pages": [7, 10],  # page 7 misread (UNMATCHED), page 10 fuzzy
    },
    2: {  # SKM_C250i26070816150.pdf — 16 pages, TIB batch
        "pdf": "/home/ubuntu/upload/SKM_C250i26070816150.pdf",
        "batch_type": "tib",
        "whitelist": ["300291", "300871", "300588", "298404", "299198", "301053"],
        "blocks": {
            "300291": [1],
            "300871": [2],
            "300588": list(range(3, 12)),   # pages 3–11
            "298404": [12],
            "299198": [13],
            "301053": list(range(14, 17)),  # pages 14–16
        },
        "expected_detections": {
            1: "300291",
            2: "300871",
            3: "300588",
            12: "298404",
            13: "299198",   # sticker retry needed
            14: "301053",
        },
        "handwritten_pages": [],
    },
    4: {  # SKM_C250i26070816530.pdf — 18 pages, TIB batch
        "pdf": "/home/ubuntu/upload/SKM_C250i26070816530.pdf",
        "batch_type": "tib",
        "whitelist": ["300574", "300600", "300573", "253027-1"],
        "blocks": {
            "300574": list(range(1, 3)),    # pages 1–2
            "300600": list(range(3, 14)),   # pages 3–13
            "300573": list(range(14, 16)),  # pages 14–15
            "253027-1": list(range(16, 19)), # pages 16–18
        },
        "expected_detections": {
            1: "300574",    # may need second pass
            3: "300600",
            14: "300573",   # sticker retry needed
            16: "253027-1",
        },
        "handwritten_pages": [],
    },
    5: {  # SKM_C250i26070916020.pdf — 17 pages, TIB batch
        "pdf": "/home/ubuntu/upload/SKM_C250i26070916020.pdf",
        "batch_type": "tib",
        "whitelist": ["247799", "248256", "248258", "248259", "248260"],
        "blocks": {
            "247799": list(range(1, 5)),    # pages 1–4
            "248256": list(range(5, 9)),    # pages 5–8
            "248258": list(range(9, 12)),   # pages 9–11
            "248259": list(range(12, 15)),  # pages 12–14
            "248260": list(range(15, 18)),  # pages 15–17
        },
        "expected_detections": {
            1: "247799",
            5: "248256",
            9: "248258",
            12: "248259",
            15: "248260",
        },
        "handwritten_pages": [2, 3, 6, 7, 10, 13, 16],  # photo/handwritten pages
    },
}

# Fixture 3 was replaced by fixture 4; no fixture 3 PDF available.


def page_to_block(fixture: dict, page_num: int) -> str | None:
    """Return the expected ticket for a given page number."""
    for ticket, pages in fixture["blocks"].items():
        if page_num in pages:
            return ticket
    return None


def run_fixture(fixture_num: int, fixture: dict, model: str, workers: int = 5) -> list[dict]:
    """Run full detection on a fixture PDF and return per-page results."""
    pdf_path = fixture["pdf"]
    whitelist = fixture["whitelist"]
    client = OpenAI()

    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    print(f"\n{'='*70}")
    print(f"Fixture #{fixture_num}: {Path(pdf_path).name} ({total_pages} pages)")
    print(f"Model: {model}")
    print(f"Whitelist: {whitelist}")
    print(f"{'='*70}")

    # Render all pages
    print("Rendering pages...")
    page_images = {}
    for i in range(total_pages):
        page_images[i + 1] = render_page_to_jpeg(doc, i, dpi=150)
    doc.close()
    print(f"Rendered {total_pages} pages.")

    # First pass — concurrent
    results: dict[int, dict] = {}
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(detect_page, client, pn, page_images[pn], model): pn
            for pn in range(1, total_pages + 1)
        }
        for future in as_completed(futures):
            pn = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "page": pn,
                    "candidates": [],
                    "pink_marker": False,
                    "error": f"unexpected: {exc}",
                }
            results[pn] = result
    t_first_pass = time.time() - t_start
    print(f"First pass complete in {t_first_pass:.1f}s ({t_first_pass/total_pages:.1f}s/page avg)")

    # Sticker retry — concurrent on empty pages
    empty_pages = [
        pn for pn in range(1, total_pages + 1)
        if not results[pn].get("candidates") and not results[pn].get("error")
    ]
    if empty_pages:
        print(f"Sticker retry on {len(empty_pages)} empty pages: {empty_pages}")
        t_retry_start = time.time()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            retry_futures = {
                executor.submit(detect_page_sticker_retry, client, pn, page_images[pn], model): pn
                for pn in empty_pages
            }
            for future in as_completed(retry_futures):
                pn = retry_futures[future]
                try:
                    retry_result = future.result()
                except Exception as exc:
                    retry_result = {"_exhausted": True, "error": f"unexpected: {exc}"}
                if isinstance(retry_result, dict) and retry_result.get("_exhausted"):
                    results[pn]["error"] = retry_result["error"]
                elif retry_result:
                    results[pn]["candidates"] = retry_result
        t_retry = time.time() - t_retry_start
        print(f"Sticker retry complete in {t_retry:.1f}s")

    # Second pass — sequential on orphan-candidate pages
    sorted_results = [results[i + 1] for i in range(total_pages)]
    first_detected_idx = next(
        (i for i, r in enumerate(sorted_results) if r.get("candidates")), None
    )
    if first_detected_idx is not None:
        orphan_candidates = list(range(1, first_detected_idx + 1))
    else:
        orphan_candidates = list(range(1, total_pages + 1))
    second_pass_pages = [
        p for p in orphan_candidates
        if not results[p].get("candidates") and not results[p].get("error")
    ]
    if second_pass_pages and whitelist:
        print(f"Second pass on {len(second_pass_pages)} orphan pages: {second_pass_pages}")
        for pn in second_pass_pages:
            candidate = detect_page_second_pass(client, pn, page_images[pn], whitelist, model)
            if candidate and not (isinstance(candidate, dict) and candidate.get("_exhausted")):
                results[pn]["candidates"] = [candidate]
            elif isinstance(candidate, dict) and candidate.get("_exhausted"):
                results[pn]["error"] = candidate["error"]

    return [results[i + 1] for i in range(total_pages)]


def analyze_results(fixture_num: int, fixture: dict, page_results: list[dict]) -> dict:
    """Analyze results against ground truth and return summary."""
    whitelist = fixture["whitelist"]
    whitelist_set = set(whitelist)
    expected_detections = fixture.get("expected_detections", {})
    handwritten_pages = fixture.get("handwritten_pages", [])

    rows = []
    correct_blocks = 0
    wrong_blocks = 0
    errors = 0
    empty_unexpected = 0

    for result in page_results:
        pn = result["page"]
        expected_ticket = page_to_block(fixture, pn)
        candidates = result.get("candidates", [])
        error = result.get("error")
        pink = result.get("pink_marker", False)

        # Best candidate: highest confidence, not crossed out
        best = None
        for c in candidates:
            if not c.get("crossed_out"):
                if best is None or c["confidence"] > best["confidence"]:
                    best = c

        detected_value = best["value"] if best else None
        detected_source = best["source"] if best else None
        detected_conf = best["confidence"] if best else None
        is_second_pass = best.get("second_pass", False) if best else False

        # Determine match status
        if error:
            status = "ERROR"
            errors += 1
        elif detected_value is None:
            # Empty — check if this page is expected to have a direct detection
            if pn in expected_detections:
                status = "MISS"
                wrong_blocks += 1
            else:
                status = "EMPTY_OK"  # Will inherit — fine
        elif detected_value in whitelist_set:
            if detected_value == expected_ticket:
                status = "CORRECT"
                correct_blocks += 1
            else:
                status = "WRONG_TICKET"
                wrong_blocks += 1
        else:
            # Not in whitelist — check edit distance
            from grouping import digit_edit_distance
            best_match = None
            best_dist = 999
            for w in whitelist:
                d = digit_edit_distance(detected_value, w)
                if d < best_dist:
                    best_dist = d
                    best_match = w
            if best_dist <= 1 and best_match == expected_ticket:
                status = "FUZZY_CORRECT"
                correct_blocks += 1
            elif best_dist <= 1:
                status = "FUZZY_WRONG"
                wrong_blocks += 1
            else:
                status = "UNMATCHED"
                if pn in expected_detections:
                    wrong_blocks += 1

        is_handwritten = pn in handwritten_pages

        rows.append({
            "page": pn,
            "expected_ticket": expected_ticket,
            "detected_value": detected_value,
            "detected_source": detected_source,
            "detected_conf": detected_conf,
            "second_pass": is_second_pass,
            "pink_marker": pink,
            "error": error,
            "status": status,
            "handwritten": is_handwritten,
        })

    return {
        "fixture": fixture_num,
        "rows": rows,
        "correct": correct_blocks,
        "wrong": wrong_blocks,
        "errors": errors,
        "total_pages": len(page_results),
    }


def print_fixture_table(analysis: dict):
    """Print a per-page table for a fixture."""
    fn = analysis["fixture"]
    rows = analysis["rows"]
    print(f"\nFixture #{fn} — Per-page results:")
    print(f"{'Page':>4}  {'Expected':>10}  {'Detected':>12}  {'Src':>5}  {'Conf':>5}  {'2P':>2}  {'Pink':>4}  {'Status'}")
    print("-" * 80)
    for r in rows:
        conf_str = f"{r['detected_conf']:.2f}" if r['detected_conf'] is not None else "  —  "
        src_str = (r['detected_source'] or "")[:5] if r['detected_source'] else "  —  "
        det_str = r['detected_value'] or "—"
        exp_str = r['expected_ticket'] or "—"
        sp_str = "Y" if r['second_pass'] else " "
        pk_str = "Y" if r['pink_marker'] else " "
        hw_marker = "*" if r['handwritten'] else " "
        err_suffix = f" [{r['error'][:30]}]" if r['error'] else ""
        print(
            f"{r['page']:>4}{hw_marker} {exp_str:>10}  {det_str:>12}  {src_str:>5}  {conf_str:>5}  {sp_str:>2}  {pk_str:>4}  {r['status']}{err_suffix}"
        )
    print()
    print(f"Summary: {analysis['correct']} correct, {analysis['wrong']} wrong, {analysis['errors']} errors, {analysis['total_pages']} total pages")
    print(f"  (* = handwritten/photo page — detection may be empty; inherits from block)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--fixture", type=int, choices=[1, 2, 4, 5], default=None,
                        help="Run only this fixture number (default: all)")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()

    fixtures_to_run = [args.fixture] if args.fixture else [1, 2, 4, 5]

    all_analyses = []
    for fn in fixtures_to_run:
        fixture = FIXTURE_GROUND_TRUTH[fn]
        page_results = run_fixture(fn, fixture, args.model, workers=args.workers)
        analysis = analyze_results(fn, fixture, page_results)
        print_fixture_table(analysis)
        all_analyses.append(analysis)

    # Overall summary
    print("\n" + "="*70)
    print(f"OVERALL SUMMARY — model: {args.model}")
    print("="*70)
    total_correct = sum(a["correct"] for a in all_analyses)
    total_wrong = sum(a["wrong"] for a in all_analyses)
    total_errors = sum(a["errors"] for a in all_analyses)
    total_pages = sum(a["total_pages"] for a in all_analyses)
    print(f"Total pages: {total_pages}")
    print(f"Correct detections: {total_correct}")
    print(f"Wrong detections:   {total_wrong}")
    print(f"Errors:             {total_errors}")
    print(f"Detection accuracy (pages with expected detection): see per-fixture tables")

    # Save results to JSON
    out_path = Path(__file__).parent.parent / "notes" / f"fixture_suite_results_{args.model.replace('-','_').replace('.','_')}.json"
    with open(out_path, "w") as f:
        json.dump(all_analyses, f, indent=2)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
