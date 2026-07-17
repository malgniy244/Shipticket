"""
Playwright browser E2E tests for Ship Ticket Splitter — Bulk Mode.

Runs against the live deployed app (STS_APP_URL env var, default: Render URL).
Requires: pip install playwright pytest-asyncio && playwright install chromium

Scenarios covered:
  BT01 — Login renders main screen
  BT02 — Bulk Mode screen opens
  BT03 — New Batch form: validation (submit disabled until type + whitelist)
  BT04 — Create batch succeeds, card appears in dashboard
  BT05 — Add File form opens and file input retains selection across 3 poll cycles
  BT06 — Upload sub-job, sub-job row appears with detecting/ready/queued status
  BT07 — cache-busting: index.html references bulk_patch.js with a ?v= hash
  BT08 — Back-to-batch navigation from Phase A (sub-job review)
"""

import asyncio
import os
import re
import pytest
from playwright.async_api import async_playwright, Page

APP_URL = os.environ.get("STS_APP_URL", "https://ship-ticket-splitter.onrender.com")
PASSWORD = os.environ.get("STS_PASSWORD", "crystal2026")
PDF_PATH = os.path.abspath("tests/fixtures/fixture6/input.pdf")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def make_authed_page():
    """Launch a browser, log in, and return (playwright, browser, page)."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(APP_URL, timeout=30000)
    await page.fill("#login-pw", PASSWORD)
    await page.click("#login-btn")
    await page.wait_for_selector("#bulk-mode-btn", timeout=20000)
    return pw, browser, page


async def open_bulk_screen(page: Page):
    await page.click("#bulk-mode-btn")
    await page.wait_for_selector("#bulk-screen", state="visible", timeout=8000)


async def create_batch(page: Page,
                       whitelist: str = "301053, 299198, 298404, 300588, 300871, 300291") -> str:
    """Create a Non-TIB batch and return its batch_id (captured from API response)."""
    # Intercept the POST /api/batches response to get the authoritative batch_id
    batch_id_holder = []
    async def capture_batch_id(response):
        if "/api/batches" in response.url and response.request.method == "POST":
            try:
                data = await response.json()
                if "batch_id" in data:
                    batch_id_holder.append(data["batch_id"])
            except Exception:
                pass
    page.on("response", capture_batch_id)

    await page.click("#bulk-new-batch-btn")
    await page.wait_for_selector("#new-batch-screen", state="visible", timeout=5000)
    await page.click("#nb-bt-nontib")
    await page.fill("#nb-wl-input", whitelist)
    await page.click("#nb-submit-btn")
    await page.wait_for_selector("#bulk-screen", state="visible", timeout=10000)

    # Wait up to 5s for the API response to be captured
    for _ in range(50):
        if batch_id_holder:
            break
        await asyncio.sleep(0.1)

    page.remove_listener("response", capture_batch_id)

    assert batch_id_holder, "POST /api/batches response not captured — batch_id unknown"
    batch_id = batch_id_holder[0]

    # Wait for the card to appear in the DOM
    await page.wait_for_selector(f"#batch-card-{batch_id}", timeout=10000)
    return batch_id


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bt01_login():
    """BT01: Login renders the main screen with the topbar visible."""
    pw, browser, page = await make_authed_page()
    try:
        assert await page.locator("#bulk-mode-btn").is_visible()
        assert await page.locator("#new-job-btn").is_visible()
        assert await page.locator("#logout-btn").is_visible()
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_bt02_bulk_screen_opens():
    """BT02: Clicking Bulk Mode shows the bulk screen."""
    pw, browser, page = await make_authed_page()
    try:
        await open_bulk_screen(page)
        assert await page.locator("#bulk-screen").is_visible()
        assert await page.locator("#bulk-new-batch-btn").is_visible()
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_bt03_new_batch_form_validation():
    """BT03: Create Batch button is disabled until both type and whitelist are filled."""
    pw, browser, page = await make_authed_page()
    try:
        await open_bulk_screen(page)
        await page.click("#bulk-new-batch-btn")
        await page.wait_for_selector("#new-batch-screen", state="visible", timeout=5000)

        # Submit button should be disabled initially
        assert await page.locator("#nb-submit-btn").is_disabled(), \
            "Submit should be disabled before filling form"

        # Select type only — still disabled
        await page.click("#nb-bt-nontib")
        assert await page.locator("#nb-submit-btn").is_disabled(), \
            "Submit should be disabled without whitelist"

        # Fill whitelist — now enabled
        await page.fill("#nb-wl-input", "301053, 299198")
        assert await page.locator("#nb-submit-btn").is_enabled(), \
            "Submit should be enabled with type + whitelist"

        # Cancel back
        await page.click("#nb-cancel-btn")
        await page.wait_for_selector("#bulk-screen", state="visible", timeout=5000)
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_bt04_create_batch_card_appears():
    """BT04: Creating a batch produces a card in the dashboard with correct metadata."""
    pw, browser, page = await make_authed_page()
    try:
        await open_bulk_screen(page)
        batch_id = await create_batch(page)

        card = page.locator(f"#batch-card-{batch_id}")
        assert await card.is_visible(), f"Batch card {batch_id} not visible"

        card_text = await card.inner_text()
        assert "NON_TIB" in card_text.upper() or "non_tib" in card_text.lower()
        assert "6" in card_text  # 6 tickets in whitelist
        assert "Open" in card_text
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_bt05_file_input_survives_poll_cycles():
    """BT05: File input retains its selection across 3 poll cycles (15s total)."""
    pw, browser, page = await make_authed_page()
    try:
        await open_bulk_screen(page)
        batch_id = await create_batch(page)

        # Open the Add File form
        add_btn = page.locator(f".add-file-btn[data-batch='{batch_id}']")
        await add_btn.click()
        form = page.locator(f"#add-sj-form-{batch_id}")
        await form.wait_for(state="visible", timeout=5000)

        # Select the PDF
        file_input = page.locator(f"#sj-pdf-{batch_id}")
        await file_input.set_input_files(PDF_PATH)

        # Verify file selected immediately
        file_count = await page.eval_on_selector(
            f"#sj-pdf-{batch_id}", "el => el.files.length"
        )
        assert file_count == 1, f"Expected 1 file selected immediately, got {file_count}"

        # Wait through 3 poll cycles and check after each
        for cycle in range(1, 4):
            await asyncio.sleep(6)  # slightly over 5s poll interval

            # Form must still be visible
            form_visible = await form.is_visible()
            assert form_visible, \
                f"Poll cycle {cycle}: upload form became hidden or was destroyed"

            # File input must still exist
            file_input_count = await page.locator(f"#sj-pdf-{batch_id}").count()
            assert file_input_count == 1, \
                f"Poll cycle {cycle}: file input element was removed from DOM"

            # File must still be selected
            file_count = await page.eval_on_selector(
                f"#sj-pdf-{batch_id}", "el => el.files.length"
            )
            file_name = await page.eval_on_selector(
                f"#sj-pdf-{batch_id}", "el => el.files[0] ? el.files[0].name : 'NONE'"
            )
            assert file_count == 1, (
                f"Poll cycle {cycle}: file was cleared. "
                f"files.length={file_count}, name={file_name}"
            )

        # Cancel (cleanup)
        cancel_btn = page.locator(f".sj-cancel-btn[data-batch='{batch_id}']")
        await cancel_btn.click()
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_bt06_upload_subjob_appears():
    """BT06: Uploading a PDF creates a sub-job row in the batch card."""
    pw, browser, page = await make_authed_page()
    try:
        await open_bulk_screen(page)
        batch_id = await create_batch(page)

        # Open Add File form
        add_btn = page.locator(f".add-file-btn[data-batch='{batch_id}']")
        await add_btn.click()
        form = page.locator(f"#add-sj-form-{batch_id}")
        await form.wait_for(state="visible", timeout=5000)

        # Fill expected count first, then file (so validate() fires on the change event)
        expected_input = page.locator(f"#sj-expected-{batch_id}")
        await expected_input.fill("6")
        file_input = page.locator(f"#sj-pdf-{batch_id}")
        await file_input.set_input_files(PDF_PATH)
        # Trigger change event explicitly in case Playwright didn't fire it
        await page.eval_on_selector(
            f"#sj-pdf-{batch_id}",
            "el => el.dispatchEvent(new Event('change', {bubbles:true}))"
        )

        # Submit — wait for the upload button to become enabled (file selected)
        upload_btn = page.locator(f".sj-upload-btn[data-batch='{batch_id}']")
        for _ in range(20):
            if await upload_btn.is_enabled():
                break
            await asyncio.sleep(0.25)

        # Diagnostic: report button state and file count if still disabled
        btn_enabled = await upload_btn.is_enabled()
        file_count_diag = await page.eval_on_selector(
            f"#sj-pdf-{batch_id}", "el => el.files.length"
        )
        exp_val_diag = await expected_input.input_value()
        assert btn_enabled, (
            f"Upload button never became enabled. "
            f"files.length={file_count_diag}, expected_value={exp_val_diag!r}"
        )
        await upload_btn.click()

        # Wait up to 30s for progress-screen to appear (Render cold-start can add ~10s)
        progress_visible = False
        for _ in range(60):
            if await page.locator("#progress-screen").is_visible():
                progress_visible = True
                break
            # Check for error in form
            err_el = page.locator(f"#sj-error-{batch_id}")
            if await err_el.count() > 0:
                cls = await err_el.get_attribute("class") or ""
                if "hidden" not in cls:
                    err_text = await err_el.inner_text()
                    if err_text.strip():
                        pytest.fail(f"Upload error: {err_text}")
            await asyncio.sleep(0.5)

        assert progress_visible, "Progress screen never appeared after upload"

        # Navigate back to bulk screen
        await open_bulk_screen(page)

        # The batch card should now show a sub-job row
        card = page.locator(f"#batch-card-{batch_id}")
        await card.wait_for(state="visible", timeout=10000)

        # Wait for the upload form to be hidden (upload complete, form dismissed)
        # The sub-job table is only updated when the form is hidden
        form_locator = page.locator(f"#add-sj-form-{batch_id}")
        for _ in range(20):
            form_count = await form_locator.count()
            if form_count == 0:
                break  # form removed from DOM
            form_hidden = "hidden" in (await form_locator.get_attribute("class") or "")
            if form_hidden:
                break
            await asyncio.sleep(0.5)

        # Give loadBatches() time to complete its first async fetch
        await asyncio.sleep(2)

        # Wait up to 20s for the sub-job row to appear (poll cycle needed)
        sj_row_appeared = False
        for _ in range(7):
            card_text = await card.inner_text()
            if any(s in card_text.lower() for s in ("detecting", "ready", "queued", "error", "grouping")):
                sj_row_appeared = True
                break
            await asyncio.sleep(3)

        assert sj_row_appeared, (
            f"Sub-job row never appeared in batch card. "
            f"Card text: {await card.inner_text()}"
        )
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_bt07_cache_busting_hash_present():
    """BT07: index.html references bulk_patch.js with a ?v= content hash."""
    pw, browser, page = await make_authed_page()
    try:
        html = await page.content()
        match = re.search(r'bulk_patch\.js\?v=([0-9a-f]{12})', html)
        assert match, (
            "bulk_patch.js not referenced with a ?v=<hash> cache-buster in index.html. "
            f"Found: {re.findall(r'bulk_patch[^\"<]*', html)}"
        )
        hash_val = match.group(1)
        assert len(hash_val) == 12, f"Hash should be 12 hex chars, got: {hash_val}"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_bt08_back_to_batch_from_phase_a():
    """BT08: After uploading a sub-job and detection completes, Review opens Phase A
    with a Back-to-Batch button that returns to the bulk dashboard."""
    pw, browser, page = await make_authed_page()
    try:
        await open_bulk_screen(page)

        # Find a batch with a ready sub-job
        cards = await page.locator(".batch-card").all()
        review_btn = None
        for card in cards:
            btns = await card.locator(".sj-review-btn").all()
            if btns:
                review_btn = btns[0]
                break

        if review_btn is None:
            pytest.skip("No ready sub-job available for Phase A test — run BT06 first")

        await review_btn.click()

        # Phase A screen should appear
        await page.wait_for_selector("#phase-a-screen", state="visible", timeout=10000)

        # Back-to-Batch button should be present
        back_btn = page.locator(".back-to-batch-btn")
        assert await back_btn.count() > 0, "Back-to-Batch button not found in Phase A header"
        assert await back_btn.is_visible(), "Back-to-Batch button not visible"

        # Click it — should return to bulk screen
        await back_btn.click()
        await page.wait_for_selector("#bulk-screen", state="visible", timeout=5000)
        assert await page.locator("#bulk-screen").is_visible()
    finally:
        await browser.close()
        await pw.stop()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    pytest.main([__file__, "-v", "--tb=short"] + sys.argv[1:])
