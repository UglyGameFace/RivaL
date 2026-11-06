import base64, gzip, json, os
MARKER = "RIVAL_RENDER_PROBE="
if os.environ.get("RIVAL_PROBE_ASSETS", "BTC") != "BTC":
    print(MARKER + base64.b64encode(gzip.compress(b"{}")).decode()); raise SystemExit(0)
out = {}
try:
    import httpx
    from parlay_bot.config import Settings
    tok = Settings().discord_token.get_secret_value()
    h = {"Authorization": f"Bot {tok}", "User-Agent": "RivaL (probe, 1)"}
    r = httpx.get("https://discord.com/api/v10/channels/1568264700247150684", headers=h, timeout=10)
    out["status"] = r.status_code
    if r.status_code == 200:
        d = r.json(); out["channel"] = {"type": d.get("type"), "name": d.get("name"), "guild_id": d.get("guild_id")}
        me = httpx.get("https://discord.com/api/v10/users/@me", headers=h, timeout=10).json()
        out["bot_id"] = me.get("id")
    else:
        out["body"] = r.text[:200]
except Exception as exc:  # noqa: BLE001
    out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out, default=str).encode())).decode())
