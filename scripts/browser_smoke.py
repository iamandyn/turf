"""End-to-end browser check of the Turf frontend (headless Chromium via Playwright).

Fails on any console error, page error, CSP violation, or accessibility
violation (axe-core), and walks the main flow: pick an audience preset, edit
its signals, add best-performing metros, read a metro's details and brief,
export both CSVs, reload from the shareable URL, change the population filter,
toggle map layers, switch to dark mode, and check the phone layout. Saves
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
from playwright.sync_api import ConsoleMessage, Page, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", default="data/screenshots", help="screenshot directory")
    parser.add_argument("--preset", default="pets")
    parser.add_argument("--seeds", default="Denver,Austin", help="text typed into the seed box")
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
        page.goto(args.base_url, wait_until="networkidle")
        page.wait_for_selector("#map canvas", timeout=15_000)
        page.wait_for_selector(".preset", timeout=10_000)
        n_presets = page.locator(".preset").count()
        check(
            page.locator("#empty-state").is_visible() and n_presets == 6,
            f"empty state with {n_presets} audience presets",
            problems,
        )
        page.screenshot(path=str(out / "01-empty.png"))

        # 1. A preset ranks metros by signal strength.
        page.click(f'.preset[data-key="{args.preset}"]')
        wait_results(page)
        n_signals = page.locator("#signal-chips li").count()
        check(
            count(page) == 25
            and n_signals > 0
            and "Strongest" in (page.text_content("#results-title") or ""),
            f"{args.preset} preset: {count(page)} metros, {n_signals} signals",
            problems,
        )
        top_strength = first_result(page)
        print(f"top by signal strength: {top_strength}")

        # 2. Editing the signals makes it a custom audience (in the URL).
        page.click("#signal-chips li >> nth=0 >> button")
        wait_results(page)
        check(
            "signal=" in page.url and "preset=" not in page.url,
            f"removed a signal: {page.locator('#signal-chips li').count()} left, custom audience",
            problems,
        )
        page.fill("#signal-input", "yoga")
        page.wait_for_selector("#signal-results li[aria-selected=true]", timeout=10_000)
        page.keyboard.press("Enter")
        wait_results(page)
        check("yoga" in page.inner_text("#signal-chips"), "added a signal by search", problems)
        page.click(f'.preset[data-key="{args.preset}"]')  # back to the preset
        wait_results(page)

        # 3. Best markets today: lookalikes.
        for name in args.seeds.split(","):
            page.fill("#seed-input", name)
            page.wait_for_selector("#seed-results li[aria-selected=true]", timeout=10_000)
            page.keyboard.press("Enter")
            wait_results(page)
        n_seeds = page.locator("#seed-chips li").count()
        first_row = page.inner_text("#results li >> nth=0")
        check(
            n_seeds == len(args.seeds.split(","))
            and "Closest to your best markets" in (page.text_content("#results-title") or "")
            and "% match" in first_row
            and page.url.count("seed=") == n_seeds,
            f"lookalikes of {args.seeds}: {first_result(page)}",
            problems,
        )

        # 4. A metro's details: signals vs. typical, and its brief or a note.
        page.click("#results li >> nth=0")
        page.wait_for_selector("#detail:not([hidden])")
        n_bars = page.locator("#detail-signals li").count()
        check(n_bars == n_signals, f"detail panel: {n_bars} signal bars", problems)
        # The demo path (pets, Denver + Austin) has pre-written briefs for its top lookalikes.
        page.wait_for_selector(
            "#detail-brief .brief-headline, #detail-brief .muted", timeout=10_000
        )
        headline = page.locator("#detail-brief .brief-headline")
        check(
            headline.count() == 1,
            f"creative brief: {headline.inner_text() if headline.count() else '(none)'}",
            problems,
        )
        page.wait_for_timeout(800)  # map zoom
        page.screenshot(path=str(out / "02-lookalikes.png"))
        check_a11y(page, "results page", problems)

        # 5. Exports.
        for fmt, header in (
            ("zips", ["zip", "country", "metro", "metro_rank"]),
            ("metros", ["rank", "metro", "cbsa_code"]),
        ):
            with page.expect_download() as dl:
                page.click(f'.export button[data-format="{fmt}"]')
            rows = list(csv.reader(io.StringIO(Path(dl.value.path()).read_text())))
            ok = rows[0][: len(header)] == header and len(rows) > 1
            if fmt == "metros":
                ok = ok and len(rows) - 1 == 25
            check(
                ok, f"{fmt} export: {dl.value.suggested_filename}, {len(rows) - 1} rows", problems
            )

        # 6. The shareable URL restores the same view, selected metro included.
        url = page.url
        page2 = new_page()
        page2.goto(url, wait_until="networkidle")
        wait_results(page2)
        page2.wait_for_selector("#detail:not([hidden])", timeout=10_000)
        check(
            first_result(page2) == first_result(page) and "metro=" in url,
            f"URL restore ({url.split('?', 1)[-1]})",
            problems,
        )

        # 7. A higher population floor compares fewer metros.
        page.select_option("#min-pop", "1000000")
        wait_results(page)
        detail = page.inner_text("#summary-detail")
        check("1M+" in detail, f"population filter: {detail.split(' · ')[-1]}", problems)

        # 8. Layer toggles.
        toggle = page.locator('.legend button[data-layer="seeds"]')
        toggle.click()
        check(toggle.get_attribute("aria-pressed") == "false", "legend hides a layer", problems)
        toggle.click()

        # 9. Dark mode: toggle, accessible, and remembered across reloads.
        page.click("#theme-toggle")
        page.wait_for_timeout(1500)  # basemap swap
        theme = page.evaluate("document.documentElement.dataset.theme")
        check(theme == "dark", f"dark mode toggle: {theme}", problems)
        page.screenshot(path=str(out / "03-dark.png"))
        check_a11y(page, "dark mode", problems)
        page.reload(wait_until="networkidle")
        wait_results(page)
        pressed = page.get_attribute("#theme-toggle", "aria-pressed")
        theme = page.evaluate("document.documentElement.dataset.theme")
        check(theme == "dark" and pressed == "true", "dark mode kept after reload", problems)

        # 10. Phone layout.
        phone = new_page(390, 844, is_mobile=True)
        phone.goto(url, wait_until="networkidle")
        wait_results(phone)
        phone.screenshot(path=str(out / "04-phone.png"), full_page=True)
        check_a11y(phone, "phone layout", problems)
        browser.close()

    print(f"screenshots: {out}/")
    if problems:
        print("\nFAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("all checks passed")


def wait_results(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#loading[hidden]", state="attached", timeout=15_000)
    page.wait_for_selector("#summary:not([hidden])", timeout=15_000)


def count(page: Page) -> int:
    return int(page.inner_text("#n-results").replace(",", ""))


def first_result(page: Page) -> str:
    return page.inner_text("#results li >> nth=0 >> .name")


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
