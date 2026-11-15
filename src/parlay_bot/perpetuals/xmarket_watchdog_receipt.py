"""Read persisted XMarket watchdog checks, never run a new check for the observer.

Source-file parity and an empty incident list cannot prove that a running bot
executed the scope watchdog. Require its own recent census, a current process
boot and a fresh published heartbeat. All database access here is SELECT only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from parlay_bot.perpetuals.runtime_health import REGRESSION_STALE, _age

_STATUSES = frozenset({
    "invalid_import_clock", "import_grace", "insufficient_incumbents", "issuing", "silent",
})
_BASE = {"read_only": True, "does_not_prove_prediction_edge": True}


def read_xmarket_watchdog(store, now: datetime) -> dict:
    """Bounded positive execution evidence and durable silence/clear transitions.

    Missing, old, future-dated or previous-boot records stay unverified. Cached
    checks keep their original timestamp even when the main watchdog publishes
    again. Counts are operational rows, not independent evaluation outcomes.
    """
    try:
        with store.connect() as conn:
            census = conn.execute(
                "SELECT checked_at,payload_json FROM rival_runtime_diagnostics "
                "WHERE component='xmarket_issuance'"
            ).fetchone()
            if census is None:
                return {**_BASE, "status": "not_recorded", "operational_status": "unverified"}
            pulse = conn.execute(
                "SELECT checked_at FROM rival_runtime_health_checkpoints "
                "WHERE component='host_watchdog'"
            ).fetchone()
            boot = conn.execute(
                "SELECT boot_id,started_at FROM rival_runtime_health_boots "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            incidents = conn.execute(
                "SELECT issue_key,state,first_seen_at,last_seen_at,resolved_at,occurrence_count "
                "FROM rival_runtime_health_incidents WHERE issue_key LIKE 'xmarket:no_shadows:%' "
                "ORDER BY issue_key LIMIT 12"
            ).fetchall()

        saved = json.loads(census["payload_json"])
        if (not isinstance(saved, dict) or saved.get("policy") != "xmarket-issuance-v1"
                or saved.get("status") not in ("recorded", "not_initialized")
                or saved.get("checked_at") != census["checked_at"]
                or not isinstance(saved.get("scopes"), list) or len(saved["scopes"]) > 12):
            raise ValueError("invalid scope census")
        scopes, seen = [], set()
        for row in saved["scopes"]:
            scope = (row["asset"], row["horizon_minutes"])
            if (scope[0] not in ("BTC", "ETH", "SOL", "XRP") or scope[1] not in (5, 15, 60)
                    or scope in seen or row["status"] not in _STATUSES
                    or any(type(row[k]) is not int or row[k] < 0
                           for k in ("incumbent_rows", "shadow_rows"))):
                raise ValueError("invalid scope row")
            seen.add(scope)
            scopes.append({k: row[k] for k in (
                "asset", "horizon_minutes", "status", "incumbent_rows", "shadow_rows",
            )})
        window = {}
        if scopes:
            start_age = _age(now, saved.get("window_start"))
            end_age = _age(now, saved.get("window_end"))
            if start_age is None or end_age is None or start_age <= end_age:
                raise ValueError("invalid census window")
            window = {k: saved[k] for k in ("window_start", "window_end")}
        transitions = []
        for row in incidents:
            if (re.fullmatch(r"xmarket:no_shadows:(BTC|ETH|SOL|XRP):(5|15|60)", row["issue_key"])
                    is None or row["state"] not in ("OPEN", "RESOLVED")):
                raise ValueError("invalid incident")
            transitions.append(dict(row))

        scope_age = _age(now, census["checked_at"])
        pulse_age = _age(now, pulse["checked_at"]) if pulse else None
        boot_age = _age(now, boot["started_at"]) if boot else None
        fresh = (
            scope_age is not None and scope_age <= REGRESSION_STALE
            and pulse_age is not None and pulse_age <= timedelta(minutes=6)
            and boot_age is not None and scope_age <= boot_age
            and pulse_age <= scope_age and saved.get("boot_id") == boot["boot_id"]
        )
        open_count = sum(row["state"] == "OPEN" for row in transitions)
        if not fresh or saved["status"] != "recorded":
            operational = "unverified"
        elif open_count or any(row["status"] == "silent" for row in scopes):
            operational = "degraded"
        elif scopes and all(row["status"] == "issuing" for row in scopes):
            operational = "healthy"
        else:
            operational = "pending"
        return {
            **_BASE,
            "status": "fresh" if fresh else "stale_or_boot_unverified",
            "operational_status": operational,
            "census_status": saved["status"],
            **window,
            "scope_checked_at": census["checked_at"],
            "scope_age_seconds": int(scope_age.total_seconds()) if scope_age is not None else None,
            "watchdog_checked_at": pulse["checked_at"] if pulse else None,
            "boot_started_at": boot["started_at"] if boot else None,
            "fitted_scopes": len(scopes),
            "issuing_scopes": sum(row["status"] == "issuing" for row in scopes),
            "silent_scopes": sum(row["status"] == "silent" for row in scopes),
            "scopes": scopes,
            "open_silence_incidents": open_count,
            "resolved_silence_incidents": len(transitions) - open_count,
            "incidents": transitions,
        }
    except Exception:  # noqa: BLE001 - optional diagnostic cannot mask worker receipts
        return {**_BASE, "status": "read_unavailable", "operational_status": "unverified"}
