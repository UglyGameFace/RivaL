"""Read-only production probe: are the Bot Performance numbers true?

Runs on the Discloud host through the console API (see the workflow
performance-truth-audit.yml). Prints one JSON line of aggregate counts and
timings. No writes, no secrets, no row contents.
"""
from __future__ import annotations

import itertools
import json
import os
import time
from datetime import UTC, datetime, timedelta

MARKER = "RIVAL_PERFORMANCE_PROBE="


def _one(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return {k: (v if isinstance(v, (int, float, str)) or v is None else str(v))
            for k, v in dict(row).items()} if row else None


def _timed(fn):
    start = time.monotonic()
    try:
        result = fn()
        error = None
    except Exception as exc:  # noqa: BLE001
        result, error = None, type(exc).__name__
    return round(time.monotonic() - start, 2), result, error


def read_only_forward_monitor(store):
    """Existing-table monitor: no runtime schema initialization/correction."""
    from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

    return ForwardAccuracyMonitor(PerpetualMarketStore(store), read_only=True)


def probe() -> dict:
    from parlay_bot.config import Settings
    from parlay_bot.perpetuals.forward_audit import MODEL
    from parlay_bot.storage.hot import PostgresHotStore

    settings = Settings()
    store = PostgresHotStore(settings.rival_database_url.get_secret_value(),
                             read_only=True)
    now = datetime.now(UTC)
    day, week = (now - timedelta(days=1)).isoformat(), (now - timedelta(days=7)).isoformat()
    out: dict = {"now": now.isoformat(), "model": MODEL}
    try:
        with store.connect() as conn:
            out["cycle_24h"] = _one(conn, """
                SELECT COUNT(*) AS cycles, MIN(checked_at) AS first, MAX(checked_at) AS last,
                       SUM(issued_forecasts) AS issued, SUM(settled_forecasts) AS settled,
                       SUM(target_expired) AS expired, SUM(duplicate_forecasts) AS duplicates,
                       SUM(stale_last_candle) AS stale, SUM(short_history) AS short_history,
                       SUM(insufficient_training) AS insufficient_training,
                       SUM(invalid_data) AS invalid, MAX(selected_streams) AS streams
                FROM perp_forward_cycle_diagnostics WHERE checked_at>=?""", (day,))
            stamps = [r["checked_at"] for r in conn.execute(
                "SELECT checked_at FROM perp_forward_cycle_diagnostics "
                "WHERE checked_at>=? ORDER BY checked_at", (day,)).fetchall()]
            gaps = []
            for a, b in itertools.pairwise(stamps):
                gaps.append((datetime.fromisoformat(str(b)) -
                             datetime.fromisoformat(str(a))).total_seconds() / 60)
            if gaps:
                gaps.sort()
                out["cycle_gap_minutes"] = {
                    "median": round(gaps[len(gaps) // 2], 1), "max": round(gaps[-1], 1),
                    "over_6": sum(g > 6 for g in gaps), "over_11": sum(g > 11 for g in gaps),
                }
            def _secs(a, b):
                return (datetime.fromisoformat(str(b)) - datetime.fromisoformat(str(a))).total_seconds()

            def _dist(values):
                values = sorted(values)
                if not values:
                    return None
                return {"n": len(values), "median": round(values[len(values) // 2]),
                        "p90": round(values[int(len(values) * .9)]), "max": round(values[-1])}
            timing = conn.execute(
                "SELECT slot_utc, started_at, completed_at FROM perp_learning_cycle_health "
                "WHERE completed_at>=? ORDER BY completed_at",
                (os.environ.get("RIVAL_PROBE_SINCE") or day,)).fetchall()
            out["stage_seconds"] = {
                "slot_to_forecast_start": _dist([_secs(r["slot_utc"], r["started_at"]) for r in timing]),
                "forecast_stage": _dist([_secs(r["started_at"], r["completed_at"]) for r in timing]),
                "slot_to_done": _dist([_secs(r["slot_utc"], r["completed_at"]) for r in timing]),
            }
            diag = conn.execute(
                "SELECT COUNT(*) AS cycles, COALESCE(SUM(target_expired),0) AS expired, "
                "COALESCE(SUM(issued_forecasts),0) AS issued FROM perp_forward_cycle_diagnostics "
                "WHERE checked_at>=?", (os.environ.get("RIVAL_PROBE_SINCE") or day,)).fetchone()
            out["cycles_since"] = {k: int(diag[k]) for k in ("cycles", "expired", "issued")}
            out["learning_24h"] = _one(conn, """
                SELECT COUNT(*) AS cycles, SUM(math_issued) AS issued,
                       SUM(math_settled) AS settled,
                       SUM(CASE WHEN math_ok=0 THEN 1 ELSE 0 END) AS math_failed,
                       SUM(CASE WHEN model_review_ok=0 THEN 1 ELSE 0 END) AS review_failed,
                       MAX(completed_at) AS last
                FROM perp_learning_cycle_health WHERE completed_at>=?""", (day,))
            horizons = {}
            for h in (5, 15, 60):
                horizons[h] = _one(conn, """
                    SELECT COUNT(*) AS issued, COUNT(DISTINCT target_opened_at) AS windows,
                           COUNT(DISTINCT venue) AS venues, MIN(issued_at) AS first_issued,
                           SUM(CASE WHEN settled_at IS NULL AND target_opened_at<=? THEN 1 ELSE 0 END) AS overdue,
                           SUM(CASE WHEN settled_at IS NULL THEN 1 ELSE 0 END) AS unsettled
                    FROM perp_forward_audit
                    WHERE asset='BTC' AND model_version=? AND horizon_minutes=? AND issued_at>=?""",
                    ((now - timedelta(minutes=65)).isoformat(), MODEL, h, week))
            out["btc_7d"] = horizons
            out["btc_issued_by_hour_24h"] = [
                [str(r["hr"]), int(r["n"]), int(r["w"])] for r in conn.execute("""
                    SELECT SUBSTR(issued_at,1,13) AS hr, COUNT(*) AS n,
                           COUNT(DISTINCT target_opened_at) AS w
                    FROM perp_forward_audit
                    WHERE asset='BTC' AND horizon_minutes=5 AND model_version=? AND issued_at>=?
                    GROUP BY SUBSTR(issued_at,1,13) ORDER BY hr""", (MODEL, day)).fetchall()]
            out["models_7d"] = [
                [str(r["model_version"]), int(r["n"])] for r in conn.execute(
                    "SELECT model_version, COUNT(*) AS n FROM perp_forward_audit "
                    "WHERE issued_at>=? GROUP BY model_version", (week,)).fetchall()]
            out["issue_delay_seconds"] = {}
            since = os.environ.get("RIVAL_PROBE_SINCE") or day
            out["issue_delay_since"] = since
            for h in (5, 15, 60):
                rows = conn.execute(
                    "SELECT issued_at, target_opened_at FROM perp_forward_audit "
                    "WHERE model_version=? AND horizon_minutes=? "
                    "AND issued_at>=? ORDER BY issued_at DESC LIMIT 5000",
                    (MODEL, h, since)).fetchall()
                delays = sorted(
                    (datetime.fromisoformat(str(r["issued_at"]))
                     - datetime.fromisoformat(str(r["target_opened_at"]))).total_seconds()
                    for r in rows)
                if delays:
                    out["issue_delay_seconds"][h] = {
                        "n": len(delays), "min": round(delays[0]),
                        "median": round(delays[len(delays) // 2]),
                        "p95": round(delays[int(len(delays) * .95)]), "max": round(delays[-1]),
                        "after_target_open": sum(d > 0 for d in delays)}
        with store.connect() as conn:
          try:
            out["offline_candidates"] = [
                {k: (v if isinstance(v, (int, float, str)) or v is None else str(v))
                 for k, v in dict(r).items()}
                for r in conn.execute("""
                    SELECT c.asset, c.horizon_minutes AS h,
                           COALESCE(c.admission_reason,'none') AS reason,
                           COUNT(*) AS n, MIN(c.source_months) AS min_months,
                           MAX(c.source_months) AS max_months,
                           MIN(c.holdout_brier) AS best_holdout_brier,
                           MAX(c.holdout_accuracy) AS best_holdout_acc,
                           MIN(SUBSTR(c.first_imported_at,1,10)) AS first_day,
                           MAX(SUBSTR(c.first_imported_at,1,10)) AS last_day,
                           COUNT(DISTINCT c.data_version) AS data_versions
                    FROM rival_offline_candidates c
                    GROUP BY c.asset, c.horizon_minutes, COALESCE(c.admission_reason,'none')
                    ORDER BY c.asset, c.horizon_minutes""").fetchall()]
          except Exception as exc:  # noqa: BLE001
            out["offline_candidates_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        with store.connect() as conn:
          try:
            spec = conn.execute(
                "SELECT model_spec_json FROM rival_offline_model_artifacts "
                "WHERE model_spec_json LIKE '%portable-boosted%' LIMIT 1").fetchone()
            if spec:
                parsed = json.loads(spec["model_spec_json"])
                out["spec_keys"] = sorted(parsed)[:40]
                for k in ("validation", "holdout", "metrics", "evaluation"):
                    if isinstance(parsed.get(k), dict):
                        out[f"spec_{k}"] = {kk: vv for kk, vv in parsed[k].items()
                                            if isinstance(vv, (int, float, str))}
          except Exception as exc:  # noqa: BLE001
            out["spec_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        with store.connect() as conn:
          try:
            out["labels_by_venue"] = [
                [str(r["asset"]), int(r["h"]), str(r["venue"]), int(r["n"]), int(r["ups"]),
                 int(r["flats"]), round(float(r["abs_bps"] or 0), 2)]
                for r in conn.execute("""
                    SELECT asset, horizon_minutes AS h, venue, COUNT(*) AS n,
                           SUM(outcome_up) AS ups,
                           SUM(CASE WHEN realized_close=reference_close THEN 1 ELSE 0 END) AS flats,
                           AVG(ABS(realized_close/reference_close-1)*10000) AS abs_bps
                    FROM perp_forward_audit
                    WHERE model_version=? AND settled_at IS NOT NULL AND issued_at>=?
                      AND horizon_minutes IN (5,15)
                    GROUP BY asset, horizon_minutes, venue
                    ORDER BY asset, horizon_minutes, venue""", (MODEL, week)).fetchall()]
          except Exception as exc:  # noqa: BLE001
            out["labels_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        with store.connect() as conn:
          try:
            out["candle_trades"] = [
                [str(r["venue"]), str(r["contract"]), int(r["n"]), int(r["zero_trades"]),
                 int(r["unknown_trades"]), int(r["flat"]), int(r["flat_zero"])]
                for r in conn.execute("""
                    SELECT venue, contract, COUNT(*) AS n,
                           SUM(CASE WHEN trades=0 THEN 1 ELSE 0 END) AS zero_trades,
                           SUM(CASE WHEN trades<0 THEN 1 ELSE 0 END) AS unknown_trades,
                           SUM(CASE WHEN open_price=close_price THEN 1 ELSE 0 END) AS flat,
                           SUM(CASE WHEN open_price=close_price AND trades=0 THEN 1 ELSE 0 END)
                               AS flat_zero
                    FROM perp_market_candles
                    WHERE interval_minutes=5 AND opened_at>=? AND venue IN ('dydx','hyperliquid','okx')
                    GROUP BY venue, contract ORDER BY venue, contract""", (day,)).fetchall()]
          except Exception as exc:  # noqa: BLE001
            out["candle_trades_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        with store.connect() as conn:
          try:
            out["recovery"] = [
                {k: (v if isinstance(v, (int, float, str)) or v is None else str(v))
                 for k, v in dict(r).items()}
                for r in conn.execute(
                    "SELECT venue,asset,started_from,next_opened_at,attempted_at,"
                    "inserted_bars,empty_windows,status,retry_after "
                    "FROM perp_history_recovery ORDER BY venue,asset").fetchall()]
          except Exception as exc:  # noqa: BLE001
            out["recovery_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        with store.connect() as conn:
          try:
            out["polymarket_horizons"] = [
                [str(r["asset"]), int(r["horizon"]), int(r["n"])] for r in conn.execute(
                    "SELECT asset, horizon, COUNT(*) AS n FROM rival_polymarket_observations "
                    "GROUP BY asset, horizon ORDER BY asset, horizon").fetchall()]
          except Exception as exc:  # noqa: BLE001
            out["polymarket_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        with store.connect() as conn:
            out["lineage_columns"] = [str(r["column_name"]) for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='perp_forward_audit'").fetchall()]
        monitor = read_only_forward_monitor(store)
        for name, fn in (
            ("summary", lambda: monitor.summary(asset="BTC")["last_7d"][5]),
            ("benefit", lambda: monitor.active_learning_evidence(asset="BTC", horizon_minutes=5)),
            ("lineage", lambda: monitor.learning_lineage_summary(asset="BTC", horizon_minutes=5)),
        ):
            seconds, result, error = _timed(fn)
            if isinstance(result, dict):
                result = {k: v for k, v in result.items()
                          if isinstance(v, (int, float, str, bool)) or v is None}
            out[name] = {"seconds": seconds, "error": error, "result": result}
        # The live Discord page gives each reader 4 s; run benefit twice to
        # separate a cold first query from a consistently slow one.
        out["benefit_second_run_seconds"] = _timed(
            lambda: monitor.active_learning_evidence(asset="BTC", horizon_minutes=5))[0]
        try:
            with store.connect() as conn:
                out["polymarket_hourly"] = [
                    {k: (v if isinstance(v, (int, float, str)) or v is None else str(v))
                     for k, v in dict(r).items()}
                    for r in conn.execute("""
                        SELECT v.horizon AS h, COUNT(*) AS observed,
                               SUM(CASE WHEN v.expires_at<? THEN 1 ELSE 0 END) AS expired,
                               SUM(CASE WHEN o.slug IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                               SUM(CASE WHEN a.slug IS NOT NULL THEN 1 ELSE 0 END) AS attempted,
                               SUM(CASE WHEN v.observed_at<v.expires_at THEN 1 ELSE 0 END)
                                   AS observed_before_expiry,
                               MIN(v.slug) AS sample_slug, MAX(v.resolution_source) AS source
                        FROM rival_polymarket_observations v
                        LEFT JOIN rival_polymarket_ai_outcomes o ON o.slug=v.slug
                        LEFT JOIN rival_polymarket_resolution_attempts a ON a.slug=v.slug
                        WHERE v.asset='BTC' GROUP BY v.horizon""",
                        (datetime.now(UTC).isoformat(),)).fetchall()]
        except Exception as exc:  # noqa: BLE001
            out["polymarket_hourly_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        try:
            import httpx as _httpx
            with store.connect() as conn:
                hourly = conn.execute(
                    "SELECT slug, condition_id, expires_at FROM rival_polymarket_observations "
                    "WHERE horizon=60 AND expires_at<? ORDER BY expires_at DESC LIMIT 1",
                    (datetime.now(UTC).isoformat(),)).fetchone()
            with store.connect() as conn:
                many = conn.execute(
                    "SELECT slug, asset, condition_id, expires_at FROM rival_polymarket_observations "
                    "WHERE horizon=60 AND expires_at<? ORDER BY expires_at DESC LIMIT 12",
                    (datetime.now(UTC).isoformat(),)).fetchall()
            checks = []
            for row in many:
                body = _httpx.get("https://data-api.polymarket.com/v2/resolutions",
                                  params={"condition": row["condition_id"]}, timeout=20).json()
                item = (body.get("data") or [{}])[0]
                end = datetime.fromisoformat(str(row["expires_at"]))
                start = end - timedelta(hours=1)
                with store.connect() as conn:
                    c0 = conn.execute(
                        "SELECT open_price FROM perp_market_candles WHERE venue='okx' "
                        "AND contract=? AND interval_minutes=5 AND opened_at=?",
                        (f"{row['asset']}-USDT-SWAP", start.isoformat())).fetchone()
                    c1 = conn.execute(
                        "SELECT close_price FROM perp_market_candles WHERE venue='okx' "
                        "AND contract=? AND interval_minutes=5 AND opened_at=?",
                        (f"{row['asset']}-USDT-SWAP", (end - timedelta(minutes=5)).isoformat()),
                    ).fetchone()
                move = (float(c1["close_price"]) / float(c0["open_price"]) - 1) * 10000                     if c0 and c1 else None
                checks.append({"slug": row["slug"][-28:], "status": item.get("status"),
                               "price": item.get("price"), "proposed": item.get("proposed_price"),
                               "disputed": item.get("was_disputed"),
                               "okx_move_bps": round(move, 1) if move is not None else None})
            out["polymarket_hourly_checks"] = checks
            if hourly:
                resp = _httpx.get("https://data-api.polymarket.com/v2/resolutions",
                                  params={"condition": hourly["condition_id"]}, timeout=20)
                out["polymarket_hourly_resolution"] = {
                    "slug": hourly["slug"], "expires_at": str(hourly["expires_at"]),
                    "http": resp.status_code, "body": resp.text[:1500]}
        except Exception as exc:  # noqa: BLE001
            out["polymarket_hourly_resolution_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        try:
            import asyncio
            from types import SimpleNamespace

            from parlay_bot.perpetuals.bitget_history_recovery import BitgetHistoryRecovery
            from parlay_bot.perpetuals.market_store import PerpetualMarketStore as _PMS

            real = _PMS(store)
            captured: dict = {}

            def fake_ingest(batch):
                captured["ingest_rows"] = len(batch.candles)
                return SimpleNamespace(inserted_candles=len(batch.candles), unchanged_candles=0)

            def fake_advance(asset, **kw):
                captured["advance"] = {k: str(v) for k, v in kw.items()}

            async def dry_run():
                worker = BitgetHistoryRecovery(real, assets=("BTC",))
                worker._advance = fake_advance
                worker.market = SimpleNamespace(ingest=fake_ingest, store=store, initialize=lambda: None)
                try:
                    captured["returned"] = await worker.fetch_asset("BTC")
                except Exception as exc:  # noqa: BLE001
                    captured["raised"] = f"{type(exc).__name__}: {exc}"[:300]
                finally:
                    await worker.aclose()
            asyncio.run(dry_run())
            out["bitget_dry_run"] = captured
        except Exception as exc:  # noqa: BLE001
            out["bitget_dry_run_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            import httpx

            from parlay_bot.perpetuals.history_recovery import contiguous_forward_next
            cursor = datetime(2026, 6, 8, 1, 30, tzinfo=UTC)
            until = cursor + timedelta(minutes=500)
            resp = httpx.get(
                "https://api.bitget.com/api/v3/market/history-candles",
                params={"category": "USDT-FUTURES", "symbol": "BTCUSDT", "interval": "5m",
                        "type": "market",
                        "startTime": str(int(cursor.timestamp() * 1000) - 1),
                        "endTime": str(int(until.timestamp() * 1000) - 1),
                        "limit": "100"}, timeout=20)
            body = resp.json()
            rows = body.get("data") or []
            stamps = [datetime.fromtimestamp(int(r[0]) / 1000, UTC) for r in rows]
            out["bitget_page"] = {
                "http": resp.status_code, "code": body.get("code"), "msg": body.get("msg"),
                "rows": len(rows),
                "first": stamps[0].isoformat() if stamps else None,
                "last": stamps[-1].isoformat() if stamps else None,
                "in_window": sum(cursor <= t < until for t in stamps),
                "next_cursor": contiguous_forward_next(cursor, until, stamps).isoformat(),
            }
        except Exception as exc:  # noqa: BLE001
            out["bitget_page_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        # TEMP (branch only): quote coverage by family, last 2 h.
        try:
            stamp = datetime.now(UTC)
            with store.connect() as conn:
                out["quote_cov_by_family"] = [{k: str(v) for k, v in dict(r).items()} for r in conn.execute(
                    """SELECT f.venue, f.family, COUNT(*) AS n,
                              SUM(CASE WHEN q.entry_at IS NOT NULL THEN 1 ELSE 0 END) AS matched
                       FROM rival_model_shadow_forecasts AS f
                       LEFT JOIN rival_family_quote_shadow AS q
                         ON q.venue=f.venue AND q.contract=f.contract
                        AND q.quote=f.quote AND q.horizon_minutes=f.horizon_minutes
                        AND q.family=f.family AND q.reference_opened_at=f.reference_opened_at
                       WHERE f.venue IN ('hyperliquid','binance')
                         AND (f.probability_up>=0.56 OR f.probability_up<=0.44
                              OR f.incumbent_up>=0.56 OR f.incumbent_up<=0.44)
                         AND f.issued_at>=? AND f.issued_at<?
                       GROUP BY f.venue, f.family ORDER BY f.venue, f.family""",
                    ((stamp - timedelta(hours=2)).isoformat(),
                     (stamp - timedelta(minutes=5)).isoformat())).fetchall()]
                out["quote_cov_flow_first"] = _one(conn, """SELECT MIN(issued_at) AS first FROM
                    rival_model_shadow_forecasts WHERE family LIKE 'flow_%'""")
        except Exception as exc:  # noqa: BLE001
            out["quote_cov_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        # Saved warning/error history (survives restarts), grouped, last 24 h.
        try:
            since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:00:00+00:00")
            with store.connect() as conn:
                out["problem_log_24h"] = [
                    {"logger": r["logger"], "level": r["level"], "count": int(r["n"]),
                     "hours": int(r["hours"]), "last_at": str(r["last_at"]),
                     "text": str(r["text"])[:180]}
                    for r in conn.execute(
                        """SELECT logger, level, template, SUM(count) AS n,
                                  COUNT(*) AS hours, MAX(last_at) AS last_at, MAX(text) AS text
                           FROM rival_problem_log WHERE hour>=?
                           GROUP BY logger, level, template
                           ORDER BY (level='ERROR') DESC, SUM(count) DESC LIMIT 40""",
                        (since,)).fetchall()]
        except Exception as exc:  # noqa: BLE001 - table appears after the first save
            out["problem_log_error"] = type(exc).__name__
    finally:
        store.close()
    return out


def main() -> int:
    try:
        report = probe()
    except Exception as exc:  # noqa: BLE001 - never print DSN or SQL
        report = {"error": type(exc).__name__}
    print(MARKER + json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
