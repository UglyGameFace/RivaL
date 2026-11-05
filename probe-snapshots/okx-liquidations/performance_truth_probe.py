"""Read-only probe: sample OKX's public liquidation stream for 60 s on the host."""
import asyncio
import json

import websockets


async def main():
    seen, samples = 0, []
    sub = {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}
    try:
        async with websockets.connect("wss://ws.okx.com:8443/ws/v5/public",
                                      open_timeout=10) as ws:
            await ws.send(json.dumps(sub))
            loop = asyncio.get_event_loop()
            end = loop.time() + 60
            while loop.time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=end - loop.time())
                except TimeoutError:
                    break
                seen += 1
                data = json.loads(msg)
                rows = data.get("data") or []
                if any(str(r.get("instId", "")).split("-")[0] in ("BTC", "ETH", "SOL", "XRP")
                       for r in rows) or len(samples) < 2:
                    samples.append(data)
                if len(samples) >= 6:
                    break
        error = None
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    print("RIVAL_PERFORMANCE_PROBE=" + json.dumps(
        {"error": error, "messages": seen, "samples": [json.dumps(s)[:900] for s in samples]}))

asyncio.run(main())
