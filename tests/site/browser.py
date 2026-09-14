"""Exercise the actual static pages, demo controls and accessibility offline."""

import argparse
import asyncio
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2] / "site"
PAGES = ("/", "/privacy", "/terms", "/cookies", "/support", "/connect", "/missing/deep/link")


class SiteHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        result = Path(super().translate_path(path))
        if not result.suffix and result.with_suffix(".html").is_file():
            return str(result.with_suffix(".html"))
        return str(result)

    def end_headers(self):
        config = json.loads((ROOT / "vercel.json").read_text())
        for rule in config["headers"]:
            if rule["source"] == "/(.*)":
                for h in rule["headers"]:
                    # HTTP local verification must not upgrade its own transport.
                    self.send_header(
                        h["key"], h["value"].replace("; upgrade-insecure-requests", "")
                    )
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        if code != 404:
            return super().send_error(code, message, explain)
        body = (ROOT / "404.html").read_bytes()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, *_args):
        pass


async def run(origin, axe, output):
    report = {"pages": [], "failures": []}
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for width in (1440, 390):
            context = await browser.new_context(viewport={"width": width, "height": 900})
            for path in PAGES:
                page = await context.new_page()
                errors, requests, failed_responses = [], [], []
                expected = 404 if path.startswith("/missing") else 200
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                page.on(
                    "console",
                    lambda m, errors=errors, expected=expected, path=path: (
                        errors.append(m.text)
                        if m.type == "error"
                        and not (
                            expected == 404
                            and "404" in m.text
                            and m.location.get("url") == origin + path
                        )
                        else None
                    ),
                )
                page.on(
                    "response",
                    lambda r, failed_responses=failed_responses, expected=expected, path=path: (
                        failed_responses.append(f"{r.status} {r.url}")
                        if r.status >= 400
                        and not (expected == 404 and r.status == 404 and r.url == origin + path)
                        else None
                    ),
                )
                page.on(
                    "requestfailed", lambda r, errors=errors: errors.append(f"{r.failure}: {r.url}")
                )
                page.on("request", lambda r, requests=requests: requests.append(r.url))
                response = await page.goto(origin + path, wait_until="networkidle")
                await page.evaluate("document.fonts.ready")
                await page.evaluate(axe.read_text())
                violations = await page.evaluate(
                    "async () => (await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa']}})).violations.map(v=>({id:v.id, impact:v.impact, nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))"
                )
                external = [u for u in requests if urlsplit(u).netloc != urlsplit(origin).netloc]
                record = {
                    "path": path,
                    "width": width,
                    "status": response.status,
                    "title": await page.title(),
                    "axe": violations,
                    "errors": errors[:] + failed_responses[:],
                    "external_requests": external,
                }
                report["pages"].append(record)
                (output / "browser-report.json").write_text(json.dumps(report, indent=2))
                expected = 404 if path.startswith("/missing") else 200
                if (
                    response.status != expected
                    or violations
                    or errors
                    or failed_responses
                    or external
                ):
                    report["failures"].append(record)
                assert await page.locator(".nav-links a[href='/connect']").is_visible(), (
                    "Connect must remain navigable on mobile"
                )
                assert await page.locator("h1").count() == 1
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    path
                )
                assert await context.cookies() == []
                assert await page.evaluate("localStorage.length + sessionStorage.length") == 0
                if path == "/":
                    await page.screenshot(path=str(output / f"home-{width}.png"), full_page=True)
                    tabs = page.get_by_role("tab")
                    assert await tabs.count() == 6
                    await tabs.first.focus()
                    await page.keyboard.press("End")
                    assert await tabs.last.get_attribute("aria-selected") == "true"
                    await page.keyboard.press("Home")
                    assert await tabs.first.get_attribute("aria-selected") == "true"
                    for idx in range(6):
                        await tabs.nth(idx).click()
                        panel = page.get_by_role("tabpanel").filter(visible=True)
                        await panel.get_by_role("button", name="Compress", exact=True).click()
                        await panel.locator(".retrieve-btn").click()
                        assert (
                            await panel.locator(".retrieve-out").get_attribute("aria-hidden")
                            == "false"
                        )
                        assert await panel.locator(".rt-result").inner_text()
                        await panel.get_by_role("button", name="Reset", exact=True).click()
                        await page.wait_for_timeout(1050)
                        assert await panel.locator(".meter-pct").inner_text() == "0%", (
                            "reset must cancel count animation"
                        )
                        assert (
                            await panel.locator(".retrieve-out").get_attribute("aria-hidden")
                            == "true"
                        )
                    await page.screenshot(path=str(output / f"demo-{width}.png"), full_page=True)
                record["errors"] = errors[:] + failed_responses[:]
                record["external_requests"] = [
                    u for u in requests if urlsplit(u).netloc != urlsplit(origin).netloc
                ]
                if (record["errors"] or record["external_requests"]) and record not in report[
                    "failures"
                ]:
                    report["failures"].append(record)
                await page.close()
            await context.close()
        ctx = await browser.new_context(
            java_script_enabled=False, viewport={"width": 390, "height": 844}
        )
        pg = await ctx.new_page()
        await pg.goto(origin)
        assert await pg.locator(".demo-facts").is_visible()
        assert await pg.locator(".demo-facts tbody tr").count() == 6
        assert await pg.get_by_role("heading", level=1).is_visible()
        await ctx.close()
        ctx = await browser.new_context(reduced_motion="reduce")
        pg = await ctx.new_page()
        await pg.goto(origin)
        assert (
            await pg.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto"
        )
        await ctx.close()
        await browser.close()
    (output / "browser-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    assert not report["failures"], "Page audit violations: see browser-report.json"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--origin")
    p.add_argument(
        "--axe",
        type=Path,
        default=Path(os.environ.get("AXE_PATH", "/mnt/data/axe/package/axe.min.js")),
    )
    p.add_argument("--output", type=Path, default=Path("artifacts/site"))
    args = p.parse_args()
    server = None
    if not args.origin:
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SiteHandler, directory=str(ROOT)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        args.origin = f"http://127.0.0.1:{server.server_port}"
    try:
        asyncio.run(run(args.origin.rstrip("/"), args.axe, args.output))
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
