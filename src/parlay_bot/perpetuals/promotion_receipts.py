"""Promotion receipts for challenger families (spec §2.7, §4).

There was no Discord receipt path for promotion events: ``FamilyPromotion``
only logged, and ``recent_watches()`` silently substituted probabilities.
This module is the minimal receipt path, following the repo's existing
Discord-alert pattern: receipts are persisted rows (deduplicated, one per
family + stream + kind) and surfaced to Discord through the status card
(``research_alerts``), which posts to ``rival_status_channel_id`` when its
alert set changes. No new messaging channel is invented.

Two receipt kinds, for ``flow_v3`` in batch 2 and ``liq_v1`` in Phase 3:
- ``promoted``: the first ``promoted_paper`` write for the family + stream.
- ``first_serve``: the first time the family actually serves through
  ``recent_watches()``.

Every receipt carries the evidence summary (targets, days, trades,
coverage, both cost scenarios, ECE, top-bucket hit rate, calibration
status) and, for flow_v3 and liq_v1, the OKX-only liquidation coverage
disclosure (liq_v1 also discloses its Coinbase spot tape).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)

SCHEMA = """CREATE TABLE IF NOT EXISTS rival_family_promotion_receipts (
    family TEXT NOT NULL, venue TEXT NOT NULL, contract TEXT NOT NULL,
    quote TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
    kind TEXT NOT NULL, evaluated_at TEXT NOT NULL,
    receipt_text TEXT NOT NULL, evidence_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (family, venue, contract, quote, horizon_minutes, kind)
);"""

RECEIPT_FRESH_FOR = timedelta(hours=48)


def format_receipt_text(*, family, venue, contract, quote, horizon,
                        kind, evidence, calibration=None) -> str:
    """One Discord-ready receipt line from promotion evidence."""
    cost = (evidence or {}).get("cost_proof") or {}
    conservative = cost.get("conservative") or {}
    severe = cost.get("severe") or {}
    calibration = calibration or {}
    ece = (evidence or {}).get("ece")
    if ece is None:
        ece = calibration.get("ece")
    top_hit = calibration.get("top_bucket_hit_rate")
    top_n = calibration.get("top_bucket_n")
    event = ("promoted to paper" if kind == "promoted"
             else "first served" if kind == "first_serve" else kind)
    parts = [
        f"📡 RivaL {family} {event} on {venue} {contract}/{quote} {horizon}m",
        f"targets={(evidence or {}).get('targets')}",
        f"days={(evidence or {}).get('days')}",
        f"trades={(evidence or {}).get('challenger_trades')}",
        f"quote_coverage={(evidence or {}).get('quote_coverage')}",
        f"conservative_paired_gain_bps={conservative.get('paired_gain_bps')}",
        f"severe_paired_gain_bps={severe.get('paired_gain_bps')}",
        f"ece={ece}",
        f"top_bucket_hit_rate={top_hit} (n={top_n})",
        f"calibration={calibration.get('status', 'unevaluated')}",
    ]
    if family in ("flow_v3", "liq_v1"):
        parts.append("liquidations=OKX-only coverage")
    if family == "liq_v1":
        parts.append("coinbase=SPOT tape (basis context only, never perp)")
    return " | ".join(parts)


def initialize(store) -> None:
    with store.connect() as conn:
        conn.execute(SCHEMA)


def record_promotion_receipt(store, *, family, venue, contract, quote,
                             horizon, kind, evidence, calibration=None,
                             now=None) -> dict | None:
    """Persist a first-time receipt; returns it, or None if already recorded.

    Deduplication is by primary key, so repeated evaluations and repeated
    watch reads can call this freely: only the first one records.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    initialize(store)
    evidence = dict(evidence or {})
    calibration = dict(calibration or {})
    text = format_receipt_text(family=family, venue=venue, contract=contract,
                               quote=quote, horizon=horizon, kind=kind,
                               evidence=evidence, calibration=calibration)
    recorded = stamp.isoformat()
    with store.connect() as conn:
        result = conn.execute(
            """INSERT INTO rival_family_promotion_receipts
               (family,venue,contract,quote,horizon_minutes,kind,evaluated_at,
                receipt_text,evidence_json,recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO NOTHING""",
            (family, venue, contract, quote, horizon, kind,
             str(evidence.get("evaluated_at") or recorded), text,
             json.dumps({"evidence": evidence, "calibration": calibration},
                        sort_keys=True, allow_nan=False),
             recorded),
        )
        if result.rowcount == 0:
            return None
    _LOG.info("RIVAL_PROMOTION_RECEIPT %s", text)
    return {"family": family, "venue": venue, "contract": contract,
            "quote": quote, "horizon_minutes": horizon, "kind": kind,
            "receipt_text": text, "recorded_at": recorded}


def recent_receipts(store, *, now=None, family=None) -> list[dict]:
    """Receipts recorded within the last 48h, for the status card."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        initialize(store)
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT family,venue,contract,quote,horizon_minutes,kind,
                          receipt_text,recorded_at
                   FROM rival_family_promotion_receipts
                   WHERE recorded_at>=? ORDER BY recorded_at""",
                ((stamp - RECEIPT_FRESH_FOR).isoformat(),),
            ).fetchall()
    except Exception:  # noqa: BLE001 - a missing/unreadable table is no receipt, not a guess
        return []
    out = []
    for row in rows:
        if family is not None and str(row["family"]) != family:
            continue
        # Iterate .keys() deliberately: row types differ by backend (a
        # sqlite Row iterates values, a dict row iterates keys).
        out.append({key: row[key] for key in row.keys()})  # noqa: SIM118
    return out
