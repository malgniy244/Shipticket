# DECISIONS

## 2026-07-08

### Task 0 — Environment checks

The sandbox Python environment is 3.12.3. PyMuPDF 1.28.0 was installed. The OpenAI-compatible proxy is reachable at `OPENAI_API_BASE` with `OPENAI_API_KEY` pre-configured. `gpt-5` was selected as the detection model for best vision accuracy on messy handwriting.

### Task 1 — Detection module

Initial rendering DPI set to 150 (within the spec's 150–200 DPI range). Initial concurrency set to 5 workers (within the spec's 5–10 parallel requests). Checkpoint JSON is saved incrementally after each page.

The supplied `testingfile.pdf` has **16 pages**, not 15 as written in spec section 5. The owner confirmed the 16-page file is authoritative. The corrected acceptance criteria are:

| Output file | Correct pages (1-indexed) | Original spec |
|---|---|---|
| `301532.pdf` | 1–3 | 1–3 (unchanged) |
| `257535.pdf` | 4 | 4 (unchanged) |
| `253983.pdf` | 5–10 | 5–9 (corrected: +1 page) |
| `258066.pdf` | 11–13 | 10–12 (corrected: shifted +1) |
| `257086.pdf` | 14–16 | 13–15 (corrected: shifted +1) |

### Gate 1 confirmed decisions

**Edit-distance rule stays LOCKED at ≤ 1.** No auto-resolution beyond edit-distance-1-with-unique-match. This is a LOCKED grouping rule.

**Whitelist-context second pass (suggestion only, not auto-assignment):** For any detection that fails to match the whitelist, the system will re-query the vision model with the whitelist included in the prompt ("the valid ticket numbers for this batch are: [...]. Which, if any, does this page's number match?"). The result appears in the review screen as a pre-selected suggestion on the flagged block. The human still confirms. This is a UI suggestion feature, not a change to the grouping logic.

**Neighbor pre-selection (suggestion only, not auto-assignment):** When a flagged page/block sits between blocks of the same ticket (e.g., page 7 between confirmed `253983` pages), the review screen states this ("both neighbors are 253983") and pre-selects that ticket in the dropdown. Resolving the flag is one click. This is a UI convenience feature, not a change to the grouping logic.

**Team process rule (context only, no logic change):** The office is issuing a rule that the first page of every ship ticket must carry a printed number (cover page or sticker). This should make purely-handwritten tickets rare in production. The system must still handle them exactly as designed, because compliance will not be 100%.

**Page 7 misread accepted:** The model reads `247983` (edit-distance 2 from `253983`). This correctly triggers `UNMATCHED_NUMBER` flag and requires human assignment in the review screen. This is the system working as designed.

**Page 10 misread resolves via fuzzy match:** The model reads `243983` (edit-distance 1 from `253983`, unique match). The grouping engine auto-resolves this with a `FUZZY_RESOLVED` soft flag (see Gate 2 decisions below).

### Additional unit test case (added at Gate 1)

An edit-distance-2 detection sitting between two same-ticket neighbors must flag as `UNMATCHED_NUMBER` with the neighbor suggestion attached, and must never auto-assign.

---

## Gate 2 confirmed decisions (2026-07-08)

### Second regression fixture confirmed

`SKM_C250i26070816150.pdf` (16 pages) is the second permanent regression fixture. Ground truth confirmed by owner:

| Output file | Pages |
|---|---|
| `300291.pdf` | 1 |
| `300871.pdf` | 2 |
| `300588.pdf` | 3–11 |
| `298404.pdf` | 12 |
| `299198.pdf` | 13 |
| `301053.pdf` | 14–16 |

Acceptance criteria: 6 named blocks, 0 unmatched blocks, no hard flags, one `FUZZY_RESOLVED` soft flag on the `299198` block, CID `475545` nowhere assigned.

### Decision 1: FUZZY_RESOLVED soft flag (replaces stale CORRECTION_OBSERVED wording)

Edit-distance-1 auto-resolutions (where the raw detected value differs from the matched whitelist entry) now carry a dedicated `FUZZY_RESOLVED` soft flag. This is distinct from `CORRECTION_OBSERVED`, which is reserved exclusively for crossed-out-number corrections. The review screen will display `FUZZY_RESOLVED` blocks with a soft highlight so reviewers can spot auto-resolved readings.

### Decision 2: LOW_CONFIDENCE threshold unchanged at strictly below 0.7

The `LOW_CONFIDENCE` flag fires only when confidence is strictly less than 0.7. It is a separate concern from `FUZZY_RESOLVED`. A fuzzy-resolved page with confidence ≥ 0.7 carries `FUZZY_RESOLVED` only. A fuzzy-resolved page with confidence < 0.7 carries both `FUZZY_RESOLVED` and `LOW_CONFIDENCE`.

### Decision 3: NON_CONTIGUOUS fires only when separated by a different named ticket

`NON_CONTIGUOUS` fires only when the same ticket’s blocks are separated by at least one block resolved to a *different* named ticket (ticket is not None and is a different value). Separation by unresolved/inherited blocks (ticket is None) does NOT trigger `NON_CONTIGUOUS`. This prevents false-flagging the most common layout pattern: a detected run followed by trailing photo pages (which inherit the previous ticket), followed by a new ticket. The photo-page run is already folded into the first block by the inheritance rule before the non-contiguous check runs.

---

## Post-Gate-2 decisions (2026-07-08) — confirmed before Task 3

### Decision 4: Second-pass for orphan-candidate leading pages

When a page has no detection AND it is a leading page (before the first detection in the document), the detection module re-queries the vision model with the whitelist included in the prompt, asking whether any of those specific numbers appear on the page. A hit is treated as a normal detection carrying a soft `SECOND_PASS` flag visible in the review screen. This can only return whitelist members — no matching rule is loosened. Rationale: stickers on photo pages are the most common anchor and have been the lowest-confidence detection class across all fixtures (0.78, 0.80, and one outright miss on fixture #4 page 1). This is a pattern, not a one-off.

### Decision 5: Suffix-wins engine rule is mandatory regardless of vision layer behaviour

The engine-level suffix-wins rule stays in place unconditionally. The vision layer assembling a suffixed candidate directly (e.g. `253027-1`) is a bonus, not a guarantee. The engine always applies suffix-wins as a safety net.

### Decision 6: ORPHAN_LEADING_PAGES blocks get a neighbor suggestion from the next block

When an orphan block has no before-neighbor (it is at the start of the document), the neighbor suggestion is set to the first named block that follows it. This enables one-click assignment in the review screen.

### Fixture #4 accepted (replaces fixture #3)

`SKM_C250i26070816530.pdf` (18 pages) is regression fixture #4. Ground truth confirmed by owner:

| Output file | Pages |
|---|---|
| `300574.pdf` | 1–2 |
| `300600.pdf` | 3–13 |
| `300573.pdf` | 14–15 |
| `253027-1.pdf` | 16–18 |

Acceptance criteria: 4 named blocks, 0 unmatched blocks, no hard flags after second pass runs. Page 1 may carry `SECOND_PASS` soft flag. If second pass also misses page 1, fallback is `ORPHAN_LEADING_PAGES` with `300574` pre-selected as neighbor suggestion.

---

## Decision 7 — Three-tier detection pipeline (2026-07-09)

**Problem:** The first-pass prompt was confused by pages with dense numeric content (banknote serial numbers, PMG/NGC grade numbers, customer IDs) and returned EMPTY even when a white label sticker was clearly visible in the corner.

**Root cause:** The first-pass `SYSTEM_PROMPT` is general-purpose and does not specifically instruct the model to look for the white label sticker format. On pages with many competing numbers, the model failed to identify the sticker number.

**Fix:** Added a three-tier detection pipeline in `run_detection()`:
1. **First pass** (all pages, concurrent): General `SYSTEM_PROMPT` at 150 DPI
2. **Sticker retry** (any empty page after first pass): `STICKER_RETRY_SYSTEM_PROMPT` — explicitly describes the white label sticker format, lists what NOT to report (banknote serials, grade numbers, customer IDs), and focuses the model on corner stickers
3. **Whitelist second pass** (orphan-candidate pages only, if whitelist provided): `SECOND_PASS_SYSTEM_PROMPT` with whitelist context

**Evidence:**
- Fixture 2, page 13 (`299198`): First pass EMPTY → sticker retry `299198` at 0.98 (was previously fuzzy-resolved from `299188` at 0.74)
- Fixture 2, page 1 (`300291`): First pass EMPTY → sticker retry `300291` at 0.98
- Fixture 3, page 14 (`300573`): First pass EMPTY → sticker retry `300573` at 0.97

**Decision:** Sticker retry runs on ALL empty pages (not just orphan-leading pages) because sticker misses can occur anywhere in the document, not just at the start. This bounds the cost increase to empty pages only.

**Note:** DPI increase was tested and found NOT to be the cause. The fix is prompt-based, not resolution-based.

---

## Decision 8 — Amendment to LOCKED Rule 5: Three-step UNMATCHED pipeline (2026-07-09)

**Approved by owner at Gate 3 re-check.**

### Issue 1 fix: No merging of UNMATCHED pages with different raw values

Consecutive UNMATCHED pages with different raw detected values must NOT be merged into one block. Each distinct raw value gets its own separately assignable block. Pages with the same raw UNMATCHED value (or no raw value) can still be grouped.

### Issue 3 fix: Three-step UNMATCHED pipeline

**Previous behavior:** Any page whose detection did not match the whitelist (`UNMATCHED_NUMBER`) was hard-flagged and required mandatory human assignment before Confirm was enabled.

**New behavior:** When a page has `UNMATCHED_NUMBER`, apply this pipeline in order:

1. **Second-pass step:** If the detection layer already ran a whitelist-context second pass on this page (candidate has `second_pass=True`) and it resolved to a whitelist ticket, use it with the `SECOND_PASS` soft flag. If the second pass returned a ticket that conflicts with what inheritance would give AND confidence is low, hard-flag as `AMBIGUOUS_MATCH` instead.

2. **Edit-distance-2 inheritance step:** If the raw unmatched value is within edit-distance ≤ 2 of the previous block's ticket, inherit from the previous block with the `INHERITED_UNMATCHED` soft flag. Rationale: a misread of the inherited ticket will resemble it; a misread of a genuinely new ticket number would not resemble the previous ticket, and must stay hard-flagged.

3. **Hard flag (unchanged):** If neither step 1 nor step 2 applies, keep the `UNMATCHED_NUMBER` hard flag with neighbor suggestion (unchanged from original spec).

**Constraints (LOCKED):**
- `INHERITED_UNMATCHED` and `SECOND_PASS` pages remain **red pages** in the filmstrip — they are active decisions visible to the reviewer, just not Confirm-blocking.
- `MISSING_TICKET` completeness check remains a hard flag (unchanged backstop).
- The edit-distance-2 guard addresses the risk mechanism directly. No "printed source only" condition is added.
- The detection layer runs the whitelist-context second pass on UNMATCHED pages (tier 4 in `detect.py`) before grouping. The grouping engine itself remains pure logic with no API calls.

### Fixture #5 added

`SKM_C250i26070916020.pdf` (17 pages) is regression fixture #5. Ground truth confirmed by owner:

| Output file | Pages |
|---|---|
| `247799.pdf` | 1–4 |
| `248256.pdf` | 5–8 |
| `248258.pdf` | 9–11 |
| `248259.pdf` | 12–14 |
| `248260.pdf` | 15–17 |

Acceptance criteria: 5 named blocks, 0 unmatched blocks, no hard flags, `INHERITED_UNMATCHED` soft flags on handwritten photo pages.

### Decision 9: Batch Type Toggle & Pink Marker Boundary Proposal (2026-07-09)

**Context:** The system now handles two batch types: TIB (printed cover page per ticket) and non-TIB (tickets separated by a pink/magenta marker page). The frontend needs to know which type of batch is being processed to offer appropriate boundary suggestions, and the vision prompt needs to detect the pink marker.

**Decision:**
1. **Batch Type Toggle:** Added `batch_type` ("tib" or "non_tib") to job creation UI and backend state.
2. **Pink Marker Detection:** Extended `detect.py` schema with `pink_marker` boolean field. The vision model explicitly checks for bright pink/magenta rectangular stickers on every page.
3. **Proposal-Layer Flags:**
   - In **TIB** mode: If a block starts with a page that lacks a "printed" source candidate, add `NO_PRINTED_COVER` soft flag.
   - In **non-TIB** mode: If a block starts with a page that has `pink_marker=True`, add `PINK_MARKER` soft flag.
4. **Core Rules Unchanged:** These flags are strictly for the proposal layer (UI/UX) and do not affect the core identity grouping rules (Rule 5 inheritance, Rule 6 block merging).

**Constraints (LOCKED):**
- The pink marker is a boundary signal only — it never changes ticket identity or overrides whitelist matching.
- The `batch_type` is passed through the entire stack (from job creation to grouping) but only alters the final proposal flags attached to blocks.

### Decision 10: Full-Screen Phase B Redesign + Reconciliation Gate (2026-07-09)

**Context:** The original Phase B (list of block cards with separate filmstrip modal) had a broken interaction model: the reviewer had to enlarge to verify but could not act from the enlarged view. Long review sessions on large batches also risked losing work to session expiry.

**Decision:**
1. **Full-screen confirmation flow:** Phase B is now a single full-screen screen. Clicking "Confirm Boundaries" in Phase A goes directly to the first block's first page, displayed large. No intermediate list.
2. **On-screen identity panel:** Large page image on the left; identity display (green = exact whitelist match at ≥70% confidence, red = anything else), source badge, flags, and a whitelist-only type-ahead dropdown on the right. Free text that is not a whitelist entry cannot be committed.
3. **Keyboard model:** → confirm & advance | ← previous block | ↓/↑ page within block | Enter commit identity | Esc back to Phase A (preserves confirmations).
4. **Right-arrow on unassigned:** Does not advance — moves focus to identity control instead.
5. **Escape preserves confirmations:** Returning to Phase A via Esc keeps all per-block confirmation state.
6. **Reconciliation screen:** After the last block is confirmed, a reconciliation screen shows: two-column matching view (whitelist left, confirmed blocks right), three explicit checks (no missing, no duplicates, no extras), and a headline status. Confirm & Download is enabled only when all three checks pass and all blocks are confirmed.
7. **Server-side validation:** The confirm endpoint runs the same four checks (no unresolved flags, no missing, no duplicates, no extras) before building the ZIP. A stale client cannot download an inconsistent batch.
8. **Reassignment re-derives downstream state:** After any reassignment, `missing_tickets` is recomputed server-side and returned to the client.

**Tests added (Test 20):** 9 new reconciliation logic tests covering: clean batch passes, missing ticket blocks download, duplicate ticket blocks download, unassigned block blocks download, hard flag blocks download, extra ticket blocks download, 17-page TIB fixture passes all checks, right-arrow-on-unassigned condition, reassignment updates missing_tickets.

**Total tests: 99 (all pass).**

**Constraints (LOCKED):**
- Whitelist-only assignment rule enforced at both UI layer (dropdown cannot commit non-whitelist text) and server layer (reassign endpoint validates against whitelist).
- Any reassignment re-derives flags server-side via the existing re-pool endpoint (unchanged).
- MISSING_TICKET and conflict checks unchanged — the reconciliation screen is their visual surface, not a replacement.
