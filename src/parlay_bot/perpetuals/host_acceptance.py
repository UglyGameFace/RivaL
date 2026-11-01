"""Single-console operator evidence for host watchdog and process restart.

Never promotes models or assumes a successful deployment means the host runs.
A restart proof needs a deliberate before/after mark and a distinct bot boot.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage.hot import RelationalHotStore

# Audited, fixed identifiers. They never come from command-line arguments.
_EVIDENCE_TABLES = (
    ("perp_forward_audit", (
        "venue", "contract", "quote", "horizon_minutes",
        "model_version", "reference_opened_at",
    ), "settled_at IS NOT NULL"),
    ("rival_model_shadow_forecasts", (
        "venue", "contract", "quote", "horizon_minutes",
        "family", "reference_opened_at",
    ), "settled_at IS NOT NULL"),
    ("rival_family_quote_shadow", (
        "venue", "contract", "quote", "horizon_minutes",
        "family", "reference_opened_at",
    ), "state='EXIT_QUOTED'"),
)


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return stamp.astimezone(UTC) if stamp.utcoffset() is not None else None


def _digest(row: dict) -> str:
    return hashlib.sha256(json.dumps(
        row, sort_keys=True, default=str, separators=(",", ":"),
    ).encode()).hexdigest()


def host_health(store: RelationalHotStore, *, now: datetime | None = None) -> dict:
    """Read durable watchdog state directly, without PostgreSQL tooling."""
    stamp = now or datetime.now(UTC)
    snap = RuntimeHealthMonitor(store).snapshot()
    checked = _parse(snap["checked_at"])
    age = (stamp - checked).total_seconds() if checked is not None else None
    heartbeat_ok = age is not None and 0 <= age <= 360
    issues = snap["issues"]
    if not heartbeat_ok:
        status = "UNVERIFIED"
    elif issues:
        status = "DEGRADED"
    else:
        status = "HEALTHY"
    return {
        "status": status,
        "watchdog_checked_at": snap["checked_at"],
        "watchdog_age_seconds": int(age) if age is not None else None,
        "boot_id": snap["boot_id"],
        "boot_started_at": snap["boot_started_at"],
        "open_incidents": issues,
        "note": (
            "This database check does not replace independent Discloud app "
            "status monitoring; a dead process cannot run its own alerts."
        ),
    }


def _initialize_baseline(store: RelationalHotStore) -> None:
    with store.connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS rival_host_acceptance_baseline (
                 baseline_id TEXT PRIMARY KEY,
                 captured_at TEXT NOT NULL,
                 snapshot_json TEXT NOT NULL
            )"""
        )


def capture_restart_baseline(
    store: RelationalHotStore, *, now: datetime | None = None,
) -> dict:
    """Mark committed settled/quoted evidence before a real process restart."""
    stamp = now or datetime.now(UTC)
    health = host_health(store, now=stamp)
    if health["boot_id"] is None or health["status"] == "UNVERIFIED":
        return {"status": "UNVERIFIED", "reason": "bot boot / heartbeat not established"}
    rows = []
    with store.connect() as conn:
        for table, keys, predicate in _EVIDENCE_TABLES:
            for item in conn.execute(
                f"SELECT * FROM {table} WHERE {predicate} "
                "ORDER BY issued_at DESC LIMIT 8",
            ).fetchall():
                row = dict(item)
                rows.append({
                    "table": table,
                    "key": {key: row[key] for key in keys},
                    "fingerprint": _digest(row),
                })
    if not rows or not any(
        r["table"] == "rival_model_shadow_forecasts" for r in rows
    ):
        return {"status": "UNVERIFIED", "reason": "no settled challenger records to mark"}
    snapshot = {
        "captured_at": stamp.isoformat(), "boot_id": health["boot_id"],
        "watchdog_checked_at": health["watchdog_checked_at"], "rows": rows,
    }
    _initialize_baseline(store)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_host_acceptance_baseline
               (baseline_id,captured_at,snapshot_json) VALUES ('operator',?,?)
               ON CONFLICT(baseline_id) DO UPDATE SET
               captured_at=excluded.captured_at,
               snapshot_json=excluded.snapshot_json""",
            (stamp.isoformat(), json.dumps(snapshot, sort_keys=True)),
        )
    return {
        "status": "BASELINE_SAVED", "captured_at": stamp.isoformat(),
        "boot_id": health["boot_id"], "frozen_records": len(rows),
        "next": "Restart only RivaL, then use --verify-restart after new forecasts settle.",
    }


def verify_restart_baseline(
    store: RelationalHotStore, *, now: datetime | None = None,
) -> dict:
    """Check same persisted rows, new bot boot, fresh watchdog and new issue."""
    stamp = now or datetime.now(UTC)
    _initialize_baseline(store)
    with store.connect() as conn:
        baseline = conn.execute(
            """SELECT snapshot_json FROM rival_host_acceptance_baseline
               WHERE baseline_id='operator'"""
        ).fetchone()
    if baseline is None:
        return {"status": "UNVERIFIED", "reason": "run --mark-restart before restarting"}
    snapshot = json.loads(str(baseline["snapshot_json"]))
    health = host_health(store, now=stamp)
    changed = []
    with store.connect() as conn:
        for item in snapshot["rows"]:
            table = item["table"]
            keys = item["key"]
            if table not in {t[0] for t in _EVIDENCE_TABLES}:
                raise ValueError("unexpected evidence table in saved snapshot")
            expected = next(spec[1] for spec in _EVIDENCE_TABLES if spec[0] == table)
            if set(keys) != set(expected):
                raise ValueError("unexpected evidence key in saved snapshot")
            conditions = " AND ".join(f"{key}=?" for key in expected)
            actual = conn.execute(
                f"SELECT * FROM {table} WHERE {conditions}",
                tuple(keys[k] for k in expected),
            ).fetchone()
            if actual is None or _digest(dict(actual)) != item["fingerprint"]:
                changed.append({"table": table, "key": keys})
        boot_started = _parse(health["boot_started_at"])
        baseline_started = _parse(snapshot["captured_at"])
        # An ordinary in-process forecast before the operator pressed Restart
        # is not evidence that a *new* bot process resumed production.
        new_since = (
            max(boot_started, baseline_started).isoformat()
            if boot_started is not None and baseline_started is not None
            else stamp.isoformat()
        )
        fresh = conn.execute(
            """SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts
               WHERE issued_at>?""",
            (new_since,),
        ).fetchone()
    different_boot = bool(
        health["boot_id"] and health["boot_id"] != snapshot["boot_id"]
        and (_parse(health["boot_started_at"]) or stamp)
        > (_parse(snapshot["captured_at"]) or stamp)
    )
    checked = _parse(health["watchdog_checked_at"])
    advanced_watchdog = checked is not None and checked > (
        _parse(snapshot["watchdog_checked_at"]) or stamp
    )
    new_forecasts = int(fresh["n"])
    passed = (
        different_boot and advanced_watchdog and health["status"] == "HEALTHY"
        and not changed and new_forecasts > 0
    )
    return {
        "status": "PASS" if passed else "UNVERIFIED",
        "original_records_checked": len(snapshot["rows"]),
        "original_records_changed": changed,
        "distinct_host_boot": different_boot,
        "watchdog_advanced": advanced_watchdog,
        "host_health": health["status"],
        "new_forecasts_since_mark": new_forecasts,
        "baseline_at": snapshot["captured_at"],
        "note": (
            "PASS proves retained sampled finalized rows and newly issued "
            "forecasts after a distinct RivaL boot. It is not model approval "
            "or independent off-host uptime monitoring."
        ),
    }
