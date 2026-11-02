"""Inspect Bitget's public futures download dialogs without retrieving archives.

Network telemetry intentionally stores only route/host/query-key metadata.
It never records cookies, headers, downloaded bytes, or credentialed URLs.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

PORTAL = "https://www.bitget.com/data-download"
FAMILIES = ("candles", "trades", "depth")


def sanitized_url(url: str) -> dict:
    parsed = urlsplit(url)
    return {"host": parsed.hostname or "", "path": parsed.path,
            "query_keys": sorted({key for key, _ in parse_qsl(parsed.query)})}


def inspect(*, output: Path) -> dict:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    output.mkdir(parents=True, exist_ok=True)
    evidence = {"schema": "rival-bitget-portal-browser-discovery-v1",
                "observed_at": datetime.now(UTC).isoformat(),
                "portal": PORTAL, "futures_downloads_verified": False,
                "original_archives_downloaded": 0,
                "source_requests": [], "dialogs": [], "errors": []}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(accept_downloads=False, viewport={
            "width": 1365, "height": 900,
        }, locale="en-US")
        page = context.new_page()

        def record(req):
            parsed = urlsplit(req.url)
            host = parsed.hostname or ""
            if host.endswith("bitget.com") and (
                "download" in parsed.path.lower()
                or "history" in parsed.path.lower()
                or "export" in parsed.path.lower()
            ):
                item = {"method": req.method, **sanitized_url(req.url)}
                if item not in evidence["source_requests"]:
                    evidence["source_requests"].append(item)

        page.on("request", record)
        page.on("download", lambda obj: obj.cancel())
        try:
            response = page.goto(PORTAL, wait_until="domcontentloaded", timeout=40000)
            evidence["http_status"] = response.status if response else None
            page.wait_for_timeout(7000)
            evidence["page_title"] = page.title()
            evidence["initial_visible_text"] = page.locator("body").inner_text(timeout=8000)[-6000:]
            buttons = page.get_by_text("Download", exact=True)
            count = buttons.count()
            evidence["download_control_count"] = count
            page.screenshot(path=str(output / "bitget-landing.png"), full_page=False)
            initial_text = evidence["initial_visible_text"]
            restricted = (
                "Restricted IP" in initial_text
                and "not intended for users in United States" in initial_text
            )
            evidence["geographic_restriction_visible"] = restricted
            evidence["source_access_status"] = (
                "restricted_no_eligible_attestation" if restricted
                else "public_dialog_probe"
            )
            evidence["futures_control_markup"] = [
                buttons.nth(i).evaluate("(el) => el.outerHTML.slice(0, 1500)")
                for i in range(3, min(count, 6))
            ]
            if restricted:
                evidence["errors"].append("US region restricted; do not accept false citizenship declaration")
            elif count < 6:
                evidence["errors"].append("did not find six public spot/futures download controls")
            else:
                # Public site shows three spot controls followed by three futures controls.
                for offset, family in enumerate(FAMILIES):
                    try:
                        page.get_by_text("Download", exact=True).nth(3 + offset).click(
                            timeout=10000)
                        page.wait_for_timeout(3500)
                        evidence["dialogs"].append({
                            "family": family, "url": sanitized_url(page.url),
                            "visible_text": page.locator("body").inner_text(timeout=8000)[-7000:],
                            "selects": page.locator("select").count(),
                            "inputs": page.locator("input").count(),
                        })
                        page.screenshot(path=str(output / f"bitget-{family}.png"))
                        page.goto(PORTAL, wait_until="domcontentloaded", timeout=40000)
                        page.wait_for_timeout(2500)
                    except (PlaywrightError, TimeoutError, OSError, ValueError) as exc:
                        evidence["errors"].append(f"{family}: {type(exc).__name__}")
            evidence["futures_downloads_verified"] = bool(evidence["dialogs"]) and not restricted
        except (PlaywrightError, TimeoutError, OSError, ValueError) as exc:
            evidence["errors"].append(f"navigation: {type(exc).__name__}")
        finally:
            browser.close()
    report = output / "bitget-browser-source-evidence.json"
    report.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "public_page_text": evidence.get("initial_visible_text", "")[-1600:],
        "geographic_restriction_visible": evidence.get("geographic_restriction_visible", False),
        "futures_markup": evidence.get("futures_control_markup", []),
        "dialog_excerpts": [{"family": x["family"], "text": x["visible_text"][-2400:]} for x in evidence["dialogs"]],
        "status": "public_dialog_inspection_only",
        "controls": evidence.get("download_control_count", 0),
        "dialogs": [{"family": x["family"], "url": x["url"]}
                    for x in evidence["dialogs"]],
        "observed_routes": evidence["source_requests"][:40],
        "errors": evidence["errors"],
        "archive_files": 0,
    }, sort_keys=True))
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(
        "data/native-reports/bitget-public-browser"))
    opts = parser.parse_args()
    inspect(output=opts.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
