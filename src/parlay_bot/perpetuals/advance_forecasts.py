"""Source-exact advance predictions, separate from incumbent trade governance.

One extra five-minute lead predicts the next target candle's OWN direction.
Only already-observed completed bars enter training. Original forecasts are
insert-only; later candle outcomes and accepted Discord deliveries are separate
evidence. No model here is qualified, calibrated, or allowed to place orders.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.features import feature_vector, live_examples, persisted_candles
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.learning import _sigmoid
from parlay_bot.perpetuals.markets import ASSETS, HORIZONS, asset_for_contract
from parlay_bot.perpetuals.model_checkpoints import (
    FEATURE_CONTRACT,
    LiveModelCheckpoints,
    canonical,
)
from parlay_bot.perpetuals.readiness import _source_identity

_LOG = logging.getLogger(__name__)
MODEL = "advance-candle-logistic-lead5-v1"
TARGET_CONTRACT = "next-5m-candle-close-above-own-open-lead5-v1"
LEAD_MINUTES = 5
BUDGET_SECONDS = 45
MIN_EVALUATED = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS rival_advance_forecasts (
    venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
    asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
    model_version TEXT NOT NULL, target_contract TEXT NOT NULL,
    reference_opened_at TEXT NOT NULL, inputs_known_at TEXT NOT NULL,
    issued_at TEXT NOT NULL, committed_at TEXT, target_opened_at TEXT NOT NULL,
    target_closed_at TEXT NOT NULL, probability_up DOUBLE PRECISION NOT NULL,
    baseline_up DOUBLE PRECISION NOT NULL, training_evaluated INTEGER NOT NULL,
    feature_sha256 TEXT NOT NULL, weights_sha256 TEXT NOT NULL,
    settled_at TEXT, outcome_known_at TEXT, outcome_status TEXT,
    realized_open DOUBLE PRECISION, realized_close DOUBLE PRECISION, outcome_up INTEGER,
    model_brier DOUBLE PRECISION, baseline_brier DOUBLE PRECISION,
    PRIMARY KEY(venue,contract,quote,horizon_minutes,target_opened_at,model_version)
);
CREATE INDEX IF NOT EXISTS idx_rival_advance_asset_target
    ON rival_advance_forecasts(asset,horizon_minutes,target_opened_at);
CREATE INDEX IF NOT EXISTS idx_rival_advance_pending
    ON rival_advance_forecasts(settled_at,target_closed_at);
CREATE INDEX IF NOT EXISTS idx_rival_advance_asset_issued
    ON rival_advance_forecasts(asset,issued_at);
CREATE INDEX IF NOT EXISTS idx_rival_advance_issued
    ON rival_advance_forecasts(issued_at);
CREATE TABLE IF NOT EXISTS rival_advance_readiness (
    slot_utc TEXT NOT NULL, venue TEXT NOT NULL, contract TEXT NOT NULL,
    quote TEXT NOT NULL, asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
    target_opened_at TEXT NOT NULL, checked_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(slot_utc,venue,contract,quote,horizon_minutes)
);
CREATE INDEX IF NOT EXISTS idx_rival_advance_readiness_asset
    ON rival_advance_readiness(asset,slot_utc);
CREATE INDEX IF NOT EXISTS idx_rival_advance_readiness_checked
    ON rival_advance_readiness(checked_at);
CREATE TABLE IF NOT EXISTS rival_advance_deliveries (
    channel_id TEXT NOT NULL, message_id TEXT NOT NULL,
    venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
    asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
    target_opened_at TEXT NOT NULL, model_version TEXT NOT NULL,
    issued_at TEXT NOT NULL, delivered_at TEXT NOT NULL,
    card_fingerprint TEXT NOT NULL,
    PRIMARY KEY(channel_id,message_id,venue,contract,quote,horizon_minutes,
                target_opened_at,model_version)
);
CREATE INDEX IF NOT EXISTS idx_rival_advance_delivered
    ON rival_advance_deliveries(delivered_at);
"""


def target_open(now: datetime, horizon: int) -> datetime:
    """Next target after the incumbent's selected delay, on the UTC grid."""
    if horizon not in HORIZONS:
        raise ValueError("unsupported advance horizon")
    stamp = utc(now)
    boundary = stamp.replace(minute=stamp.minute // 5 * 5, second=0, microsecond=0)
    return boundary + timedelta(minutes=horizon)


class AdvanceForecastMonitor:
    def __init__(self, market):
        from parlay_bot.perpetuals.shadow_monitor import ShadowLearningMonitor

        self.market, self.store = market, market.store
        self.discovery = ShadowLearningMonitor(self.store)
        self.checkpoints = LiveModelCheckpoints(
            self.store, feature_contract=FEATURE_CONTRACT,
            target_contract=TARGET_CONTRACT,
        )
        self._ready = False
        self._memos: dict = {}

    def initialize(self):
        if self._ready:
            return
        self.market.initialize()
        self.discovery.initialize()
        self.checkpoints.initialize()
        with self.store.connect() as conn:
            conn.executescript(SCHEMA)
        self._ready = True

    def _readiness(self, source, horizon, stamp, reason, *, checked_at=None):
        venue, contract, quote = source
        slot = stamp.replace(minute=stamp.minute // 5 * 5, second=0, microsecond=0)
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_advance_readiness
                (slot_utc,venue,contract,quote,asset,horizon_minutes,
                 target_opened_at,checked_at,reason) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT DO NOTHING""",
                (slot.isoformat(), venue, contract, quote, asset_for_contract(contract),
                 horizon, target_open(stamp, horizon).isoformat(),
                 utc(checked_at or datetime.now(UTC)).isoformat(), reason),
            )

    def run_once(self, *, now=None, budget_seconds=BUDGET_SECONDS):
        """Priority pass on the shared collector cycle, with no network reads.

        Explicit clocks exist for deterministic replay tests only. Production
        stamps AFTER fitting and checks the deadline again inside the write
        transaction. A lingering thread is guarded by AutonomousResearch.
        """
        from parlay_bot.perpetuals.forward_audit import select_live_streams

        stamp = utc(now or datetime.now(UTC))
        if stamp > datetime.now(UTC) + timedelta(seconds=5) or not 0 < budget_seconds <= 60:
            raise ValueError("invalid advance forecast clock/budget")
        started = time.monotonic()
        self.initialize()
        selected = select_live_streams(
            self.discovery._eligible_streams(now=stamp, limit=40),
            slot=int(stamp.timestamp() // 300),
        )
        issued = 0
        for source in selected:
            venue, contract, quote = source
            asset = asset_for_contract(contract)
            reason = None
            candles = ()
            try:
                if _source_identity(venue, asset) != (contract, quote):
                    reason = "source_mismatch"
                elif time.monotonic() - started >= budget_seconds:
                    reason = "cycle_budget_exhausted"
                else:
                    candles = persisted_candles(self.market.recent_candles(
                        venue=venue, contract=contract, quote=quote,
                        interval_minutes=5, count=1000, as_of=stamp,
                    ))
                    boundary = target_open(stamp, 5) - timedelta(minutes=5)
                    if len(candles) < 65:
                        reason = "insufficient_history"
                    elif (utc(candles[-1].opened_at) != boundary - timedelta(minutes=5)
                          or not candles[-1].trades
                          or not timedelta(0) <= stamp - utc(candles[-1].observed_at)
                          <= timedelta(minutes=5)):
                        reason = "stale_candle"
                features = feature_vector(candles) if reason is None else None
                memo = self._memos.get(source, {})
                # Cache only windows touched this cycle; never grow with uptime.
                from parlay_bot.perpetuals.features import WindowFeatureMemo
                feature_cache = WindowFeatureMemo(memo)
                for horizon in HORIZONS:
                    outcome = reason
                    if outcome is None and time.monotonic() - started >= budget_seconds:
                        outcome = "cycle_budget_exhausted"
                    if outcome is None:
                        samples = live_examples(candles, horizon_minutes=horizon,
                                                lead_minutes=LEAD_MINUTES,
                                                feature_cache=feature_cache)
                        samples = tuple(s for s in samples if utc(s.label_known_at) < stamp)
                        if len(samples) < MIN_EVALUATED + 20:
                            outcome = "insufficient_causal_labels"
                        else:
                            fitted = self.checkpoints.fit(samples, as_of=stamp)
                            if fitted.evaluated < MIN_EVALUATED:
                                outcome = "insufficient_training"
                            else:
                                outcome = self._issue(source, asset, horizon, candles[-1],
                                                      features, fitted, samples, now,
                                                      started, budget_seconds)
                                issued += int(outcome == "issued")
                    self._readiness(source, horizon, stamp, outcome, checked_at=now)
                self._memos[source] = feature_cache.current
            except Exception:  # noqa: BLE001 - one native source cannot stop the others
                _LOG.exception("RivaL advance head unavailable venue=%s asset=%s", venue, asset)
                for horizon in HORIZONS:
                    self._readiness(source, horizon, stamp, "source_read_or_fit_failed",
                                    checked_at=now)
        self._memos = {s: m for s, m in self._memos.items() if s in selected}
        settled = self.settle(stamp)
        _LOG.info("RivaL advance forecasts issued=%d settled=%d seconds=%.2f",
                  issued, settled, time.monotonic() - started)
        return issued, settled

    def _issue(self, source, asset, horizon, last, features, fitted, samples,
               now, started, budget_seconds):
        target = utc(last.opened_at) + timedelta(minutes=horizon + LEAD_MINUTES)
        p = _sigmoid(sum(w*x for w, x in zip(fitted.final_weights, (1., *features))))
        baseline = (sum(s.label_up for s in samples) + 5) / (len(samples) + 10)
        with self.store.connect() as conn:
            issued_at = utc(now or datetime.now(UTC))
            if time.monotonic() - started >= budget_seconds:
                return "cycle_budget_exhausted"
            if issued_at >= target:
                return "target_already_started"
            row = conn.execute(
                """INSERT INTO rival_advance_forecasts
                (venue,contract,quote,asset,horizon_minutes,model_version,target_contract,
                 reference_opened_at,inputs_known_at,issued_at,target_opened_at,
                 target_closed_at,probability_up,baseline_up,training_evaluated,
                 feature_sha256,weights_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT DO NOTHING""",
                (*source, asset, horizon, MODEL, TARGET_CONTRACT,
                 utc(last.opened_at).isoformat(), utc(last.observed_at).isoformat(),
                 issued_at.isoformat(), target.isoformat(),
                 (target + timedelta(minutes=5)).isoformat(), p, baseline, fitted.evaluated,
                 hashlib.sha256(canonical(features).encode()).hexdigest(),
                 hashlib.sha256(canonical(fitted.final_weights).encode()).hexdigest()),
            )
        if not row.rowcount:
            return "already_issued"
        # The INSERT's timestamp alone cannot prove it committed in time: a
        # database lock or slow commit can span the boundary. Preserve a later,
        # independently observed commit clock; never fill it retroactively on
        # a retry/restart. Readers require BOTH clocks before the target starts.
        committed = utc(now or datetime.now(UTC))
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_advance_forecasts SET committed_at=?,settled_at=?,outcome_status=?
                WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=?
                AND target_opened_at=? AND model_version=? AND committed_at IS NULL""",
                (committed.isoformat(), committed.isoformat() if committed >= target else None,
                 "commit_after_deadline" if committed >= target else None,
                 *source, horizon, target.isoformat(), MODEL),
            )
        return "issued" if committed < target else "commit_after_deadline"

    def settle(self, now):
        """Exact native five-minute candle only, after original issuance."""
        stamp = utc(now)
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT f.*,c.open_price,c.close_price,c.trades,c.closed_at,
                          c.first_observed_at FROM rival_advance_forecasts f
                   JOIN perp_market_candles c ON c.venue=f.venue AND c.contract=f.contract
                    AND c.quote=f.quote AND c.interval_minutes=5
                    AND c.opened_at=f.target_opened_at
                   WHERE f.settled_at IS NULL AND f.target_closed_at<=?
                    AND f.committed_at<f.target_opened_at AND f.committed_at>=f.issued_at
                    AND c.first_observed_at<=? ORDER BY f.target_closed_at DESC LIMIT 120""",
                (stamp.isoformat(), stamp.isoformat()),
            ).fetchall()
            settled = 0
            for row in rows:
                known = utc(datetime.fromisoformat(row["first_observed_at"]))
                closed = utc(datetime.fromisoformat(row["closed_at"]))
                target = utc(datetime.fromisoformat(row["target_opened_at"]))
                issued = utc(datetime.fromisoformat(row["issued_at"]))
                opened, price = float(row["open_price"]), float(row["close_price"])
                if (known <= closed or closed < target + timedelta(minutes=5, milliseconds=-1)
                        or closed >= target + timedelta(minutes=5) or issued >= target
                        or known <= issued or not all(math.isfinite(x) and x > 0
                                                     for x in (opened, price))):
                    continue
                status = "no_trades" if int(row["trades"]) == 0 else "observed"
                y = int(price > opened) if status == "observed" else None
                result = conn.execute(
                    """UPDATE rival_advance_forecasts SET settled_at=?,outcome_known_at=?,
                    outcome_status=?,realized_open=?,realized_close=?,outcome_up=?,
                    model_brier=?,baseline_brier=? WHERE venue=? AND contract=? AND quote=?
                    AND horizon_minutes=? AND target_opened_at=? AND model_version=?
                    AND settled_at IS NULL""",
                    (stamp.isoformat(), known.isoformat(), status, opened, price, y,
                     (float(row["probability_up"])-y)**2 if y is not None else None,
                     (float(row["baseline_up"])-y)**2 if y is not None else None,
                     row["venue"], row["contract"], row["quote"], row["horizon_minutes"],
                     row["target_opened_at"], row["model_version"]),
                )
                settled += result.rowcount
        return settled


REASONS = {
    "insufficient_history": "Not enough completed native candles.",
    "insufficient_causal_labels": "Not enough genuinely observed training outcomes.",
    "insufficient_training": "The advance model has insufficient evaluated examples.",
    "stale_candle": "The latest completed source candle is missing or stale.",
    "cycle_budget_exhausted": "The forecast worker reached its time budget.",
    "target_already_started": "Issuance missed the pre-start deadline.",
    "commit_after_deadline": "The forecast write completed after the target started.",
    "source_mismatch": "The native instrument identity could not be verified.",
    "source_read_or_fit_failed": "The source or model read failed this cycle.",
}


def read_advance(market, *, asset, horizon, now, venue=None, contract=None, quote=None,
                 next_target_at=None):
    """Display-only exact-scope read; never initialize, train or backfill."""
    if asset not in ASSETS:
        raise ValueError("unsupported advance asset")
    stamp = utc(now)
    wanted = utc(next_target_at) if next_target_at is not None else target_open(stamp, horizon)
    if market is None:
        return {"next": None, "previous": None, "reason": "Live market collector unavailable."}
    params = [asset, horizon, MODEL, (wanted-timedelta(minutes=5)).isoformat(), wanted.isoformat()]
    source_sql = ""
    if venue is not None:
        source_sql = " AND venue=? AND contract=? AND quote=?"
        params.extend((venue, contract, quote))
    try:
        with market.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rival_advance_forecasts WHERE asset=? AND horizon_minutes=?
                AND model_version=? AND target_opened_at>=? AND target_opened_at<=?"""
                + source_sql + " ORDER BY issued_at DESC LIMIT 80", params,
            ).fetchall()
            receipts = conn.execute(
                """SELECT * FROM rival_advance_readiness WHERE asset=? AND horizon_minutes=?
                AND target_opened_at=?""" + source_sql + " ORDER BY checked_at DESC LIMIT 1",
                (asset, horizon, wanted.isoformat(),
                 *((venue, contract, quote) if venue is not None else ())),
            ).fetchall()
        valid = []
        for raw in rows:
            row = dict(raw)
            issued = utc(datetime.fromisoformat(row["issued_at"]))
            start = utc(datetime.fromisoformat(row["target_opened_at"]))
            known = utc(datetime.fromisoformat(row["inputs_known_at"]))
            committed = (utc(datetime.fromisoformat(row["committed_at"]))
                         if row["committed_at"] else None)
            reference = utc(datetime.fromisoformat(row["reference_opened_at"]))
            p = float(row["probability_up"])
            if (known <= issued < start and committed is not None
                    and issued <= committed < start and committed <= stamp
                    and start-reference == timedelta(minutes=horizon+LEAD_MINUTES)
                    and math.isfinite(p)
                    and 0 < p < 1 and row["target_contract"] == TARGET_CONTRACT
                    and _source_identity(row["venue"], asset) == (row["contract"], row["quote"])):
                row.update(issued=issued, target=start)
                valid.append(row)
        next_row = next((r for r in valid if r["target"] == wanted), None)
        previous = next((r for r in valid if r["target"] == wanted-timedelta(minutes=5)), None)
        reason = (REASONS.get(receipts[0]["reason"], "No advance forecast recorded for this target.")
                  if receipts else "The forecast worker has not recorded this target yet.")
        return {"next": next_row, "previous": previous, "reason": reason}
    except Exception:  # noqa: BLE001 - missing rollout/schema is not a fabricated zero
        return {"next": None, "previous": None, "reason": "Advance forecast evidence unavailable."}


def read_advance_receipt(store, now):
    """Bounded real-host evidence; no DDL/inference or user/channel identifiers."""
    since = (utc(now)-timedelta(hours=2)).isoformat()
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT asset,horizon_minutes,venue,COUNT(*) AS issued,
                MIN(issued_at) AS first_issued_at,MAX(issued_at) AS latest_issued_at,
                SUM(CASE WHEN issued_at<target_opened_at AND committed_at<target_opened_at
                    THEN 1 ELSE 0 END) AS pre_start,
                SUM(CASE WHEN outcome_up IS NOT NULL THEN 1 ELSE 0 END) AS scored,
                AVG(model_brier) AS brier,AVG(baseline_brier) AS baseline_brier
                FROM rival_advance_forecasts WHERE issued_at>=? AND issued_at<=?
                GROUP BY asset,horizon_minutes,venue""", (since, utc(now).isoformat()),
            ).fetchall()
            readiness = conn.execute(
                """SELECT reason,COUNT(*) AS attempts FROM rival_advance_readiness
                WHERE checked_at>=? GROUP BY reason""", (since,),
            ).fetchall()
            deliveries = conn.execute(
                """SELECT asset,horizon_minutes,COUNT(*) AS accepted_discord_deliveries,
                SUM(CASE WHEN delivered_at<target_opened_at THEN 1 ELSE 0 END) AS pre_start,
                MAX(delivered_at) AS latest_delivered_at FROM rival_advance_deliveries
                WHERE delivered_at>=? GROUP BY asset,horizon_minutes""", (since,),
            ).fetchall()
            examples = conn.execute(
                """SELECT asset,horizon_minutes,venue,issued_at,committed_at,
                target_opened_at,target_closed_at FROM rival_advance_forecasts
                WHERE issued_at>=? AND issued_at<=? ORDER BY issued_at DESC LIMIT 24""",
                (since, utc(now).isoformat()),
            ).fetchall()
            delivery_examples = conn.execute(
                """SELECT asset,horizon_minutes,venue,issued_at,delivered_at,target_opened_at
                FROM rival_advance_deliveries WHERE delivered_at>=? AND delivered_at<=?
                ORDER BY delivered_at DESC LIMIT 24""", (since, utc(now).isoformat()),
            ).fetchall()
        samples = []
        for row in examples:
            sample = dict(row)
            sample["seconds_of_advance_warning"] = (
                (utc(datetime.fromisoformat(row["target_opened_at"]))
                 - utc(datetime.fromisoformat(row["committed_at"]))
                 ).total_seconds() if row["committed_at"] else None)
            samples.append(sample)
        delivery_samples = []
        for row in delivery_examples:
            sample = dict(row)
            delivered = utc(datetime.fromisoformat(row["delivered_at"]))
            sample["issuance_to_delivery_seconds"] = (
                delivered - utc(datetime.fromisoformat(row["issued_at"]))
            ).total_seconds()
            sample["seconds_of_advance_warning"] = (
                utc(datetime.fromisoformat(row["target_opened_at"])) - delivered
            ).total_seconds()
            delivery_samples.append(sample)
        try:
            with store.connect() as conn:
                cards = conn.execute(
                    """SELECT market_kind,asset,horizon_minutes,COUNT(*) AS cards
                    FROM rival_live_call_board GROUP BY market_kind,asset,horizon_minutes"""
                ).fetchall()
            channel_cards = {"status": "recorded", "active": sum(int(r["cards"]) for r in cards),
                             "scopes": [dict(r) for r in cards]}
        except Exception:  # noqa: BLE001 - no card requested is not a forecast failure
            channel_cards = {"status": "read_unavailable", "active": None}
        return {"status": "recorded", "model": MODEL, "target_contract": TARGET_CONTRACT,
                "scopes": [dict(r) for r in rows], "readiness": [dict(r) for r in readiness],
                "deliveries": [dict(r) for r in deliveries], "window_hours": 2,
                "recent_forecasts": samples, "recent_deliveries": delivery_samples,
                "channel_cards": channel_cards,
                "read_only": True, "qualified": False,
                "does_not_prove_prediction_edge": True,
                "discord_delivery_is_api_acceptance_not_independent_observation": True}
    except Exception:  # noqa: BLE001 - absent schema/host read remains unknown
        return {"status": "read_unavailable", "read_only": True, "qualified": False}
