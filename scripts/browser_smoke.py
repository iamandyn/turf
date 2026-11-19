"""End-to-end browser check of the Turf frontend (headless Chromium via Playwright).

Fails on any console error, page error, CSP violation, or accessibility
violation (axe-core), and walks the main flow: pick a competitor, add a second
one, add your own chain (only near it, then skip near it), check the results,
export both CSV formats, reload from the shareable URL, open an old-style link, pick a metro area,
upload a store list, and toggle map layers. Saves screenshots.

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

from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import ConsoleMessage, FilePayload, Page, sync_playwright

# Downtown and north Austin, for the store list upload.
STORE_LIST = b"name,latitude,longitude\nDowntown,30.2672,-97.7431\nNorth,30.4021,-97.7253\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", default="data/screenshots", help="screenshot directory")
    parser.add_argument("--chain", default="orangetheory fitness")
    parser.add_argument("--chain2", default="Anytime Fitness", help="second competitor")
    parser.add_argument("--own", default="Planet Fitness", help="text typed into your chain box")
    parser.add_argument("--state", default="TX")
    parser.add_argument("--metro", default="Austin", help="text typed into the metro box")
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
        n_one = count(page)
        check(n_one > 0, f"targets in {args.state}: {n_one}", problems)
        check(page.locator("#zoom-hint").is_visible(), "zoom hint shown at state zoom", problems)

        # 2b. A second competitor in the same list.
        page.fill("#target-input", args.chain2)
        page.wait_for_selector("#target-results li[role=option]", timeout=10_000)
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelectorAll('#target-selected li').length === 2")
        wait_idle(page)
        n_all = count(page)
        check(n_all > n_one, f"with {args.chain2} added: {n_all}", problems)

        # 3. Your own chain: only near it (the default), then skip near it.
        page.fill("#own-input", args.own)
        page.wait_for_selector("#own-results li[role=option]", timeout=10_000)
        page.keyboard.press("Enter")
        page.wait_for_selector("#own-selected:not([hidden])")
        wait_idle(page)
        n_near = count(page)
        detail = page.inner_text("#summary-detail")
        check(
            0 < n_near < n_all and "only within 3 mi" in detail,
            f"only near {args.own}: {n_near} ({detail})",
            problems,
        )
        page.check('input[name="own-filter"][value="skip_near"]')
        wait_idle(page)
        n_skip = count(page)
        detail = page.inner_text("#summary-detail")
        check(
            0 < n_skip < n_all and "skipped within 1 mi" in detail,
            f"skip near {args.own}: {n_skip} ({detail})",
            problems,
        )
        page.check('input[name="own-filter"][value="only_near"]')
        wait_idle(page)
        check(count(page) == n_near, "back to only near restores the count and 3 mi", problems)
        check(page.locator("#target-list li").count() > 0, "sidebar list has rows", problems)
        check(page.locator("#dropped-section").is_visible(), "skipped list shown", problems)
        page.screenshot(path=str(out / "02-results.png"))
        check_a11y(page, "results page", problems)

        # 3b. Layer toggles.
        toggle = page.locator('.legend button[data-layer="own"]')
        toggle.click()
        check(toggle.get_attribute("aria-pressed") == "false", "legend hides your layer", problems)
        toggle.click()

        # 4. Exports.
        for fmt, header in (("google_ads_editor", ["Campaign", "Location"]), ("generic", None)):
            with page.expect_download() as dl:
                page.click(f'.export button[data-format="{fmt}"]')
            path = dl.value.path()
            rows = list(csv.reader(io.StringIO(Path(path).read_text())))
            ok = len(rows) - 1 == n_near and (header is None or rows[0] == header)
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
        restored = count(page2)
        check(restored == n_near, f"URL restore ({url.split('?', 1)[-1]}): {restored}", problems)

        # 5b. Links made before "only near" existed meant skip-near; they still do.
        chain_q = args.chain.replace(" ", "+")
        old = browser.new_page(viewport={"width": 1400, "height": 900})
        old.on("console", on_console)
        old.goto(
            f"{args.base_url}/?chain={chain_q}&own={args.own.lower().replace(' ', '+')}"
            f"&state={args.state}&skip=1",
            wait_until="networkidle",
        )
        old.wait_for_selector("#summary:not([hidden])", timeout=15_000)
        wait_idle(old)
        skip_checked = old.is_checked('input[name="own-filter"][value="skip_near"]')
        check(skip_checked, f"old link opens in skip mode: {count(old)}", problems)

        # 5c. Upload a store list.
        up = browser.new_page(viewport={"width": 1400, "height": 900})
        up.on("console", on_console)
        up.goto(f"{args.base_url}/?chain={chain_q}&state={args.state}", wait_until="networkidle")
        up.wait_for_selector("#summary:not([hidden])", timeout=15_000)
        upload: FilePayload = {"name": "stores.csv", "mimeType": "text/csv", "buffer": STORE_LIST}
        up.set_input_files("#own-file", files=[upload])
        up.wait_for_selector("#upload-selected:not([hidden])")
        wait_idle(up)
        detail = up.inner_text("#summary-detail")
        check(
            count(up) > 0 and "uploaded stores" in detail,
            f"uploaded store list: {count(up)} ({detail})",
            problems,
        )
        up.screenshot(path=str(out / "05-upload.png"))

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

        # 6b. A metro area instead of a state; it lands in the shareable URL.
        metro = browser.new_page(viewport={"width": 1400, "height": 900})
        metro.on("console", on_console)
        metro.goto(f"{args.base_url}/?chain={chain_q}", wait_until="networkidle")
        metro.wait_for_selector("#summary:not([hidden])", timeout=15_000)
        wait_idle(metro)
        n_us = count(metro)
        metro.fill("#metro-input", args.metro)
        metro.wait_for_selector("#metro-results li[role=option]", timeout=10_000)
        metro.keyboard.press("Enter")
        metro.wait_for_selector("#metro-selected:not([hidden])")
        wait_idle(metro)
        n_metro, detail = count(metro), metro.inner_text("#summary-detail")
        check(
            0 < n_metro < n_us and args.metro.lower() in detail.lower() and "metro=" in metro.url,
            f"{args.metro} metro: {n_metro} of {n_us} ({detail[:60]}…)",
            problems,
        )
        metro.wait_for_timeout(1000)  # let the map finish zooming to the metro
        metro.screenshot(path=str(out / "06-metro.png"))

        # 7. Phone layout.
        phone = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        phone.on("console", on_console)
        phone.goto(url, wait_until="networkidle")
        phone.wait_for_selector("#summary:not([hidden])", timeout=15_000)
        wait_idle(phone)
        phone.screenshot(path=str(out / "03-phone.png"), full_page=True)
        check_a11y(phone, "phone layout", problems)
        browser.close()

    print(f"screenshots: {out}/")
    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("all checks passed")


def count(page: Page) -> int:
    return int(page.inner_text("#n-targets").replace(",", ""))


def check_a11y(page: Page, label: str, problems: list[str]) -> None:
    results = Axe().run(page)
    violations = results.response["violations"]
    for v in violations:
        targets = ", ".join(str(n["target"]) for n in v["nodes"][:3])
        print(f"     axe {v['impact']}: {v['id']}: {v['help']} ({targets})")
    check(not violations, f"accessibility ({label}): {len(violations)} axe violations", problems)


def wait_idle(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#loading[hidden]", state="attached", timeout=15_000)


def check(ok: bool, label: str, problems: list[str]) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        problems.append(label)


if __name__ == "__main__":
    main()
