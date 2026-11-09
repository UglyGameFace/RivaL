"""TEMPORARY read-only diagnostic (auto-repair 2026-11-09): liq_v1 pending-learn SELECT plan."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

MARKER = "RIVAL_PERFORMANCE_PROBE="

CURRENT = """SELECT x.venue,x.contract,x.quote,x.horizon_minutes,
          x.reference_opened_at,x.asset,x.features,x.raw_features,
          f.outcome_up,f.settled_at
   FROM rival_liq1_features x
   JOIN perp_forward_audit f
     ON f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
    AND f.horizon_minutes=x.horizon_minutes
    AND f.reference_opened_at=x.reference_opened_at
   WHERE x.learned=0 AND f.settled_at IS NOT NULL AND f.settled_at<=?
   ORDER BY f.settled_at, x.issued_at LIMIT 4000"""


NEW = CURRENT.replace(
    "AND f.reference_opened_at=x.reference_opened_at",
    "AND f.reference_opened_at=x.reference_opened_at\n    AND f.asset=x.asset AND f.issued_at=x.issued_at")


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore

    store = PostgresHotStore(Settings().rival_database_url.get_secret_value(), read_only=True)
    now = datetime.now(UTC).isoformat()
    out: dict = {"now": now}

    def q(name, sql, args=()):
        t = time.monotonic()
        try:
            rows = conn.execute(sql, args).fetchall()
            out[name] = [list(dict(r).values()) for r in rows][:80]
        except Exception as exc:  # noqa: BLE001
            out[name] = f"{type(exc).__name__}: {str(exc)[:300]}"
            conn.execute("ROLLBACK")
        out[name + "_s"] = round(time.monotonic() - t, 2)

    try:
        with store.connect() as conn:
            q("issued_match", """SELECT COUNT(*) FILTER (WHERE f.issued_at=x.issued_at) AS same_issued,
                    COUNT(*) FILTER (WHERE f.issued_at<>x.issued_at) AS diff_issued,
                    COUNT(*) FILTER (WHERE f.asset<>x.asset) AS diff_asset,
                    COUNT(*) FILTER (WHERE f.model_version<>'prospective-logistic-persistent-v3') AS other_model,
                    COUNT(*) AS total
                 FROM rival_liq1_features x JOIN perp_forward_audit f
                   ON f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
                  AND f.horizon_minutes=x.horizon_minutes
                  AND f.reference_opened_at=x.reference_opened_at""")
            q("orphans", """SELECT COUNT(*) AS n FROM rival_liq1_features x WHERE NOT EXISTS (
                   SELECT 1 FROM perp_forward_audit f WHERE f.asset=x.asset
                    AND f.horizon_minutes=x.horizon_minutes AND f.issued_at=x.issued_at
                    AND f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
                    AND f.reference_opened_at=x.reference_opened_at)""")
            q("explain_current", "EXPLAIN (ANALYZE, BUFFERS) " + CURRENT, (now,))
            q("explain_new", "EXPLAIN (ANALYZE, BUFFERS) " + NEW, (now,))
            q("rows_current", "SELECT md5(string_agg(venue||contract||quote||horizon_minutes||reference_opened_at||coalesce(outcome_up::text,'n')||settled_at, ',' ORDER BY venue||contract||quote||horizon_minutes||reference_opened_at||coalesce(outcome_up::text,'n')||settled_at)), COUNT(*) FROM (" + CURRENT.replace("4000", "4000").replace("x.learned=0", "x.learned=1").replace("f.settled_at<=?", "f.settled_at<=? AND x.issued_at>'2026-11-09T06'") + ") s", (now,))
            q("rows_new", "SELECT md5(string_agg(venue||contract||quote||horizon_minutes||reference_opened_at||coalesce(outcome_up::text,'n')||settled_at, ',' ORDER BY venue||contract||quote||horizon_minutes||reference_opened_at||coalesce(outcome_up::text,'n')||settled_at)), COUNT(*) FROM (" + NEW.replace("x.learned=0", "x.learned=1").replace("f.settled_at<=?", "f.settled_at<=? AND x.issued_at>'2026-11-09T06'") + ") s", (now,))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        store.close()
    return out


def main() -> int:
    try:
        report = probe()
    except Exception as exc:  # noqa: BLE001
        report = {"error": type(exc).__name__}
    print(MARKER + json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
