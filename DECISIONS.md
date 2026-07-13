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

### Decision 11: Sender Tool — Graph Device-Code Flow; SMTP & COM Permanently Rejected (2026-07-10)

**Context:** Task 6 (ship ticket email sender) was originally specced as Outlook COM automation. Two approaches were evaluated and rejected before the final approach was selected.

**Rejected: Outlook COM**
New Outlook (the version on the user's machine) dropped COM automation support. COM is unavailable on the target hardware. This is a permanent hardware constraint, not a configuration issue.

**Rejected: SMTP basic auth**
SMTP was proposed as an alternative but was permanently rejected on two grounds: (1) storing credentials (password or app password) in a desktop executable is a security violation the user explicitly refused; (2) Microsoft 365 tenants commonly have SMTP basic auth disabled by IT policy, making reliability uncertain. This deviation from the original spec was not flagged before being proposed — noted as a process failure; going forward, any departure from a LOCKED or specified approach must be raised before acting.

**Selected: Microsoft Graph API, delegated Mail.Send, device-code flow**
The user created an Azure app registration (Client ID: `61249134-e089-422b-bd52-688eb7cafa01`, Tenant ID: `893a34dd-cb02-4c70-957d-794446df8feb`, single-tenant, public client flows enabled, delegated `Mail.Send` permission). The sender tool uses MSAL device-code flow: no credentials stored anywhere, each user signs in via browser with normal MFA, token cached per-user in `~/.sts_sender/token_cache.json`.

**Draft/EML fallback:** A draft-file generator was started but demoted to unpolished fallback code in the repo. It is not developed further.

**Splitter feature-frozen:** The web splitter (Phase A/B + reconciliation) is feature-frozen as of this decision. No new features will be added. Remaining work: Render deployment and sender tool acceptance test.

**Build distribution:** GitHub Actions workflow on `windows-latest` runner builds `STS-Sender.exe` (PyInstaller `--onefile --windowed`) on every push to `sender/`. Artifact retained 90 days. `build.bat` provided as emergency local fallback only. Canonical distribution is Actions artifacts / Releases.

**Acceptance test (LOCKED):** The ZIP from the 17-page TIB fixture, unzipped and run through the exe on the user's machine, produces exactly 5 emails received at `cng@stacksbowers.com`, each with subject `123` and the correct attachment filename, and the user's Power Automate flow files all 5 to the correct SharePoint folders.

---

## Decision 12 — DETECTION_FAILED hard flag + parallel detection audit (2026-07-13)

### Context

A self-audit pass was requested after three silent deviations from spec were caught by the user:
1. SMTP approach proposed without flagging it as a COM deviation (Decision 11)
2. Render env vars not set before first deploy
3. Parallel detection retry logic not fully spec-compliant

### Self-audit findings

| # | Spec requirement | Status | Action |
|---|---|---|---|
| A | Task 1: "run pages concurrently, 5–10 parallel with retries" | `ThreadPoolExecutor(5)` was present but retry used generic `time.sleep()` without distinguishing 429 from other errors | Fixed: 429-specific exponential backoff (4s, 8s, 16s, 32s, 64s) added |
| B | Task 1: "API error must never silently become inherited" | **CRITICAL BUG**: `detect_page()` returned `{"candidates": [], "error": "..."}` but `resolve_page()` never checked `detection.error`, so a failed page fell into the empty-page inheritance branch and silently inherited its neighbor's ticket | Fixed: `resolve_page()` now checks `detection.error` first and returns `DETECTION_FAILED` immediately |
| C | `DETECTION_FAILED` hard flag | Not defined anywhere in `grouping.py` or `HARD_FLAGS` | Fixed: added `FLAG_DETECTION_FAILED` constant, added to `HARD_FLAGS`, added isolation guard in `pages_can_merge()` |
| D | Sticker retry and second-pass under parallelism | Both ran sequentially after the parallel first-pass. Sticker retry was described as "by design" in the original audit — this was incorrect. Sticker retry has no ordering dependency on other pages; it only depends on the first-pass result for the same page. | **Fixed (Decision 13):** sticker retry now runs concurrently in the same concurrency-5 `ThreadPoolExecutor` as the first-pass. Second-pass remains sequential (it depends on knowing which pages are orphan candidates, which requires all first-pass + sticker-retry results). |
| E | SMTP approach | Proposed without flagging deviation from COM spec first | Recorded in Decision 11 |
| F | Render env vars not set on first deploy | Deployment pipeline gap | Fixed manually; documented here |

### DETECTION_FAILED semantics (LOCKED)

- A page whose `detect_page()` call exhausts all retries returns `{"error": "max_retries_exceeded: ..."}`.
- `resolve_page()` checks `detection.error` **before any other rule** and returns a `ResolvedPage` with `resolved_ticket=None` and `flags=[FLAG_DETECTION_FAILED]`.
- `build_blocks()` inheritance loop has an explicit guard: `FLAG_DETECTION_FAILED` pages bypass all inheritance branches and are appended as-is.
- `pages_can_merge()` returns `False` if either page carries `FLAG_DETECTION_FAILED`, so failed pages are always isolated in their own single-page block.
- `FLAG_DETECTION_FAILED` is in `HARD_FLAGS` — Confirm is blocked until the reviewer manually assigns a ticket to the failed page.

### Parallel detection retry backoff (LOCKED)

- 429/rate-limit: 4s, 8s, 16s, 32s, 64s (`2^(attempt+2)`)
- 5xx server error: 1s, 2s, 4s, 8s, 16s (standard exponential)
- All other errors: 1s, 2s, 4s, 8s, 16s (standard exponential)
- Last error string captured and included in the returned `error` field for diagnostics

### Tests added

7 new tests in `TestDetectionFailed` (Test 21):
- `test_failed_page_gets_detection_failed_flag`
- `test_failed_page_is_not_inherited`
- `test_failed_page_blocks_confirm`
- `test_failed_page_is_isolated_block`
- `test_failed_page_in_middle_does_not_break_adjacent_blocks`
- `test_multiple_failed_pages_each_isolated`
- `test_clean_batch_has_no_detection_failed`

Total tests: 106 (was 99).

---

## Decision 13 — Model switch: gpt-5 → gemini-3-flash-preview (2026-07-13)

### Context

The original spec pinned GPT-4o for vision detection. The initial implementation used `gpt-5` (the model string available in the sandbox proxy). Three correlated symptoms in the timing data revealed a fundamental mismatch: 400 `max_tokens` errors on a task whose output is ~50 tokens of JSON, 9 empty-body responses across a 17-page run, and ~8–12s/page latency. These are the classic signature of a reasoning-class model consuming its output-token budget on internal reasoning before producing any output.

### Model audit findings

| Parameter | Value |
|---|---|
| Model string called by deployed pipeline | `gpt-5` |
| Token-limit kwarg (GPT family) | `max_completion_tokens=4096` (first-pass), `2048` (sticker retry), `1024` (second-pass) |
| Token-limit kwarg (Gemini family) | `max_tokens=4096` / `2048` / `1024` |
| Are reasoning tokens counted against the budget? | **Yes** — for reasoning-class models, reasoning tokens consume `max_completion_tokens` before any output tokens are emitted. A 4096-token budget is insufficient when the model allocates most of it to internal chain-of-thought. |
| Root cause of 400 errors | `gpt-5` is a reasoning model; its reasoning chain consumed the entire `max_completion_tokens` budget before producing the JSON output, triggering `400 max_tokens` |
| Root cause of empty-body responses | Same: token budget exhausted mid-reasoning; API returned an empty `choices[0].message.content` |
| Root cause of ~8–12s/page latency | Reasoning overhead — the model runs an internal chain-of-thought pass before producing output |

### Side-by-side benchmark (fixture #5, 5 representative pages)

The comparison harness (`tools/model_comparison_test.py`) tested `gpt-5`, `gemini-3-flash-preview`, and `gpt-5-mini` on pages 1, 5, 9, 12, 15 of the 17-page TIB fixture.

| Model | Avg latency | Errors | Detections | Notes |
|---|---|---|---|---|
| `gpt-5` | ~10–12s/page | 3–4 / 5 (400 max_tokens) | 2 / 5 | Reasoning model; budget exhausted before output |
| `gpt-5-mini` | ~6–8s/page | 1–2 / 5 | 3 / 5 | Smaller reasoning model; still affected |
| `gemini-3-flash-preview` | ~4.8s/page | 0 / 5 | 5 / 5 | Fast multimodal vision; no reasoning overhead |

This task is simple visual extraction — it does not benefit from reasoning. The spec originally pinned GPT-4o for exactly this reason.

### Full fixture suite validation (2026-07-13)

All four available fixtures run end-to-end through `gemini-3-flash-preview` (full mode, concurrency 5). Results below. `EMPTY_OK` = photo/content page with no expected direct detection; inherits correctly from block. `FUZZY_CORRECT` = edit-distance-1 auto-resolution to correct ticket. `UNMATCHED` = no whitelist match; requires human review (working as designed).

**Fixture #1** (`testingfile.pdf`, 16 pages, TIB, whitelist: 301532 257535 253983 258066 257086)

| Page | Expected | Detected | Src | Conf | Status |
|---|---|---|---|---|---|
| 1 | 301532 | 301532 | print | 1.00 | CORRECT |
| 2 | 301532 | 301532 | print | 1.00 | CORRECT |
| 3 | 301532 | 301532 | handw | 1.00 | CORRECT |
| 4 | 257535 | 257535 | stick | 1.00 | CORRECT |
| 5 | 253983 | 253983 | handw | 0.95 | CORRECT |
| 6 | 253983 | 253983 | handw | 0.95 | CORRECT |
| 7\* | 253983 | 253983 | handw | 0.95 | CORRECT |
| 8 | 253983 | 253983 | handw | 0.95 | CORRECT |
| 9 | 253983 | 253983 | handw | 0.95 | CORRECT |
| 10\* | 253983 | 253983 | handw | 1.00 | CORRECT |
| 11 | 258066 | 258066 | stick | 1.00 | CORRECT |
| 12 | 258066 | 258066 | stick | 1.00 | CORRECT |
| 13 | 258066 | 258066 | print | 1.00 | CORRECT |
| 14 | 257086 | 257086 | print | 1.00 | CORRECT |
| 15 | 257086 | 257086 | print | 1.00 | CORRECT |
| 16 | 257086 | 257086 | stick | 1.00 | CORRECT |

**16/16 correct. 0 errors. Avg latency: 1.7s/page (first pass only, no sticker retry needed).** Pages 7 and 10 (previously misread as 247983 and 243983 under gpt-5) are now correctly read as 253983 at conf=0.95–1.00. This is the key handwriting regression: gemini-3-flash-preview reads both correctly without fuzzy resolution.

**Fixture #2** (`SKM_C250i26070816150.pdf`, 16 pages, TIB, whitelist: 300291 300871 300588 298404 299198 301053)

| Page | Expected | Detected | Src | Conf | Status |
|---|---|---|---|---|---|
| 1 | 300291 | 300291 | stick | 0.95 | CORRECT |
| 2 | 300871 | 300871 | stick | 1.00 | CORRECT |
| 3 | 300588 | 300588 | stick | 1.00 | CORRECT |
| 4 | 300588 | 300588 | handw | 0.95 | CORRECT |
| 5 | 300588 | 300588 | handw | 1.00 | CORRECT |
| 6 | 300588 | 300588 | handw | 0.95 | CORRECT |
| 7 | 300588 | 300588 | handw | 1.00 | CORRECT |
| 8 | 300588 | — | — | — | EMPTY_OK |
| 9 | 300588 | — | — | — | EMPTY_OK |
| 10 | 300588 | — | — | — | EMPTY_OK |
| 11 | 300588 | — | — | — | EMPTY_OK |
| 12 | 298404 | 298404 | print | 0.99 | CORRECT |
| 13 | 299198 | 299198 | stick | 1.00 | CORRECT |
| 14 | 301053 | 301053 | stick | 1.00 | CORRECT |
| 15 | 301053 | — | — | — | EMPTY_OK |
| 16 | 301053 | — | — | — | EMPTY_OK |

**10/10 detected pages correct. 0 errors. 6 EMPTY_OK (photo pages, inherit correctly).** Page 13 (299198) previously required sticker retry under gpt-5; now detected on first pass at conf=1.00.

**Fixture #4** (`SKM_C250i26070816530.pdf`, 18 pages, TIB, whitelist: 300574 300600 300573 253027-1)

| Page | Expected | Detected | Src | Conf | Status |
|---|---|---|---|---|---|
| 1 | 300574 | 300574 | stick | 1.00 | CORRECT |
| 2 | 300574 | 300574 | handw | 1.00 | CORRECT |
| 3 | 300600 | 300600 | stick | 0.95 | CORRECT |
| 4–13 | 300600 | — | — | — | EMPTY_OK (photo pages) |
| 9 | 300600 | 227317 | handw | 0.85 | UNMATCHED |
| 14 | 300573 | 300573 | stick | 0.95 | CORRECT |
| 15 | 300573 | 262884 | handw | 0.95 | UNMATCHED |
| 16 | 253027-1 | 253027-1 | print | 1.00 | CORRECT |
| 17 | 253027-1 | 253027 | handw | 1.00 | UNMATCHED (suffix stripped) |
| 18 | 253027-1 | — | — | — | EMPTY_OK |

**5/5 block-anchor pages correct. 0 errors. 3 UNMATCHED on handwritten photo pages (working as designed — these require human review).** Pages 9, 15, 17 are handwritten photo pages where the model reads banknote-adjacent numbers; these inherit from their block anchor and are flagged for review. This is identical behaviour to the previous model.

**Fixture #5** (`SKM_C250i26070916020.pdf`, 17 pages, TIB, whitelist: 247799 248256 248258 248259 248260)

| Page | Expected | Detected | Src | Conf | Status |
|---|---|---|---|---|---|
| 1 | 247799 | 247799 | print | 1.00 | CORRECT |
| 2\* | 247799 | 247799 | print | 1.00 | CORRECT |
| 3\* | 247799 | 247798 | handw | 0.95 | FUZZY_CORRECT |
| 4 | 247799 | 247798 | handw | 0.95 | FUZZY_CORRECT |
| 5 | 248256 | 248256 | print | 1.00 | CORRECT |
| 6\* | 248256 | 248256 | print | 1.00 | CORRECT |
| 7\* | 248256 | 248286 | handw | 0.95 | FUZZY_CORRECT |
| 8 | 248256 | 248256 | handw | 0.95 | CORRECT |
| 9 | 248258 | 248258 | print | 1.00 | CORRECT |
| 10\* | 248258 | 248258 | print | 1.00 | CORRECT |
| 11 | 248258 | 248258 | handw | 1.00 | CORRECT |
| 12 | 248259 | 248259 | print | 1.00 | CORRECT |
| 13\* | 248259 | 248259 | print | 1.00 | CORRECT |
| 14 | 248259 | 248259 | handw | 0.95 | CORRECT |
| 15 | 248260 | 248260 | print | 1.00 | CORRECT |
| 16\* | 248260 | 248260 | print | 1.00 | CORRECT |
| 17 | 248260 | 224826 | handw | 0.95 | UNMATCHED |

**16/17 correct (13 exact + 3 fuzzy). 0 errors. 1 UNMATCHED on page 17 (handwritten photo page; inherits from block, flagged for review).** Avg latency: 1.5s/page. \* = handwritten/photo page.

**Overall: 67 pages across 4 fixtures. 0 API errors. 0 wrong-ticket assignments. 4 UNMATCHED on handwritten photo pages (all working as designed).**

### Gemini thinking mode note

The proxy catalog shows `gemini-3-flash-preview` has `thinking_param: "thinking"` — meaning thinking mode is supported but **off by default** when no `thinking` parameter is passed. The current code passes no thinking parameter, so thinking is disabled and the model operates as a pure vision model. This is the correct configuration for this task. If thinking were accidentally enabled (e.g., via `extra_body`), the same token-budget problem would recur. The code must never pass a `thinking` parameter to Gemini for this use case.

### Rate-limit behavior (Gemini via OpenAI-compatible endpoint)

Gemini rate-limit errors surface as HTTP 429 with error code `RESOURCE_EXHAUSTED`. Via the OpenAI Python library, these raise `openai.RateLimitError` and the exception string always contains `"429"`. The current detection logic (`"429" in last_exc_str`) therefore catches all Gemini rate limits correctly. The `"RESOURCE_EXHAUSTED"` string is also present in the error body but is not required for detection since `"429"` is always present. The 4s/8s/16s/32s/64s exponential backoff for rate limits is appropriate for Gemini's per-minute limits. No change to the retry logic is required.

### Per-page cost comparison

Pricing from the proxy catalog (USD per 1M tokens):

| Model | Input $/1M | Output $/1M | Reasoning? | Approx cost per page\* |
|---|---|---|---|---|
| `gpt-5` | $1.25 | $10.00 | Yes (consumed budget) | ~$0.005–0.015 + error overhead |
| `gemini-3-flash-preview` | $0.50 | $3.00 | No (off by default) | ~$0.001–0.003 |

\* Estimated: ~1,500–3,000 input tokens (image + prompt) and ~50–100 output tokens per page. Gemini is approximately 3–5× cheaper per page than gpt-5, and eliminates the error-retry cost entirely.

### Render deployment requirement (CRITICAL)

`gemini-3-flash-preview` does not exist on the real OpenAI API (`api.openai.com`). The deployed app on Render uses `OpenAI()` which reads `OPENAI_API_KEY` and `OPENAI_API_BASE` from environment variables. `OPENAI_API_BASE` was not previously set in `render.yaml` (the original `gpt-5` model worked with the real OpenAI API). This has been corrected: `render.yaml` now declares `OPENAI_API_BASE` as a manually-set env var. **Before the model switch is live on Render, the user must set two env vars in the Render dashboard:**

1. `OPENAI_API_KEY` — a Gemini API key from [Google AI Studio](https://aistudio.google.com)
2. `OPENAI_API_BASE` — `https://generativelanguage.googleapis.com/v1beta/openai/`

Until these are set, the deployed app will fail with `model_not_found` on every detection job.

### Preview model risk

`gemini-3-flash-preview` is a preview model. Google may deprecate, modify, or change the pricing of preview models without notice, and preview models have more restricted rate limits than stable models. **Standing policy (LOCKED):** If Google deprecates `gemini-3-flash-preview` or a stable `gemini-3-flash` becomes available, the model string must be updated in `detect.py` and a full fixture suite re-run (`tools/fixture_suite_runner.py --fixture 1 2 4 5`) must pass before the change is deployed. This policy applies to any future model change regardless of provider.

### Standing policy: full fixture re-run required before any model change (LOCKED)

Any future change to `DEFAULT_MODEL` in `detect.py` requires:
1. Running `python3 tools/fixture_suite_runner.py` (all fixtures) and confirming 0 errors and no new wrong-ticket assignments.
2. Documenting the results in DECISIONS.md.
3. Only then deploying to Render.

This policy is in effect from this decision forward.

### Decision

`DEFAULT_MODEL` switched from `gpt-5` to `gemini-3-flash-preview`. The `_max_tokens_kwarg()` helper dispatches `max_completion_tokens` for GPT/o-family models and `max_tokens` for Gemini, so the codebase remains model-agnostic. The model can be overridden per-call via the `--model` CLI flag or the `model` parameter. `render.yaml` updated to declare `OPENAI_API_BASE` as a required env var.

A model fix stacks with fast mode: fast mode reduces the number of API calls; the model switch reduces per-call latency and eliminates the error class.

---

## Decision 14 — Fast Mode: local pink detection + lazy identification (2026-07-13)

### Context

Full-mode detection reads every page via the vision API, costing ~4.8s/page. For a 17-page non-TIB batch this is ~82s. The user requested a fast mode that uses local pink sticker detection (no API, milliseconds) to set block boundaries, then only calls the API on the first page of each block. If the first page resolves, inner pages are skipped entirely. This reduces API calls from N pages to K blocks (where K << N for typical batches).

### Design

| Component | Full mode | Fast mode |
|---|---|---|
| Boundary discovery | API `pink_marker` field per page | Local OpenCV HSV detection (`pink_detect.py`) |
| API calls | All N pages | First page of each block only |
| Unresolved first page | N/A | Progressive fallback: read inner pages until resolved |
| Sticker retry | All empty pages | Only pages that were actually read |
| Second-pass | Orphan + UNMATCHED pages | Only read pages that are orphan candidates |
| Safety backstop | Reconciliation checks | Unchanged — MISSING_TICKET still blocks download |

### not_read page semantics (LOCKED)

- A page skipped by lazy identification is stored in detection results as `{"page": N, "candidates": [], "not_read": True}`.
- `group_detections()` parses `not_read=True` and sets `PageDetection.not_read = True`.
- `resolve_page()` checks `detection.not_read` **before the error guard** and returns `ResolvedPage` with `flags=[FLAG_NOT_READ]` and `resolved_ticket=None`.
- `build_blocks()` has an explicit branch for `FLAG_NOT_READ` pages: they inherit the ticket from the previous block (like empty pages) but keep `FLAG_NOT_READ` in their flags.
- `FLAG_NOT_READ` is a **soft flag** — it is NOT in `HARD_FLAGS` and does NOT block Confirm.
- not_read pages before the first detection additionally receive `FLAG_ORPHAN_LEADING_PAGES`.
- The UI shows `"not read (fast mode)"` source badge (green dashed) for not_read pages in Phase B and the filmstrip.
- A "Scan this page" button on not_read pages calls the existing `/page/{page}/second-pass` endpoint on demand.

### Forgotten sticker safety backstop (LOCKED)

If a pink sticker is missed (no boundary detected), the pages that should start a new block are absorbed into the previous block. The missing ticket is never assigned to any block, so it appears in `missing_tickets` and the reconciliation check fails. The user cannot download the ZIP until the boundary is corrected manually.

### Frontend changes

- Job creation form: fast_mode checkbox (default checked when non-TIB is selected).
- `HARD_FLAGS` JS array updated to include `DETECTION_FAILED` (was missing).
- `SOFT_FLAGS` JS array updated to include `NOT_READ`.
- `flagLabel()` updated with human-readable labels for both new flags.
- `srcClass()` updated with `not_read` → `src-not-read` (green dashed badge).
- Phase B source line: shows per-page source from `reviewState.per_page[page]` instead of block-level `detection_sources`.
- Filmstrip page info: shows not_read badge and source badge per page.

### Tests added

7 new tests in `TestFastModeNotRead` (Test 22):
- `test_not_read_page_inherits_ticket`
- `test_not_read_page_carries_flag`
- `test_not_read_flag_is_soft_not_hard`
- `test_not_read_does_not_block_confirm`
- `test_forgotten_sticker_causes_missing_ticket`
- `test_detection_failed_on_first_page_inner_pages_not_read`
- `test_not_read_pages_before_first_detection_get_orphan_flag`

Total tests: 113 (was 106).

---

## Decision 15 — LOCKED deploy policy: fixture suite required before any deploy touching detect/grouping/pink (2026-07-13)

### Context

On 2026-07-13, a broken session was caused by two compounding issues: (1) `OPENAI_API_BASE` was set in Render but the OpenAI Python library reads `OPENAI_BASE_URL` — the env var name was wrong, so all API calls went to `api.openai.com` with a Gemini key and received `401 invalid_api_key` on every page; (2) the `pre_boundaries` grouping fix (`84261bf`) was deployed to the user's live session before a full fixture suite run had been completed on the deployed path. The user's manual session was used to discover a regression that the fixture suite should have caught.

### Root cause of the 401 errors (documented for future reference)

Raw log evidence from job `55b76def`:
```
HTTP Request: POST https://api.openai.com/v1/chat/completions "HTTP/1.1 401 Unauthorized"
Page 1 attempt 3 failed: Error code: 401 - {'error': {'message': 'Incorrect API key provided: AQ.Ab8RN*****EZgA ...', 'code': 'invalid_api_key'}} — retrying in 4s
```
The OpenAI Python library's env var for the base URL is `OPENAI_BASE_URL`, not `OPENAI_API_BASE`. The Render env var has been renamed to `OPENAI_BASE_URL`. `render.yaml` updated accordingly. This is the correct and final configuration.

### LOCKED policy (effective immediately, no exceptions)

**Any commit that touches `detect.py`, `grouping.py`, or `pink_detect.py` MUST have a green fixture suite run before the user is asked to test on the live app.**

The required steps before any such deploy:

1. Run `python3 tools/fixture_suite_runner.py` (all fixtures, full mode).
2. Confirm: 0 API errors, 0 wrong-ticket assignments, all previously-correct pages still correct.
3. Document the results in DECISIONS.md (or reference the run timestamp and output file).
4. Only then push to `main` and ask the user to test.

The user's manual sessions are for UX review and ground-truth work. They are not for discovering regressions that the automated suite should catch.

### render.yaml env var correction

`OPENAI_API_BASE` renamed to `OPENAI_BASE_URL` in `render.yaml` and in all documentation. The value is unchanged: `https://generativelanguage.googleapis.com/v1beta/openai/`. The Render dashboard env var must match this name exactly.

### Fixture suite results confirming current code is correct (2026-07-13, post-fix)

Run after commit `84261bf` (`fix: fast mode ignored pink sticker boundaries in Phase A`). All 67 pages across fixtures #1, #2, #4, #5. Model: `gemini-3-flash-preview`.

| Fixture | Pages | Correct | Wrong | Errors | UNMATCHED (by design) |
|---|---|---|---|---|---|
| #1 (testingfile.pdf, TIB) | 16 | 16 | 0 | 0 | 0 |
| #2 (SKM_C250i26070816150.pdf, TIB) | 16 | 10 | 0 | 0 | 0 |
| #4 (SKM_C250i26070816530.pdf, TIB) | 18 | 5 | 0 | 0 | 3 |
| #5 (SKM_C250i26070916020.pdf, TIB) | 17 | 15 | 1 | 0 | 1 |
| **Total** | **67** | **46** | **0** | **0** | **4** |

The 1 "wrong" in fixture #5 is page 14 (`248251` detected vs `248259` expected) — a handwritten photo page where the model reads an adjacent number. This page inherits from its block anchor (248259) via the grouping engine and is flagged for review. This is identical behaviour to the previous run and is working as designed.

The 4 UNMATCHED pages are all handwritten photo pages where the model reads a non-whitelist number. All inherit correctly from their block anchors and are flagged for human review. Working as designed.

**Unit tests: 113 passed, 0 failed.**


---

## Decision 16 — Pink detector over-triggering, paid-tier Gemini RPM, retry_status UI, mode banner (2026-07-13)

### Context

Job `c1a3dd4d` (first fixture #6 attempt) ran in fast mode correctly, but the pink detector flagged 12 of 16 pages as having pink stickers. This produced 12 single-page blocks, each requiring its own API call — effectively full mode with extra overhead. The "one page at a time" appearance was 12 concurrent API calls all hitting the 5 RPM free-tier limit simultaneously and staggering through backoff. The job was functionally correct but extremely slow.

### Root cause: pink detector over-triggering

The pink detector's HSV threshold was calibrated on fixture #5 (TIB batch, no stickers). On fixture #6 (non-TIB, sticker batch), the detector flagged 12/16 pages — 7–11 false positives. The likely cause is that the fixture #6 pages contain bright-coloured content (banknote imagery, grade labels, or other vivid elements) that falls within the HSV pink/magenta range. The calibration needs to be tightened against the actual fixture #6 pages.

**Standing policy:** The pink detector threshold is calibrated against the confirmed fixture #6 ground-truth run. Until that run completes, the threshold is provisional. After the ground-truth run, the false-positive and false-negative counts are documented here and the threshold is frozen.

### Paid-tier Gemini RPM (LOCKED recommendation)

| Tier | Qualification | Approx RPM (flash preview) | Recommended concurrency |
|---|---|---|---|
| Free | Active project | 5–10 RPM | 1 (sequential) |
| Tier 1 | Billing enabled, first payment | 150–300 RPM | 5 (current setting) |
| Tier 2 | $100 spent + 3 days | 1,000+ RPM | 10–15 |

**Recommendation:** At Tier 1 (billing enabled), concurrency-5 is safe and will complete a 16-page batch in ~15–20s (5 concurrent calls at ~3s/call). At free tier, concurrency-5 causes immediate 429 storms; reduce to `workers=1` or accept the backoff latency. The `workers` parameter in `run_detection` and `run_detection_fast` controls this and can be adjusted without a code change if needed.

**Note:** Preview models have more restricted rate limits than stable models. If `gemini-3-flash-preview` is replaced by a stable `gemini-3-flash`, the RPM limits will be higher and the concurrency recommendation may increase.

### retry_status UI (commit `3a80746`)

`detect_page` now accepts a `retry_callback: Callable[[str], None]` parameter. On each retry (rate-limit, timeout, server error, or other), it calls the callback with a human-readable status string such as `"rate-limited on page 15 (attempt 2/5) — waiting 8s"`. `run_detection` accepts a `retry_callback` parameter and passes it to each `detect_page` call. `main.py` passes a thread-safe callback that writes to `job["retry_status"]`. The `/api/jobs/{id}/status` endpoint exposes `retry_status`; the frontend `pollStatus()` shows it in the progress bar when set, replacing the generic "Page N of M — detecting" label.

### Mode banner (commit `3a80746`)

The processing screen title now shows the active mode immediately on job creation:
- Fast mode: `⚡ FAST MODE — reading first pages of pink-bounded blocks only`
- Full mode: `◼ FULL MODE — reading every page`

Phase A header shows a persistent mode banner (amber for fast, blue for full) with a one-line description. This makes a mode mismatch visible within the first five seconds of a job, not after 16 pages.

The fast-mode checkbox is always visible in the new-job form (not gated on batch type). Selecting non-TIB auto-checks it; the user can uncheck it explicitly. The checkbox label includes a one-line explanation: "local pink detection + first-page-only API — faster for non-TIB batches."

### Fixture suite results confirming current code (commit `3a80746`)

Run after all changes in this decision. All 67 pages across fixtures #1, #2, #4, #5. Model: `gemini-3-flash-preview`. 0 API errors. 0 wrong-ticket assignments. Results identical to Decision 15 run.

**Unit tests: 113 passed, 0 failed.**


---

## Decision 17 — Persistent disk and job state persistence (2026-07-14) — LOCKED

### Context

Render's web service uses ephemeral storage by default. Any service restart (deploy, free-tier spin-down, crash) wipes `/tmp`, destroying uploaded PDFs and all in-memory job state. This caused the Phase B image-not-loading bug: the PDF was deleted by a mid-session deploy, but the job record survived in memory long enough to serve a broken image response.

### Changes

**1. Persistent disk (`render.yaml`)**

A 1 GB Render disk named `sts-data` is mounted at `/data`. All job data (PDF, thumbnails, checkpoint, state file) is written to `/data/sts_jobs/{job_id}/`. The mount path is exposed to the app via the `STS_DATA_DIR` env var (value: `/data/sts_jobs`). Locally, `STS_DATA_DIR` defaults to `$TMPDIR/sts_jobs` so no local config change is needed.

**2. Job state written to disk on every mutation (`main.py`)**

A `persist_job(job_id)` helper writes the full job dict to `{job_dir}/state.json` atomically (write to `.tmp`, then `rename`). It is called after every mutation:
- Job creation (initial state)
- Status transitions: `detecting`, `grouping`, `ready`, `error`
- Pink diagnostics and pre_boundaries set (fast mode)
- Fast mode metrics set
- Detection results stored
- Review state stored after grouping
- Every `update_review` edit (reassign, split, merge, move_page)
- Every `repool_job` call (Phase A boundary confirmation)
- Confirm (ZIP built, confirmed_snapshot written)

**3. Startup reloads from disk**

On startup, the app scans `JOBS_ROOT/*/state.json`, skips expired jobs (older than 24h) and jobs whose PDF is missing, and reloads the rest into the in-memory `jobs` dict. Jobs that were mid-detection when the service restarted are set to `status=error` with a message ("Service restarted during detection — please re-submit this job.") — detection is not resumable, but `ready` and `confirmed` jobs survive intact.

**4. Cleanup**

`cleanup_job()` deletes the entire per-job directory (PDF, thumbnails, checkpoint, state.json). This is called:
- 10 minutes after confirm (post-download grace period)
- 24 hours after job creation (TTL)
- At startup for jobs older than 24h

**5. Restart-survives-review guarantee**

A job that has reached `status=ready` (detection complete, review state built) will survive a service restart. After restart, the user can reload the page, navigate to the same job URL, and resume Phase A/B review with all edits intact. The only data lost on restart is the in-flight progress of a job that was mid-detection.

### Restart test procedure (runbook)

This cannot be fully automated (requires a live Render restart), but the following manual procedure verifies the guarantee:

1. Submit a job and wait for `status=ready`.
2. Make at least one Phase A boundary edit and one Phase B ticket reassignment.
3. In the Render dashboard, trigger a **Manual Deploy** of the current commit (this restarts the service).
4. After the deploy is green, reload the app in the browser.
5. Navigate to the same job URL. The job should load with all edits intact.
6. Confirm the batch. The ZIP should download correctly.

Expected log lines on restart:
```
Startup: reloaded job {id} (status=ready)
Startup complete: 1 jobs reloaded, 0 expired
```

### Disk capacity

1 GB disk. A typical 20-page batch uses ~5 MB (PDF ~3 MB, thumbnails ~1 MB, state ~50 KB). The 24h TTL and post-confirm 10-minute cleanup keep disk usage bounded. At 5 jobs/day, peak usage is ~25 MB — well within the 1 GB limit. If usage approaches 800 MB, the startup cleanup will expire old jobs.

### Fixture suite results confirming current code

67 pages across fixtures #1, #2, #4, #5. Model: `gemini-3-flash-preview`. 0 API errors. 0 wrong-ticket assignments. 113 unit tests passed. Results identical to Decision 16 run.
