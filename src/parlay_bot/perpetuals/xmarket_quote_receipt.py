"""Recent source-exact quote capture, independent of the hourly cost verdict.

These are operational ledger rows, not independent targets, fills or a second
profit evaluator. SELECT only; no schema setup, forecast, settlement or replay.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.family_quotes import SUPPORTED_QUOTE_SOURCES
from parlay_bot.perpetuals.live_quotes import source_contract

_BASE = {"read_only": True,"does_not_prove_prediction_edge": True,
         "counts_are_independent_targets": False}


def read_xmarket_quotes(store,now: datetime,progress: dict) -> dict:
    """At most 84 indexed recent-stream reads for fitted asset/horizon scopes.

    Use the same canonical instruments accepted by the existing quote
    observers' canonical native instruments and quote currencies. Join the
    original forecast, artifact SHA and issue time before counting a quote.
    An absent/unreadable ledger is never reported as zero recorded quotes.
    """
    try:
        if now.utcoffset() is None:
            raise ValueError("quote receipt clock requires timezone")
        now = now.astimezone(UTC)
        scoped = progress.get("scope_progress")
        if not isinstance(scoped,list):
            raise TypeError("scope census unavailable")
        fits = sorted({(row["asset"],int(row["horizon_minutes"])) for row in scoped
                       if int(row.get("stored_gate_passing_artifacts") or 0)>0})
        if any(asset not in ("BTC","ETH","SOL","XRP") or horizon not in (5,15,60)
               for asset,horizon in fits):
            raise ValueError("invalid scope")
        if not fits:
            return {**_BASE,"status":"no_fitted_scopes","scopes":[]}
        rows = []
        since = (now-timedelta(hours=2)).isoformat()
        until = now.isoformat()
        with store.connect() as conn:
            for asset,horizon in fits:
                for venue,currency in sorted(SUPPORTED_QUOTE_SOURCES):
                    contract = source_contract(venue, asset)
                    row = conn.execute(
                        """SELECT COUNT(*) AS forecast_rows,
                           SUM(CASE WHEN q.family IS NOT NULL THEN 1 ELSE 0 END) AS entry_rows,
                           SUM(CASE WHEN q.state='EXIT_QUOTED' AND q.exit_at<=?
                                    THEN 1 ELSE 0 END) AS closed_exit_rows,
                           SUM(CASE WHEN q.state='MISSED_EXIT' THEN 1 ELSE 0 END) AS missed_exit_rows,
                           MAX(q.entry_at) AS latest_entry_at,
                           MAX(CASE WHEN q.state='EXIT_QUOTED' AND q.exit_at<=?
                                    THEN q.exit_at END) AS latest_exit_at
                           FROM rival_model_shadow_forecasts s
                           LEFT JOIN rival_family_quote_shadow q
                            ON q.venue=s.venue AND q.contract=s.contract AND q.quote=s.quote
                            AND q.asset=s.asset AND q.horizon_minutes=s.horizon_minutes
                            AND q.family=s.family AND q.reference_opened_at=s.reference_opened_at
                            AND q.training_sha256=s.training_sha256 AND q.issued_at=s.issued_at
                            AND q.entry_at>=s.issued_at AND q.entry_at<=?
                           WHERE s.asset=? AND s.horizon_minutes=? AND s.family='xmarket_v1'
                             AND s.issued_at>=? AND s.issued_at<=?
                             AND s.venue=? AND s.contract=? AND s.quote=?""",
                        (until,until,until,asset,horizon,since,until,venue,contract,currency),
                    ).fetchone()
                    rows.append({"asset":asset,"horizon_minutes":horizon,
                                 "venue":venue,"contract":contract,"quote":currency,
                                 **{key:int(row[key] or 0) for key in (
                                     "forecast_rows","entry_rows","closed_exit_rows","missed_exit_rows",
                                 )},
                                 "latest_entry_at":row["latest_entry_at"],
                                 "latest_exit_at":row["latest_exit_at"]})
        return {**_BASE,"status":"recorded","window_start":since,"captured_at":until,
                "scopes":rows}
    except Exception:  # noqa: BLE001 - observational receipts cannot break the bot
        return {**_BASE,"status":"read_unavailable","scopes":[]}
