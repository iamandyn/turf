"""End-to-end browser check of the Turf frontend (headless Chromium via Playwright).

Fails on any console error, page error, CSP violation, or accessibility
violation (axe-core), and walks the report flow: the demo disclaimer on first
visit, opening a sample brand's report, expanding a recommendation (data and
brief), both exports, the shareable URL, the sample with no real relationship,
uploading an ad report and choosing its category, adjusting the signals,
connecting a simulated ad account (live backfill, then an incremental sync
after moving the platform's date), dark mode, and the phone layout. Saves
screenshots.

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

# Markets as an ad platform labels them; all should match a metro.
UPLOAD_MARKETS = [
    "Denver, CO", "Austin, TX", "Seattle, WA", "Portland OR", "Boston, MA", "Atlanta, GA",
    "Chicago, IL", "Phoenix, AZ", "Dallas, TX", "Minneapolis, MN", "Raleigh, NC", "Nashville, TN",
]  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", default="data/screenshots", help="screenshot directory")
    parser.add_argument("--sample", default="pets")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    def on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            problems.append(f"console error: {msg.text}")

    def new_page(width: int = 1400, height: int = 900, **kw: object) -> Page:
        page = browser.new_page(viewport={"width": width, "height": height}, **kw)  # type: ignore[arg-type]
        page.on("console", on_console)
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
        return page

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = new_page(accept_downloads=True)

        # 1. First visit: the demo disclaimer opens; it doesn't come back once closed.
        page.goto(args.base_url, wait_until="networkidle")
        page.wait_for_selector("#about[open]", timeout=10_000)
        about = page.inner_text("#about")
        check(
            "fictional" in about and "never stored" in about and "No real-time AI" in about,
            "demo disclaimer shown on first visit",
            problems,
        )
        page.screenshot(path=str(out / "01-about.png"))
        page.click("#about button")
        page.reload(wait_until="networkidle")
        page.wait_for_selector(".sample-card", timeout=10_000)
        check(page.locator("#about[open]").count() == 0, "disclaimer not shown again", problems)
        n_samples = page.locator(".sample-card").count()
        check(n_samples == 4, f"start screen: {n_samples} sample brands", problems)
        page.screenshot(path=str(out / "02-start.png"))

        # 2. A sample brand's report.
        page.click(f'.sample-card[data-key="{args.sample}"]')
        wait_report(page)
        n_recs = page.locator(".rec").count()
        badge = page.inner_text("#verdict-badge")
        first = page.locator(".rec >> nth=0")
        check(
            n_recs == 5
            and "expected" in first.inner_text().lower()
            and "not running yet" in first.inner_text()
            and "confidence" in badge
            and page.locator("#synthetic-note").is_visible()
            and page.locator("#drivers li").count() >= 1
            and page.locator("#today-best li").count() == 5
            and f"sample={args.sample}" in page.url,
            f"sample report: {n_recs} recommendations, {badge}, first: {first_name(page)}",
            problems,
        )
        page.screenshot(path=str(out / "03-report.png"))

        # 3. A recommendation's details and brief.
        first.locator(".rec-head").click()
        page.wait_for_selector(".rec.open .rec-body:not([hidden])")
        page.wait_for_selector(".rec.open .brief-headline", timeout=10_000)
        body = page.inner_text(".rec.open .rec-body")
        check(
            "Most similar markets" in body and "metro=" in page.url,
            f"recommendation details with brief: {page.inner_text('.rec.open .brief-headline')}",
            problems,
        )
        page.wait_for_timeout(800)  # map zoom
        page.screenshot(path=str(out / "04-recommendation.png"))
        check_a11y(page, "report", problems)
        more = page.locator("#more-recs")
        more.click()
        check(page.locator(".rec").count() > 5, "show more recommendations", problems)
        more.click()

        # 4. Exports.
        for fmt, header in (
            ("zips", ["zip", "country", "metro", "metro_rank"]),
            ("metros", ["rank", "metro", "cbsa_code"]),
        ):
            with page.expect_download() as dl:
                page.click(f'.actionbar button[data-format="{fmt}"]')
            rows = list(csv.reader(io.StringIO(Path(dl.value.path()).read_text())))
            ok = rows[0][: len(header)] == header and len(rows) > 1
            if fmt == "metros":
                ok = ok and rows[0][-1] == "brand_status"
            check(
                ok, f"{fmt} export: {dl.value.suggested_filename}, {len(rows) - 1} rows", problems
            )

        # 5. The shareable URL opens the same report and recommendation.
        url = page.url
        page2 = new_page()
        page2.goto(url, wait_until="networkidle")
        page2.wait_for_selector("#about[open]", timeout=10_000)  # a new browser context
        page2.click("#about button")
        wait_report(page2)
        page2.wait_for_selector(".rec.open", timeout=10_000)
        check(
            first_name(page2) == first_name(page),
            f"URL restore ({url.split('?', 1)[-1]})",
            problems,
        )

        # 6. The sample with no real relationship says so.
        page.click("#change-brand")
        page.wait_for_selector("#start:not([hidden])")
        page.click('.sample-card[data-key="beauty_wellness"]')
        wait_report(page)
        badge = page.inner_text("#verdict-badge")
        check(badge == "Low confidence", f"no-relationship sample: {badge!r}", problems)

        # 7. Upload an ad report, pick its category, adjust a signal.
        upload = new_page()
        upload.goto(args.base_url, wait_until="networkidle")
        upload.wait_for_selector("#about[open]", timeout=10_000)
        upload.click("#about button")
        lines = ["Market,Amount spent (USD),Purchases,Purchases conversion value"]
        for i, market in enumerate(UPLOAD_MARKETS):
            lines.append(f'"{market}",{1000 + 50 * i},{20 + i},{1500 + 120 * i}')
        lines.append('"Atlantis, ZZ",100,1,50')
        report: FilePayload = {
            "name": "report.csv",
            "mimeType": "text/csv",
            "buffer": ("\n".join(lines) + "\n").encode(),
        }
        upload.set_input_files("#results-file", files=[report])
        upload.wait_for_selector("#upload-step:not([hidden])")
        summary = upload.inner_text("#file-summary")
        upload.select_option("#category", "fitness")
        upload.click("#build-report")
        wait_report(upload)
        method = upload.text_content("#method-matched") or ""  # in a collapsed section
        check(
            "13 rows by market" in summary and "ROAS" in summary
            and f"{len(UPLOAD_MARKETS)} of {len(UPLOAD_MARKETS) + 1} rows" in method
            and "Atlantis" in method,
            f"uploaded report: {method[:70]}",
            problems,
        )  # fmt: skip
        upload.click(".adjust summary")
        n_before = upload.locator("#signal-chips li").count()
        upload.click("#signal-chips li >> nth=0 >> button")
        wait_report(upload)
        n_after = upload.locator("#signal-chips li").count()
        check(n_after == n_before - 1, f"adjust signals: {n_before} -> {n_after}", problems)

        # 7b. Connect a simulated ad account: live backfill, report, incremental sync.
        live = new_page()
        live.goto(args.base_url, wait_until="networkidle")
        live.wait_for_selector("#about[open]", timeout=10_000)
        live.click("#about button")
        live.select_option("#connect-sample", args.sample)
        live.click("#connect")
        live.wait_for_selector("#sync-status:text('Synced')", timeout=60_000)
        live.wait_for_selector("#report:not(.awaiting)", timeout=15_000)
        wait_report(live)
        log = live.text_content("#sync-log") or ""
        check(
            "Backfill" in log and "Page 1:" in log and "connection=" in live.url
            and live.locator(".rec").count() == 5,
            f"connector backfill: {log.count('Page ')} pages, {log.count('retrying')} retries",
            problems,
        )  # fmt: skip
        live.screenshot(path=str(out / "07-connector.png"))
        check_a11y(live, "connector", problems)
        live.click("#advance-1")
        live.wait_for_selector("#sync-log li.platform", state="attached")
        live.click("#sync-now")
        live.wait_for_selector("#sync-status:text('Syncing')", timeout=10_000)
        live.wait_for_selector("#sync-status:text('Synced')", timeout=60_000)
        changes = live.inner_text("#sync-changes")
        log = live.text_content("#sync-log") or ""
        check(
            "Incremental" in log and "updated" in log and "over the last 28 days" in changes,
            f"connector incremental sync: {changes.splitlines()[0][:70]}",
            problems,
        )
        live.click("#disconnect")
        live.wait_for_selector("#start:not([hidden])")

        # 8. Dark mode: toggle, accessible, and remembered across reloads.
        page.click("#theme-toggle")
        page.wait_for_timeout(1500)  # basemap swap
        theme = page.evaluate("document.documentElement.dataset.theme")
        check(theme == "dark", f"dark mode toggle: {theme}", problems)
        page.screenshot(path=str(out / "05-dark.png"))
        check_a11y(page, "dark mode", problems)
        page.reload(wait_until="networkidle")
        wait_report(page)
        pressed = page.get_attribute("#theme-toggle", "aria-pressed")
        check(pressed == "true", "dark mode kept after reload", problems)

        # 9. Phone layout.
        phone = new_page(390, 844, is_mobile=True)
        phone.goto(url, wait_until="networkidle")
        phone.wait_for_selector("#about[open]", timeout=10_000)  # a new browser context
        phone.click("#about button")
        wait_report(phone)
        phone.screenshot(path=str(out / "06-phone.png"), full_page=True)
        check_a11y(phone, "phone layout", problems)
        browser.close()

    print(f"screenshots: {out}/")
    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("all checks passed")


def wait_report(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#loading[hidden]", state="attached", timeout=15_000)
    page.wait_for_selector("#report:not([hidden])", timeout=15_000)


def first_name(page: Page) -> str:
    return page.inner_text(".rec >> nth=0 >> .name")


def check_a11y(page: Page, label: str, problems: list[str]) -> None:
    results = Axe().run(page)
    violations = results.response["violations"]
    for v in violations:
        targets = ", ".join(str(n["target"]) for n in v["nodes"][:3])
        print(f"     axe {v['impact']}: {v['id']}: {v['help']} ({targets})")
    check(not violations, f"accessibility ({label}): {len(violations)} axe violations", problems)


def check(ok: bool, label: str, problems: list[str]) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        problems.append(label)


if __name__ == "__main__":
    main()
