"""
Playwright diagnostic: reproduce the file-clear bug in bulk mode.
Runs against the live deployed app.
Steps:
  1. Login
  2. Open Bulk Mode
  3. Create a Non-TIB batch
  4. Click + Add File
  5. Select the fixture6 PDF
  6. Wait 15 seconds (3 × 5s poll cycles)
  7. Check whether the file input still has a file selected
  8. Print DOM state, console errors, and network requests for diagnosis
"""
import asyncio
import os
import sys
from playwright.async_api import async_playwright

APP_URL = "https://ship-ticket-splitter.onrender.com"
PASSWORD = "crystal2026"
PDF_PATH = os.path.abspath("tests/fixtures/fixture6/input.pdf")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # Capture console messages
        console_msgs = []
        page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))

        # Capture network requests that touch batches-container or batch cards
        network_log = []
        page.on("response", lambda r: network_log.append(f"{r.status} {r.request.method} {r.url}") if "/api/batches" in r.url else None)

        print("=== Step 1: Login ===")
        await page.goto(APP_URL)
        await page.fill("#login-pw", PASSWORD)
        await page.click("#login-btn")
        await page.wait_for_selector("#bulk-mode-btn", timeout=10000)
        print("  Logged in OK")

        print("=== Step 2: Open Bulk Mode ===")
        await page.click("#bulk-mode-btn")
        await page.wait_for_selector("#bulk-screen", state="visible", timeout=5000)
        print("  Bulk screen visible")

        print("=== Step 3: Create Non-TIB batch ===")
        await page.click("#bulk-new-batch-btn")
        await page.wait_for_selector("#new-batch-screen", state="visible", timeout=5000)
        await page.click("#nb-bt-nontib")
        await page.fill("#nb-wl-input", "301053, 299198, 298404, 300588, 300871, 300291")
        await page.click("#nb-submit-btn")
        await page.wait_for_selector("#bulk-screen", state="visible", timeout=10000)
        # Wait for the batch card to appear
        await page.wait_for_selector(".batch-card", timeout=10000)
        batch_id = await page.eval_on_selector(".batch-card", "el => el.id.replace('batch-card-', '')")
        print(f"  Batch created: {batch_id}")

        print("=== Step 4: Click + Add File ===")
        add_btn = page.locator(f".add-file-btn[data-batch='{batch_id}']")
        await add_btn.click()
        form = page.locator(f"#add-sj-form-{batch_id}")
        await form.wait_for(state="visible", timeout=5000)
        print("  Upload form visible")

        print("=== Step 5: Select PDF file ===")
        file_input = page.locator(f"#sj-pdf-{batch_id}")
        await file_input.set_input_files(PDF_PATH)
        # Verify file is selected
        file_count_before = await page.eval_on_selector(
            f"#sj-pdf-{batch_id}", "el => el.files.length"
        )
        print(f"  Files selected immediately after set_input_files: {file_count_before}")

        print("=== Step 6: Wait 15s (3 × 5s poll cycles) ===")
        for i in range(3):
            await asyncio.sleep(5)
            # Check if the form is still in the DOM and file still selected
            form_exists = await page.locator(f"#add-sj-form-{batch_id}").count()
            file_input_exists = await page.locator(f"#sj-pdf-{batch_id}").count()
            if file_input_exists > 0:
                file_count = await page.eval_on_selector(
                    f"#sj-pdf-{batch_id}", "el => el.files.length"
                )
                file_name = await page.eval_on_selector(
                    f"#sj-pdf-{batch_id}", "el => el.files[0] ? el.files[0].name : 'NONE'"
                )
            else:
                file_count = 0
                file_name = "INPUT ELEMENT GONE"
            form_hidden = await page.eval_on_selector(
                f"#add-sj-form-{batch_id}",
                "el => el.classList.contains('hidden')"
            ) if form_exists else "FORM ELEMENT GONE"
            print(f"  Poll {i+1}: form_exists={form_exists} form_hidden={form_hidden} "
                  f"file_input_exists={file_input_exists} files={file_count} name={file_name}")

        print("\n=== Step 7: DOM snapshot of batch card ===")
        card_html = await page.eval_on_selector(
            f"#batch-card-{batch_id}",
            "el => el.outerHTML"
        )
        # Print first 2000 chars
        print(card_html[:2000])

        print("\n=== Console messages ===")
        for m in console_msgs:
            print(" ", m)

        print("\n=== Network /api/batches calls ===")
        for r in network_log:
            print(" ", r)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
