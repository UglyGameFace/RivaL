"""Read-only: what does the live board read for each coin/timeframe right now?"""
import asyncio, base64, gzip, json, time
from datetime import UTC, datetime
MARKER = "RIVAL_RENDER_PROBE="

async def main():
    from parlay_bot.discord_app.polymarket_watch import PolymarketReader
    reader = PolymarketReader()
    out = []
    try:
        for round_ in range(4):
            for asset in ("BTC", "ETH"):
                for horizon in (5, 15, 60):
                    t = time.monotonic()
                    try:
                        b = await asyncio.wait_for(reader.window_board(asset, horizon), 12)
                        q = b.current_quote
                        out.append([round_, asset, horizon, round(time.monotonic()-t, 1),
                                    b.current is not None, q is not None, b.quote_unavailable_reason,
                                    (round(q.up_ask, 3), round(q.down_ask, 3)) if q else None,
                                    str(b.current.closes)[11:19] if b.current else None,
                                    b.next_window is not None])
                    except Exception as exc:  # noqa: BLE001
                        out.append([round_, asset, horizon, "ERR", type(exc).__name__, str(exc)[:60]])
            await asyncio.sleep(8)
    finally:
        await reader.aclose()
    return out

try:
    report = {"rows": asyncio.run(main()), "at": datetime.now(UTC).isoformat()}
except Exception as exc:  # noqa: BLE001
    report = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
print(MARKER + base64.b64encode(gzip.compress(json.dumps(report, default=str).encode())).decode())
