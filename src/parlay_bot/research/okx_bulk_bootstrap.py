"""Paced high-throughput public OKX history, kept separate from RivaL trading.

One runner owns the shared OKX IP budget across BTC/ETH/SOL/XRP. Each asset
advances through its own source-native months with existing immutable SHA-256
private Drive readback. No retrospective record is a forward prediction.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import httpx

from parlay_bot.config import Settings
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.official_okx_bootstrap import ASSETS, bootstrap
from parlay_bot.research.publish import _drive

# Historical candles: 20 requests / 2 seconds / public IP. 0.16 seconds
# allocates 12.5 requests / 2 seconds across ALL assets on one runner.
MIN_SPACING = 0.16
MAX_RETRIES = 3


class SharedOkxPacer:
    def __init__(self, *, spacing: float = MIN_SPACING) -> None:
        if not math.isfinite(spacing) or not 0.16 <= spacing <= 5:
            raise ValueError("invalid conservative OKX IP pacing")
        self.spacing = spacing
        self._lock = asyncio.Lock()
        self._next = 0.0
        self.requests = 0
        self.throttles = 0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            due = max(loop.time(), self._next)
            self._next = due + self.spacing
            await asyncio.sleep(max(0, due - loop.time()))
            self.requests += 1

    async def cooldown(self, delay: float) -> None:
        async with self._lock:
            self.throttles += 1
            self._next = max(self._next, asyncio.get_running_loop().time() + delay)


class PacedOkxClient:
    """Compatible with the official bootstrap's small AsyncClient surface."""

    def __init__(self, raw: httpx.AsyncClient, pacer: SharedOkxPacer) -> None:
        self.raw = raw
        self.pacer = pacer

    async def get(self, url: str, **kwargs) -> httpx.Response:
        for attempt in range(MAX_RETRIES):
            await self.pacer.wait()
            try:
                response = await self.raw.get(url, **kwargs)
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == MAX_RETRIES - 1:
                    raise
                await self.pacer.cooldown(2 ** (attempt + 2))
                continue
            rate_code = False
            if response.status_code == 200:
                try:
                    obj = response.json()
                    rate_code = isinstance(obj, dict) and obj.get("code") == "50011"
                except (UnicodeError, ValueError):
                    pass
            throttled = response.status_code in (429, 503) or rate_code
            transient = throttled or response.status_code in (500, 502, 504)
            if not transient or attempt == MAX_RETRIES - 1:
                return response
            # Avoid untrusted arbitrary server sleep directives.
            retry_after = response.headers.get("retry-after", "")
            delay = float(retry_after) if retry_after.isascii() and retry_after.isdecimal() else 0
            await self.pacer.cooldown(min(60, max(delay, 2 ** (attempt + 2))))
        raise AssertionError("unreachable")


async def run_bulk(
    *, assets: tuple[str, ...] = ASSETS, passes: int = 3,
    months_per_pass: int = 6,
    root: Path = Path("data/native-research"),
    report_dir: Path = Path("data/native-reports"),
    spacing: float = MIN_SPACING,
) -> dict:
    if (
        not assets or len(set(assets)) != len(assets)
        or any(asset not in ASSETS for asset in assets)
        or not 1 <= passes <= 3 or not 1 <= months_per_pass <= 6
    ):
        raise ValueError("invalid bounded multi-asset OKX backfill plan")
    pacer = SharedOkxPacer(spacing=spacing)
    report_dir.mkdir(parents=True, exist_ok=True)

    async def worker(asset: str) -> dict:
        outcomes = []
        drive = _drive(Settings())
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=False) as raw:
                client = PacedOkxClient(raw, pacer)
                for _ in range(passes):
                    try:
                        outcome = await bootstrap(
                            drive=drive, client=client, asset=asset,
                            root=root / asset, report_dir=report_dir / asset,
                            max_new_months=months_per_pass, download_delay=0,
                        )
                    except (HistoryImportError, httpx.HTTPError,
                            OSError, RuntimeError, ValueError) as exc:
                        outcomes.append({
                            "status": "blocked", "asset": asset, "error": str(exc)[:300],
                        })
                        break
                    outcomes.append(outcome)
                    if outcome["full_2023_to_present_verified"]:
                        break
                    if not outcome["newly_published_months"]:
                        break
            return {"asset": asset, "passes": outcomes}
        finally:
            await drive.aclose()

    results = await asyncio.gather(*(worker(asset) for asset in assets))
    failures = [
        {"asset": item["asset"], "error": result["error"]}
        for item in results for result in item["passes"]
        if result.get("status") == "blocked"
    ]
    counts = {item["asset"]: sum(
        len(result.get("newly_published_months", ()))
        for result in item["passes"]
    ) for item in results}
    receipt = {
        "schema_version": "rival-okx-bulk-history-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "assets": list(assets), "monthly_batch_max": months_per_pass,
        "passes_budget": passes, "new_months_by_asset": counts,
        "new_months_total": sum(counts.values()),
        "requests": pacer.requests, "throttles": pacer.throttles,
        "failures": failures, "results": results,
        "complete": not failures and all(
            item["passes"][-1].get("full_2023_to_present_verified") is True
            for item in results
        ),
        "historical_only": True, "live_model_promoted": False,
        "forward_model_improvement_verified": False,
        "after_cost_profitability_verified": False,
    }
    (report_dir / "okx-bulk-progress.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OKX multi-asset private history backfill")
    parser.add_argument("--asset", choices=(*ASSETS, "all"), default="all")
    parser.add_argument("--passes", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--months-per-pass", type=int,
                        choices=tuple(range(1, 7)), default=6)
    parser.add_argument("--root", type=Path, default=Path("data/native-research"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_bulk(
            assets=ASSETS if args.asset == "all" else (args.asset,),
            passes=args.passes, months_per_pass=args.months_per_pass,
            root=args.root, report_dir=args.report_dir,
        ))
    except (HistoryImportError, httpx.HTTPError, OSError, RuntimeError,
            ValueError) as exc:
        parser.exit(2, f"OKX bounded backfill UNVERIFIED: {exc}\n")
    print(json.dumps({
        "new_months_by_asset": report["new_months_by_asset"],
        "requests": report["requests"], "throttles": report["throttles"],
        "failures": report["failures"], "complete": report["complete"],
        "live_model_promoted": False,
    }, sort_keys=True))
    return 2 if report["failures"] or report["throttles"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
