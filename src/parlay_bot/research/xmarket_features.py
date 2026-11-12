"""Causal offline/live price-disagreement inputs for xmarket_v1.

The spec's named contract has 18 inputs, not 17. With fewer than two
verified common venues dispersion is omitted (15 inputs), never zero-filled.
The artifact pins that choice. PM means the Up-token /prices-history price;
Kalshi means the native one-minute YES-ask close. Neither is a two-ask ratio.
Native 15-minute contracts are covariates, including for 5/60-minute targets.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta

from parlay_bot.research.archive_features import FEATURES as ARCHIVE_FEATURES

FEATURE_CONTRACT = "xmarket-native15-history-price-v1"
MODEL_VERSION = "xmarket-offline-v1"
SCHEMA = "rival-xmarket-model-v1"
WINDOW = 420
VENUES = ("binance", "bitget", "okx", "gate")
BASE_FEATURES = ARCHIVE_FEATURES[:10]
MARKET_FEATURES = ("pm_up", "pm_up_chg_3", "pm_up_chg_12", "kalshi_up", "kalshi_pm_gap")
DISPERSION_FEATURES = ("venue_spread_ret1", "venue_up_share", "venue_spread_ret5")
FEATURES = BASE_FEATURES + MARKET_FEATURES + DISPERSION_FEATURES
PRICE_SEMANTICS = {"polymarket": "up-token-prices-history",
                   "kalshi": "native-1m-yes-ask-close"}


def timestamp(value) -> datetime:
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if value.utcoffset() is None:
        raise ValueError("xmarket timestamps require a timezone")
    return value.astimezone(UTC)


def feature_names(venues) -> tuple[str, ...]:
    venues = tuple(venues)
    if len(set(venues)) != len(venues) or any(v not in VENUES for v in venues):
        raise ValueError("invalid xmarket dispersion venues")
    if len(venues) == 1:
        raise ValueError("one venue cannot define cross-venue dispersion")
    return BASE_FEATURES + MARKET_FEATURES + (DISPERSION_FEATURES if venues else ())


def feature_digest(venues, pm_fidelity=1) -> str:
    return hashlib.sha256(json.dumps({"contract": FEATURE_CONTRACT,
        "features": feature_names(venues), "venues": list(venues),
        "price_semantics": PRICE_SEMANTICS, "pm_fidelity_minutes": pm_fidelity}, sort_keys=True).encode()).hexdigest()


FEATURE_DIGEST = feature_digest(VENUES)


def validate_sample(row: dict) -> dict:
    """Reject guessed identities, future source points, bad prices and hash substitutions."""
    source = row.get("source")
    if source not in PRICE_SEMANTICS or row.get("price_semantics") != PRICE_SEMANTICS[source]:
        raise ValueError("unknown xmarket price semantics")
    if row.get("asset") not in ("BTC", "ETH", "SOL", "XRP"):
        raise ValueError("unknown xmarket asset")
    opens, closes, at, observed = (timestamp(row[k]) for k in
                                  ("opens_at", "closes_at", "at", "observed_at"))
    if (closes - opens != timedelta(minutes=15) or not opens <= at <= closes
            or at > observed or not isinstance(row.get("contract_id"), str)
            or not 1 <= len(row["contract_id"]) <= 120):
        raise ValueError("invalid xmarket contract/time binding")
    if source == "polymarket":
        expected_slug = f"{row['asset'].lower()}-updown-15m-{int(opens.timestamp())}"
        if (not re.fullmatch(r"0x[0-9a-fA-F]{64}", row["contract_id"])
                or row.get("slug") != expected_slug
                or not re.fullmatch(r"[0-9]{20,90}", str(row.get("token_id", "")))):
            raise ValueError("unverified PM Up-token contract binding")
    elif not re.fullmatch(r"KX" + row["asset"] + r"15M-[A-Z0-9-]{1,90}", row["contract_id"]):
        raise ValueError("unverified Kalshi native contract binding")
    p = row.get("price")
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) \
            or not 0 <= p <= 1 or row.get("fidelity_minutes") not in (1, 5, 15):
        raise ValueError("invalid xmarket price/fidelity")
    sha = row.get("provider_sha256")
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("missing xmarket provider hash")
    if sha == row.get("normalized_sha256"):
        raise ValueError("xmarket provider hash is a normalized hash")
    return row


def sampled_bars(samples, *, asset: str, as_of: datetime | None = None, pm_fidelity=1) -> dict:
    """Use an observed source point in the final minute of ONE bar only.

    No nearest-future match, interpolation or carry into another bar. At an
    event boundary use the just-completed native window (open < t <= close).
    A sparse 15-minute source therefore never becomes a dense 5-minute series.
    """
    by_source = {"polymarket": {}, "kalshi": {}}
    for raw in samples:
        row = validate_sample(raw)
        if row["asset"] != asset:
            continue
        at = timestamp(row["at"])
        if as_of is not None and timestamp(row["observed_at"]) > timestamp(as_of):
            continue
        seconds = at.timestamp()
        boundary = datetime.fromtimestamp(math.ceil(seconds / 300) * 300, UTC)
        if (not timedelta(0) <= boundary - at <= timedelta(seconds=60)
                or not timestamp(row["opens_at"]) < boundary <= timestamp(row["closes_at"])):
            continue
        if row["source"] == "polymarket" and pm_fidelity == 15 and boundary.minute % 15:
            continue
        dest = by_source[row["source"]]
        old = dest.get(boundary)
        if old is not None and old["contract_id"] != row["contract_id"]:
            raise ValueError("ambiguous concurrent native contract")
        if old is None or timestamp(old["at"]) < at:
            dest[boundary] = row
        elif timestamp(old["at"]) == at and float(old["price"]) != float(row["price"]):
            raise ValueError("conflicting immutable source prices")
    return by_source


def market_values(bars: dict, at: datetime) -> tuple[float, ...] | None:
    pm, kalshi = bars["polymarket"], bars["kalshi"]
    try:
        p, p3, p12 = (float(pm[at - timedelta(minutes=5 * lag)]["price"])
                      for lag in (0, 3, 12))
        k = float(kalshi[at]["price"])
    except KeyError:
        return None
    return p, p - p3, p - p12, k, k - p


def spread(row) -> float | None:
    bid, ask = row.get("yes_bid"), row.get("yes_ask")
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) for v in (bid, ask)):
        return None
    return float(ask - bid) if 0 <= bid <= ask <= 1 else None


def live_vector(candles, *, asset: str, samples, venue_candles: dict, venues,
                now: datetime | None = None, pm_fidelity=1):
    from parlay_bot.perpetuals.features import feature_vector
    from parlay_bot.perpetuals.pooled_features import bar_close, contiguous

    names = feature_names(venues)
    if len(candles) < WINDOW or not contiguous(candles[-WINDOW:]):
        return None
    at = bar_close(candles[-1])
    bars = sampled_bars(samples, asset=asset, as_of=now, pm_fidelity=pm_fidelity)
    extra = market_values(bars, at)
    if extra is None:
        return None
    try:
        base = feature_vector(candles)
        dispersion = []
        for venue in venues:
            rows = venue_candles.get(venue, ())
            if len(rows) < 40 or not contiguous(rows[-40:]) or bar_close(rows[-1]) != at:
                return None
            vector = feature_vector(rows)
            dispersion.append((vector[0], vector[1]))
        values = (*base, *extra)
        if dispersion:
            r1, r5 = zip(*dispersion)
            values += (max(r1) - min(r1), sum(v > 0 for v in r1) / len(r1), max(r5) - min(r5))
        if len(values) != len(names) or not all(math.isfinite(v) for v in values):
            return None
        diagnostics = {"pm_spread": spread(bars["polymarket"][at]),
                       "kalshi_spread": spread(bars["kalshi"][at]),
                       "native_horizon_minutes": 15, "pm_fidelity_minutes": pm_fidelity,
                       "source_at": {s: bars[s][at]["at"] for s in bars},
                       "contract_ids": {s: bars[s][at]["contract_id"] for s in bars}}
        return tuple(values), diagnostics
    except (ValueError, TypeError, OverflowError):
        return None


def frame_features(candles, archive_frame, *, asset: str, samples, frames: dict, venues, pm_fidelity=1):
    """Same feature order and sampling rules as live_vector; all joins are exact."""
    import pandas as pd

    names = feature_names(venues)
    bars = sampled_bars(samples, asset=asset, pm_fidelity=pm_fidelity)
    market = pd.DataFrame({at: market_values(bars, at)
                           for at in bars["polymarket"]}).T
    market = market.reindex(columns=range(5))
    market.columns = MARKET_FEATURES
    result = archive_frame[list(BASE_FEATURES)].join(market)
    if venues:
        ret1, ret5 = [], []
        for venue in venues:
            key = f"{venue}|{asset}"
            if key not in frames:
                raise ValueError("missing pinned dispersion frame")
            f = frames[key][1].reindex(result.index)
            ret1.append(f["ret_1"])
            ret5.append(f["ret_5"])
        r1, r5 = pd.concat(ret1, axis=1), pd.concat(ret5, axis=1)
        valid = r1.notna().all(axis=1) & r5.notna().all(axis=1)
        result["venue_spread_ret1"] = (r1.max(axis=1) - r1.min(axis=1)).where(valid)
        result["venue_up_share"] = (r1.gt(0).sum(axis=1) / len(venues)).where(valid)
        result["venue_spread_ret5"] = (r5.max(axis=1) - r5.min(axis=1)).where(valid)
    return result[list(names)].astype(float)
