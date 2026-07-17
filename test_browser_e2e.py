"""
Playwright browser E2E tests for Ship Ticket Splitter — Bulk Mode.

Runs against the live deployed app (APP_URL env var, default: Render URL).
Requires: pip install playwright && playwright install chromium

Scenarios covered:
  BT01 — Login renders main screen
  BT02 — Bulk Mode screen opens
  BT03 — New Batch form: validation (submit disabled until type + whitelist)
  BT04 — Create batch succeeds, card appears in dashboard
  BT05 — Add File form opens and file input retains selection across 3 poll cycles
  BT06 — Upload sub-job, sub-job row appears with 'detecting' or later status
  BT07 — cache-busting: index.html references bulk_patch.js with a ?v= hash
  BT08 — Back-to-batch navigation from Phase A (sub-job review)
"""

import asyncio
import os
import re
import pytest
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

APP_URL = os.environ.get("STS_APP_URL", "https://ship-ticket-splitter.onrender.com")
PASSWORD = os.environ.get("STS_PASSWORD", "crystal2026")
PDF_PATH = os.path.abspath("tests/fixtures/fixture6/input.pdf")
POLL_INTERVAL_MS = 5000  # must match the app's poll interval


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
async def browser():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        yield b
        await b.close()


@pytest.fixture(scope="module")
async def authed_page(browser):
    """A page that is already logged in."""
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(APP_URL)
    await page.fill("#login-pw", PASSWORD)
    await page.click("#login-btn")
    await page.wait_for_selector("#bulk-mode-btn", timeout=15000)
    yield page
    await ctx.close()


# ── Helper ────────────────────────────────────────────────────────────────────

async def open_bulk_screen(page: Page):
    await page.click("#bulk-mode-btn")
    await page.wait_for_selector("#bulk-screen", state="visible", timeout=8000)


async def create_batch(page: Page, whitelist: str = "301053, 299198, 298404, 300588, 300871, 300291") -> str:
    """Create a Non-TIB batch and return its batch_id."""
    await page.click("#bulk-new-batch-btn")
    await page.wait_for_selector("#new-batch-screen", state="visible", timeout=5000)
    await page.click("#nb-bt-nontib")
    await page.fill("#nb-wl-input", whitelist)
    await page.click("#nb-submit-btn")
    await page.wait_for_selector("#bulk-screen", state="visible", timeout=10000)
    await page.wait_for_selector(".batch-card", timeout=10000)
    # Get the first batch card's id
    batch_id = await page.eval_on_selector(
        ".batch-card", "el => el.id.replace('batch-card-', '')"
    )
    return batch_id


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bt01_login(authed_page):
    """BT01: Login renders the main screen with the topbar visible."""
    page = authed_page
    assert await page.locator("#bulk-mode-btn").is_visible()
    assert await page.locator("#new-job-btn").is_visible()
    assert await page.locator("#logout-btn").is_visible()


@pytest.mark.asyncio
async def test_bt02_bulk_screen_opens(authed_page):
    """BT02: Clicking Bulk Mode shows the bulk screen."""
    page = authed_page
    await open_bulk_screen(page)
    assert await page.locator("#bulk-screen").is_visible()
    assert await page.locator("#bulk-new-batch-btn").is_visible()


@pytest.mark.asyncio
async def test_bt03_new_batch_form_validation(authed_page):
    """BT03: Create Batch button is disabled until both type and whitelist are filled."""
    page = authed_page
    await open_bulk_screen(page)
    await page.click("#bulk-new-batch-btn")
    await page.wait_for_selector("#new-batch-screen", state="visible", timeout=5000)

    # Submit button should be disabled initially
    submit_disabled = await page.locator("#nb-submit-btn").is_disabled()
    assert submit_disabled, "Submit should be disabled before filling form"

    # Select type only — still disabled
    await page.click("#nb-bt-nontib")
    submit_disabled = await page.locator("#nb-submit-btn").is_disabled()
    assert submit_disabled, "Submit should be disabled without whitelist"

    # Fill whitelist — now enabled
    await page.fill("#nb-wl-input", "301053, 299198")
    submit_enabled = await page.locator("#nb-submit-btn").is_enabled()
    assert submit_enabled, "Submit should be enabled with type + whitelist"

    # Cancel back
    await page.click("#nb-cancel-btn")
    await page.wait_for_selector("#bulk-screen", state="visible", timeout=5000)


@pytest.mark.asyncio
async def test_bt04_create_batch_card_appears(authed_page):
    """BT04: Creating a batch produces a card in the dashboard with correct metadata."""
    page = authed_page
    await open_bulk_screen(page)
    batch_id = await create_batch(page)

    card = page.locator(f"#batch-card-{batch_id}")
    assert await card.is_visible(), f"Batch card {batch_id} not visible"

    # Check metadata text
    card_text = await card.inner_text()
    assert "NON_TIB" in card_text.upper() or "non_tib" in card_text.lower()
    assert "6" in card_text  # 6 tickets in whitelist
    assert "Open" in card_text


@pytest.mark.asyncio
async def test_bt05_file_input_survives_poll_cycles(authed_page):
    """BT05: File input retains its selection across 3 poll cycles (15s total)."""
    page = authed_page
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
    assert file_count == 1, f"Expected 1 file selected, got {file_count}"

    # Wait through 3 poll cycles (5s each = 15s total) and check after each
    for cycle in range(1, 4):
        await asyncio.sleep(5)

        # Form must still be visible (not hidden, not destroyed)
        form_visible = await form.is_visible()
        assert form_visible, f"Poll cycle {cycle}: upload form became hidden or was destroyed"

        # File input must still exist and have the file
        file_input_count = await page.locator(f"#sj-pdf-{batch_id}").count()
        assert file_input_count == 1, f"Poll cycle {cycle}: file input element was removed from DOM"

        file_count = await page.eval_on_selector(
            f"#sj-pdf-{batch_id}", "el => el.files.length"
        )
        file_name = await page.eval_on_selector(
            f"#sj-pdf-{batch_id}", "el => el.files[0] ? el.files[0].name : 'NONE'"
        )
        assert file_count == 1, (
            f"Poll cycle {cycle}: file was cleared. files.length={file_count}, name={file_name}"
        )

    # Cancel the form (cleanup)
    cancel_btn = page.locator(f".sj-cancel-btn[data-batch='{batch_id}']")
    await cancel_btn.click()


@pytest.mark.asyncio
async def test_bt06_upload_subjob_appears(authed_page):
    """BT06: Uploading a PDF creates a sub-job row in the batch card."""
    page = authed_page
    await open_bulk_screen(page)
    batch_id = await create_batch(page)

    # Open Add File form
    add_btn = page.locator(f".add-file-btn[data-batch='{batch_id}']")
    await add_btn.click()
    form = page.locator(f"#add-sj-form-{batch_id}")
    await form.wait_for(state="visible", timeout=5000)

    # Fill form
    file_input = page.locator(f"#sj-pdf-{batch_id}")
    await file_input.set_input_files(PDF_PATH)
    expected_input = page.locator(f"#sj-expected-{batch_id}")
    await expected_input.fill("6")

    # Submit
    upload_btn = page.locator(f".sj-upload-btn[data-batch='{batch_id}']")
    await upload_btn.wait_for(state="enabled", timeout=5000)
    await upload_btn.click()

    # Should navigate to progress screen
    await page.wait_for_selector("#progress-screen", state="visible", timeout=15000)

    # Navigate back to bulk screen
    await open_bulk_screen(page)

    # The batch card should now show 1 file and a sub-job row
    card = page.locator(f"#batch-card-{batch_id}")
    await card.wait_for(state="visible", timeout=10000)

    # Wait up to 10s for the sub-job row to appear (poll may need one cycle)
    sj_row_appeared = False
    for _ in range(5):
        card_text = await card.inner_text()
        if "detecting" in card_text.lower() or "ready" in card_text.lower() or "queued" in card_text.lower():
            sj_row_appeared = True
            break
        await asyncio.sleep(2)

    assert sj_row_appeared, (
        f"Sub-job row never appeared in batch card. Card text: {await card.inner_text()}"
    )


@pytest.mark.asyncio
async def test_bt07_cache_busting_hash_present(authed_page):
    """BT07: index.html references bulk_patch.js with a ?v= content hash."""
    page = authed_page
    html = await page.content()
    # Should contain bulk_patch.js?v=<12-char hex hash>
    match = re.search(r'bulk_patch\.js\?v=([0-9a-f]{12})', html)
    assert match, (
        "bulk_patch.js not referenced with a ?v=<hash> cache-buster in index.html. "
        f"Found: {re.findall(r'bulk_patch[^\"]*', html)}"
    )
    hash_val = match.group(1)
    assert len(hash_val) == 12, f"Hash should be 12 hex chars, got: {hash_val}"


@pytest.mark.asyncio
async def test_bt08_back_to_batch_from_phase_a(authed_page):
    """BT08: After uploading a sub-job and detection completes, Review opens Phase A
    with a Back-to-Batch button that returns to the bulk dashboard."""
    page = authed_page
    await open_bulk_screen(page)

    # Find a batch with a ready sub-job, or create one
    # First check if any existing batch has a ready sub-job
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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    pytest.main([__file__, "-v", "--tb=short"] + sys.argv[1:])
