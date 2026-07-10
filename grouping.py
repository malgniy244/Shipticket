"""
grouping.py — Task 2: Grouping engine (pure logic, no API calls)

Implements section 3.4 of the spec exactly, in the order specified.
Input:  whitelist (list of ticket number strings) + per-page detection results
Output: list of Block objects with flags, ready for the review screen

All rules are applied in the order given in spec section 3.4.

AMENDMENT (Decision 8, 2026-07-09):
  Rule 5 extended with a three-step UNMATCHED pipeline:
    1. If the page has a second-pass candidate (second_pass=True), use it with SECOND_PASS flag.
    2. Else if the raw unmatched value is within edit-distance ≤ 2 of the previous block's
       ticket, inherit with INHERITED_UNMATCHED soft flag (not Confirm-blocking).
    3. Otherwise: keep UNMATCHED_NUMBER hard flag with neighbor suggestion (unchanged).
  INHERITED_UNMATCHED and SECOND_PASS pages remain RED in the filmstrip.
  MISSING_TICKET completeness check remains a hard flag (unchanged backstop).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TICKET_RE = re.compile(r"^\d{5,7}(-\d{1,2})?$")

# Flag codes (string constants — used in Block.flags)
FLAG_UNMATCHED_NUMBER = "UNMATCHED_NUMBER"
FLAG_AMBIGUOUS_SUFFIX = "AMBIGUOUS_SUFFIX"
FLAG_AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"        # second-pass conflicts with inheritance
FLAG_CORRECTION_CONFLICT = "CORRECTION_CONFLICT"
FLAG_CORRECTION_OBSERVED = "CORRECTION_OBSERVED"
FLAG_ORPHAN_LEADING_PAGES = "ORPHAN_LEADING_PAGES"
FLAG_NON_CONTIGUOUS = "NON_CONTIGUOUS"
FLAG_MISSING_TICKET = "MISSING_TICKET"
FLAG_LOW_CONFIDENCE = "LOW_CONFIDENCE"
FLAG_FUZZY_RESOLVED = "FUZZY_RESOLVED"          # soft: edit-distance-1 auto-resolution
FLAG_SECOND_PASS = "SECOND_PASS"                # soft: found via whitelist-context second pass
FLAG_INHERITED_UNMATCHED = "INHERITED_UNMATCHED"  # soft: unmatched page inherited from prev block
FLAG_NO_PRINTED_COVER = "NO_PRINTED_COVER"       # soft (TIB only): block start not a printed cover
FLAG_PINK_MARKER = "PINK_MARKER"                 # soft (non-TIB): boundary signal page

# Hard flags that block Confirm until resolved
HARD_FLAGS = {FLAG_UNMATCHED_NUMBER, FLAG_AMBIGUOUS_SUFFIX, FLAG_AMBIGUOUS_MATCH,
              FLAG_ORPHAN_LEADING_PAGES}


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """A single detected number on a page (from the detection module)."""
    value: str
    source: str          # "printed" | "sticker" | "handwritten"
    confidence: float
    crossed_out: bool
    corrected_from: str  # empty string if not a correction
    second_pass: bool = False  # True if found via whitelist-context second pass


@dataclass
class PageDetection:
    """Detection results for one page."""
    page: int            # 1-indexed
    candidates: list[Candidate]
    error: Optional[str] = None


@dataclass
class ResolvedPage:
    """
    The result of applying grouping rules to a single page's detections.
    Produced by resolve_page(); consumed by build_blocks().
    """
    page: int
    resolved_ticket: Optional[str]    # None = unresolved (ORPHAN or UNMATCHED)
    flags: list[str]
    detection_sources: list[str]      # sources of the winning candidates
    max_confidence: float
    unmatched_raw: list[str]          # raw detected values that had no whitelist match
    corrected_from: Optional[str]     # if a crossed-out correction was observed
    # Suggestion fields (UI only — never used for auto-assignment)
    suggestion: Optional[str] = None  # whitelist-context second-pass suggestion
    neighbor_suggestion: Optional[str] = None  # set later by build_blocks()


@dataclass
class Block:
    """
    A contiguous run of pages with the same resolved ticket.
    The review screen shows one row per block.
    """
    ticket: Optional[str]    # None = unassigned (orphan or unmatched)
    pages: list[int]         # 1-indexed, sorted
    flags: list[str]
    detection_sources: list[str]
    max_confidence: float
    unmatched_raw: list[str]
    corrected_from: Optional[str]
    # Suggestion fields (UI only)
    suggestion: Optional[str] = None
    neighbor_suggestion: Optional[str] = None

    @property
    def page_range(self) -> str:
        if not self.pages:
            return ""
        if len(self.pages) == 1:
            return str(self.pages[0])
        return f"{self.pages[0]}–{self.pages[-1]}"

    @property
    def has_hard_flag(self) -> bool:
        return any(f in HARD_FLAGS for f in self.flags)


@dataclass
class GroupingResult:
    """Top-level output of the grouping engine."""
    blocks: list[Block]
    missing_tickets: list[str]        # whitelist entries with zero pages
    unmatched_values: list[str]       # detected values that matched nothing
    total_pages: int


# ─────────────────────────────────────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────────────────────────────────────

def digit_edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two strings (digit or otherwise)."""
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[j] = prev[j - 1]
            else:
                dp[j] = 1 + min(prev[j - 1], prev[j], dp[j - 1])
    return dp[n]


def base_number(ticket: str) -> str:
    """Return the base number without any suffix (e.g. '12345-1' → '12345')."""
    return ticket.split("-")[0]


def fuzzy_match_whitelist(value: str, whitelist: list[str]) -> list[str]:
    """
    Match a detected value against the whitelist.
    Returns a list of matching whitelist entries:
      - Exact match → [exact]
      - Edit-distance ≤ 1, unique → [that entry]
      - Edit-distance ≤ 1, multiple → all of them (AMBIGUOUS)
      - No match → []
    """
    if value in whitelist:
        return [value]
    close = [w for w in whitelist if digit_edit_distance(value, w) <= 1]
    return close


def validate_ticket_format(value: str) -> bool:
    """Return True if value matches the ticket number regex."""
    return bool(TICKET_RE.match(value))


# ─────────────────────────────────────────────────────────────────────────────
# Rule 1+2+3+4: Resolve a single page's detections to a ticket identity
# ─────────────────────────────────────────────────────────────────────────────

def resolve_page(detection: PageDetection, whitelist: list[str]) -> ResolvedPage:
    """
    Apply rules 1–4 from spec section 3.4 to a single page's candidates.
    Returns a ResolvedPage (ticket may be None if unresolved).
    """
    candidates = detection.candidates
    flags: list[str] = []
    unmatched_raw: list[str] = []
    corrected_from: Optional[str] = None
    suggestion: Optional[str] = None

    # ── Separate active (non-crossed-out) from crossed-out candidates ──
    active = [c for c in candidates if not c.crossed_out]
    crossed = [c for c in candidates if c.crossed_out]

    # ── Rule 4: Crossed-out handling ──
    for c in active:
        if c.corrected_from:
            corrected_from = c.corrected_from
            replacement_in_wl = c.value in whitelist or bool(fuzzy_match_whitelist(c.value, whitelist))
            old_in_wl = c.corrected_from in whitelist
            if old_in_wl and replacement_in_wl:
                flags.append(FLAG_CORRECTION_CONFLICT)
            else:
                flags.append(FLAG_CORRECTION_OBSERVED)

    # ── Rule 1: Whitelist fuzzy match for each active candidate ──
    matched_candidates: list[tuple[Candidate, str]] = []  # (candidate, resolved_ticket)
    for c in active:
        if not validate_ticket_format(c.value):
            unmatched_raw.append(c.value)
            continue

        # Special case: bare base number with multiple suffixed whitelist entries → AMBIGUOUS_SUFFIX
        if "-" not in c.value:
            suffixed_matches = [w for w in whitelist
                                if "-" in w and base_number(w) == c.value]
            if len(suffixed_matches) > 1:
                flags.append(FLAG_AMBIGUOUS_SUFFIX)
                unmatched_raw.append(c.value)
                continue
            elif len(suffixed_matches) == 1:
                matched_candidates.append((c, suffixed_matches[0]))
                continue

        matches = fuzzy_match_whitelist(c.value, whitelist)
        if len(matches) == 0:
            unmatched_raw.append(c.value)
            flags.append(FLAG_UNMATCHED_NUMBER)
        elif len(matches) == 1:
            if c.value != matches[0]:
                flags.append(FLAG_FUZZY_RESOLVED)
            matched_candidates.append((c, matches[0]))
        else:
            bases = {base_number(m) for m in matches}
            if len(bases) == 1:
                for m in matches:
                    matched_candidates.append((c, m))
            else:
                flags.append(FLAG_AMBIGUOUS_SUFFIX)
                unmatched_raw.append(c.value)

    if not matched_candidates:
        return ResolvedPage(
            page=detection.page,
            resolved_ticket=None,
            flags=flags,
            detection_sources=[c.source for c in active],
            max_confidence=max((c.confidence for c in active), default=0.0),
            unmatched_raw=unmatched_raw,
            corrected_from=corrected_from,
            suggestion=suggestion,
        )

    # ── Rule 2: Suffix wins ──
    suffixed = [(c, t) for c, t in matched_candidates if "-" in t]
    if suffixed:
        matched_candidates = suffixed

    # ── Rule 3: Suffix ambiguity ──
    resolved_tickets = list(dict.fromkeys(t for _, t in matched_candidates))

    if len(resolved_tickets) > 1:
        bases = {base_number(t) for t in resolved_tickets}
        if len(bases) == 1:
            flags.append(FLAG_AMBIGUOUS_SUFFIX)
            return ResolvedPage(
                page=detection.page,
                resolved_ticket=None,
                flags=flags,
                detection_sources=[c.source for c, _ in matched_candidates],
                max_confidence=max(c.confidence for c, _ in matched_candidates),
                unmatched_raw=unmatched_raw,
                corrected_from=corrected_from,
                suggestion=suggestion,
            )
        else:
            best = max(matched_candidates, key=lambda ct: ct[0].confidence)
            resolved_tickets = [best[1]]
            matched_candidates = [best]

    resolved_ticket = resolved_tickets[0]

    # ── Rule 9: Low confidence ──
    max_conf = max(c.confidence for c, _ in matched_candidates)
    if max_conf < 0.7:
        flags.append(FLAG_LOW_CONFIDENCE)

    # ── SECOND_PASS soft flag ──
    if any(c.second_pass for c, _ in matched_candidates):
        flags.append(FLAG_SECOND_PASS)

    return ResolvedPage(
        page=detection.page,
        resolved_ticket=resolved_ticket,
        flags=flags,
        detection_sources=[c.source for c, _ in matched_candidates],
        max_confidence=max_conf,
        unmatched_raw=unmatched_raw,
        corrected_from=corrected_from,
        suggestion=suggestion,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rule 5+6+7+8: Build blocks from resolved pages
# ─────────────────────────────────────────────────────────────────────────────

def build_blocks(
    resolved_pages: list[ResolvedPage],
    whitelist: list[str],
) -> GroupingResult:
    """
    Apply rules 5–8 from spec section 3.4 to build the final block list.

    AMENDMENT (Decision 8): Rule 5 extended with a three-step UNMATCHED pipeline.
    When a page has FLAG_UNMATCHED_NUMBER:
      Step 1: If the page has a second-pass candidate (FLAG_SECOND_PASS on the
              resolved page), it was already resolved — use it.
              If that ticket conflicts with what inheritance would give AND
              confidence is low → AMBIGUOUS_MATCH hard flag instead.
      Step 2: Else if the raw unmatched value is within edit-distance ≤ 2 of the
              previous block's ticket → inherit with INHERITED_UNMATCHED soft flag.
      Step 3: Otherwise → keep UNMATCHED_NUMBER hard flag (unchanged).
    """
    total_pages = len(resolved_pages)

    # ── Rule 5: Inheritance + UNMATCHED pipeline ──
    inherited: list[ResolvedPage] = []
    first_resolved_idx: Optional[int] = None

    for i, rp in enumerate(resolved_pages):
        if rp.resolved_ticket is not None:
            # Page resolved cleanly (or via second-pass/fuzzy)
            if first_resolved_idx is None:
                first_resolved_idx = i
            inherited.append(rp)

        elif FLAG_UNMATCHED_NUMBER in rp.flags or FLAG_AMBIGUOUS_SUFFIX in rp.flags:
            # ── Three-step UNMATCHED pipeline ──
            if first_resolved_idx is None:
                # Before first detection → ORPHAN (unchanged)
                orphan = ResolvedPage(
                    page=rp.page,
                    resolved_ticket=None,
                    flags=list(set(rp.flags) | {FLAG_ORPHAN_LEADING_PAGES}),
                    detection_sources=rp.detection_sources,
                    max_confidence=rp.max_confidence,
                    unmatched_raw=rp.unmatched_raw,
                    corrected_from=rp.corrected_from,
                    suggestion=rp.suggestion,
                )
                inherited.append(orphan)
                continue

            # Determine what inheritance would give
            prev_ticket: Optional[str] = None
            for prev_rp in reversed(inherited):
                if prev_rp.resolved_ticket is not None:
                    prev_ticket = prev_rp.resolved_ticket
                    break

            # Step 1: Second-pass already resolved this page
            # (The detection layer ran second-pass on UNMATCHED pages and injected
            # a second_pass=True candidate. resolve_page() would have resolved it
            # and set FLAG_SECOND_PASS. But if we're here, resolved_ticket is None,
            # meaning the second-pass candidate was also unmatched. So step 1 is
            # already handled — if second-pass succeeded, resolved_ticket would not
            # be None. We only need to handle the AMBIGUOUS_MATCH case: if the page
            # carries FLAG_SECOND_PASS but resolved_ticket is None, that means the
            # second pass returned a ticket that conflicts. Flag AMBIGUOUS_MATCH.)
            if FLAG_SECOND_PASS in rp.flags and rp.resolved_ticket is None:
                # Second pass returned something but it conflicted → AMBIGUOUS_MATCH
                ambig = ResolvedPage(
                    page=rp.page,
                    resolved_ticket=None,
                    flags=[FLAG_AMBIGUOUS_MATCH],
                    detection_sources=rp.detection_sources,
                    max_confidence=rp.max_confidence,
                    unmatched_raw=rp.unmatched_raw,
                    corrected_from=rp.corrected_from,
                    suggestion=rp.suggestion,
                )
                inherited.append(ambig)
                continue

            # Step 2: Edit-distance ≤ 2 from previous block's ticket → inherit
            if prev_ticket is not None and rp.unmatched_raw:
                raw_val = rp.unmatched_raw[0]  # primary unmatched value
                dist = digit_edit_distance(raw_val, prev_ticket)
                if dist <= 2:
                    inherited_page = ResolvedPage(
                        page=rp.page,
                        resolved_ticket=prev_ticket,
                        flags=[FLAG_INHERITED_UNMATCHED],
                        detection_sources=rp.detection_sources,
                        max_confidence=rp.max_confidence,
                        unmatched_raw=rp.unmatched_raw,
                        corrected_from=rp.corrected_from,
                        suggestion=rp.suggestion,
                    )
                    inherited.append(inherited_page)
                    continue

            # Step 3: Keep UNMATCHED_NUMBER hard flag (unchanged)
            inherited.append(rp)

        else:
            # No detection (empty page) → standard inheritance
            if first_resolved_idx is None:
                orphan = ResolvedPage(
                    page=rp.page,
                    resolved_ticket=None,
                    flags=list(set(rp.flags) | {FLAG_ORPHAN_LEADING_PAGES}),
                    detection_sources=rp.detection_sources,
                    max_confidence=rp.max_confidence,
                    unmatched_raw=rp.unmatched_raw,
                    corrected_from=rp.corrected_from,
                    suggestion=rp.suggestion,
                )
                inherited.append(orphan)
            else:
                prev_ticket = None
                for prev_rp in reversed(inherited):
                    if prev_rp.resolved_ticket is not None:
                        prev_ticket = prev_rp.resolved_ticket
                        break
                prev_sources = []
                for prev_rp in reversed(inherited):
                    if prev_rp.detection_sources:
                        prev_sources = prev_rp.detection_sources
                        break
                inherited_page = ResolvedPage(
                    page=rp.page,
                    resolved_ticket=prev_ticket,
                    flags=rp.flags,
                    detection_sources=prev_sources,
                    max_confidence=rp.max_confidence,
                    unmatched_raw=rp.unmatched_raw,
                    corrected_from=rp.corrected_from,
                    suggestion=rp.suggestion,
                )
                inherited.append(inherited_page)

    # ── Rule 6: Build contiguous blocks ──
    # KEY CHANGE (Issue 1 fix): UNMATCHED pages with DIFFERENT raw detected values
    # must NOT be merged into one block. Each distinct raw value gets its own block.
    # Pages with the same raw UNMATCHED value (or no raw value) can still be grouped.
    raw_blocks: list[Block] = []
    if not inherited:
        return GroupingResult(blocks=[], missing_tickets=whitelist[:], unmatched_values=[], total_pages=total_pages)

    def pages_can_merge(rp_a: ResolvedPage, rp_b: ResolvedPage) -> bool:
        """
        Return True if rp_b can be appended to the current block ending with rp_a.
        Two pages can merge if:
          - They have the same resolved ticket (or both None)
          - If both are UNMATCHED (resolved_ticket is None), they must have the
            same primary raw unmatched value (so different misreads don't merge)
        """
        if rp_a.resolved_ticket != rp_b.resolved_ticket:
            return False
        if rp_a.resolved_ticket is None:
            # Both unresolved — only merge if same raw value (or both empty)
            raw_a = rp_a.unmatched_raw[0] if rp_a.unmatched_raw else None
            raw_b = rp_b.unmatched_raw[0] if rp_b.unmatched_raw else None
            if raw_a != raw_b:
                return False
        return True

    current_rp = inherited[0]
    current_ticket = current_rp.resolved_ticket
    current_pages = [current_rp.page]
    current_flags = list(current_rp.flags)
    current_sources = list(current_rp.detection_sources)
    current_max_conf = current_rp.max_confidence
    current_unmatched = list(current_rp.unmatched_raw)
    current_corrected = current_rp.corrected_from
    current_suggestion = current_rp.suggestion

    def flush_block():
        raw_blocks.append(Block(
            ticket=current_ticket,
            pages=list(current_pages),
            flags=list(dict.fromkeys(current_flags)),
            detection_sources=list(dict.fromkeys(current_sources)),
            max_confidence=current_max_conf,
            unmatched_raw=list(current_unmatched),
            corrected_from=current_corrected,
            suggestion=current_suggestion,
        ))

    for rp in inherited[1:]:
        if pages_can_merge(current_rp, rp):
            current_pages.append(rp.page)
            current_flags.extend(rp.flags)
            current_sources.extend(rp.detection_sources)
            current_max_conf = max(current_max_conf, rp.max_confidence)
            current_unmatched.extend(rp.unmatched_raw)
            if rp.corrected_from:
                current_corrected = rp.corrected_from
            if rp.suggestion:
                current_suggestion = rp.suggestion
            # Update current_rp to the latest page for future merge checks
            current_rp = rp
        else:
            flush_block()
            current_rp = rp
            current_ticket = rp.resolved_ticket
            current_pages = [rp.page]
            current_flags = list(rp.flags)
            current_sources = list(rp.detection_sources)
            current_max_conf = rp.max_confidence
            current_unmatched = list(rp.unmatched_raw)
            current_corrected = rp.corrected_from
            current_suggestion = rp.suggestion

    flush_block()

    # ── Rule 7: Non-contiguous ticket detection ──
    ticket_block_indices: dict[Optional[str], list[int]] = {}
    for idx, b in enumerate(raw_blocks):
        ticket_block_indices.setdefault(b.ticket, []).append(idx)

    def has_different_ticket_between(indices: list[int]) -> bool:
        ticket_set = {raw_blocks[i].ticket for i in indices}
        first_idx = min(indices)
        last_idx = max(indices)
        for j in range(first_idx + 1, last_idx):
            t = raw_blocks[j].ticket
            if t is not None and t not in ticket_set:
                return True
        return False

    merged_blocks: list[Block] = []
    merged_tickets: set[Optional[str]] = set()

    for b in raw_blocks:
        if b.ticket is None:
            # Unresolved blocks are never merged with each other
            merged_blocks.append(b)
            continue

        if b.ticket in merged_tickets:
            continue

        all_indices = ticket_block_indices[b.ticket]

        if len(all_indices) == 1:
            merged_blocks.append(b)
            merged_tickets.add(b.ticket)
        else:
            is_non_contiguous = has_different_ticket_between(all_indices)
            all_pages = []
            all_flags = []
            all_sources = []
            max_conf = 0.0
            all_unmatched = []
            merged_corrected = None
            merged_suggestion = None
            for idx in all_indices:
                ob = raw_blocks[idx]
                all_pages.extend(ob.pages)
                all_flags.extend(ob.flags)
                all_sources.extend(ob.detection_sources)
                max_conf = max(max_conf, ob.max_confidence)
                all_unmatched.extend(ob.unmatched_raw)
                if ob.corrected_from:
                    merged_corrected = ob.corrected_from
                if ob.suggestion:
                    merged_suggestion = ob.suggestion
            all_pages.sort()
            if is_non_contiguous:
                all_flags.append(FLAG_NON_CONTIGUOUS)
            merged_blocks.append(Block(
                ticket=b.ticket,
                pages=all_pages,
                flags=list(dict.fromkeys(all_flags)),
                detection_sources=list(dict.fromkeys(all_sources)),
                max_confidence=max_conf,
                unmatched_raw=list(dict.fromkeys(all_unmatched)),
                corrected_from=merged_corrected,
                suggestion=merged_suggestion,
            ))
            merged_tickets.add(b.ticket)

    # ── Rule 8: Completeness checks ──
    assigned_tickets = {b.ticket for b in merged_blocks if b.ticket is not None}
    missing_tickets = [w for w in whitelist if w not in assigned_tickets]

    all_unmatched_values: list[str] = []
    for b in merged_blocks:
        all_unmatched_values.extend(b.unmatched_raw)
    all_unmatched_values = list(dict.fromkeys(all_unmatched_values))

    # ── Neighbor pre-selection (UI suggestion — never auto-assignment) ──
    page_to_ticket: dict[int, str] = {}
    for b in merged_blocks:
        if b.ticket is not None:
            for p in b.pages:
                page_to_ticket[p] = b.ticket

    for b in merged_blocks:
        needs_suggestion = (
            FLAG_UNMATCHED_NUMBER in b.flags
            or FLAG_AMBIGUOUS_SUFFIX in b.flags
            or FLAG_AMBIGUOUS_MATCH in b.flags
            or FLAG_ORPHAN_LEADING_PAGES in b.flags
        )
        if not needs_suggestion:
            continue
        if not b.pages:
            continue
        first_page = min(b.pages)
        last_page = max(b.pages)

        prev_ticket: Optional[str] = None
        for p in range(first_page - 1, 0, -1):
            if p in page_to_ticket:
                prev_ticket = page_to_ticket[p]
                break

        next_ticket: Optional[str] = None
        for p in range(last_page + 1, total_pages + 1):
            if p in page_to_ticket:
                next_ticket = page_to_ticket[p]
                break

        if prev_ticket is not None and prev_ticket == next_ticket:
            b.neighbor_suggestion = prev_ticket
        elif FLAG_ORPHAN_LEADING_PAGES in b.flags and prev_ticket is None and next_ticket is not None:
            b.neighbor_suggestion = next_ticket

    return GroupingResult(
        blocks=merged_blocks,
        missing_tickets=missing_tickets,
        unmatched_values=all_unmatched_values,
        total_pages=total_pages,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Top-level entry point
# ─────────────────────────────────────────────────────────────────────────────

def group_detections(
    raw_detections: list[dict],
    whitelist: list[str],
    batch_type: str = "tib",
) -> GroupingResult:
    """
    Main entry point.
    raw_detections: list of dicts as produced by detect.py
    whitelist: list of validated ticket number strings
    batch_type: "tib" | "non_tib" — affects proposal-layer flags only, not core rules
    Returns GroupingResult.
    """
    page_detections: list[PageDetection] = []
    for d in raw_detections:
        candidates = [
            Candidate(
                value=c["value"],
                source=c["source"],
                confidence=c["confidence"],
                crossed_out=c["crossed_out"],
                corrected_from=c.get("corrected_from", ""),
                second_pass=bool(c.get("second_pass", False)),
            )
            for c in d.get("candidates", [])
        ]
        page_detections.append(PageDetection(
            page=d["page"],
            candidates=candidates,
            error=d.get("error"),
        ))

    page_detections.sort(key=lambda p: p.page)
    resolved = [resolve_page(pd, whitelist) for pd in page_detections]
    result = build_blocks(resolved, whitelist)

    # ── Proposal-layer flags (batch_type-specific, never affect core rules) ──
    # Build a lookup: page → raw detection dict (for pink_marker access)
    raw_by_page: dict[int, dict] = {d["page"]: d for d in raw_detections}

    if batch_type == "tib":
        # TIB: each block should start with a printed cover page.
        # If the first page of a block has no printed-source candidate, add soft flag.
        for block in result.blocks:
            if not block.pages:
                continue
            first_page = block.pages[0]
            raw = raw_by_page.get(first_page, {})
            first_page_candidates = raw.get("candidates", [])
            has_printed_cover = any(
                c.get("source") == "printed" for c in first_page_candidates
            )
            if not has_printed_cover:
                if FLAG_NO_PRINTED_COVER not in block.flags:
                    block.flags.append(FLAG_NO_PRINTED_COVER)

    elif batch_type == "non_tib":
        # Non-TIB: pink marker is a boundary signal.
        # Add PINK_MARKER soft flag to any block whose first page has pink_marker=True.
        # The marker is a signal only — it never changes ticket identity.
        for block in result.blocks:
            if not block.pages:
                continue
            first_page = block.pages[0]
            raw = raw_by_page.get(first_page, {})
            if raw.get("pink_marker", False):
                if FLAG_PINK_MARKER not in block.flags:
                    block.flags.append(FLAG_PINK_MARKER)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Phase A → Phase B: Repool identities from confirmed boundaries
# ─────────────────────────────────────────────────────────────────────────────

def repool_from_boundaries(
    raw_detections: list[dict],
    whitelist: list[str],
    boundaries: list[int],  # page numbers where new blocks START (1-indexed)
    batch_type: str = "tib",
) -> GroupingResult:
    """
    Given confirmed block boundaries (list of start pages), pool ALL detections
    within each block and apply identity rules to the pool as a whole.

    This is the same code path used by Phase B and the final Confirm.
    The grouping engine's identity rules (whitelist matching, suffix-wins,
    fuzzy-match, flags) are applied identically — only the block structure
    (boundaries) is supplied by the human instead of derived by inheritance.

    Block identity pooling rules:
      1. Collect all candidates from all pages in the block.
      2. Apply resolve_page() logic to the pooled candidates (treating the
         block as a single virtual "page").
      3. If the pool has zero usable detections → block is unidentified.
      4. If the pool has exact-match detections for TWO different whitelist
         tickets → CORRECTION_CONFLICT hard flag with suggested split point
         (the page boundary between the two detection groups).
      5. Completeness checks (MISSING_TICKET) applied as before.
    """
    total_pages = len(raw_detections)

    # Build per-page detection lookup
    page_detections: dict[int, PageDetection] = {}
    for d in raw_detections:
        candidates = [
            Candidate(
                value=c["value"],
                source=c["source"],
                confidence=c["confidence"],
                crossed_out=c["crossed_out"],
                corrected_from=c.get("corrected_from", ""),
                second_pass=bool(c.get("second_pass", False)),
            )
            for c in d.get("candidates", [])
        ]
        page_detections[d["page"]] = PageDetection(
            page=d["page"],
            candidates=candidates,
            error=d.get("error"),
        )

    # Determine block page ranges from boundaries
    sorted_boundaries = sorted(set(boundaries))
    if not sorted_boundaries or sorted_boundaries[0] != 1:
        sorted_boundaries = [1] + [b for b in sorted_boundaries if b != 1]
    sorted_boundaries = sorted(sorted_boundaries)

    block_ranges: list[tuple[int, int]] = []
    for i, start in enumerate(sorted_boundaries):
        end = sorted_boundaries[i + 1] - 1 if i + 1 < len(sorted_boundaries) else total_pages
        block_ranges.append((start, end))

    blocks: list[Block] = []

    for (start, end) in block_ranges:
        block_pages = list(range(start, end + 1))

        # Pool all candidates from all pages in this block
        all_candidates: list[Candidate] = []
        page_candidate_map: dict[int, list[Candidate]] = {}  # for split suggestion
        for pg in block_pages:
            pd = page_detections.get(pg)
            if pd:
                all_candidates.extend(pd.candidates)
                page_candidate_map[pg] = pd.candidates

        # ── Pool filtering: discard noisy unmatched candidates when exact/fuzzy matches exist ──
        # In a multi-page block, photo pages often return garbled handwriting readings
        # that are far from any whitelist entry. These should not pollute the pool when
        # the block already has clear exact-match or fuzzy-match (edit-dist ≤ 1) candidates.
        def _is_matchable(c: Candidate) -> bool:
            """True if this candidate is an exact or fuzzy (edit-dist ≤ 1) whitelist match."""
            if c.value in whitelist:
                return True
            return any(digit_edit_distance(c.value, w) <= 1 for w in whitelist)

        matchable = [c for c in all_candidates if not c.crossed_out and _is_matchable(c)]
        filtered_candidates = matchable if matchable else all_candidates

        # Apply identity rules to the pooled candidates
        pool_detection = PageDetection(
            page=start,  # virtual page number = block start
            candidates=filtered_candidates,
        )
        resolved = resolve_page(pool_detection, whitelist)

        flags = list(resolved.flags)
        ticket = resolved.resolved_ticket
        sources = list(resolved.detection_sources)
        max_conf = resolved.max_confidence
        unmatched = list(resolved.unmatched_raw)
        corrected_from = resolved.corrected_from
        suggestion = resolved.suggestion
        neighbor_suggestion = None

        # ── Conflict detection: two different exact-match tickets in one block ──
        # If the pool contains exact matches for two different whitelist tickets,
        # this is a missed boundary. Hard-flag and suggest the split point.
        exact_by_ticket: dict[str, list[int]] = {}  # ticket → pages where it appears
        for pg in block_pages:
            for c in page_candidate_map.get(pg, []):
                if not c.crossed_out and c.value in whitelist:
                    exact_by_ticket.setdefault(c.value, []).append(pg)

        if len(exact_by_ticket) >= 2:
            # Missed boundary — find the split point between the two ticket groups
            ticket_pages = sorted(exact_by_ticket.items(), key=lambda kv: min(kv[1]))
            split_after = max(ticket_pages[0][1])  # last page of first ticket group
            flags = [FLAG_CORRECTION_CONFLICT]  # repurpose as missed-boundary flag
            suggestion = f"Split after page {split_after} (detected: {', '.join(t for t,_ in ticket_pages)})"
            ticket = ticket_pages[0][0]  # tentatively assign to first ticket

        # ── Edit-distance-2 suggestion for zero-detection blocks ──
        # If the block has zero usable detections, surface the whitelist ticket
        # within edit-distance ≤ 2 of the best raw value as a soft suggestion.
        if ticket is None and unmatched:
            best_raw = unmatched[0]
            close = [(digit_edit_distance(best_raw, w), w) for w in whitelist]
            close.sort()
            if close and close[0][0] <= 2:
                suggestion = close[0][1]
                flags = list(set(flags) | {FLAG_UNMATCHED_NUMBER})

        blocks.append(Block(
            ticket=ticket,
            pages=block_pages,
            flags=list(dict.fromkeys(flags)),
            detection_sources=list(dict.fromkeys(sources)),
            max_confidence=max_conf,
            unmatched_raw=unmatched,
            corrected_from=corrected_from,
            suggestion=suggestion,
            neighbor_suggestion=neighbor_suggestion,
        ))

    # ── Completeness checks ──
    assigned_tickets = {b.ticket for b in blocks if b.ticket is not None}
    missing_tickets = [w for w in whitelist if w not in assigned_tickets]

    all_unmatched: list[str] = []
    for b in blocks:
        all_unmatched.extend(b.unmatched_raw)
    all_unmatched = list(dict.fromkeys(all_unmatched))

    # ── Proposal-layer flags (batch_type-specific, never affect core rules) ──
    raw_by_page: dict[int, dict] = {d["page"]: d for d in raw_detections}

    if batch_type == "tib":
        for block in blocks:
            if not block.pages:
                continue
            first_page = block.pages[0]
            raw = raw_by_page.get(first_page, {})
            first_page_candidates = raw.get("candidates", [])
            has_printed_cover = any(
                c.get("source") == "printed" for c in first_page_candidates
            )
            if not has_printed_cover:
                if FLAG_NO_PRINTED_COVER not in block.flags:
                    block.flags.append(FLAG_NO_PRINTED_COVER)

    elif batch_type == "non_tib":
        for block in blocks:
            if not block.pages:
                continue
            first_page = block.pages[0]
            raw = raw_by_page.get(first_page, {})
            if raw.get("pink_marker", False):
                if FLAG_PINK_MARKER not in block.flags:
                    block.flags.append(FLAG_PINK_MARKER)

    return GroupingResult(
        blocks=blocks,
        missing_tickets=missing_tickets,
        unmatched_values=all_unmatched,
        total_pages=total_pages,
    )


def parse_whitelist(raw: str) -> list[str]:
    """
    Parse a whitelist string (one per line, or comma/space separated).
    Returns validated ticket numbers. Raises ValueError for malformed entries.
    """
    import re as _re
    tokens = _re.split(r"[\s,]+", raw.strip())
    tokens = [t.strip() for t in tokens if t.strip()]
    valid = []
    invalid = []
    for t in tokens:
        if TICKET_RE.match(t):
            valid.append(t)
        else:
            invalid.append(t)
    if invalid:
        raise ValueError(f"Invalid ticket number format: {invalid}")
    return valid
