"""Immutable native-history observations, collected by the existing venue loops.

No new service/auth system. PM's existing reader and Kalshi's existing
tracker supply their clients and verified contract identities. Network work
is async; database work is off the Discord event loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import statistics
from datetime import UTC, datetime, timedelta

from parlay_bot.research.supplemental_history import polymarket_history_points
from parlay_bot.research.xmarket_features import PRICE_SEMANTICS, timestamp, validate_sample

BUNDLE_SCHEMA = "rival-xmarket-inputs-v1"
BUNDLE_PREFIX = "xmarket-inputs-"
POLY_HISTORY = "https://clob.polymarket.com/prices-history"


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def parse_points(source: str, payload: dict, *, identity: dict, observed_at: datetime,
                 origin: str = "live", fidelity: int = 1) -> list[dict]:
    """One parser shared by prospective captures and historical verification."""
    if source not in PRICE_SEMANTICS:
        raise ValueError("unsupported XMarket history source")
    sha = hashlib.sha256(canonical(payload)).hexdigest()
    points = payload.get("history" if source == "polymarket" else "candlesticks")
    if not isinstance(points, list) or len(points) > 100_000:
        raise ValueError("invalid native price history")
    if source == "polymarket":
        points = polymarket_history_points(payload, allow_boundaries=True)
    effective_fidelity = fidelity
    if source == "polymarket" and len(points) > 1:
        times = [p.get("t") for p in points]
        if all(type(t) is int for t in times):
            spacing = statistics.median(b - a for a, b in itertools.pairwise(times)) / 60
            effective_fidelity = next((n for n in (1, 5, 15) if n >= max(fidelity, spacing)), 15)
    output = []
    previous = None
    for p in points:
        raw_at = p.get("t" if source == "polymarket" else "end_period_ts")
        if type(raw_at) is not int:
            raise ValueError("invalid native point timestamp")
        at = datetime.fromtimestamp(raw_at, UTC)
        if previous is not None and at <= previous:
            raise ValueError("native history is not strictly ordered")
        previous = at
        if not timestamp(identity["opens_at"]) <= at <= timestamp(identity["closes_at"]):
            continue
        if at > observed_at:
            raise ValueError("future native price point")
        if source == "polymarket":
            price, bid, ask = p.get("p"), None, None
        else:
            def close(side, point=p):
                value = point.get(side) or {}
                raw = value.get("close_dollars", value.get("close"))
                if isinstance(raw, bool):
                    raise TypeError("boolean native price")
                return None if raw is None else float(raw)
            bid, ask = close("yes_bid"), close("yes_ask")
            price = ask
            if price is None:
                continue
        if isinstance(price, bool):
            raise TypeError("boolean native price")
        row = {**identity, "source": source, "at": at.isoformat(), "price": float(price),
               "observed_at": observed_at.isoformat(), "price_semantics": PRICE_SEMANTICS[source],
               "provider_sha256": sha, "origin": origin, "fidelity_minutes": effective_fidelity,
               "requested_fidelity_minutes": fidelity,
               "yes_bid": bid, "yes_ask": ask}
        validate_sample(row)
        output.append(row)
    return output


class XmarketInputs:
    def __init__(self, store):
        self.store = store
        self._ready = False
        self._attempts: dict[tuple, datetime] = {}

    def initialize(self):
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rival_xmarket_observations (
                    asset TEXT NOT NULL, source TEXT NOT NULL, contract_id TEXT NOT NULL,
                    at TEXT NOT NULL, observed_at TEXT NOT NULL, sample_json TEXT NOT NULL,
                    PRIMARY KEY(source,contract_id,at));
                CREATE INDEX IF NOT EXISTS idx_xmarket_inputs_scope
                    ON rival_xmarket_observations(asset,source,at);
                CREATE TABLE IF NOT EXISTS rival_xmarket_live_venues (
                    asset TEXT NOT NULL, venue TEXT NOT NULL, observed_at TEXT NOT NULL,
                    PRIMARY KEY(asset,venue));
                CREATE TABLE IF NOT EXISTS rival_xmarket_exports (
                    asset TEXT NOT NULL, day TEXT NOT NULL, drive_file_id TEXT NOT NULL,
                    PRIMARY KEY(asset,day));
            """)
        self._ready = True

    def record(self, points) -> int:
        points = list(points)
        for point in points:
            validate_sample(point)
        self.initialize()
        saved = 0
        with self.store.connect() as conn:
            for offset in range(0, len(points), 100):
                batch = points[offset:offset + 100]
                values = [(p["asset"], p["source"], p["contract_id"], p["at"],
                           p["observed_at"], canonical(p).decode()) for p in batch]
                saved += conn.execute(
                    "INSERT INTO rival_xmarket_observations "
                    "(asset,source,contract_id,at,observed_at,sample_json) VALUES "
                    + ",".join("(?,?,?,?,?,?)" for _ in values) + " ON CONFLICT DO NOTHING",
                    tuple(value for row in values for value in row)).rowcount
            groups = {}
            for p in points:
                groups.setdefault((p["source"], p["contract_id"]), []).append(p)
            for (source, contract), group in groups.items():
                rows = conn.execute("SELECT at,sample_json FROM rival_xmarket_observations "
                    "WHERE source=? AND contract_id=? AND at>=? AND at<=?",
                    (source, contract, min(p["at"] for p in group), max(p["at"] for p in group))).fetchall()
                existing = {r["at"]: json.loads(r["sample_json"]) for r in rows}
                for p in group:
                    previous = existing[p["at"]]
                    if any(previous.get(k) != p.get(k) for k in
                           ("price", "asset", "opens_at", "closes_at", "price_semantics")):
                        raise ValueError("native history revised an immutable XMarket observation")
        return saved

    def recent(self, asset: str, *, at: datetime, now: datetime) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT sample_json FROM rival_xmarket_observations WHERE asset=? "
                "AND at>=? AND at<=? AND observed_at<=? ORDER BY at",
                (asset, (at - timedelta(minutes=61)).isoformat(), at.isoformat(), now.isoformat()),
            ).fetchall()
        return [json.loads(r["sample_json"]) for r in rows]

    def note_venues(self, streams, *, now: datetime):
        from parlay_bot.perpetuals.markets import asset_for_contract
        from parlay_bot.research.xmarket_features import VENUES

        self.initialize()
        with self.store.connect() as conn:
            for venue, contract, _ in streams:
                asset = asset_for_contract(contract)
                if venue in VENUES and asset is not None:
                    conn.execute("INSERT INTO rival_xmarket_live_venues VALUES (?,?,?) "
                                 "ON CONFLICT(asset,venue) DO UPDATE SET observed_at=excluded.observed_at",
                                 (asset, venue, now.isoformat()))

    def due(self, source: str, contract: str, now: datetime) -> bool:
        self._attempts = {k: v for k, v in self._attempts.items()
                          if now - v < timedelta(minutes=20)}
        key = (source, contract)
        if key in self._attempts and now - self._attempts[key] < timedelta(seconds=55):
            return False
        self._attempts[key] = now
        return True

    def _previous_pm(self, asset, clock):
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute("SELECT sample_json FROM rival_xmarket_observations "
                "WHERE asset=? AND source='polymarket' AND at>=? ORDER BY at DESC LIMIT 64",
                (asset, (clock - timedelta(minutes=20)).isoformat())).fetchall()
        for raw in rows:
            row = json.loads(raw["sample_json"])
            if clock - timedelta(minutes=2) <= timestamp(row["closes_at"]) <= clock:
                return {k: row[k] for k in
                        ("asset", "contract_id", "token_id", "slug", "opens_at", "closes_at")}
        return None

    async def capture_polymarket(self, notice, client, *, now: datetime | None = None) -> int:
        clock = now or datetime.now(UTC)
        if notice.horizon != 15 or not notice.issued <= clock < notice.expires \
                or not self.due("polymarket", notice.slug, clock):
            return 0
        identity = {"asset": notice.asset, "contract_id": notice.condition_id,
                    "token_id": notice.up_token_id, "slug": notice.slug,
                    "opens_at": notice.issued.isoformat(), "closes_at": notice.expires.isoformat()}
        previous = (await asyncio.to_thread(self._previous_pm, notice.asset, clock)
                    if clock.minute % 15 <= 1 else None)
        identities = [previous, identity] if previous is not None else [identity]
        saved = 0
        for item in identities:
            end = min(clock, timestamp(item["closes_at"]))
            response = await client.get(POLY_HISTORY, params={"market": item["token_id"],
                "startTs": int(timestamp(item["opens_at"]).timestamp()),
                "endTs": int(end.timestamp()), "fidelity": 1})
            response.raise_for_status()
            if response.is_redirect or len(response.content) > 100_000:
                raise ValueError("unbounded PM history response")
            received = now or datetime.now(UTC)
            points = parse_points("polymarket", response.json(), identity=item, observed_at=received)
            # The quote diagnostic has its own source time and is never a model input.
            if item is identity:
                for row in points:
                    if abs((timestamp(row["at"]) - notice.quote_at).total_seconds()) <= 60:
                        row.update(yes_bid=getattr(notice, "up_bid", None), yes_ask=notice.up_ask,
                                   diagnostic_quote_at=notice.quote_at.isoformat())
            saved += await asyncio.to_thread(self.record, points)
        return saved

    async def capture_kalshi(self, *, asset, ticker, opens, closes, clock, get) -> int:
        if not self.due("kalshi", ticker, clock):
            return 0
        series = "KX" + asset + "15M"
        if not ticker.startswith(series + "-") or closes - opens != timedelta(minutes=15):
            raise ValueError("unverified Kalshi native contract")
        payload = await get(f"/series/{series}/markets/{ticker}/candlesticks", {
            "start_ts": int(opens.timestamp()), "end_ts": int(clock.timestamp()), "period_interval": 1})
        if payload.get("ticker", ticker) != ticker:
            raise ValueError("Kalshi history ticker mismatch")
        points = parse_points("kalshi", payload, identity={"asset": asset, "contract_id": ticker,
            "opens_at": opens.isoformat(), "closes_at": closes.isoformat()}, observed_at=datetime.now(UTC))
        return await asyncio.to_thread(self.record, points)

    def day_bundle(self, asset: str, day: str, *, now: datetime) -> dict:
        self.initialize()
        start = timestamp(day + "T00:00:00+00:00")
        with self.store.connect() as conn:
            rows = conn.execute("SELECT sample_json FROM rival_xmarket_observations "
                "WHERE asset=? AND at>=? AND at<? ORDER BY source,at",
                (asset, start.isoformat(), (start + timedelta(days=1)).isoformat())).fetchall()
            venues = conn.execute("SELECT venue FROM rival_xmarket_live_venues "
                "WHERE asset=? AND observed_at>=? ORDER BY venue",
                (asset, (now - timedelta(minutes=15)).isoformat())).fetchall()
        return {"schema": BUNDLE_SCHEMA, "asset": asset, "day": day,
                "samples": [json.loads(r["sample_json"]) for r in rows],
                "live_venues": [r["venue"] for r in venues], "verification": {},
                "created_at": now.isoformat()}

    def _mark_export(self, asset, day, file_id):
        with self.store.connect() as conn:
            conn.execute("INSERT INTO rival_xmarket_exports VALUES (?,?,?) ON CONFLICT DO NOTHING",
                         (asset, day, file_id))

    def _pending_days(self, asset, clock):
        cutoff = (clock - timedelta(hours=3)).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.store.connect() as conn:
            return [r["day"] for r in conn.execute(
                "SELECT DISTINCT SUBSTR(i.at,1,10) AS day FROM rival_xmarket_observations i "
                "LEFT JOIN rival_xmarket_exports e ON e.asset=i.asset AND e.day=SUBSTR(i.at,1,10) "
                "WHERE i.asset=? AND i.at<? AND e.day IS NULL ORDER BY day DESC LIMIT 3",
                (asset, cutoff.isoformat())).fetchall()]

    async def publish_days(self, drive, *, now: datetime | None = None) -> int:
        """Piggyback the existing nightly records export; at most three days per scope."""
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from parlay_bot.research.publish import _artifact

        clock = now or datetime.now(UTC)
        await asyncio.to_thread(self.initialize)
        published = 0
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            for day in await asyncio.to_thread(self._pending_days, asset, clock):
                bundle = await asyncio.to_thread(self.day_bundle, asset, day, now=clock)
                if not bundle["samples"]:
                    continue
                with TemporaryDirectory() as folder:
                    digest = hashlib.sha256(canonical(bundle)).hexdigest()
                    path = Path(folder) / f"{BUNDLE_PREFIX}{asset}-{day}-{digest[:16]}.json"
                    path.write_bytes(canonical(bundle))
                    obj = await drive.ensure_research_output(artifact=_artifact(path, len(bundle["samples"])),
                        batch_id="xmarket-inputs-" + digest[:24], kind="report")
                await asyncio.to_thread(self._mark_export, asset, day, obj.file_id)
                published += 1
        return published
