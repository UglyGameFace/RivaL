"""TEMPORARY read-only diagnostic (auto-repair 2026-11-09): acceptance old vs new query."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

MARKER = "RIVAL_PERFORMANCE_PROBE="

OLD = """SELECT f.venue,f.contract,f.quote,f.horizon_minutes,
          f.model_version,f.reference_opened_at,
          f.issued_at,f.target_opened_at,f.settled_at,
          f.outcome_up,f.model_brier AS incumbent_brier,
          s.family,s.training_sha256,s.probability_up,
          s.model_brier AS challenger_brier,
          s.incumbent_brier AS paired_incumbent_brier,
          s.settled_at AS challenger_settled_at,
          q.state AS quote_state,q.entry_at,q.exit_at,
          q.entry_bid,q.entry_ask,q.exit_bid,q.exit_ask
   FROM perp_forward_audit AS f
   JOIN rival_model_shadow_forecasts AS s
    ON s.venue=f.venue AND s.contract=f.contract
   AND s.quote=f.quote AND s.asset=f.asset
   AND s.horizon_minutes=f.horizon_minutes
   AND s.reference_opened_at=f.reference_opened_at
   AND s.issued_at=f.issued_at
   AND s.target_opened_at=f.target_opened_at
   LEFT JOIN rival_family_quote_shadow AS q
    ON q.venue=s.venue AND q.contract=s.contract
   AND q.quote=s.quote AND q.horizon_minutes=s.horizon_minutes
   AND q.family=s.family
   AND q.reference_opened_at=s.reference_opened_at
   WHERE f.asset=? AND f.horizon_minutes=?
     AND f.issued_at>=? AND f.issued_at<=?
     AND f.settled_at IS NOT NULL AND s.settled_at IS NOT NULL
   ORDER BY f.issued_at DESC LIMIT ?"""

NEW = """SELECT f.venue,f.contract,f.quote,f.horizon_minutes,
                              f.model_version,f.reference_opened_at,
                              f.issued_at,f.target_opened_at,f.settled_at,
                              f.outcome_up,f.model_brier AS incumbent_brier,
                              s.family,s.training_sha256,s.probability_up,
                              s.model_brier AS challenger_brier,
                              s.incumbent_brier AS paired_incumbent_brier,
                              s.settled_at AS challenger_settled_at,
                              q.state AS quote_state,q.entry_at,q.exit_at,
                              q.entry_bid,q.entry_ask,q.exit_bid,q.exit_ask
                       FROM (SELECT a.venue,a.contract,a.quote,a.asset,
                                    a.horizon_minutes,a.model_version,
                                    a.reference_opened_at,a.issued_at,
                                    a.target_opened_at,a.settled_at,
                                    a.outcome_up,a.model_brier
                             FROM perp_forward_audit AS a
                             WHERE a.asset=? AND a.horizon_minutes=?
                               AND a.issued_at>=? AND a.issued_at<=?
                               AND a.settled_at IS NOT NULL
                               AND EXISTS (
                                SELECT 1 FROM rival_model_shadow_forecasts AS p
                                WHERE p.venue=a.venue AND p.contract=a.contract
                                  AND p.quote=a.quote
                                  AND p.horizon_minutes=a.horizon_minutes
                                  AND p.reference_opened_at=a.reference_opened_at
                                  AND p.asset=a.asset AND p.issued_at=a.issued_at
                                  AND p.target_opened_at=a.target_opened_at
                                  AND p.settled_at IS NOT NULL)
                             ORDER BY a.issued_at DESC LIMIT ?) AS f
                       JOIN rival_model_shadow_forecasts AS s
                        ON s.venue=f.venue AND s.contract=f.contract
                       AND s.quote=f.quote AND s.asset=f.asset
                       AND s.horizon_minutes=f.horizon_minutes
                       AND s.reference_opened_at=f.reference_opened_at
                       AND s.issued_at=f.issued_at
                       AND s.target_opened_at=f.target_opened_at
                       LEFT JOIN rival_family_quote_shadow AS q
                        ON q.venue=s.venue AND q.contract=s.contract
                       AND q.quote=s.quote AND q.horizon_minutes=s.horizon_minutes
                       AND q.family=s.family
                       AND q.reference_opened_at=s.reference_opened_at
                       WHERE s.settled_at IS NOT NULL
                       ORDER BY f.issued_at DESC LIMIT ?"""


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore

    store = PostgresHotStore(Settings().rival_database_url.get_secret_value(), read_only=True)
    # Fixed end 30 min back so settlement in between cannot change the rows.
    stamp = datetime.now(UTC) - timedelta(minutes=30)
    start = (stamp - timedelta(days=2)).isoformat()
    out: dict = {"stamp": stamp.isoformat()}

    def digest(rows):
        edge = str(rows[-1]["issued_at"]) if rows else None
        keys = sorted(json.dumps([str(v) for v in dict(r).values()]) for r in rows
                      if str(r["issued_at"]) != edge)
        order = [str(r["issued_at"]) for r in rows]
        h = lambda x: hashlib.sha256(json.dumps(x).encode()).hexdigest()[:10]  # noqa: E731
        return f"rows={h(keys)} order={h(order)} edge_n={len(rows)-len(keys)}", len(rows)

    try:
        with store.connect() as conn:
            tot = {"old": 0.0, "new": 0.0}
            for a in ("BTC", "ETH", "SOL", "XRP"):
                for h in (5, 15, 60):
                    res = {}
                    for name, sql, args in (("old", OLD, (a, h, start, stamp.isoformat(), 200)),
                                            ("new", NEW, (a, h, start, stamp.isoformat(), 200, 200)),
                                            ("old2", OLD, (a, h, start, stamp.isoformat(), 200))):
                        t = time.monotonic()
                        rows = conn.execute(sql, args).fetchall()
                        dt = time.monotonic() - t
                        tot[name[:3]] = tot.get(name[:3], 0) + dt
                        res[name] = [*digest(rows), round(dt, 2)]
                    out[f"{a}_{h}"] = res
            out["total_s"] = {k: round(v, 2) for k, v in tot.items()}
            out["explain_skip"] = None
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
