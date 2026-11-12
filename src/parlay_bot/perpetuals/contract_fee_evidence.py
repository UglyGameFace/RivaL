"""Read-only, fail-closed economic source receipts for prediction markets.

Never interpret an API fee type, a paper matched ask, or a canonical SHA256 as an
executed fill. Kalshi series/event overrides are source-specific and can change
between snapshots; this module does not auto-approve models or place orders.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

KALSHI_TYPES = {
    "quadratic", "quadratic_with_maker_fees",
    "quadratic_with_combo_maker_fees", "flat",
}
# Published general *taker* coefficient. Specific/flat fee tables and account-
# level rounding/rebates cannot be inferred from the metadata API.
KALSHI_GENERAL_TAKER_COEFFICIENT = Decimal("0.07")
SOURCE_MAX_AGE = timedelta(seconds=25)
POLY_PRICE_CEILING = Decimal("0.000001")


def _date(raw):
    if isinstance(raw, datetime):
        stamp = raw
    elif isinstance(raw, str):
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    return stamp.astimezone(UTC) if stamp.utcoffset() is not None else None


def _number(raw, *, max_value=10):
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return (float(raw) if math.isfinite(float(raw))
            and 0 <= raw <= max_value else None)


def _hash(obj):
    return hashlib.sha256(json.dumps(
        obj, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def resolve_kalshi_fee(*, series, event, series_changes, event_changes,
                       series_ticker: str, event_ticker: str, received_at: datetime,
                       expires_at: datetime) -> dict:
    """Resolve official series + event fee overrides with scheduled-change checks.

    The exchange's fee-change endpoints must return *complete* lists (cursor
    empty). They are queried at receipt time, never reconstructed from future
    market results. If any source is absent, mismatched, inconsistent or has an
    upcoming fee change before expiry, no numeric fee may qualify a paper edge.
    """
    pending = {"status": "fee_metadata_unavailable", "fee_verified": False}
    at, end = _date(received_at), _date(expires_at)
    if (at is None or end is None or at >= end
            or not re.fullmatch(r"[A-Z0-9-]{2,80}", series_ticker)
            or not re.fullmatch(r"[A-Z0-9-]{2,110}", event_ticker)
            or not all(isinstance(x, dict)
                       for x in (series, event, series_changes, event_changes))):
        return pending
    if (series.get("ticker") != series_ticker
            or event.get("event_ticker") != event_ticker
            or event.get("series_ticker") != series_ticker):
        return {**pending, "status": "fee_source_mismatch"}
    stype, smult = series.get("fee_type"), _number(series.get("fee_multiplier"))
    if stype not in KALSHI_TYPES or smult is None:
        return {**pending, "status": "unsupported_series_fee"}
    etype = event.get("fee_type_override")
    emult = event.get("fee_multiplier_override")
    if etype is None and emult is None:
        effective_type, effective_multiplier = stype, smult
        level = "series"
    elif (etype in KALSHI_TYPES and _number(emult) is not None):
        effective_type, effective_multiplier = etype, _number(emult)
        level = "event_override"
    else:
        return {**pending, "status": "invalid_event_override"}
    specs = (
        (series_changes, "series_fee_change_arr", "series_ticker", series_ticker),
        (event_changes, "event_fee_changes", "event_ticker", event_ticker),
    )
    for payload, field, key, ticker in specs:
        if payload.get("cursor") not in ("", None):
            return {**pending, "status": "fee_change_history_incomplete"}
        entries = payload.get(field)
        if not isinstance(entries, list):
            return {**pending, "status": "fee_change_history_unavailable"}
        for item in entries:
            if not isinstance(item, dict) or item.get(key) != ticker:
                return {**pending, "status": "fee_change_scope_mismatch"}
            when = _date(item.get("scheduled_ts"))
            if when is None:
                return {**pending, "status": "invalid_fee_change_clock"}
            if at <= when <= end:
                return {**pending, "status": "fee_change_during_contract"}
    source = {
        "series_ticker": series_ticker, "event_ticker": event_ticker,
        "series_type": stype, "series_multiplier": smult,
        "event_fee_type_override": etype, "event_fee_multiplier_override": emult,
        "fee_type": effective_type, "fee_multiplier": effective_multiplier,
        "level": level, "received_at": at.isoformat(),
        "expires_at": end.isoformat(),
        "series_change_receipt_sha256": _hash(series_changes),
        "event_change_receipt_sha256": _hash(event_changes),
        "series_receipt_sha256": _hash(series),
        "event_receipt_sha256": _hash(event),
    }
    return {
        "status": ("metadata_verified_fee_formula_unknown" if effective_type == "flat"
                   else "fee_metadata_verified"),
        "fee_verified": True,
        "fee_type": effective_type, "fee_multiplier": effective_multiplier,
        "source": source, "receipt_sha256": _hash(source),
        "fee_formula_verified": effective_type != "flat",
        "fills_verified": False, "account_rounding_verified": False,
        "after_cost_qualified": False,
    }


def kalshi_taker_paper_fee(*, price: float, quantity: float, fee_type: str,
                           multiplier: float) -> dict:
    """Estimate a single taker fill at a known general quadratic fee.

    Base fee is 6-decimal ceil per Kalshi's documented fee rounding. Account
    balance precision, fill fragmentation, rebates, fee waivers and execution
    remain unknown. Do not use this estimate as a trading approval.
    """
    if fee_type not in KALSHI_TYPES or fee_type == "flat":
        return {"status": "unsupported_specific_fee_table", "verified_after_cost": False}
    p, n, m = _number(price), _number(quantity, max_value=100000), _number(multiplier)
    if (p is None or not 0 < p < 1 or n is None or n <= 0 or m is None):
        return {"status": "invalid_fee_inputs", "verified_after_cost": False}
    fee = (KALSHI_GENERAL_TAKER_COEFFICIENT * Decimal(str(n))
           * Decimal(str(p)) * (1 - Decimal(str(p))) * Decimal(str(m)))
    fee = fee.quantize(POLY_PRICE_CEILING, rounding=ROUND_CEILING)
    return {
        "status": "paper_fee_estimate_only",
        "estimated_model_fee_usd": float(fee),
        "estimated_per_contract_fee_usd": float(fee / Decimal(str(n))),
        "model": "kalshi-general-taker-quadratic",
        "precision": "ceil_6dp_before_account_rounding",
        "verified_after_cost": False,
        "fills_verified": False,
    }


def polymarket_taker_paper_cost(*, side: str, token_id: str, quote_price: float,
                                 quote_depth: float, shares: float, fee_rate: float,
                                 fee_received_at: datetime, quote_received_at: datetime,
                                 issue_at: datetime) -> dict:
    """Same-token, same-quote paper ask plus official market fee-rate estimate.

    Caller must obtain the rate from official market metadata at/before issue,
    bind its receipt to this exact token, and not treat a hash as a signed
    exchange attestation. The function never grades a real fill.
    """
    issue, quote, fee_at = map(_date, (issue_at, quote_received_at, fee_received_at))
    if not all((issue, quote, fee_at)):
        return {"status": "missing_clock", "verified_after_cost": False}
    if (not isinstance(token_id, str) or not token_id.isdecimal()
            or not 1 <= len(token_id) <= 90
            or side not in ("UP", "DOWN")):
        return {"status": "wrong_token_or_side", "verified_after_cost": False}
    if (not 0 <= (issue - quote).total_seconds() <= 25
            or not 0 <= (issue - fee_at).total_seconds() <= 25):
        return {"status": "stale_fee_or_quote", "verified_after_cost": False}
    p, d, n, rate = (
        _number(quote_price), _number(quote_depth, max_value=100000),
        _number(shares, max_value=100000), _number(fee_rate),
    )
    if (p is None or not 0 < p < 1 or d is None or n is None or n <= 0
            or rate is None or rate > 1):
        return {"status": "invalid_quote_or_fee", "verified_after_cost": False}
    if d < n:
        return {"status": "insufficient_ask_depth", "verified_after_cost": False}
    fee = rate * p * (1 - p)
    return {
        "status": "same_token_paper_cost_estimate",
        "token_id": token_id, "side": side, "shares": n,
        "ask_price": p, "fee_rate": rate,
        "estimated_total_entry_usd": round(n * (p + fee), 8),
        "estimated_fee_usd": round(n * fee, 8),
        "estimated_cost_per_share": round(p + fee, 9),
        "verified_after_cost": False, "fills_verified": False,
        "maker_or_rebate_credit_counted": False,
    }
