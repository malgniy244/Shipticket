"""
detect.py — Task 1: Detection module
Renders each PDF page to JPEG at 150 DPI, sends to OpenAI vision model,
returns structured per-page detection results with checkpointing.

Second-pass logic: when a page has no detection AND it is the first page of
the document (or would become ORPHAN_LEADING_PAGES), the module re-queries
the vision model with the whitelist included in the prompt, asking whether
any of those specific numbers appear on the page. A hit is treated as a
normal detection carrying a soft SECOND_PASS flag visible in the review screen.
This can only return whitelist members, so no matching rule is loosened.

Usage:
    python3 detect.py <pdf_path> [--checkpoint <json_path>] [--workers <n>]
                      [--model <model_id>] [--whitelist <csv_or_newline>]
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF
from openai import OpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# ── Vision model ──────────────────────────────────────────────────────────────
DEFAULT_MODEL = "gpt-5"  # Best vision accuracy for messy handwriting

# ── JSON schema for structured output ────────────────────────────────────────
CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "source": {"type": "string", "enum": ["printed", "sticker", "handwritten"]},
        "confidence": {"type": "number"},
        "crossed_out": {"type": "boolean"},
        "corrected_from": {"type": "string"},
        "second_pass": {"type": "boolean"},
    },
    "required": ["value", "source", "confidence", "crossed_out", "corrected_from", "second_pass"],
    "additionalProperties": False,
}

PAGE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": CANDIDATE_SCHEMA,
        },
        "pink_marker": {
            "type": "boolean",
            "description": "True if a bright pink or magenta rectangular sticker is visible anywhere on the page.",
        },
    },
    "required": ["candidates", "pink_marker"],
    "additionalProperties": False,
}

# Schema for the second-pass (whitelist-context) query — returns a single match or null
SECOND_PASS_SCHEMA = {
    "type": "object",
    "properties": {
        "matched_ticket": {
            "type": ["string", "null"],
            "description": "The exact whitelist ticket number visible on the page, or null if none found.",
        },
        "source": {
            "type": ["string", "null"],
            "enum": ["printed", "sticker", "handwritten", None],
        },
        "confidence": {"type": "number"},
    },
    "required": ["matched_ticket", "source", "confidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an expert at reading ship ticket / consignment paperwork, including scanned documents with handwriting, printed text, and label stickers.

Your task: find ALL ship ticket numbers / consignment numbers on the page image.

CRITICAL RULES FOR HANDWRITTEN NUMBERS:
- Handwritten numbers on these pages are ALWAYS exactly 6 digits (e.g. "253983", "301532", "258066").
- The handwriting is messy and some digits may look ambiguous. Common misread pairs: 2↔3, 5↔6, 7↔1, 0↔6.
- If you see what looks like a 5-digit number in handwriting (e.g. "37983"), look more carefully — the leading digit is almost certainly present but faint or cramped. Re-read it as 6 digits.
- Numbers always appear after a prefix like "ST:", "ST#", "ST ", "Consignment #:", "Ship Ticket #", etc.
- Do NOT truncate or drop leading digits from handwritten numbers.

GENERAL RULES:
1. Numbers are 5–7 digits, sometimes with a suffix like "-1" or "-2" (e.g. "301532", "12345-1").
2. They appear as:
   - Computer-printed text: e.g. "Consignment #: 301532", "Ship Ticket # 257086"
   - Printed label stickers: e.g. "ST#258066", "ST# 258066", "ST#257086"
   - Handwriting: e.g. "ST 301532", "ST: 253983" — often messy, sometimes on photo pages of coins/banknotes
3. CROSSED-OUT RULE (critical): If a number is struck through / crossed out, do NOT report it as the ticket number. Instead:
   - Set crossed_out = true for the struck-through number
   - Report the replacement number written nearby as a separate candidate with crossed_out = false
   - Set corrected_from = the crossed-out value on the replacement candidate
4. If no number is visible (e.g. the page is just a photo of coins or items with no annotation), return an empty candidates list.
5. For each candidate return:
   - value: the digit string including any suffix (e.g. "301532" or "12345-1")
   - source: "printed" | "sticker" | "handwritten"
   - confidence: 0.0–1.0 (your certainty this is a real ticket number)
   - crossed_out: true if this number is struck through
   - corrected_from: the crossed-out value this replaces (empty string "" if not a correction)
   - second_pass: always false in this prompt (set to false for all candidates)

Be thorough — check all corners, margins, and any handwritten annotations. Do not hallucinate numbers that are not present. Do not truncate handwritten numbers.

PINK MARKER FIELD:
Also report whether a bright pink or magenta rectangular sticker is visible anywhere on the page.
- Set pink_marker = true if you see a vivid pink/magenta solid-colour rectangular sticker (not a white label, not a pale pink tint — it must be a clearly bright pink or hot-pink/magenta rectangle).
- Set pink_marker = false otherwise.
- This is a boundary marker used by the batch processing system; report it accurately."""

SECOND_PASS_SYSTEM_PROMPT = """You are an expert at reading ship ticket / consignment paperwork, including scanned documents with handwriting, printed text, and label stickers.

You are performing a targeted second-pass check. The first-pass detection found no ticket number on this page. You are given a list of the ONLY valid ticket numbers for this batch. Your task is to look very carefully at the page — including all corners, margins, stickers, and handwritten annotations — and determine whether ANY of those specific numbers appear on the page.

Important: Only return a number that is actually visible on the page. Do not guess or hallucinate. If none of the listed numbers appear, return null."""

STICKER_RETRY_SYSTEM_PROMPT = """You are an expert at reading ship ticket / consignment paperwork.

This page may contain many numbers — banknote serial numbers, PMG/NGC grade numbers, customer IDs, certificate numbers, and other numeric content. Your task is to find ONLY the ship ticket / consignment number.

The ship ticket number appears on a WHITE RECTANGULAR LABEL STICKER affixed to the page (usually in a corner — top-left, top-right, or bottom corner). The sticker format is:
- A 6-digit number printed at the top (this is the ship ticket number), e.g. "299198", "300291"
- Below it: a customer/consignor name
- Below that: a date or location code like "26 OCT HK", "260CTHK", "26OCTHK"
- Possibly a barcode above the number

DO NOT report any of these as the ship ticket number:
- Banknote serial numbers (alphanumeric, e.g. "B392987B", "A2192389A")
- PMG/NGC grade numbers (short, e.g. "63", "30", "55", "MS62")
- Customer IDs (e.g. "CID: 475545", "475545")
- Handwritten names or crossed-out text
- Certificate numbers on slabs
- Any number that is NOT on the white label sticker

Look carefully at ALL corners of the page for the white label sticker. Return ONLY the 6-digit number from the sticker. If no white label sticker is visible, return an empty candidates list."""


def render_page_to_jpeg(doc: fitz.Document, page_idx: int, dpi: int = 150) -> bytes:
    """Render a PDF page to JPEG bytes at the given DPI."""
    page = doc[page_idx]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return pix.tobytes("jpeg")


def detect_page(
    client: OpenAI,
    page_num: int,  # 1-indexed
    jpeg_bytes: bytes,
    model: str = DEFAULT_MODEL,
    max_retries: int = 5,
) -> dict:
    """Send one page image to the vision model and return structured detection result."""
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"This is page {page_num} of a scanned ship ticket batch. "
                        "Find all ship ticket / consignment numbers. "
                        "Remember: handwritten numbers are always 6 digits — do not drop leading digits."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "high"},
                },
            ],
        },
    ]

    last_exc_str = "unknown"
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "page_detection",
                        "strict": True,
                        "schema": PAGE_RESULT_SCHEMA,
                    },
                },
                max_completion_tokens=1024,
            )
            raw = resp.choices[0].message.content
            result = json.loads(raw)
            # Ensure second_pass=False on all first-pass candidates
            for c in result.get("candidates", []):
                c.setdefault("second_pass", False)
            # Propagate pink_marker (default False if model omits it)
            pink_marker = bool(result.get("pink_marker", False))
            # Log summary (no page content — privacy)
            candidates = result.get("candidates", [])
            log.info(
                "Page %d: %d candidate(s)%s: %s",
                page_num,
                len(candidates),
                " [PINK MARKER]" if pink_marker else "",
                [
                    f"{c['value']}({c['source'][:3]},conf={c['confidence']:.2f})"
                    for c in candidates
                ],
            )
            return {"page": page_num, "candidates": candidates, "pink_marker": pink_marker, "error": None}
        except Exception as exc:
            last_exc_str = str(exc)
            # Detect 429 (rate-limit) vs other errors for appropriate backoff
            exc_str_lower = last_exc_str.lower()
            is_rate_limit = "429" in last_exc_str or "rate limit" in exc_str_lower or "rate_limit" in exc_str_lower
            is_server_error = any(code in last_exc_str for code in ("500", "502", "503", "504"))
            if is_rate_limit:
                # Exponential backoff with jitter for rate limits: 4s, 8s, 16s, 32s, 64s
                wait = (2 ** (attempt + 2))
                log.warning("Page %d attempt %d rate-limited (429) — retrying in %ds", page_num, attempt + 1, wait)
            elif is_server_error:
                wait = 2 ** attempt
                log.warning("Page %d attempt %d server error — retrying in %ds", page_num, attempt + 1, wait)
            else:
                wait = 2 ** attempt
                log.warning("Page %d attempt %d failed: %s — retrying in %ds", page_num, attempt + 1, exc, wait)
            time.sleep(wait)

    log.error("Page %d: all %d retries exhausted. Last error: %s", page_num, max_retries, last_exc_str)
    return {
        "page": page_num,
        "candidates": [],
        "pink_marker": False,
        "error": f"max_retries_exceeded: {last_exc_str}",
    }


def detect_page_second_pass(
    client: OpenAI,
    page_num: int,
    jpeg_bytes: bytes,
    whitelist: list[str],
    model: str = DEFAULT_MODEL,
    max_retries: int = 3,
) -> dict | None:
    """
    Second-pass detection for a page that returned no candidates on the first pass.
    Only called for pages that are candidates for ORPHAN_LEADING_PAGES (i.e., the
    first page of the document or early pages before any detection).

    Returns a candidate dict with second_pass=True if a whitelist member is found,
    or None if still no match.
    """
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"
    whitelist_str = ", ".join(whitelist)

    messages = [
        {"role": "system", "content": SECOND_PASS_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"This is page {page_num} of a scanned ship ticket batch. "
                        f"The valid ticket numbers for this batch are: {whitelist_str}. "
                        "Look very carefully at every part of the page — corners, margins, stickers, "
                        "handwritten annotations, and any labels. "
                        "Does any of these specific numbers appear on this page? "
                        "If yes, return the exact number from the list, its source type, and your confidence. "
                        "If no, return null."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "high"},
                },
            ],
        },
    ]

    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "second_pass_detection",
                        "strict": True,
                        "schema": SECOND_PASS_SCHEMA,
                    },
                },
                max_completion_tokens=256,
            )
            raw = resp.choices[0].message.content
            result = json.loads(raw)
            matched = result.get("matched_ticket")
            if matched and matched in whitelist:
                source = result.get("source") or "sticker"
                confidence = float(result.get("confidence", 0.75))
                log.info(
                    "Page %d second-pass: found %s (%s, conf=%.2f)",
                    page_num, matched, source, confidence,
                )
                return {
                    "value": matched,
                    "source": source,
                    "confidence": confidence,
                    "crossed_out": False,
                    "corrected_from": "",
                    "second_pass": True,
                }
            else:
                log.info("Page %d second-pass: no whitelist match found", page_num)
                return None
        except Exception as exc:
            wait = 2 ** attempt
            log.warning("Page %d second-pass attempt %d failed: %s — retrying in %ds", page_num, attempt + 1, exc, wait)
            time.sleep(wait)

    log.error("Page %d: second-pass all retries exhausted", page_num)
    return None


def detect_page_sticker_retry(
    client: OpenAI,
    page_num: int,
    jpeg_bytes: bytes,
    model: str = DEFAULT_MODEL,
    max_retries: int = 3,
) -> list[dict]:
    """
    Sticker-focused retry for a page that returned no candidates on the first pass.
    Uses a prompt that explicitly focuses on the white rectangular label sticker
    and instructs the model to ignore banknote serials, grade numbers, and other
    numeric content that is not the ship ticket number.

    Returns a list of candidate dicts (may be empty), each with second_pass=False
    (this is a first-pass retry, not a whitelist-context second pass).
    """
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"

    messages = [
        {"role": "system", "content": STICKER_RETRY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"This is page {page_num} of a scanned ship ticket batch. "
                        "Find the ship ticket number on the white label sticker. "
                        "Ignore all other numbers on the page."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "high"},
                },
            ],
        },
    ]

    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "page_detection",
                        "strict": True,
                        "schema": PAGE_RESULT_SCHEMA,
                    },
                },
                max_completion_tokens=512,
            )
            raw = resp.choices[0].message.content
            result = json.loads(raw)
            candidates = result.get("candidates", [])
            # Ensure second_pass=False (this is a retry, not a whitelist-context pass)
            for c in candidates:
                c["second_pass"] = False
            log.info(
                "Page %d sticker-retry: %d candidate(s): %s",
                page_num,
                len(candidates),
                [f"{c['value']}({c['source'][:3]},conf={c['confidence']:.2f})" for c in candidates],
            )
            return candidates
        except Exception as exc:
            wait = 2 ** attempt
            log.warning(
                "Page %d sticker-retry attempt %d failed: %s — retrying in %ds",
                page_num, attempt + 1, exc, wait,
            )
            time.sleep(wait)

    log.error("Page %d: sticker-retry all retries exhausted", page_num)
    return []


def run_detection(
    pdf_path: str,
    checkpoint_path: str | None = None,
    workers: int = 5,
    model: str = DEFAULT_MODEL,
    dpi: int = 150,
    force_pages: list[int] | None = None,
    whitelist: list[str] | None = None,
) -> list[dict]:
    """
    Run detection on all pages of a PDF.
    Returns list of per-page results sorted by page number.
    Checkpoints results incrementally to checkpoint_path (JSON).
    force_pages: if set, re-run only these 1-indexed page numbers regardless of checkpoint.
    whitelist: if provided, enables second-pass detection for orphan-candidate pages.
    """
    client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_API_BASE from env

    # Load checkpoint if it exists
    checkpoint: dict[int, dict] = {}
    if checkpoint_path and Path(checkpoint_path).exists():
        with open(checkpoint_path) as f:
            saved = json.load(f)
        checkpoint = {r["page"]: r for r in saved}
        log.info("Loaded checkpoint with %d pages already done", len(checkpoint))

    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    log.info("PDF has %d pages", total_pages)

    # Determine which pages still need processing
    if force_pages:
        pending = [p for p in force_pages if 1 <= p <= total_pages]
        log.info("Force-rerunning %d pages: %s", len(pending), pending)
    else:
        pending = [i + 1 for i in range(total_pages) if (i + 1) not in checkpoint]
    log.info("%d pages pending detection", len(pending))

    # Render all pending pages first (fast, local)
    page_images: dict[int, bytes] = {}
    for page_num in pending:
        page_images[page_num] = render_page_to_jpeg(doc, page_num - 1, dpi=dpi)
    doc.close()

    # When force-rerunning specific pages, start from the full checkpoint
    # so the output file contains all pages, not just the forced ones.
    results: dict[int, dict] = dict(checkpoint)

    def save_checkpoint():
        if checkpoint_path:
            with open(checkpoint_path, "w") as f:
                json.dump(list(results.values()), f, indent=2)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(detect_page, client, pn, page_images[pn], model): pn
            for pn in pending
        }
        for future in as_completed(futures):
            pn = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                log.error("Page %d unexpected error: %s", pn, exc)
                result = {"page": pn, "candidates": [], "error": str(exc)}
            results[pn] = result
            save_checkpoint()
            log.info("Progress: %d / %d pages done", len(results), total_pages)

    # ── Sticker retry for any page that returned no candidates ─────────────────
    # After first-pass completes, retry any empty page with the sticker-focused
    # prompt. This catches pages where the first-pass prompt was confused by
    # dense numeric content (banknote serials, grade numbers, etc.) and missed
    # the white label sticker. This retry runs on ALL empty pages, not just
    # orphan-candidate pages.
    empty_pages = [pn for pn in pending if not results[pn].get("candidates")]
    if empty_pages:
        log.info(
            "Running sticker-retry on %d empty pages: %s",
            len(empty_pages), empty_pages,
        )
        # Ensure all empty pages are rendered
        missing_renders = [p for p in empty_pages if p not in page_images]
        if missing_renders:
            doc_retry = fitz.open(pdf_path)
            for pn in missing_renders:
                page_images[pn] = render_page_to_jpeg(doc_retry, pn - 1, dpi=dpi)
            doc_retry.close()

        for pn in empty_pages:
            retry_candidates = detect_page_sticker_retry(client, pn, page_images[pn], model)
            if retry_candidates:
                results[pn]["candidates"] = retry_candidates
                # pink_marker is already in results[pn] from the first pass; preserve it
                save_checkpoint()

    # ── Second-pass for orphan-candidate pages ────────────────────────────────
    # After sticker retry, identify pages that would still become ORPHAN_LEADING_PAGES
    # (i.e., leading empty pages before the first detection). Re-query these pages
    # with the whitelist context to catch any remaining misses.
    if whitelist:
        sorted_pages = [results[i + 1] for i in range(total_pages)]
        # Find the index of the first page with at least one candidate
        first_detected_idx = next(
            (i for i, r in enumerate(sorted_pages) if r.get("candidates")),
            None,
        )
        # Orphan candidates: all pages before the first detection
        orphan_candidate_pages = (
            list(range(1, first_detected_idx + 1)) if first_detected_idx is not None
            else list(range(1, total_pages + 1))
        )
        # Only run second pass on pages that had no candidates
        second_pass_pages = [
            p for p in orphan_candidate_pages
            if not results[p].get("candidates")
        ]
        if second_pass_pages:
            log.info(
                "Running second-pass on %d orphan-candidate pages: %s",
                len(second_pass_pages), second_pass_pages,
            )
            # Render pages if not already in page_images (e.g. loaded from checkpoint)
            if not page_images:
                doc2 = fitz.open(pdf_path)
                for pn in second_pass_pages:
                    if pn not in page_images:
                        page_images[pn] = render_page_to_jpeg(doc2, pn - 1, dpi=dpi)
                doc2.close()
            else:
                # Some pages may not be in page_images if they came from checkpoint
                missing_renders = [p for p in second_pass_pages if p not in page_images]
                if missing_renders:
                    doc2 = fitz.open(pdf_path)
                    for pn in missing_renders:
                        page_images[pn] = render_page_to_jpeg(doc2, pn - 1, dpi=dpi)
                    doc2.close()

            for pn in second_pass_pages:
                candidate = detect_page_second_pass(
                    client, pn, page_images[pn], whitelist, model
                )
                if candidate:
                    results[pn]["candidates"] = [candidate]
                    save_checkpoint()

    # ── Second-pass for UNMATCHED pages ─────────────────────────────────────
    # For pages that returned candidates but none matched the whitelist (UNMATCHED),
    # run the whitelist-context second-pass. The result is injected as an additional
    # candidate with second_pass=True so the grouping engine can apply Step 1 of
    # the UNMATCHED pipeline (Decision 8).
    # This only runs when a whitelist is provided.
    if whitelist:
        # Identify pages with candidates where none are in the whitelist
        # (We do a quick check: any candidate value exactly in whitelist?)
        def has_whitelist_match(page_result: dict) -> bool:
            for c in page_result.get("candidates", []):
                if c.get("value") in whitelist:
                    return True
            return False

        unmatched_pages = [
            pn for pn in range(1, total_pages + 1)
            if results[pn].get("candidates") and not has_whitelist_match(results[pn])
        ]
        if unmatched_pages:
            log.info(
                "Running second-pass on %d UNMATCHED pages: %s",
                len(unmatched_pages), unmatched_pages,
            )
            # Render any pages not already in page_images
            missing_renders = [p for p in unmatched_pages if p not in page_images]
            if missing_renders:
                doc3 = fitz.open(pdf_path)
                for pn in missing_renders:
                    page_images[pn] = render_page_to_jpeg(doc3, pn - 1, dpi=dpi)
                doc3.close()

            for pn in unmatched_pages:
                candidate = detect_page_second_pass(
                    client, pn, page_images[pn], whitelist, model
                )
                if candidate:
                    # Inject as additional candidate — grouping engine will prefer it
                    # via the SECOND_PASS flag and resolve_page logic
                    results[pn]["candidates"].append(candidate)
                    save_checkpoint()

    # Return sorted by page number
    return [results[i + 1] for i in range(total_pages)]


def main():
    parser = argparse.ArgumentParser(description="Detect ship ticket numbers in a PDF")
    parser.add_argument("pdf", help="Path to input PDF")
    parser.add_argument("--checkpoint", default=None, help="Path to checkpoint JSON file")
    parser.add_argument("--workers", type=int, default=5, help="Concurrent API workers")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Vision model ID")
    parser.add_argument("--dpi", type=int, default=150, help="Render DPI")
    parser.add_argument("--output", default=None, help="Output JSON path (default: stdout)")
    parser.add_argument(
        "--force-pages",
        default=None,
        help="Comma-separated 1-indexed page numbers to re-run regardless of checkpoint",
    )
    parser.add_argument(
        "--whitelist",
        default=None,
        help="Comma-separated whitelist ticket numbers for second-pass orphan detection",
    )
    args = parser.parse_args()

    force_pages = None
    if args.force_pages:
        force_pages = [int(p.strip()) for p in args.force_pages.split(",")]

    whitelist = None
    if args.whitelist:
        whitelist = [t.strip() for t in args.whitelist.replace("\n", ",").split(",") if t.strip()]

    results = run_detection(
        pdf_path=args.pdf,
        checkpoint_path=args.checkpoint,
        workers=args.workers,
        model=args.model,
        dpi=args.dpi,
        force_pages=force_pages,
        whitelist=whitelist,
    )

    output = json.dumps(results, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        log.info("Results written to %s", args.output)
    else:
        print(output)


if __name__ == "__main__":
    main()
