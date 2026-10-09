import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1366, 'height': 768})
        page = await context.new_page()
        
        print("Opening HMIS...")
        await page.goto("http://127.0.0.1:8000")
        
        print("Clicking 'Register patient'...")
        await page.click("text=Register patient")
        
        print("Filling registration form...")
        await page.fill("#rName", "Test Patient")
        await page.fill("#rAge", "30")
        await page.select_option("#rGender", "male")
        await page.fill("#rReason", "Fever and cough")
        await page.click("button[type='submit']")
        
        print("Waiting for row in queue...")
        await page.wait_for_selector("text=Test Patient")
        
        print("Clicking 'Launch Voice Scribe'...")
        async with page.expect_navigation():
            await page.click("tbody#queue tr:nth-child(1) a:has-text('Launch Voice Scribe')")
        
        print("Waiting for Live badge on Scribe page...")
        await page.wait_for_selector("#mode.live")
        
        try:
            print("Selecting Upload source...")
            await page.click("button[data-src='upload']", timeout=10000)
        except Exception as e:
            content = await page.content()
            with open("debug.html", "w", encoding="utf-8") as f:
                f.write(content)
            await page.screenshot(path="debug_screenshot.png")
            raise e
        
        print("Setting input file...")
        await page.set_input_files("#file", "test.wav")
        
        print("Clicking Start...")
        await page.click("#btnStart")
        
        print("Waiting for 'Awaiting approval'...")
        await page.wait_for_selector("text=Awaiting approval", timeout=120000)
        
        # Small sleep to let the UI settle
        await asyncio.sleep(2)
        
        print("Taking screenshot of review screen...")
        await page.screenshot(path="review_screen.png")
        
        print("Clicking 'Approve and save to HMIS'...")
        await page.click("#btnApprove")
        
        print("Waiting for 'Open report (PDF)'...")
        await page.wait_for_selector("text=Open report (PDF)", timeout=10000)
        
        print("Clicking 'Open report (PDF)' and capturing new page...")
        async with context.expect_page() as report_page_info:
            await page.click("text=Open report (PDF)")
        report_page = await report_page_info.value
        await report_page.wait_for_load_state()
        
        print("Taking screenshot of report...")
        await asyncio.sleep(3)
        await report_page.screenshot(path="report.png", full_page=True)
        
        print("Closing browser...")
        await browser.close()
        print("UI tests completed successfully.")

if __name__ == "__main__":
    asyncio.run(run())
