"""Gate source-authentic L2 snapshots -> conservative offline shadow features.

Original archived books are never assumed executable, continuously replayable,
or observable to our bot when the exchange originally recorded them. Only
source-labelled retrospective comparisons may consume these features.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.gate_l2_quality import (
    MAX_LINE,
    _number,
    inspect_snapshot_hour,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_features(raw: bytes, manifest: dict) -> list[dict]:
    """Use real source timestamps; never fill unobserved five-minute buckets."""
    if (not isinstance(manifest, dict)
        or manifest.get("schema_version") != "rival-gate-hourly-l2-original-v1"
        or manifest.get("venue") != "gate"
        or manifest.get("family") != "orderbooks_slice"
        or manifest.get("asset") not in ("BTC", "ETH", "SOL", "XRP")
        or manifest.get("contract") != f"{manifest['asset']}_USDT"
        or manifest.get("quote") != "USDT"
        or manifest.get("original_sha256") != _digest(raw)
        or manifest.get("original_file") !=
        f"gate-hourly-l2-{manifest['asset']}-orderbooks_slice-"
        f"{manifest.get('utc_hour')}--{_digest(raw)[:16]}.gz"
        or manifest.get("model_training_eligible") is not False
        or manifest.get("forward_model_promoted") is not False):
        raise HistoryImportError("untrusted Gate L2 raw/manifest identity")
    hour = manifest.get("utc_hour")
    # Schema validator bounds decompressed bytes, rows, timestamps, ids,
    # depth ordering and bid < ask. It does not prove continuous L2.
    quality = inspect_snapshot_hour(raw, hour=hour)
    if not quality["source_jsonl_snapshot_schema_verified"]:
        raise HistoryImportError("unverified Gate snapshot source schema")
    start = datetime.strptime(hour, "%Y%m%d%H").replace(tzinfo=UTC)
    bucket_last: dict[datetime, dict] = {}
    counts: dict[datetime, int] = {}
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        while line := stream.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE:
                raise HistoryImportError("unbounded Gate snapshot line")
            row = json.loads(line)
            instant = datetime.fromtimestamp(float(row["current"]), UTC)
            bucket = instant.replace(minute=instant.minute // 5 * 5,
                                     second=0, microsecond=0)
            if not start <= bucket < start + timedelta(hours=1):
                raise HistoryImportError("snapshot outside exact Gate hour")
            bids, asks = row["bids"][:5], row["asks"][:5]
            bid_size = sum((_number(x["s"]) for x in bids), Decimal(0))
            ask_size = sum((_number(x["s"]) for x in asks), Decimal(0))
            bid, ask = _number(row["bids"][0]["p"]), _number(row["asks"][0]["p"])
            spread = float((ask - bid) * Decimal(20000) / (ask + bid))
            imbalance = float((bid_size - ask_size) / (bid_size + ask_size))
            if not (math.isfinite(spread) and 0 < spread < 1000
                    and math.isfinite(imbalance) and -1 <= imbalance <= 1):
                raise HistoryImportError("invalid Gate normalized depth feature")
            counts[bucket] = counts.get(bucket, 0) + 1
            bucket_last[bucket] = {
                "source_at": instant.isoformat(),
                "source_snapshot_id": row["id"],
                "last_snapshot_spread_bps": round(spread, 9),
                "l2_top5_imbalance": round(imbalance, 9),
            }
    rows = []
    for bucket, data in sorted(bucket_last.items()):
        # This is a nominal retrospective cutoff, NOT an actual original
        # publication timestamp or a verified live market quote.
        rows.append({
            "venue": "gate", "asset": manifest["asset"],
            "contract": manifest["contract"], "quote": "USDT",
            "source_hour": hour, "source_at": data["source_at"],
            "event_bucket_open": bucket.isoformat(),
            "available_after": (bucket + timedelta(minutes=5)).isoformat(),
            "features": {
                "l2_snapshot_count": counts[bucket],
                "last_snapshot_spread_bps": data["last_snapshot_spread_bps"],
                "l2_top5_imbalance": data["l2_top5_imbalance"],
                "last_snapshot_id": data["source_snapshot_id"],
            },
            "live_observation_time_verified": False,
            "continuous_orderbook_replay_verified": False,
        })
    return rows


def write_sidecar(raw_path: Path, manifest_path: Path) -> tuple[Path, Path]:
    """Immutable local source-matched derived bytes and research-only receipt."""
    if manifest_path.name != raw_path.name + ".manifest.json":
        raise HistoryImportError("Gate source/manifest path mismatch")
    if raw_path.stat().st_size > 24 * 1024 * 1024:
        raise HistoryImportError("Gate original above offline research byte limit")
    raw = raw_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw_path.name != manifest.get("original_file"):
        raise HistoryImportError("source file not authenticated by manifest")
    rows = derive_features(raw, manifest)
    encoded = ("".join(json.dumps(row, sort_keys=True, allow_nan=False)
                       + "\n" for row in rows)).encode()
    sidecar = raw_path.with_name(raw_path.name + ".research.ndjson")
    receipt = raw_path.with_name(raw_path.name + ".research.receipt.json")
    info = {
        "schema_version": "gate-l2-snapshot-shadow-features-v1",
        "venue": "gate", "asset": manifest["asset"],
        "contract": manifest["contract"], "quote": manifest["quote"],
        "source_hour": manifest["utc_hour"],
        "source_original_sha256": _digest(raw),
        "source_manifest_sha256": _digest(manifest_path.read_bytes()),
        "derived_sha256": _digest(encoded), "derived_bucket_count": len(rows),
        "observed_snapshots": sum(x["features"]["l2_snapshot_count"] for x in rows),
        "full_utc_hour_covered": len(rows) == 12,
        "l2_sequence_continuity_verified": False,
        "original_source_availability_verified": False,
        "source_contract_multiplier_verified": False,
        "prospective_forecasts": 0,
        "after_cost_edge_verified": False,
        "live_model_import_allowed": False,
        "model_promoted": False,
    }
    receipt_bytes = (json.dumps(info, sort_keys=True, indent=2) + "\n").encode()
    for path, data in ((sidecar, encoded), (receipt, receipt_bytes)):
        if path.exists() and path.read_bytes() != data:
            raise HistoryImportError("immutable Gate L2 feature conflict")
        if not path.exists():
            path.write_bytes(data)
    return sidecar, receipt


def load_sidecars(receipt_paths: list[Path], *, contract: str) -> list:
    """Rehash source, manifest, feature bytes before any offline comparison."""
    if not receipt_paths:
        raise HistoryImportError("no Gate L2 shadow feature receipts")
    from parlay_bot.research.learning import _as_datetime

    result = {}
    for path in receipt_paths:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        name = path.name.removesuffix(".research.receipt.json")
        root = path.parent
        raw, manifest, sidecar = (
            root / name, root / (name + ".manifest.json"),
            root / (name + ".research.ndjson"),
        )
        if (name == path.name or receipt.get("schema_version") !=
            "gate-l2-snapshot-shadow-features-v1"
            or receipt.get("venue") != "gate"
            or receipt.get("contract") != contract
            or receipt.get("live_model_import_allowed") is not False
            or receipt.get("model_promoted") is not False
            or not raw.exists() or not sidecar.exists() or not manifest.exists()
            or _digest(raw.read_bytes()) != receipt.get("source_original_sha256")
            or _digest(manifest.read_bytes()) != receipt.get("source_manifest_sha256")
            or _digest(sidecar.read_bytes()) != receipt.get("derived_sha256")):
            raise HistoryImportError("Gate shadow receipt source verification failed")
        rows = derive_features(
            raw.read_bytes(), json.loads(manifest.read_text(encoding="utf-8")),
        )
        if len(rows) != receipt.get("derived_bucket_count"):
            raise HistoryImportError("Gate shadow bucket count mismatch")
        observed_bytes = ("".join(json.dumps(row, sort_keys=True, allow_nan=False)
                                  + "\n" for row in rows)).encode()
        if _digest(observed_bytes) != receipt["derived_sha256"]:
            raise HistoryImportError("Gate shadow features modified after source read")
        for row in rows:
            cutoff = _as_datetime(row["available_after"])
            source_at = _as_datetime(row["source_at"])
            if not _as_datetime(row["event_bucket_open"]) < source_at < cutoff:
                raise HistoryImportError("Gate L2 feature has future information")
            f = row["features"]
            spread = float(f["last_snapshot_spread_bps"])
            imbalance = float(f["l2_top5_imbalance"])
            if cutoff in result:
                raise HistoryImportError("duplicate Gate L2 feature time")
            result[cutoff] = (min(4.0, spread / 20), imbalance)
    return sorted(result.items())


def evaluate_shadow(candle_manifests: list[Path], receipts: list[Path],
                    *, horizon: int) -> dict:
    """Same-paired outcomes retrospective ablation; never production approval."""
    from parlay_bot.research.learning import assemble
    from parlay_bot.research.live_training import (
        compatible_partitions,
        historical_features,
    )
    from parlay_bot.research.okx_auxiliary_ablation import _asof, _fit, _scores

    identity, candles, provenance = assemble(candle_manifests)
    if (identity["venue"] != "gate" or identity["interval_minutes"] != 5
        or len(provenance["months"]) < 6
        or provenance["gaps_between_rows"] != 0):
        raise HistoryImportError("six continuous authentic Gate candle months needed")
    series = load_sidecars(receipts, contract=identity["contract"])
    train, valid, test = compatible_partitions(
        candles, historical_features(candles, identity),
        horizon=horizon, identity=identity,
    )
    datasets = [_asof(rows, series, module="orderbook_l2_400")
                for rows in (train, valid, test)]
    paired = sum(len(rows) for rows in datasets)
    total = len(train) + len(valid) + len(test)
    if (total == 0 or paired / total < .80 or len(datasets[0]) < 300
        or min(map(len, datasets[1:])) < 100):
        raise HistoryImportError("Gate L2 future-safe retrospective coverage insufficient")
    incumbent, base = _fit(datasets[0], with_aux=False)
    augmented, aug_base = _fit(datasets[0], with_aux=True)
    scores = {}
    for name, cohort in zip(("validation", "test"), datasets[1:]):
        old = _scores(cohort, incumbent, base, with_aux=False)
        new = _scores(cohort, augmented, aug_base, with_aux=True)
        scores[name] = {
            "incumbent": old, "augmented": new,
            "paired_brier_gain": round(old["brier"] - new["brier"], 8),
        }
    return {
        "schema_version": "gate-l2-snapshot-shadow-ablation-v1",
        "identity": identity, "horizon_minutes": horizon,
        "original_snapshot_receipts": len(receipts),
        "paired_coverage": round(paired / total, 6),
        "validation_count": len(datasets[1]), "test_count": len(datasets[2]),
        "scores": scores,
        "retrospective_gain_supported": all(
            scores[name]["paired_brier_gain"] > .005
            for name in ("validation", "test")
        ),
        "source_event_available_at_original_time_proven": False,
        "prospective_outcomes": 0, "after_cost_edge_verified": False,
        "live_model_import_allowed": False, "promoted_live": False,
    }
