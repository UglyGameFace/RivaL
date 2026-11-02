"""Causal, source-exact spike contexts for historical model challengers.

Never attach an anomaly discovered *after* a model prediction. Observations
do not become training targets just because their later market move is known.
"""
from __future__ import annotations

import json
import logging

LOG = logging.getLogger(__name__)
KINDS = ("price", "volume", "price_and_volume")


def spike_context_at_issuance(conn, *, asset: str, opened_at: str,
                              issued_at: str, venue: str, contract: str,
                              quote: str) -> dict | None:
    """Return an existing immutable spike only if this source was part of it."""
    try:
        item = conn.execute(
            """SELECT kind,detected_at,source_count,sources_json
               FROM perp_spike_events WHERE asset=? AND opened_at=?
                 AND detected_at<=?""",
            (asset, opened_at, issued_at),
        ).fetchone()
        if item is None or item["kind"] not in KINDS or item["source_count"] < 3:
            return None
        sources = json.loads(item["sources_json"])
        if not isinstance(sources, list) or not 3 <= len(sources) <= 12:
            return None
        if not any(
            isinstance(part, dict)
            and (part.get("venue"), part.get("contract"), part.get("quote"))
            == (venue, contract, quote)
            for part in sources
        ):
            return None
        return {
            "kind": str(item["kind"]),
            "detected_at": str(item["detected_at"]),
        }
    except (ValueError, TypeError, KeyError):
        LOG.warning("Invalid prospective spike provenance; no challenger context")
        return None
    except Exception:  # noqa: BLE001 - missing legacy schema must fail closed
        LOG.warning("Spike context unavailable; not assigning prospective regime")
        return None


def spike_score_rows(paired: list) -> list:
    """Only previously stamped, source-exact, future-settled challenger rows."""
    result = []
    for row in paired:
        if (row["spike_kind"] in KINDS
                and row["spike_detected_at"] is not None
                and row["spike_detected_at"] <= row["issued_at"]
                and row["settled_at"] > row["issued_at"]):
            result.append(row)
    return result
