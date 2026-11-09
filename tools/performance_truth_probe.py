"""TEMPORARY read-only diagnostic (auto-repair 2026-11-09 17:40): acceptance + liq_v1 refit plans."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

MARKER = "RIVAL_PERFORMANCE_PROBE="

UA = """SELECT f.venue,f.contract,f.quote,f.horizon_minutes,
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

REFIT = """SELECT features, outcome_up FROM (
                 SELECT x.features, f.outcome_up, f.settled_at AS s_at,
                        x.issued_at AS i_at, x.venue AS v, x.contract AS c,
                        x.quote AS q
                 FROM rival_liq1_features x
                 JOIN perp_forward_audit f
                   ON f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
                  AND f.horizon_minutes=x.horizon_minutes
                  AND f.reference_opened_at=x.reference_opened_at
                 WHERE x.asset=? AND x.horizon_minutes=? AND x.learned=1
                   AND f.asset=? AND f.horizon_minutes=?
                   AND f.settled_at IS NOT NULL AND f.outcome_up IN (0,1)
                 ORDER BY f.settled_at DESC, x.issued_at DESC, x.venue DESC,
                          x.contract DESC, x.quote DESC
                 LIMIT ?) recent
               ORDER BY s_at, i_at, v, c, q"""


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore

    store = PostgresHotStore(Settings().rival_database_url.get_secret_value(), read_only=True)
    now = datetime.now(UTC)
    start = (now - timedelta(days=2)).isoformat()
    out: dict = {"now": now.isoformat()}

    def q(name, sql, args=()):
        t = time.monotonic()
        try:
            rows = conn.execute(sql, args).fetchall()
            out[name] = [list(dict(r).values()) for r in rows][:90]
        except Exception as exc:  # noqa: BLE001
            out[name] = f"{type(exc).__name__}: {str(exc)[:300]}"
            conn.execute("ROLLBACK")
        out[name + "_s"] = round(time.monotonic() - t, 2)

    try:
        with store.connect() as conn:
            for a in ("BTC", "SOL"):
                for h in (5, 60):
                    q(f"ua_{a}_{h}", "SELECT COUNT(*) FROM (" + UA + ") s",
                      (a, h, start, now.isoformat(), 200))
            q("explain_ua", "EXPLAIN (ANALYZE, BUFFERS) " + UA, ("BTC", 5, start, now.isoformat(), 200))
            q("shadow_families_2d", """SELECT family, COUNT(*), COUNT(settled_at)
                 FROM rival_model_shadow_forecasts WHERE venue='okx' AND issued_at>=?
                 GROUP BY family""", (start,))
            q("audit_btc5_2d", """SELECT COUNT(*), COUNT(settled_at) FROM perp_forward_audit
                 WHERE asset='BTC' AND horizon_minutes=5 AND issued_at>=?""", (start,))
            q("explain_refit", "EXPLAIN (ANALYZE, BUFFERS) " + REFIT, ("BTC", 5, "BTC", 5, 1000))
            q("liq_scope_counts", """SELECT asset,horizon_minutes,COUNT(*),SUM(learned)
                 FROM rival_liq1_features GROUP BY 1,2""")
            q("indexes", """SELECT tablename, indexdef FROM pg_indexes WHERE tablename IN
                 ('rival_liq1_features','rival_model_shadow_forecasts','perp_forward_audit',
                  'rival_family_quote_shadow')""")
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
