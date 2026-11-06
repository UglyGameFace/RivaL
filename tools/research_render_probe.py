import base64, gzip, json, os
MARKER = "RIVAL_RENDER_PROBE="
keys = ("RIVAL_ARCHIVE_ENABLED", "RIVAL_PERP_ARCHIVE_ENABLED", "RIVAL_ARCHIVE_AUTOSTART_WHEN_CONFIGURED")
out = {"env": {k: os.environ.get(k, "<unset>") for k in keys}}
try:
    from dotenv import dotenv_values  # noqa
except Exception:
    pass
for name in (".env",):
    try:
        lines = [l.split("=", 1)[0].strip() for l in open(name) if "=" in l]
        out["dotenv_has"] = {k: (k in lines) for k in keys}
        vals = {}
        for l in open(name):
            k, _, v = l.partition("=")
            if k.strip() in keys:
                vals[k.strip()] = v.strip()
        out["dotenv_values"] = vals
    except OSError:
        out["dotenv"] = "none"
try:
    from parlay_bot.config import Settings
    s = Settings()
    out["settings"] = {"archive_enabled": s.rival_archive_enabled, "perp": s.rival_perp_archive_enabled,
                       "autostart": s.rival_archive_autostart_when_configured,
                       "explicit": sorted(f for f in s.model_fields_set if "archive" in f)}
except Exception as exc:  # noqa: BLE001
    out["error"] = type(exc).__name__
print(MARKER + base64.b64encode(gzip.compress(json.dumps(out).encode())).decode())
