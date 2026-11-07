"""Liquidity backfill converter (Track A, spec §2.10.1).

Goal: produce history in the live recorder's bucket shape
(``perpetuals.liquidity_record`` ``COLUMNS``) from the depth/trades
archives that already exist, so an offline model can train on depth/tape
features at all. This module ships a converter and a validation report,
not a model.

Hard rules (spec §2.10.1, verified, not negotiable):
- Converted rows are sidecars, never live rows. Nothing here touches
  ``perp_liquidity_5m`` or any production table: this module takes no
  store, only bytes/rows in and files out. Each converted row carries
  source identity (venue, family, source SHA-256, converter version,
  per-bucket availability) and ``model_training_eligible: False`` until
  the validation report passes.
- Per-field labels, measured against the live bucket shape: ``exact``
  where the archive carries the same quantity, ``approximation``
  (labelled as such, never as exact) where it does not, ``unavailable``
  where the archive cannot produce the field. Nothing is zero-filled,
  interpolated, or invented: a bucket the archive did not sample
  produces no row, and a field the archive cannot produce is None.
- Gate ``orderbooks_slice`` first: the existing ``derive_features``
  already produces exactly the two quantities the slice family can
  carry (last-snapshot spread, top-5 imbalance); the converter maps
  them into live-bucket shape and labels them. OKX L2 replays through
  the existing ``ArchivedBook`` (a delta stream with no anchor snapshot
  is unusable, not assumable). OKX/Tardis trades produce exact tape
  buckets (buy/sell volume and trade counts per 5 m bucket).
- OKX L2_400 day size: a single day was advertised at 478.63 MB. The
  converter refuses any OKX L2 day above the caller-stated byte budget
  unless the caller passes explicit per-batch approval
  (``approve_large_day=True``). The budget must be stated: a missing
  budget is an error, never an implicit pass.
- No liquidation backfill exists in this track. Liquidation features
  train only on live history from 2026-11-04. This is a boundary, not
  a TODO: no function here produces liquidation fields.

Validation (spec §2.10.1): ``overlap_report`` compares converted rows
against live-recorded buckets on overlapping days, per field: exact
fields are checked for agreement, approximation fields for bias and
correlation, never equality. After validation, the converted features
must pass the retrospective ablation precedent
(``gate_l2_shadow_features.evaluate_shadow`` for Gate,
``okx_auxiliary_ablation.evaluate_ablation`` for OKX: 6 continuous
candle months, paired coverage >= 0.80, train >= 300, validation/test
>= 100, paired Brier gain > 0.005 on validation AND test) before they
enter any training set. The ablation is reused, not reimplemented:
this module does not contain one.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_l2_shadow_features import derive_features
from parlay_bot.research.okx_l2_replay import ArchivedBook

CONVERTER_VERSION = "rival-liquidity-converter-v1"
SCHEMA_VERSION = "rival-liquidity-converted-buckets-v1"
REPORT_SCHEMA = "rival-liquidity-overlap-report-v1"

# Live bucket columns (liquidity_record.COLUMNS) the converter maps to.
# Identity columns (venue/asset/bucket_start) are row keys; the rest are
# carried in "fields" with a per-field label.
LIVE_NUMERIC_FIELDS = (
    "complete",
    "book_samples",
    "spread_bps_mean",
    "spread_bps_max",
    "mid_last",
    "bid_depth_10bps_mean",
    "ask_depth_10bps_mean",
    "bid_depth_25bps_mean",
    "ask_depth_25bps_mean",
    "depth_25bps_covered_fraction",
    "buy_trades",
    "sell_trades",
    "buy_volume",
    "sell_volume",
    "first_event_at",
    "last_event_at",
    "recorded_at",
    "long_liq_volume",
    "short_liq_volume",
    "long_liq_count",
    "short_liq_count",
)
# Live columns that are measured quantities (numeric) and therefore
# comparable in the overlap report. Timestamps (recorded_at,
# first/last_event_at) are identity/cutoff fields, not measurements:
# they are carried for provenance, never compared as numbers.
COMPARABLE_FIELDS = tuple(
    name for name in LIVE_NUMERIC_FIELDS
    if name not in ("recorded_at", "first_event_at", "last_event_at"))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bucket_start(instant: datetime) -> datetime:
    return instant.astimezone(UTC).replace(
        minute=instant.minute - instant.minute % 5, second=0, microsecond=0)


def _base_row(*, venue, asset, contract, source_family, source_sha256,
              bucket_start, available_after) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "venue": venue,
        "asset": asset,
        "contract": contract,
        "quote": "USDT",
        "bucket_start": bucket_start.isoformat(),
        "source_family": source_family,
        "source_sha256": source_sha256,
        "converter_version": CONVERTER_VERSION,
        # Converted rows are sidecars: never training-eligible until the
        # validation report passes, never readable as live-recorded rows.
        "model_training_eligible": False,
        "available_after": available_after.isoformat(),
        "fields": {name: None for name in LIVE_NUMERIC_FIELDS},
        "labels": {name: "unavailable" for name in LIVE_NUMERIC_FIELDS},
        "extra": {},
    }


def _set(row: dict, field: str, value, label: str) -> None:
    row["fields"][field] = value
    row["labels"][field] = label


def convert_gate_slice(raw: bytes, manifest: dict) -> list[dict]:
    """Gate ``orderbooks_slice`` hour -> live-bucket-shaped sidecar rows.

    ``derive_features`` verifies the raw/manifest identity and the
    snapshot schema and returns one row per sampled 5 m bucket (never
    the unsampled buckets). This maps those rows into live-bucket shape:

    - ``spread_bps_mean``: APPROXIMATION from ``last_snapshot_spread_bps``
      (last snapshot in the bucket, not the live mean over 500 ms samples).
    - ``book_samples``: EXACT, the snapshot count in the bucket.
    - ``recorded_at``: EXACT, the retrospective cutoff (bucket + 5 m).
    - ``last_event_at``: APPROXIMATION, the last snapshot's source time
      (the bucket's last event is not recorded by the sidecar).
    - ``complete``: UNAVAILABLE. The live flag requires first/last event
      within 30 s + samples > 0; the sidecar reports only whether all
      twelve buckets were sampled, which is not the live flag.
    - Depth means, tape, mid, liquidations: UNAVAILABLE. The slice
      family carries no trades and no depth means (top-5 imbalance is
      not a 10/25 bps depth mean).
    - ``extra`` carries ``l2_top5_imbalance`` and ``l2_snapshot_count``
      (the quantities the ablation consumes).
    """
    derived = derive_features(raw, manifest)
    asset = manifest["asset"]
    contract = manifest["contract"]
    source_sha256 = manifest["original_sha256"]
    rows = []
    for item in derived:
        bucket = datetime.fromisoformat(item["event_bucket_open"])
        row = _base_row(
            venue="gate", asset=asset, contract=contract,
            source_family="gate/orderbooks_slice",
            source_sha256=source_sha256, bucket_start=bucket,
            available_after=datetime.fromisoformat(item["available_after"]),
        )
        features = item["features"]
        _set(row, "spread_bps_mean",
             float(features["last_snapshot_spread_bps"]), "approximation")
        _set(row, "book_samples", int(features["l2_snapshot_count"]), "exact")
        _set(row, "recorded_at", item["available_after"], "exact")
        _set(row, "last_event_at", item["source_at"], "approximation")
        row["extra"] = {
            "l2_top5_imbalance": float(features["l2_top5_imbalance"]),
            "l2_snapshot_count": int(features["l2_snapshot_count"]),
        }
        rows.append(row)
    return rows


def convert_okx_l2(events, *, asset: str, contract: str, source_sha256: str,
                   day_bytes: int | None, max_day_bytes: int,
                   approve_large_day: bool = False,
                   max_levels: int = 400) -> list[dict]:
    """OKX L2 archive events -> live-bucket-shaped sidecar rows.

    ``events`` are dicts with ``ts`` (epoch milliseconds) plus the
    ``ArchivedBook`` event fields (``action``, ``seqId``, ``prevSeqId``,
    ``bids``, ``asks``). One book is replayed chronologically; per 5 m
    bucket the last successfully applied metrics are kept. A bucket with
    no successful apply produces no row. A delta stream with no anchor
    snapshot is unusable (the replay returns None until a snapshot
    anchors it), never assumed. A gap invalidates the book until the
    next snapshot; a nonmonotonic timestamp fails the conversion.

    Field mapping mirrors the Gate converter: spread is an approximation
    from the last replayed snapshot (not a live mean), ``book_samples``
    is an approximation (count of applied replay events, not live 500 ms
    samples), tape/depth-means/mid/liquidations are unavailable.

    Day-size guard: ``day_bytes`` is the size of the archive day being
    converted and ``max_day_bytes`` is the caller-stated per-day budget.
    A missing budget is an error. A day above budget converts only with
    explicit ``approve_large_day=True`` (per-batch approval).
    """
    if day_bytes is None:
        raise HistoryImportError("OKX L2 day byte budget must be stated")
    if max_day_bytes is None or int(max_day_bytes) <= 0:
        raise HistoryImportError("OKX L2 per-day byte budget must be positive")
    if int(day_bytes) > int(max_day_bytes) and not approve_large_day:
        raise HistoryImportError(
            f"OKX L2 day is {int(day_bytes)} bytes, above the {int(max_day_bytes)}"
            " byte per-day budget; pass approve_large_day=True for explicit"
            " per-batch approval (a single OKX L2_400 day was advertised at"
            " 478.63 MB)")
    book = ArchivedBook(max_levels=max_levels)
    per_bucket: dict[datetime, dict] = {}
    counts: dict[datetime, int] = {}
    gaps = 0
    for event in events:
        if not isinstance(event, dict) or "ts" not in event:
            raise HistoryImportError("OKX L2 event without timestamp")
        instant = datetime.fromtimestamp(float(event["ts"]) / 1000, UTC)
        bucket = _bucket_start(instant)
        before_gaps = book.gaps
        metrics = book.apply(event, int(event["ts"]))
        gaps += book.gaps - before_gaps
        if metrics is None:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        per_bucket[bucket] = {
            "source_at": instant.isoformat(),
            "spread": float(metrics["last_snapshot_spread_bps"]),
            "imbalance": float(metrics["l2_top5_imbalance"]),
        }
    rows = []
    for bucket, data in sorted(per_bucket.items()):
        row = _base_row(
            venue="okx", asset=asset, contract=contract,
            source_family="okx/orderbook_l2_400" if max_levels == 400
            else "okx/orderbook_l2_5000",
            source_sha256=source_sha256, bucket_start=bucket,
            available_after=bucket + timedelta(minutes=5),
        )
        _set(row, "spread_bps_mean", data["spread"], "approximation")
        _set(row, "book_samples", counts[bucket], "approximation")
        _set(row, "recorded_at",
             (bucket + timedelta(minutes=5)).isoformat(), "exact")
        _set(row, "last_event_at", data["source_at"], "approximation")
        row["extra"] = {
            "l2_top5_imbalance": data["imbalance"],
            "l2_snapshot_count": counts[bucket],
        }
        rows.append(row)
    return rows


def _tape_rows(rows, *, source_family, asset, contract, source_sha256) -> list[dict]:
    """Shared exact tape-bucket builder for trades converters."""
    buckets: dict[datetime, dict] = {}
    for item in rows:
        instant = datetime.fromtimestamp(float(item["timestamp"]) / 1000, UTC)
        bucket = _bucket_start(instant)
        slot = buckets.setdefault(bucket, {
            "buy_trades": 0, "sell_trades": 0,
            "buy_volume": 0.0, "sell_volume": 0.0,
        })
        side = item["side"]
        if side == "buy":
            slot["buy_trades"] += 1
            slot["buy_volume"] += float(item["amount"])
        elif side == "sell":
            slot["sell_trades"] += 1
            slot["sell_volume"] += float(item["amount"])
        else:
            raise HistoryImportError(f"unknown trade side: {side!r}")
    out = []
    for bucket, slot in sorted(buckets.items()):
        row = _base_row(
            venue=("okx" if source_family.startswith("okx") else "bitget"),
            asset=asset, contract=contract, source_family=source_family,
            source_sha256=source_sha256, bucket_start=bucket,
            available_after=bucket + timedelta(minutes=5),
        )
        # Tape buckets are exact: buy/sell volume and trade counts per
        # 5 m bucket, taker side per row. Everything else (spread, depth,
        # mid, liquidations, the live complete flag) is unavailable from
        # a trades-only archive.
        for field in ("buy_trades", "sell_trades", "buy_volume", "sell_volume"):
            _set(row, field, slot[field], "exact")
        _set(row, "recorded_at",
             (bucket + timedelta(minutes=5)).isoformat(), "exact")
        out.append(row)
    return out


def convert_okx_trades(rows, *, asset: str, contract: str,
                       source_sha256: str) -> list[dict]:
    """OKX ``trades`` archive rows -> exact tape-bucket sidecar rows.

    Input contract (build-time assumption, stated, not verified against
    the archive here -- spec §7 item 21): each row carries ``timestamp``
    (epoch milliseconds), ``side`` (``"buy"``/``"sell"``), ``price``,
    ``amount``. The Drive-job adapter maps the archive's columns onto
    this contract and the mapping is verified at the first real run.
    Rows that do not match the contract are refused, never coerced.
    """
    checked = []
    for item in rows:
        if not isinstance(item, dict):
            raise HistoryImportError("OKX trades row is not an object")
        for key in ("timestamp", "side", "price", "amount"):
            if key not in item:
                raise HistoryImportError(f"OKX trades row missing {key}")
        if item["side"] not in ("buy", "sell"):
            raise HistoryImportError(f"OKX trades row bad side: {item['side']!r}")
        for key in ("timestamp", "price", "amount"):
            value = float(item[key])
            if not math.isfinite(value):
                raise HistoryImportError(f"OKX trades row non-finite {key}")
        checked.append(item)
    return _tape_rows(checked, source_family="okx/trades", asset=asset,
                      contract=contract, source_sha256=source_sha256)


def convert_tardis_trades(rows, *, asset: str, contract: str,
                          source_sha256: str) -> list[dict]:
    """Tardis Bitget ``trades`` sample rows -> exact tape-bucket rows.

    Required columns (verified, spec §2.10.1): ``exchange``, ``symbol``,
    ``timestamp``, ``local_timestamp``, ``id``, ``side``, ``price``,
    ``amount``. Buckets partition by arrival timestamp
    (``local_timestamp``), as the vendor does; exchange clocks can
    differ, so ``timestamp`` is never used for partitioning. Coverage is
    sample days only (~3.3% of days): buckets outside the input produce
    no rows, never interpolated.
    """
    checked = []
    for item in rows:
        if not isinstance(item, dict):
            raise HistoryImportError("Tardis trades row is not an object")
        for key in ("exchange", "symbol", "timestamp", "local_timestamp",
                    "id", "side", "price", "amount"):
            if key not in item:
                raise HistoryImportError(f"Tardis trades row missing {key}")
        if str(item["exchange"]).lower() != "bitget":
            raise HistoryImportError("Tardis trades row is not Bitget")
        if str(item["symbol"]).upper() != contract.upper():
            raise HistoryImportError("Tardis trades row is not this contract")
        if item["side"] not in ("buy", "sell"):
            raise HistoryImportError(f"Tardis trades row bad side: {item['side']!r}")
        checked.append({**item, "timestamp": item["local_timestamp"]})
    return _tape_rows(checked, source_family="tardis/trades", asset=asset,
                      contract=contract, source_sha256=source_sha256)


# ---------- sidecar IO (immutable, hash-verified, Gate precedent) ----------

def write_converted_sidecar(rows: list[dict], *, source_family: str,
                            source_sha256: str, out_dir: Path,
                            name: str) -> tuple[Path, Path]:
    """Write converted rows + receipt; refuse to overwrite differing bytes.

    The receipt binds the sidecar bytes to the source SHA-256 and the
    converter version. ``model_training_eligible`` stays False until a
    validation report passes; nothing here can mark it eligible.
    """
    if not rows:
        raise HistoryImportError("no converted buckets to write")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    encoded = ("".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n"
                       for row in rows)).encode()
    sidecar = out_dir / f"{name}.research.ndjson"
    receipt_path = out_dir / f"{name}.research.receipt.json"
    label_counts: dict[str, int] = {}
    for row in rows:
        for label in row["labels"].values():
            label_counts[label] = label_counts.get(label, 0) + 1
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "converter_version": CONVERTER_VERSION,
        "venue": rows[0]["venue"],
        "asset": rows[0]["asset"],
        "contract": rows[0]["contract"],
        "source_family": source_family,
        "source_sha256": source_sha256,
        "derived_sha256": _digest(encoded),
        "derived_bucket_count": len(rows),
        "label_counts": label_counts,
        "model_training_eligible": False,
        "live_model_import_allowed": False,
        "model_promoted": False,
        "prospective_forecasts": 0,
        "after_cost_edge_verified": False,
    }
    receipt_bytes = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    for path, data in ((sidecar, encoded), (receipt_path, receipt_bytes)):
        if path.exists() and path.read_bytes() != data:
            raise HistoryImportError("immutable converted sidecar conflict")
        if not path.exists():
            path.write_bytes(data)
    return sidecar, receipt_path


def load_converted_sidecar(receipt_path: Path) -> list[dict]:
    """Re-hash the receipt and sidecar bytes before any use."""
    receipt_path = Path(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    name = receipt_path.name.removesuffix(".research.receipt.json")
    sidecar = receipt_path.parent / f"{name}.research.ndjson"
    if (receipt.get("schema_version") != SCHEMA_VERSION
            or receipt.get("converter_version") != CONVERTER_VERSION
            or receipt.get("model_training_eligible") is not False
            or receipt.get("live_model_import_allowed") is not False
            or not sidecar.exists()
            or _digest(sidecar.read_bytes()) != receipt.get("derived_sha256")):
        raise HistoryImportError("converted sidecar verification failed")
    rows = [json.loads(line) for line in
            sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != receipt.get("derived_bucket_count"):
        raise HistoryImportError("converted sidecar bucket count mismatch")
    for row in rows:
        if (row.get("schema_version") != SCHEMA_VERSION
                or row.get("converter_version") != CONVERTER_VERSION
                or row.get("source_sha256") != receipt.get("source_sha256")
                or row.get("model_training_eligible") is not False):
            raise HistoryImportError("converted sidecar row identity mismatch")
    return rows


# ---------- overlap validation report ----------

def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def overlap_report(live_rows: list[dict], converted_rows: list[dict], *,
                   exact_max_mean_abs_diff: float,
                   approx_min_correlation: float,
                   reference_venue: str | None = None) -> dict:
    """Per-field overlap report: converted vs live buckets, same days.

    ``live_rows`` are dicts in the live bucket shape (``venue``,
    ``asset``, ``bucket_start`` plus numeric live columns).
    ``converted_rows`` are converter rows. Joined on
    (venue, asset, bucket_start) by default. With ``reference_venue``
    set, live rows are restricted to that venue and joined on
    (asset, bucket_start): the Gate archive has no live recorder of its
    own, so its approximation fields are checked against the live spread
    of a reference venue for the same asset. Exact fields are never
    evaluated under a reference join (different venues' tape is not the
    same quantity); they are reported as not applicable. For every
    evaluated field: overlapping bucket count, mean difference
    (converted - live), mean absolute difference, and Pearson
    correlation. Exact fields are additionally checked for agreement
    (share within 1e-9, max absolute difference); approximation fields
    are checked for bias/correlation only, never equality. Thresholds
    are caller-set and recorded in the report; a field with no overlap
    or an undefined correlation fails. A converter that cannot produce
    this report does not ship.
    """
    if reference_venue is not None:
        live = {(str(r["asset"]), str(r["bucket_start"])): r
                for r in live_rows if str(r["venue"]) == reference_venue}
        key_of = lambda row: (str(row["asset"]), str(row["bucket_start"]))
    else:
        live = {(str(r["venue"]), str(r["asset"]), str(r["bucket_start"])): r
                for r in live_rows}
        key_of = lambda row: (str(row["venue"]), str(row["asset"]),
                              str(row["bucket_start"]))
    claimed: dict[str, str] = {}
    for row in converted_rows:
        for field, label in row["labels"].items():
            if field in COMPARABLE_FIELDS and label in ("exact", "approximation"):
                claimed[field] = label
    fields: dict[str, dict] = {}
    overall = True
    for field, kind in sorted(claimed.items()):
        if kind == "exact" and reference_venue is not None:
            # Not evaluated under a reference join (different venues'
            # tape is not the same quantity) -- and not a failure: the
            # report passes or fails on the fields it can evaluate.
            fields[field] = {
                "kind": kind,
                "overlapping_buckets": 0,
                "mean_difference": None,
                "mean_abs_difference": None,
                "correlation": None,
                "exact_agreement_fraction": None,
                "max_abs_difference": None,
                "passes": None,
                "reason": "exact agreement requires same-venue overlap",
            }
            continue
        pairs = []
        for row in converted_rows:
            match = live.get(key_of(row))
            if match is None:
                continue
            converted = row["fields"].get(field)
            observed = match.get(field)
            if converted is None or observed is None:
                continue
            pairs.append((float(converted), float(observed)))
        n = len(pairs)
        diffs = [c - o for c, o in pairs]
        mean_diff = sum(diffs) / n if n else None
        mean_abs_diff = sum(abs(d) for d in diffs) / n if n else None
        correlation = _pearson([c for c, _ in pairs],
                               [o for _, o in pairs]) if n else None
        entry: dict = {
            "kind": kind,
            "overlapping_buckets": n,
            "mean_difference": mean_diff,
            "mean_abs_difference": mean_abs_diff,
            "correlation": correlation,
        }
        if kind == "exact":
            entry["exact_agreement_fraction"] = (
                sum(1 for d in diffs if abs(d) <= 1e-9) / n if n else None)
            entry["max_abs_difference"] = max((abs(d) for d in diffs),
                                              default=None)
            passed = (n > 0 and mean_abs_diff is not None
                      and mean_abs_diff <= exact_max_mean_abs_diff)
        else:
            passed = (n >= 2 and correlation is not None
                      and correlation >= approx_min_correlation)
        entry["passes"] = bool(passed)
        if not passed:
            overall = False
        fields[field] = entry
    return {
        "schema_version": REPORT_SCHEMA,
        "converter_version": CONVERTER_VERSION,
        "thresholds": {
            "exact_max_mean_abs_diff": exact_max_mean_abs_diff,
            "approx_min_correlation": approx_min_correlation,
        },
        "live_reference_venue": reference_venue,
        "live_buckets": len(live_rows),
        "converted_buckets": len(converted_rows),
        "fields": fields,
        "passes": overall,
    }


# ---------- CLI ----------

def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    """Convert liquidity archives to sidecars and validate the overlap."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Liquidity backfill converter (Track A, spec §2.10.1).")
    sub = parser.add_subparsers(dest="command", required=True)

    gate = sub.add_parser("gate-slice", help="Convert a Gate orderbooks_slice hour.")
    gate.add_argument("--raw", required=True, help="Gate .gz hour file.")
    gate.add_argument("--manifest", required=True, help="Matching .manifest.json.")
    gate.add_argument("--out-dir", required=True)
    gate.add_argument("--name", help="Sidecar stem (default: raw file stem).")

    okx_l2 = sub.add_parser("okx-l2", help="Convert OKX L2 archive events (JSONL).")
    okx_l2.add_argument("--events", required=True, help="JSONL: ts + ArchivedBook fields.")
    okx_l2.add_argument("--asset", required=True)
    okx_l2.add_argument("--contract", required=True)
    okx_l2.add_argument("--source-sha256", required=True)
    okx_l2.add_argument("--day-bytes", type=int, required=True)
    okx_l2.add_argument("--max-day-bytes", type=int, required=True)
    okx_l2.add_argument("--approve-large-day", action="store_true",
                        help="Explicit per-batch approval for an oversized day.")
    okx_l2.add_argument("--max-levels", type=int, default=400, choices=(400, 5000))
    okx_l2.add_argument("--out-dir", required=True)
    okx_l2.add_argument("--name", required=True)

    okx_trades = sub.add_parser("okx-trades", help="Convert OKX trades rows (JSONL).")
    okx_trades.add_argument("--trades", required=True)
    okx_trades.add_argument("--asset", required=True)
    okx_trades.add_argument("--contract", required=True)
    okx_trades.add_argument("--source-sha256", required=True)
    okx_trades.add_argument("--out-dir", required=True)
    okx_trades.add_argument("--name", required=True)

    tardis = sub.add_parser("tardis-trades", help="Convert Tardis Bitget trades (JSONL).")
    tardis.add_argument("--trades", required=True)
    tardis.add_argument("--asset", required=True)
    tardis.add_argument("--contract", required=True)
    tardis.add_argument("--source-sha256", required=True)
    tardis.add_argument("--out-dir", required=True)
    tardis.add_argument("--name", required=True)

    overlap = sub.add_parser("overlap-report", help="Validate converted vs live buckets.")
    overlap.add_argument("--live", required=True, help="JSONL live bucket rows.")
    overlap.add_argument("--receipt", required=True, help="Converted sidecar receipt.")
    overlap.add_argument("--exact-max-mean-abs-diff", type=float, required=True)
    overlap.add_argument("--approx-min-correlation", type=float, required=True)
    overlap.add_argument("--reference-venue", default=None,
                         help="Live venue to compare against (Gate has no live "
                              "recorder; its approximation fields are checked "
                              "against a reference venue for the same asset).")
    overlap.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "gate-slice":
            raw_path = Path(args.raw)
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            rows = convert_gate_slice(raw_path.read_bytes(), manifest)
            name = args.name or raw_path.name.removesuffix(".gz")
            _, receipt = write_converted_sidecar(
                rows, source_family="gate/orderbooks_slice",
                source_sha256=manifest["original_sha256"],
                out_dir=Path(args.out_dir), name=name)
            print(json.dumps({"converted_buckets": len(rows),
                              "receipt": str(receipt)}, indent=2))
        elif args.command == "okx-l2":
            rows = convert_okx_l2(
                _read_jsonl(args.events), asset=args.asset, contract=args.contract,
                source_sha256=args.source_sha256, day_bytes=args.day_bytes,
                max_day_bytes=args.max_day_bytes,
                approve_large_day=args.approve_large_day,
                max_levels=args.max_levels)
            _, receipt = write_converted_sidecar(
                rows, source_family=("okx/orderbook_l2_400" if args.max_levels == 400
                                     else "okx/orderbook_l2_5000"),
                source_sha256=args.source_sha256, out_dir=Path(args.out_dir),
                name=args.name)
            print(json.dumps({"converted_buckets": len(rows),
                              "receipt": str(receipt)}, indent=2))
        elif args.command == "okx-trades":
            rows = convert_okx_trades(
                _read_jsonl(args.trades), asset=args.asset, contract=args.contract,
                source_sha256=args.source_sha256)
            _, receipt = write_converted_sidecar(
                rows, source_family="okx/trades",
                source_sha256=args.source_sha256, out_dir=Path(args.out_dir),
                name=args.name)
            print(json.dumps({"converted_buckets": len(rows),
                              "receipt": str(receipt)}, indent=2))
        elif args.command == "tardis-trades":
            rows = convert_tardis_trades(
                _read_jsonl(args.trades), asset=args.asset, contract=args.contract,
                source_sha256=args.source_sha256)
            _, receipt = write_converted_sidecar(
                rows, source_family="tardis/trades",
                source_sha256=args.source_sha256, out_dir=Path(args.out_dir),
                name=args.name)
            print(json.dumps({"converted_buckets": len(rows),
                              "receipt": str(receipt)}, indent=2))
        elif args.command == "overlap-report":
            live = _read_jsonl(args.live)
            converted = load_converted_sidecar(Path(args.receipt))
            report = overlap_report(
                live, converted,
                exact_max_mean_abs_diff=args.exact_max_mean_abs_diff,
                approx_min_correlation=args.approx_min_correlation,
                reference_venue=args.reference_venue)
            out = Path(args.out)
            out.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8")
            print(json.dumps(report, sort_keys=True, indent=2))
            return 0 if report["passes"] else 1
        return 0
    except (HistoryImportError, ValueError, KeyError, OSError) as exc:
        print(f"liquidity converter failed: {exc}")
        return 1
