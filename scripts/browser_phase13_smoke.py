"""Headless browser smoke check for the Phase 13 project work surface."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8014")
    parser.add_argument("--project", required=True)
    parser.add_argument("--screenshot", default=str(Path.home() / "AppData/Local/Temp/mireye-phase13.png"))
    parser.add_argument("--chat", action="store_true", help="Exercise the configured live diligence agent")
    args = parser.parse_args()
    console_errors, failed_requests = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--use-angle=swiftshader", "--enable-webgl"])
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("requestfailed", lambda request: failed_requests.append({"url": request.url, "error": request.failure}))
        await page.goto(args.base_url, wait_until="networkidle")
        await page.evaluate("project => sessionStorage.setItem('mireye-active-project-id', project)", args.project)
        await page.reload(wait_until="networkidle")
        await page.locator("#projectIntelligence:not([hidden])").wait_for()
        await page.locator("#projectChanges:not([hidden])").wait_for()
        await page.locator("#checkProject").click()
        await page.locator("#projectAgentResponse").filter(has_text="currently fresh").wait_for()
        agent_response = None
        if args.chat:
            await page.locator("#projectChatInput").fill("What changed since the last snapshot?")
            await page.locator("#projectChatForm button[type=submit]").click()
            await page.wait_for_function(
                "() => document.querySelector('#projectChatInput').value === '' && !document.querySelector('#projectAgentResponse').textContent.startsWith('Reviewing')",
                timeout=150000,
            )
            agent_response = (await page.locator("#projectAgentResponse").inner_text()).strip()
        sandbox_url = await page.locator("#candidateList a").first.get_attribute("href")
        change_text = await page.locator("#projectChanges").inner_text()
        readiness_text = await page.locator("#projectIntelligence").inner_text()
        await page.screenshot(path=args.screenshot, full_page=True)
        if sandbox_url:
            await page.goto(args.base_url + sandbox_url, wait_until="networkidle")
            await page.locator(".maplibregl-canvas").wait_for(timeout=30000)
        report = {
            "project_loaded": True, "what_changed_visible": "WHAT CHANGED" in change_text.upper(),
            "readiness_visible": "POWER READINESS" in readiness_text.upper() and "ENTITLEMENT" in readiness_text.upper(),
            "check_now_current": True, "sandbox_url": sandbox_url, "sandbox_map_loaded": bool(sandbox_url),
            "agent_response": agent_response,
            "console_errors": console_errors, "failed_requests": failed_requests, "screenshot": args.screenshot,
        }
        print(json.dumps(report, indent=2))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
