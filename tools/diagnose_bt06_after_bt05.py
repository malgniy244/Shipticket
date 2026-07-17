"""
Reproduce the BT06 failure when run after BT05 (two batches in DOM).
Captures console errors and network calls during the upload attempt.
"""
import asyncio
import os
from playwright.async_api import async_playwright

APP_URL = "https://ship-ticket-splitter.onrender.com"
PASSWORD = "crystal2026"
PDF_PATH = os.path.abspath("tests/fixtures/fixture6/input.pdf")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        console_msgs = []
        page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))

        network_log = []
        page.on("response", lambda r: network_log.append(
            f"{r.status} {r.request.method} {r.url}"
        ) if "/api/batches" in r.url or "/api/jobs" in r.url else None)

        # Login
        await page.goto(APP_URL)
        await page.fill("#login-pw", PASSWORD)
        await page.click("#login-btn")
        await page.wait_for_selector("#bulk-mode-btn", timeout=20000)
        print("Logged in")

        # === Simulate BT05 ===
        print("\n=== BT05 simulation: create batch 1, open form, select file, cancel ===")
        await page.click("#bulk-mode-btn")
        await page.wait_for_selector("#bulk-screen", state="visible")

        # Create batch 1
        batch1_holder = []
        async def cap1(r):
            if "/api/batches" in r.url and r.request.method == "POST":
                try:
                    d = await r.json()
                    if "batch_id" in d: batch1_holder.append(d["batch_id"])
                except: pass
        page.on("response", cap1)
        await page.click("#bulk-new-batch-btn")
        await page.wait_for_selector("#new-batch-screen", state="visible")
        await page.click("#nb-bt-nontib")
        await page.fill("#nb-wl-input", "301053, 299198")
        await page.click("#nb-submit-btn")
        await page.wait_for_selector("#bulk-screen", state="visible")
        for _ in range(50):
            if batch1_holder: break
            await asyncio.sleep(0.1)
        page.remove_listener("response", cap1)
        b1 = batch1_holder[0]
        print(f"  Batch 1: {b1}")

        await page.wait_for_selector(f"#batch-card-{b1}")
        await page.locator(f".add-file-btn[data-batch='{b1}']").click()
        await page.locator(f"#add-sj-form-{b1}").wait_for(state="visible")
        await page.locator(f"#sj-pdf-{b1}").set_input_files(PDF_PATH)
        print("  File selected in batch 1 form")
        await asyncio.sleep(3)  # simulate user pausing
        await page.locator(f".sj-cancel-btn[data-batch='{b1}']").click()
        print("  Cancelled batch 1 form")

        # === Simulate BT06 ===
        print("\n=== BT06 simulation: create batch 2, upload file ===")

        # Create batch 2
        batch2_holder = []
        async def cap2(r):
            if "/api/batches" in r.url and r.request.method == "POST":
                try:
                    d = await r.json()
                    if "batch_id" in d: batch2_holder.append(d["batch_id"])
                except: pass
        page.on("response", cap2)
        await page.click("#bulk-new-batch-btn")
        await page.wait_for_selector("#new-batch-screen", state="visible")
        await page.click("#nb-bt-nontib")
        await page.fill("#nb-wl-input", "301053, 299198, 298404, 300588, 300871, 300291")
        await page.click("#nb-submit-btn")
        await page.wait_for_selector("#bulk-screen", state="visible")
        for _ in range(50):
            if batch2_holder: break
            await asyncio.sleep(0.1)
        page.remove_listener("response", cap2)
        b2 = batch2_holder[0]
        print(f"  Batch 2: {b2}")

        await page.wait_for_selector(f"#batch-card-{b2}")
        await page.locator(f".add-file-btn[data-batch='{b2}']").click()
        await page.locator(f"#add-sj-form-{b2}").wait_for(state="visible")
        print("  Form open")

        # Fill form
        await page.locator(f"#sj-expected-{b2}").fill("6")
        await page.locator(f"#sj-pdf-{b2}").set_input_files(PDF_PATH)
        await page.eval_on_selector(
            f"#sj-pdf-{b2}",
            "el => el.dispatchEvent(new Event('change', {bubbles:true}))"
        )

        # Check button state
        upload_btn = page.locator(f".sj-upload-btn[data-batch='{b2}']")
        for _ in range(20):
            if await upload_btn.is_enabled(): break
            await asyncio.sleep(0.25)
        btn_enabled = await upload_btn.is_enabled()
        file_count = await page.eval_on_selector(f"#sj-pdf-{b2}", "el => el.files.length")
        print(f"  Upload btn enabled: {btn_enabled}, files: {file_count}")

        if not btn_enabled:
            print("  ERROR: button not enabled, cannot proceed")
        else:
            await upload_btn.click()
            print("  Upload button clicked")

            # Wait for progress screen or error
            for i in range(30):
                ps_visible = await page.locator("#progress-screen").is_visible()
                err_el = page.locator(f"#sj-error-{b2}")
                err_text = ""
                if await err_el.count() > 0:
                    cls = await err_el.get_attribute("class") or ""
                    if "hidden" not in cls:
                        err_text = await err_el.inner_text()
                btn_text = await upload_btn.inner_text() if await upload_btn.count() > 0 else "gone"
                print(f"  t+{i*0.5:.1f}s: progress_screen={ps_visible} err={err_text!r} btn={btn_text!r}")
                if ps_visible or err_text:
                    break
                await asyncio.sleep(0.5)

        print("\n=== Console messages ===")
        for m in console_msgs[-20:]:
            print(" ", m)

        print("\n=== Network log ===")
        for r in network_log[-20:]:
            print(" ", r)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
