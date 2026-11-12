"""Publish tiny authenticated, read-only RivaL host/DB/Drive evidence receipts.

The public dashboard is never given Discloud credentials, PostgreSQL URLs,
Google OAuth tokens, raw reports, model weights, or private Drive file IDs.
Only a GitHub Actions main-branch job with a GitHub OIDC identity may publish.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from urllib import error, parse, request

from discloud_learning_cycle_receipt import CMD, parse_receipt

ASSETS = {"BTC", "ETH", "SOL", "XRP"}
HORIZONS = {5, 15, 60}
REPORT_NAME = re.compile(r"xmarket-v1-(BTC|ETH|SOL|XRP)-(5|15|60)m-report\.json")
INGEST_URL = "https://rival-evidence-collector.staytrippynlw.workers.dev/ingest"
AUDIENCE = "https://rival-evidence-collector.staytrippynlw.workers.dev"


def _url_json(url: str, *, method: str = "GET", body: bytes | None = None,
              headers: dict[str, str] | None = None, timeout: int = 25,
              limit: int = 500_000) -> dict:
    req = request.Request(url, data=body, method=method, headers=headers or {})
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("provider_response_too_large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise TypeError("provider_response_not_object")
        return result


def _nonnegative(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 10_000_000 else None


def summarize_host(report: dict | None) -> tuple[dict, dict]:
    """Only persisted, bounded host aggregates, with unknown distinct from 0."""
    host = {
        "status": "unavailable", "forecast_status": "read_unavailable",
        "learning_status": "read_unavailable", "forecast_issued": None,
        "learning_settled": None, "xmarket_fresh_inputs": None,
        "xmarket_expected_inputs": None, "xmarket_shadow_rows": None,
        "xmarket_imported_scopes": None,
    }
    db = {
        "status": "unavailable", "weight_updates_observed": None,
        "compared_streams": None, "qualified_paper_scopes": None,
        "evaluated_paper_scopes": None,
    }
    if not isinstance(report, dict):
        return host, db
    forecast, learning = report.get("forecast"), report.get("learning")
    forecast = forecast if isinstance(forecast, dict) else {}
    learning = learning if isinstance(learning, dict) else {}
    host["status"] = (
        "verified" if report.get("outcome") == "verified"
        and forecast.get("status") == "fresh"
        and learning.get("status") == "fresh"
        and learning.get("math_ok") is True
        and learning.get("model_review_ok") is True else "unverified"
    )
    host["forecast_status"] = str(forecast.get("status", "read_unavailable"))
    host["learning_status"] = str(learning.get("status", "read_unavailable"))
    host["forecast_issued"] = _nonnegative(forecast.get("issued"))
    host["learning_settled"] = _nonnegative(learning.get("settled"))

    xmarket = report.get("xmarket")
    if isinstance(xmarket, dict) and xmarket.get("status") != "read_unavailable":
        cov = xmarket.get("source_coverage")
        if isinstance(cov, dict):
            host["xmarket_fresh_inputs"] = _nonnegative(cov.get("fresh_scopes"))
            host["xmarket_expected_inputs"] = _nonnegative(cov.get("expected_scopes"))
        host["xmarket_shadow_rows"] = _nonnegative(xmarket.get("shadow_rows_24h"))
        if isinstance(xmarket.get("imported_scopes"), list):
            host["xmarket_imported_scopes"] = len(xmarket["imported_scopes"])

    lineage = report.get("actual_learning")
    if isinstance(lineage, dict) and lineage.get("status") not in (
        "read_unavailable", "unavailable", None
    ):
        db["status"] = "verified_read"
        db["weight_updates_observed"] = _nonnegative(
            lineage.get("streams_with_verified_weight_updates")
        )
        db["compared_streams"] = _nonnegative(lineage.get("streams_with_before_after"))
    paper = report.get("forward_proof")
    if isinstance(paper, dict) and paper.get("status") == "recorded":
        db["qualified_paper_scopes"] = _nonnegative(paper.get("qualified_paper_scopes"))
        db["evaluated_paper_scopes"] = _nonnegative(paper.get("evaluated_scopes"))
    return host, db


def read_host() -> dict | None:
    """Existing authenticated Discloud console action executes only the host's
    already shipped read-only receipt command. Never log the shell response."""
    token, app_id = os.getenv("DISCLOUD_TOKEN"), os.getenv("DISCLOUD_APP_ID")
    if not token or not app_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id):
        return None
    url = "https://api.discloud.app/v2/app/" + parse.quote(app_id, safe="") + "/console"
    raw = _url_json(
        url, method="PUT", body=json.dumps({"command": CMD}).encode("utf-8"),
        headers={"api-token": token, "Content-Type": "application/json"},
        timeout=110, limit=3_000_000,
    )
    stdout = raw.get("apps", {}).get("shell", {}).get("stdout", "")
    return parse_receipt(str(stdout))


def _oauth_access_token() -> str:
    parts = {
        "client_id": os.getenv("RIVAL_DRIVE_OAUTH_CLIENT_ID", ""),
        "client_secret": os.getenv("RIVAL_DRIVE_OAUTH_CLIENT_SECRET", ""),
        "refresh_token": os.getenv("RIVAL_DRIVE_OAUTH_REFRESH_TOKEN", ""),
        "grant_type": "refresh_token",
    }
    if not all(parts.values()):
        raise ValueError("drive_credentials_not_configured")
    result = _url_json(
        "https://oauth2.googleapis.com/token", method="POST",
        body=parse.urlencode(parts).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20, limit=50_000,
    )
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("drive_access_token_unavailable")
    return token


def _drive_json(path: str, token: str, *, limit: int = 400_000) -> dict:
    return _url_json(
        "https://www.googleapis.com/drive/v3/" + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        timeout=30, limit=limit,
    )


def summarize_xmarket_report(doc: dict, modified_at: str | None) -> dict | None:
    """Validate the model contract before making per-scope claims."""
    if not isinstance(doc, dict) or doc.get("schema") != "rival-xmarket-model-v1":
        return None
    asset, horizon = doc.get("asset"), doc.get("horizon_minutes")
    if asset not in ASSETS or horizon not in HORIZONS:
        return None
    checks = doc.get("data_checks")
    checks = checks if isinstance(checks, dict) else {}
    pm = checks.get("polymarket")
    kalshi = checks.get("kalshi")
    pm = pm if isinstance(pm, dict) else {}
    kalshi = kalshi if isinstance(kalshi, dict) else {}
    reason = pm.get("blocked_reason") or kalshi.get("blocked_reason")
    if reason not in (
        "no_prospective_export", "insufficient_overlap", "missing_history", "mismatch"
    ):
        failures = doc.get("data_errors") or []
        reason = (
            "http_429" if isinstance(failures, list) and any(
                isinstance(e, dict) and e.get("failure_code") == "http_429"
                for e in failures
            ) else "unverified"
        )
    status = doc.get("training_status")
    if status not in ("blocked", "fitted", "accepted", "rejected"):
        status = "unverified"
    window = doc.get("effective_window")
    return {
        "asset": asset, "horizon": horizon, "status": status,
        "pm_status": pm.get("status") if pm.get("status") in (
            "verified", "unverified", "blocked"
        ) else "unknown",
        "kalshi_status": kalshi.get("status") if kalshi.get("status") in (
            "verified", "unverified", "blocked"
        ) else "unknown",
        "pm_pairs": _nonnegative(pm.get("paired_points")),
        "kalshi_pairs": _nonnegative(kalshi.get("paired_points")),
        "eligible_rows": _nonnegative(window.get("rows")) if isinstance(window, dict) else None,
        "gate_passed": doc.get("gates_passed") is True,
        "report_modified_at": modified_at, "reason": reason,
    }


def read_drive() -> dict:
    """Read only the named reports folder, not the Drive root or arbitrary files."""
    out = {
        "status": "unavailable", "reports_considered": None,
        "latest_report_at": None, "reports": [],
    }
    folder = os.getenv("RIVAL_DRIVE_REPORTS_FOLDER_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", folder):
        return out
    token = _oauth_access_token()
    query = f"'{folder}' in parents and trashed = false and name contains 'xmarket-v1-'"
    found: dict[tuple[str, int], dict] = {}
    page_token = ""
    fully_listed = False
    for _ in range(5):
        params = {
            "q": query, "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
            "pageSize": "100", "orderBy": "modifiedTime desc",
        }
        if page_token:
            params["pageToken"] = page_token
        page = _drive_json("files?" + parse.urlencode(params), token)
        for f in page.get("files", []):
            if not isinstance(f, dict):
                continue
            name = f.get("name")
            match = REPORT_NAME.fullmatch(name) if isinstance(name, str) else None
            if not match or f.get("mimeType") not in ("application/json", "text/plain"):
                continue
            key = (match.group(1), int(match.group(2)))
            if key not in found:
                found[key] = f
        page_token = page.get("nextPageToken") or ""
        if not page_token:
            fully_listed = True
            break
    out["status"] = "verified_metadata" if fully_listed else "unavailable"
    if not fully_listed:
        return out
    out["reports_considered"] = len(found)
    if found:
        out["latest_report_at"] = max(
            (f.get("modifiedTime") or "" for f in found.values()), default=None
        ) or None
    all_parsed = True
    for (asset, horizon), info in sorted(found.items()):
        try:
            file_id = info["id"]
            if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", file_id):
                raise ValueError("invalid_drive_id")
            document = _drive_json(
                "files/" + parse.quote(file_id, safe="") + "?alt=media",
                token, limit=100_000,
            )
            item = summarize_xmarket_report(document, info.get("modifiedTime"))
            if item is None or (item["asset"], item["horizon"]) != (asset, horizon):
                raise ValueError("mismatched_report_identity")
            out["reports"].append(item)
        except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
            all_parsed = False
    if all_parsed and len(out["reports"]) == len(found):
        out["status"] = "verified_reports"
    return out


def _github_oidc() -> str:
    url = os.getenv("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    token = os.getenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url.startswith("https://") or not token:
        raise ValueError("github_oidc_not_available")
    joiner = "&" if "?" in url else "?"
    answer = _url_json(
        url + joiner + parse.urlencode({"audience": AUDIENCE}),
        headers={"Authorization": "Bearer " + token}, timeout=15, limit=20000,
    )
    value = answer.get("value")
    if not isinstance(value, str) or not value:
        raise ValueError("github_oidc_token_missing")
    return value


def publish() -> int:
    host, db = summarize_host(None)
    try:
        host, db = summarize_host(read_host())
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        pass  # A failed read is unavailable, never zero.
    try:
        drive = read_drive()
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        drive = {"status": "unavailable", "reports_considered": None,
                 "latest_report_at": None, "reports": []}
    now = datetime.now(UTC).isoformat()
    payload = {
        "schema_version": 1, "captured_at": now,
        "host": host, "database": db, "drive": drive,
    }
    jwt = _github_oidc()
    result = _url_json(
        INGEST_URL, method="POST", body=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": "Bearer " + jwt, "Content-Type": "application/json"},
        timeout=20, limit=15000,
    )
    if result.get("status") not in ("accepted", "stale_ignored"):
        raise ValueError("receipt_not_accepted")
    # The only logged output is a bounded status. Never print provider output.
    print(json.dumps({
        "publication": result["status"], "host": host["status"],
        "database": db["status"], "drive": drive["status"],
        "drive_scopes": drive["reports_considered"],
        "source": "read-only", "no_trades_or_db_writes": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(publish())
    except (error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        print("Evidence-board publication unavailable:", type(exc).__name__)
        sys.exit(1)
