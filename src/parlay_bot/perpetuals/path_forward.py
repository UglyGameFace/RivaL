"""Source-exact, always-on paper TP1/TP2-before-stop forecasts.

This is an ADDITIVE research ledger, never an order/filled trade. Training
uses closed source candles known at issuance. Future labels are matched to
the same market and issued time, with first-observation proof. Five-minute
OHLC ambiguity excludes the path rather than inventing a profitable fill.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import statistics
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.first_passage import trace_barriers
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.governance import score_paired_forward

VERSION = "prospective-empirical-path-v1"
HORIZONS = (15, 60)
SIDES = ("long", "short")
LOOKBACK = 24
MIN_TRAIN = 25
MAX_TRAIN = 280
MAX_SETTLE = 100


def _sigma(candles, index: int, steps: int) -> float:
    if index < LOOKBACK:
        raise ValueError("not enough causal volatility observations")
    closes = [float(c.close_price) for c in candles[index-LOOKBACK:index+1]]
    if any(not math.isfinite(v) or v <= 0 for v in closes):
        raise ValueError("invalid prior prices")
    returns = [math.log(b/a) for a,b in itertools.pairwise(closes)]
    return max(.0001, min(.25, statistics.pstdev(returns)*math.sqrt(steps)))


def _quantile(values, p):
    sequence = sorted(values)
    at = (len(sequence)-1)*p
    lower, upper = math.floor(at), math.ceil(at)
    return sequence[lower]*(upper-at) + sequence[upper]*(at-lower) if upper != lower else sequence[lower]


def empirical_paths(candles, *, horizon: int, now: datetime):
    """Both sides' source-specific priors from past matured NONOVERLAP paths.

    The aligned/trend bucket is fixed using only information known at each
    reference bar, not hindsight labels or market regimes selected after test.
    Historical periods with late first observations are not *forward proof*,
    but may train a prior after their data is actually known.
    """
    if horizon not in HORIZONS:
        return {}
    instant = utc(now)
    steps = horizon//5
    if len(candles) < LOOKBACK+steps+MIN_TRAIN*steps+1:
        return {}
    previous = None
    for c in candles:
        if (utc(c.observed_at) > instant
                or int(c.interval_minutes) != 5
                or (previous is not None and
                    (c.venue,c.contract,c.quote) !=
                    (previous.venue,previous.contract,previous.quote))):
            raise ValueError("future or cross-market path input")
        previous = c
    latest = len(candles)-1
    side_samples = {side: [] for side in SIDES}
    # A step-wide grid avoids training on 12 copies of one 60m outcome.
    for index in range(LOOKBACK, latest-steps+1, steps):
        reference = candles[index]
        ahead = candles[index+1:index+steps+1]
        if any(utc(item.opened_at) != utc(reference.opened_at)+timedelta(minutes=5*(j+1))
               or utc(item.closed_at) >= instant or utc(item.observed_at) > instant
               for j,item in enumerate(ahead)):
            continue
        try:
            vol = _sigma(candles,index,steps)
        except ValueError:
            continue
        entry = float(reference.close_price)
        movement = math.log(entry / float(candles[index-3].close_price))
        bars = [{"high":c.high_price,"low":c.low_price} for c in ahead]
        for side in SIDES:
            flags = trace_barriers(
                entry=entry, sigma_horizon=vol, side=side, future=bars,
            )
            if flags is None:
                continue
            sign = 1 if side=="long" else -1
            realized = sign*math.log(float(ahead[-1].close_price)/entry)
            side_samples[side].append((
                utc(reference.opened_at).isoformat(),
                bool(sign*movement >= 0), flags[0], flags[1], realized,
            ))
    if not all(len(rows) >= MIN_TRAIN for rows in side_samples.values()):
        return {}
    current_movement = math.log(
        float(candles[-1].close_price)/float(candles[-4].close_price),
    )
    current_sigma = _sigma(candles,latest,steps)
    result = {}
    for side, history in side_samples.items():
        history = history[-MAX_TRAIN:]
        sign = 1 if side=="long" else -1
        aligned = bool(sign*current_movement >= 0)
        matching = [row for row in history if row[1]==aligned]
        if len(matching) < 12:
            matching = history
        # Honest baseline: unconditional source history known at issuance.
        baseline = [(sum(row[index] for row in history)+2)/(len(history)+4)
                    for index in (2,3)]
        probabilities = [
            (sum(row[index] for row in matching)+16*base)/(len(matching)+16)
            for index,base in zip((2,3),baseline,strict=True)
        ]
        # Nested events must remain nested after conservative shrinkage.
        probabilities[1] = min(probabilities[0],probabilities[1])
        signed_returns = [row[4] for row in matching]
        quantiles = [_quantile(signed_returns,p) for p in (.1,.5,.9)]
        digest = hashlib.sha256(json.dumps(
            history,sort_keys=True,separators=(",",":"),allow_nan=False,
        ).encode()).hexdigest()
        result[side] = {
            "sigma": current_sigma,
            "tp1_probability": probabilities[0],
            "tp2_probability": probabilities[1],
            "tp1_baseline": baseline[0],
            "tp2_baseline": baseline[1],
            "p10": quantiles[0], "p50": quantiles[1], "p90": quantiles[2],
            "training_count": len(history), "cohort_count": len(matching),
            "training_sha256": digest,
        }
    return result


class ProspectivePathLedger:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS rival_forward_paths (
                venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
                asset TEXT NOT NULL,horizon_minutes INTEGER NOT NULL,
                model_version TEXT NOT NULL,side TEXT NOT NULL,
                reference_opened_at TEXT NOT NULL,issued_at TEXT NOT NULL,
                target_opened_at TEXT NOT NULL,reference_close DOUBLE PRECISION NOT NULL,
                sigma_horizon DOUBLE PRECISION NOT NULL,
                tp1_probability DOUBLE PRECISION NOT NULL,
                tp2_probability DOUBLE PRECISION NOT NULL,
                tp1_baseline DOUBLE PRECISION NOT NULL,
                tp2_baseline DOUBLE PRECISION NOT NULL,
                signed_return_p10 DOUBLE PRECISION NOT NULL,
                signed_return_p50 DOUBLE PRECISION NOT NULL,
                signed_return_p90 DOUBLE PRECISION NOT NULL,
                training_count INTEGER NOT NULL,cohort_count INTEGER NOT NULL,
                training_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,settled_at TEXT,
                tp1_before_stop INTEGER,tp2_before_stop INTEGER,
                realized_signed_return DOUBLE PRECISION,
                PRIMARY KEY(venue,contract,quote,horizon_minutes,side,reference_opened_at)
            );
            CREATE INDEX IF NOT EXISTS idx_rival_forward_paths_pending
                ON rival_forward_paths(state,target_opened_at);
            CREATE INDEX IF NOT EXISTS idx_rival_forward_paths_score
                ON rival_forward_paths(asset,horizon_minutes,side,issued_at);
            """)

    def issue(self, conn, *, candles, asset, horizon, now, issued_at):
        if horizon not in HORIZONS:
            return 0
        now,issued_at=utc(now),utc(issued_at)
        last=candles[-1]
        if (issued_at < utc(last.observed_at)
                or issued_at >= utc(last.opened_at)+timedelta(minutes=10)):
            return 0
        options=empirical_paths(candles,horizon=horizon,now=now)
        if not options:
            return 0
        target=utc(last.opened_at)+timedelta(minutes=horizon)
        count=0
        for side,sample in options.items():
            result=conn.execute(
                """INSERT INTO rival_forward_paths
                 (venue,contract,quote,asset,horizon_minutes,model_version,side,
                  reference_opened_at,issued_at,target_opened_at,reference_close,
                  sigma_horizon,tp1_probability,tp2_probability,tp1_baseline,
                  tp2_baseline,signed_return_p10,signed_return_p50,
                  signed_return_p90,training_count,cohort_count,training_sha256,state)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT DO NOTHING""",
                (last.venue,last.contract,last.quote,asset,horizon,VERSION,side,
                 utc(last.opened_at).isoformat(),issued_at.isoformat(),target.isoformat(),
                 last.close_price,sample["sigma"],sample["tp1_probability"],
                 sample["tp2_probability"],sample["tp1_baseline"],
                 sample["tp2_baseline"],sample["p10"],sample["p50"],sample["p90"],
                 sample["training_count"],sample["cohort_count"],
                 sample["training_sha256"],"pending"),
            )
            count+=int(result.rowcount==1)
        return count

    def settle(self, *, now: datetime) -> dict:
        """Never settle using an old backfill, a different book or OHLC guess."""
        stamp=utc(now)
        self.initialize()
        with self.store.connect() as conn:
            waiting=conn.execute(
                """SELECT * FROM rival_forward_paths
                   WHERE state='pending' AND target_opened_at<=?
                    AND target_opened_at>=?
                   ORDER BY target_opened_at LIMIT ?""",
                ((stamp-timedelta(minutes=5)).isoformat(),
                 (stamp-timedelta(days=14)).isoformat(),MAX_SETTLE),
            ).fetchall()
            scored=ambiguous=0
            for p in waiting:
                target=utc(datetime.fromisoformat(p["target_opened_at"]))
                reference=utc(datetime.fromisoformat(p["reference_opened_at"]))
                issued=utc(datetime.fromisoformat(p["issued_at"]))
                steps=int(p["horizon_minutes"])//5
                if not (issued < reference+timedelta(minutes=10) and issued<=stamp
                        and target==reference+timedelta(minutes=5*steps)):
                    continue
                bars=conn.execute(
                    """SELECT opened_at,closed_at,first_observed_at,high_price,
                              low_price,close_price FROM perp_market_candles
                       WHERE venue=? AND contract=? AND quote=?
                         AND interval_minutes=5 AND opened_at>?
                         AND opened_at<=? ORDER BY opened_at""",
                    (p["venue"],p["contract"],p["quote"],
                     reference.isoformat(),target.isoformat()),
                ).fetchall()
                if len(bars)!=steps:
                    continue
                try:
                    valid=all(
                        utc(datetime.fromisoformat(b["opened_at"])) ==
                            reference+timedelta(minutes=5*(j+1))
                        and issued < utc(datetime.fromisoformat(
                            b["first_observed_at"])) <= stamp
                        and utc(datetime.fromisoformat(b["closed_at"])) < stamp
                        and utc(datetime.fromisoformat(b["closed_at"])) <=
                            utc(datetime.fromisoformat(b["first_observed_at"]))
                        for j,b in enumerate(bars)
                    )
                    if not valid:
                        continue
                    result=trace_barriers(
                        entry=float(p["reference_close"]),
                        sigma_horizon=float(p["sigma_horizon"]),
                        side=str(p["side"]),
                        future=[{"high":b["high_price"],"low":b["low_price"]}
                                for b in bars],
                    )
                    terminal=float(bars[-1]["close_price"])
                    signed=(1 if p["side"]=="long" else -1)*math.log(
                        terminal/float(p["reference_close"]),
                    )
                    if not math.isfinite(signed):
                        continue
                except (ValueError,TypeError,OverflowError,ZeroDivisionError):
                    continue
                state="ambiguous" if result is None else "settled"
                updated=conn.execute(
                    """UPDATE rival_forward_paths SET
                       state=?,settled_at=?,tp1_before_stop=?,
                       tp2_before_stop=?,realized_signed_return=?
                       WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=?
                         AND side=? AND reference_opened_at=? AND state='pending'""",
                    (state,stamp.isoformat(),
                     result[0] if result is not None else None,
                     result[1] if result is not None else None,
                     signed,p["venue"],p["contract"],p["quote"],
                     p["horizon_minutes"],p["side"],p["reference_opened_at"]),
                )
                if updated.rowcount==1:
                    scored+=int(state=="settled")
                    ambiguous+=int(state=="ambiguous")
        return {"settled":scored,"ambiguous":ambiguous}

    def report(self, *, asset, horizon, now=None) -> dict:
        """Independent source/side evaluation; research never live approval."""
        stamp=utc(now or datetime.now(UTC))
        self.initialize()
        with self.store.connect() as conn:
            rows=conn.execute(
                """SELECT * FROM rival_forward_paths WHERE asset=?
                   AND horizon_minutes=? AND issued_at>=? AND settled_at<=?
                   AND state='settled' ORDER BY target_opened_at""",
                (asset,horizon,(stamp-timedelta(days=14)).isoformat(),
                 stamp.isoformat()),
            ).fetchall()
        scopes={}
        for row in rows:
            key="/".join((row["venue"],row["contract"],row["quote"],row["side"]))
            target=str(row["target_opened_at"])
            if int(datetime.fromisoformat(target).timestamp())%(horizon*60):
                continue
            scopes.setdefault(key,{}).setdefault(target,[]).append(row)
        reports={}
        for key,targets in scopes.items():
            comparisons={}
            for level in (1,2):
                paired=[]
                for target,group in sorted(targets.items()):
                    if len(group)!=1 or group[0][f"tp{level}_before_stop"] not in (0,1):
                        continue
                    r=group[0]
                    y=int(r[f"tp{level}_before_stop"])
                    m=float(r[f"tp{level}_probability"])
                    b=float(r[f"tp{level}_baseline"])
                    if not all(math.isfinite(v) and 0<=v<=1 for v in (m,b)):
                        continue
                    paired.append((target,(m-y)**2,(b-y)**2))
                proof=score_paired_forward(paired)
                comparisons[f"tp{level}"]={
                    "targets":proof.targets,"days":proof.days,
                    "model_brier":proof.model_brier,
                    "baseline_brier":proof.baseline_brier,
                    "qualified_research":proof.status=="qualified_shadow",
                    "reason":proof.reason,
                }
            reports[key]={
                "tp1":comparisons["tp1"],"tp2":comparisons["tp2"],
                "qualified_research":all(x["qualified_research"]
                                         for x in comparisons.values()),
                "live_approved":False,
                "after_cost_qualified":False,
            }
        return reports
