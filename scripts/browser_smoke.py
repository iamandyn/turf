"""End-to-end browser check of the Turf frontend (headless Chromium via Playwright).

Fails on any console error, page error, or CSP violation, and walks the main
flow: pick a competitor, add your own chain to skip, check the results, export
both CSV formats, and reload from the shareable URL. Saves screenshots.

Usage:
    uv run python scripts/browser_smoke.py --base-url http://127.0.0.1:8000
    uv run python scripts/browser_smoke.py --base-url https://turf.dunkasaur.gg --out /tmp/shots

One-time setup: uv run playwright install chromium
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

from playwright.sync_api import ConsoleMessage, Page, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", default="data/screenshots", help="screenshot directory")
    parser.add_argument("--chain", default="orangetheory fitness")
    parser.add_argument("--own", default="Planet Fitness", help="text typed into the skip box")
    parser.add_argument("--state", default="TX")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    def on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            problems.append(f"console error: {msg.text}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900}, accept_downloads=True)
        page.on("console", on_console)
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))

        page.goto(args.base_url, wait_until="networkidle")
        page.wait_for_selector("#map canvas", timeout=15_000)
        check(
            page.locator("#empty-state").is_visible(), "empty state shown on first load", problems
        )
        page.screenshot(path=str(out / "01-empty.png"))

        # 1. Competitor via search (types, waits for the list, presses Enter).
        page.fill("#target-input", args.chain[:8])
        page.wait_for_selector("#target-results li[role=option]", timeout=10_000)
        first = page.locator("#target-results li[role=option]").first.inner_text()
        page.keyboard.press("Enter")
        page.wait_for_selector("#summary:not([hidden])", timeout=15_000)
        print(f"search picked: {first.splitlines()[0]}")

        # 2. Narrow to a state, widen the radius.
        page.select_option("#state-select", args.state)
        page.locator("#radius").fill("3")
        wait_idle(page)
        n_all = int(page.inner_text("#n-targets").replace(",", ""))
        check(n_all > 0, f"targets in {args.state}: {n_all}", problems)

        # 3. Skip areas near your own chain.
        page.fill("#own-input", args.own)
        page.wait_for_selector("#own-results li[role=option]", timeout=10_000)
        page.keyboard.press("Enter")
        page.wait_for_selector("#own-selected:not([hidden])")
        wait_idle(page)
        n_skip = int(page.inner_text("#n-targets").replace(",", ""))
        detail = page.inner_text("#summary-detail")
        check(n_skip <= n_all, f"after skipping near {args.own}: {n_skip} ({detail})", problems)
        check(page.locator("#target-list li").count() > 0, "sidebar list has rows", problems)
        page.screenshot(path=str(out / "02-results.png"))

        # 4. Exports.
        for fmt, header in (("google_ads_editor", ["Campaign", "Location"]), ("generic", None)):
            with page.expect_download() as dl:
                page.click(f'.export button[data-format="{fmt}"]')
            path = dl.value.path()
            rows = list(csv.reader(io.StringIO(Path(path).read_text())))
            ok = len(rows) - 1 == n_skip and (header is None or rows[0] == header)
            check(
                ok, f"{fmt} export: {dl.value.suggested_filename}, {len(rows) - 1} rows", problems
            )

        # 5. Shareable URL restores the same view.
        url = page.url
        page2 = browser.new_page(viewport={"width": 1400, "height": 900})
        page2.on("console", on_console)
        page2.goto(url, wait_until="networkidle")
        page2.wait_for_selector("#summary:not([hidden])", timeout=15_000)
        wait_idle(page2)
        restored = int(page2.inner_text("#n-targets").replace(",", ""))
        check(restored == n_skip, f"URL restore ({url.split('?', 1)[-1]}): {restored}", problems)

        # 6. Categories: a too-broad one asks for a state; narrowing it works.
        cat = browser.new_page(viewport={"width": 1400, "height": 900})
        # The 422 is expected here; Chrome logs it as "Failed to load resource".
        cat.on("console", lambda m: None if "status of 422" in m.text else on_console(m))
        cat.goto(f"{args.base_url}/?cat=pet_groomer", wait_until="networkidle")
        cat.wait_for_selector("#error:not([hidden])", timeout=15_000)
        msg = cat.inner_text("#error")
        check("Pick a state" in msg, f"broad category asks for a state: {msg[:70]}", problems)
        cat.select_option("#state-select", args.state)
        cat.wait_for_selector("#summary:not([hidden])", timeout=15_000)
        wait_idle(cat)
        n_cat = int(cat.inner_text("#n-targets").replace(",", ""))
        check(
            n_cat > 0 and cat.locator("#error").is_hidden(),
            f"pet groomers in {args.state}: {n_cat}",
            problems,
        )
        cat.screenshot(path=str(out / "04-category.png"))

        # 7. Phone layout.
        phone = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        phone.on("console", on_console)
        phone.goto(url, wait_until="networkidle")
        phone.wait_for_selector("#summary:not([hidden])", timeout=15_000)
        wait_idle(phone)
        phone.screenshot(path=str(out / "03-phone.png"), full_page=True)
        browser.close()

    print(f"screenshots: {out}/")
    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("all checks passed")


def wait_idle(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#loading[hidden]", state="attached", timeout=15_000)


def check(ok: bool, label: str, problems: list[str]) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        problems.append(label)


if __name__ == "__main__":
    main()
