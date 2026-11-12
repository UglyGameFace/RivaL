"""Exact offline source-run outcomes, including failures and later recovery.

Source verification and model acceptance are separate outcomes. A rejected
model never changes a source into missing data; incomplete scopes stay visible
even when another scope produced a model. Original reports remain immutable.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

SCHEMA = """CREATE TABLE IF NOT EXISTS rival_source_research_runs (
    venue TEXT NOT NULL, asset TEXT NOT NULL, report_sha256 TEXT NOT NULL,
    requested_months_json TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
    missing_months_json TEXT NOT NULL, scope_count INTEGER NOT NULL,
    qualified_scopes INTEGER NOT NULL, scope_details_json TEXT NOT NULL,
    run_url TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY (venue,asset,report_sha256))"""


def _valid_months(months) -> bool:
    return (isinstance(months, list) and len(months) == 6
            and all(isinstance(m, str) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', m)
                    for m in months) and len(set(months)) == 6)


def source_run_receipt(report: dict) -> dict:
    venue = str(report.get("venue", ""))
    if not re.fullmatch(r"[a-z0-9_-]{2,24}", venue):
        raise ValueError("invalid source venue")
    asset = report.get("asset") or ""
    if asset not in ("", "BTC", "ETH", "SOL", "XRP"):
        raise ValueError("invalid source asset")
    months = report.get("requested_months") or report.get("months") or []
    if not _valid_months(months):
        raise ValueError("source receipt needs its exact six requested months")
    scopes = report.get("coverage_scopes", report.get("source_scopes")) or []
    missing = sorted({str(month) for scope in scopes
                      for month in scope.get("missing_or_incomplete_months", [])})
    if not scopes:
        missing = sorted(months)
    failed = report.get("status") == "run_failed"
    complete = bool(scopes) and not missing and not failed and all(
        scope.get('qualified', True) for scope in scopes)
    reason = (str(report.get("reason_code") or "source_check_failed") if failed else
              "no_source_scopes_restored" if not scopes else
              "source_partitions_incomplete" if missing else
              "source_quality_unqualified" if not complete else "source_verified")
    if not re.fullmatch(r"[a-z0-9_]{2,80}", reason):
        raise ValueError("source reason must be a code, not raw provider text")
    return {"venue": venue, "asset": asset, "requested_months": months,
            "status": "failed" if failed else "verified" if complete else "incomplete",
            "reason": reason, "missing_months": missing, "scope_count": len(scopes),
            "checked_at": report.get('checked_at'),
            "qualified_scopes": sum(not s.get("missing_or_incomplete_months")
                                    and s.get('qualified', True) for s in scopes),
            "scopes": [{key: scope.get(key) for key in
                        ('contract', 'quote', 'interval', 'missing_or_incomplete_months', 'qualified')}
                       for scope in scopes]}


def record_source_run(store, *, receipt: dict, report_sha256: str, run_url: str = "",
                      now: datetime | None = None) -> int:
    if not re.fullmatch(r"[0-9a-f]{64}", report_sha256):
        raise ValueError("invalid report digest")
    if run_url and not re.fullmatch(
            r"https://github.com/UglyGameFace/RivaL/actions/runs/\d+", run_url):
        raise ValueError("invalid first-party source run URL")
    # Recheck the wire receipt; never let arbitrary strings enter the card.
    if receipt.get("status") not in ("verified", "incomplete", "failed"):
        raise ValueError("invalid source status")
    if not re.fullmatch(r"[a-z0-9_-]{2,24}", receipt.get("venue", "")):
        raise ValueError("invalid source venue")
    if not re.fullmatch(r"[a-z0-9_]{2,80}", receipt.get("reason", "")):
        raise ValueError("invalid source reason")
    if receipt.get('asset') not in ('', 'BTC', 'ETH', 'SOL', 'XRP'):
        raise ValueError('invalid source asset')
    if not _valid_months(receipt.get('requested_months')):
        raise ValueError('invalid source months')
    missing = receipt.get('missing_months')
    scopes = receipt.get('scopes')
    if (not isinstance(missing, list)
            or any(m not in receipt['requested_months'] for m in missing)
            or not isinstance(scopes, list)
            or receipt.get('scope_count') != len(scopes)
            or not isinstance(receipt.get('qualified_scopes'), int)
            or not 0 <= receipt['qualified_scopes'] <= len(scopes)):
        raise ValueError('invalid source scope evidence')
    if receipt['status'] == 'verified' and (
            missing or not scopes or receipt['qualified_scopes'] != len(scopes)):
        raise ValueError('unverified scopes cannot clear a source blocker')
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    if receipt.get('checked_at'):
        checked = datetime.fromisoformat(receipt['checked_at'])
        if checked.utcoffset() is None or (checked - stamp).total_seconds() > 300:
            raise ValueError('invalid source observation clock')
        stamp = checked.astimezone(UTC)
    with store.connect() as conn:
        conn.execute(SCHEMA)
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_source_research_latest
            ON rival_source_research_runs(venue,asset,recorded_at)''')
        result = conn.execute("""INSERT INTO rival_source_research_runs VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (receipt['venue'], receipt['asset'], report_sha256,
             json.dumps(receipt['requested_months']), receipt['status'], receipt['reason'],
             json.dumps(receipt['missing_months']), receipt['scope_count'],
             receipt['qualified_scopes'], json.dumps(receipt['scopes']), run_url, stamp.isoformat()))
    return int(result.rowcount == 1)


def latest_runs(conn) -> dict[tuple[str, str], dict]:
    conn.execute(SCHEMA)
    rows = conn.execute("""SELECT * FROM (
        SELECT r.*,ROW_NUMBER() OVER (PARTITION BY venue,asset
          ORDER BY recorded_at DESC,report_sha256 DESC) AS position
        FROM rival_source_research_runs r) ranked WHERE position=1""").fetchall()
    latest = {}
    for row in rows:
        latest.setdefault((str(row['venue']), str(row['asset'])), dict(row))
    return latest


def supersedes_legacy(run: dict | None, blocked: dict) -> bool:
    """A different period or a partial venue cannot silently clear an old gap."""
    if not run or run['status'] != 'verified' or run['recorded_at'] < blocked['recorded_at']:
        return False
    months = set(json.loads(run['requested_months_json']))
    missing = set(json.loads(blocked['missing_months_json'] or '[]'))
    enough_scopes = run['scope_count'] >= (1 if run['asset'] else 4)
    return (len(months) == int(blocked['requested_months'])
            and missing <= months and enough_scopes)
