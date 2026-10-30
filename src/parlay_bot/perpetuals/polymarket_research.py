"""Exact-contract experimental AI calls, with immutable inputs and shared quota.

This is a research issuance path, not an approved quantitative strategy.
Exchange-candle forecasts and self-reported AI confidence are never substituted
for contract probabilities. A missing/expired/budget-blocked call stays missing.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.gemini_research import ResearchUnavailable
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContractCall:
    slug: str
    asset: str
    horizon: int
    issued_at: datetime
    expires: datetime
    direction: str
    model: str
    thesis: str
    risk: str
    evidence_sha256: str
    input_up_ask: float | None = None
    input_down_ask: float | None = None


class PolymarketResearch:
    def __init__(self, store: RelationalHotStore, *, ai=None) -> None:
        self.store = store
        self.ai = ai
        self._initialized = False
        self._closed = False
        self._init_lock = threading.Lock()
        self._pending: dict[str, asyncio.Task] = {}
        self._calls: dict[str, ContractCall] = {}
        self._status: dict[str, tuple[datetime, str]] = {}
        self._settle_task: asyncio.Task | None = None
        self._settle_after = datetime.min.replace(tzinfo=UTC)

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self.store.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS rival_polymarket_ai_calls (
                        slug TEXT NOT NULL, asset TEXT NOT NULL,
                        horizon INTEGER NOT NULL, condition_id TEXT NOT NULL,
                        opens_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                        issued_at TEXT NOT NULL, model TEXT NOT NULL,
                        direction TEXT NOT NULL, thesis TEXT NOT NULL,
                        risk TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        PRIMARY KEY (slug,evidence_sha256)
                    );
                    CREATE INDEX IF NOT EXISTS rival_polymarket_ai_expiry
                        ON rival_polymarket_ai_calls(expires_at);
                    CREATE TABLE IF NOT EXISTS rival_polymarket_ai_outcomes (
                        slug TEXT PRIMARY KEY, condition_id TEXT NOT NULL,
                        direction TEXT NOT NULL, resolved_at TEXT NOT NULL,
                        observed_at TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
                        receipt_json TEXT NOT NULL
                    );
                """)
            self._initialized = True

    def _saved(self, slug: str) -> ContractCall | None:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rival_polymarket_ai_calls WHERE slug=? ORDER BY issued_at DESC LIMIT 1", (slug,),
            ).fetchone()
        if row is None:
            return None
        return ContractCall(
            slug=row["slug"], asset=row["asset"], horizon=int(row["horizon"]),
            issued_at=datetime.fromisoformat(row["issued_at"]),
            expires=datetime.fromisoformat(row["expires_at"]),
            direction=row["direction"], model=row["model"], thesis=row["thesis"],
            risk=row["risk"], evidence_sha256=row["evidence_sha256"],
            input_up_ask=json.loads(row["evidence_json"]).get("up_ask"),
            input_down_ask=json.loads(row["evidence_json"]).get("down_ask"),
        )

    def _save(self, call: ContractCall, notice, evidence: dict) -> ContractCall:
        self.initialize()
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_polymarket_ai_calls "
                "(slug,asset,horizon,condition_id,opens_at,expires_at,issued_at,model,"
                "direction,thesis,risk,evidence_sha256,evidence_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug,evidence_sha256) DO NOTHING",
                (call.slug, call.asset, call.horizon, notice.condition_id,
                 notice.issued.isoformat(), call.expires.isoformat(),
                 call.issued_at.isoformat(), call.model, call.direction,
                 call.thesis, call.risk, call.evidence_sha256,
                 json.dumps(evidence, sort_keys=True, separators=(",", ":"))),
            )
        # Concurrent processes cannot overwrite the first genuine issuance.
        return self._saved(call.slug)

    def peek(self, slug: str) -> ContractCall | None:
        call = self._calls.get(slug)
        return call if call is not None and call.issued_at <= datetime.now(UTC) < call.expires else None

    def status(self, slug: str) -> str:
        if slug in self._pending:
            return "Analyzing this Polymarket contract with free AI…"
        saved = self._status.get(slug)
        return saved[1] if saved else "No contract-specific AI call has been issued yet."

    def refresh_note(self, slug: str) -> str | None:
        return self.status(slug) if slug in self._pending or slug in self._status else None

    def observe(self, notice) -> ContractCall | None:
        """Start at most one bounded review per contract, without blocking UI."""
        if self._closed:
            return None
        now = datetime.now(UTC)
        for slug in list(self._calls):
            if self._calls[slug].expires <= now:
                self._calls.pop(slug, None)
        if len(self._status) > 48:
            for slug in list(self._status):
                if slug not in self._pending:
                    self._status.pop(slug, None)
                if len(self._status) <= 24:
                    break
        cached = self.peek(notice.slug)
        if cached is not None and not self._needs_refresh(cached, notice, now):
            return cached
        if notice.slug in self._pending:
            return cached
        previous = self._status.get(notice.slug)
        if previous is not None and now < previous[0]:
            return cached
        if len(self._pending) >= 12:
            return None
        if not (
            notice.issued <= now < notice.expires
            and timedelta(0) <= now - notice.quote_at <= timedelta(seconds=25)
            and isinstance(notice.condition_id, str)
            and re.fullmatch(r"0x[0-9a-fA-F]{64}", notice.condition_id)
            and notice.up_token_id and notice.down_token_id
            and notice.up_token_id != notice.down_token_id
            and notice.resolution_source
            and all(math.isfinite(p) and 0 < p < 1 for p in (notice.up_ask, notice.down_ask))
        ):
            self._status[notice.slug] = (now + timedelta(seconds=15), "Verified contract inputs are incomplete or stale.")
            return None
        task = asyncio.create_task(self._produce(notice), name="rival-polymarket-ai-contract")
        self._pending[notice.slug] = task

        def done(finished: asyncio.Task) -> None:
            if self._pending.get(notice.slug) is finished:
                self._pending.pop(notice.slug, None)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(done)
        return cached

    @staticmethod
    def _needs_refresh(call: ContractCall, notice, now: datetime) -> bool:
        return (
            now - call.issued_at >= timedelta(seconds=60)
            and call.input_up_ask is not None and call.input_down_ask is not None
            and max(abs(notice.up_ask-call.input_up_ask),
                    abs(notice.down_ask-call.input_down_ask)) >= 0.05
        )

    async def _produce(self, notice) -> None:
        try:
            saved = await asyncio.to_thread(self._saved, notice.slug)
            if saved is not None and not self._needs_refresh(saved, notice, datetime.now(UTC)):
                if saved.expires > datetime.now(UTC):
                    self._calls[notice.slug] = saved
                return
            if self.ai is None or not self.ai.available:
                raise ResearchUnavailable("Free contract AI is not configured.")
            now = datetime.now(UTC)
            if notice.expires - now < timedelta(seconds=45):
                raise ResearchUnavailable("Too little time remains to issue a new AI call.")
            evidence = {
                "target": "polymarket", "slug": notice.slug,
                "asset": notice.asset, "horizon_minutes": notice.horizon,
                "condition_id": notice.condition_id,
                "up_token_id": notice.up_token_id, "down_token_id": notice.down_token_id,
                "opens_at": notice.issued.isoformat(), "closes_at": notice.expires.isoformat(),
                "source_quote_at": notice.quote_at.isoformat(),
                "observed_at": (notice.received_at or notice.quote_at).isoformat(),
                "resolution_source": notice.resolution_source,
                "published_settlement_rules": notice.rules,
                "price_to_beat": notice.price_to_beat,
                "up_ask": notice.up_ask, "down_ask": notice.down_ask,
                "up_10_share_average_ask": notice.up_average_ask,
                "down_10_share_average_ask": notice.down_average_ask,
                "contract_outcome_training_verified": False,
            }
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
            result = await asyncio.wait_for(self.ai.review_contract(evidence), timeout=22)
            issued = datetime.now(UTC)
            if issued >= notice.expires or result.get("slug") != notice.slug:
                raise ResearchUnavailable("The reviewed contract expired or did not match.")
            call = ContractCall(
                slug=notice.slug, asset=notice.asset, horizon=notice.horizon,
                issued_at=issued, expires=notice.expires,
                direction={"up": "UP", "down": "DOWN", "uncertain": "WAIT"}[result["direction"]],
                model=result["model"], thesis=result["thesis"], risk=result["risk"],
                evidence_sha256=digest,
                input_up_ask=notice.up_ask, input_down_ask=notice.down_ask,
            )
            call = await asyncio.to_thread(self._save, call, notice, evidence)
            if call is not None and call.expires > datetime.now(UTC):
                self._calls[notice.slug] = call
                self._status.pop(notice.slug, None)
                _LOG.info("RivaL Polymarket AI issued asset=%s horizon=%s slug=%s direction=%s model=%s",
                          call.asset, call.horizon, call.slug, call.direction, call.model)
        except ResearchUnavailable as exc:
            self._status[notice.slug] = (datetime.now(UTC) + timedelta(seconds=60), str(exc))
            _LOG.info("RivaL Polymarket AI unavailable asset=%s horizon=%s reason=%s",
                      notice.asset, notice.horizon, str(exc))
        except Exception:  # noqa: BLE001 - no substitute exchange forecast
            self._status[notice.slug] = (datetime.now(UTC) + timedelta(seconds=60), "Contract AI unavailable; no source was substituted.")
            _LOG.exception("RivaL Polymarket AI research failed")

    async def wait(self, slug: str) -> ContractCall | None:
        pending = self._pending.get(slug)
        if pending is not None:
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=23)
            except TimeoutError:
                return None
        return self.peek(slug)

    def _unsettled(self, now: datetime) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT c.slug,c.condition_id,c.expires_at FROM rival_polymarket_ai_calls c "
                "LEFT JOIN rival_polymarket_ai_outcomes o ON o.slug=c.slug "
                "WHERE o.slug IS NULL AND c.expires_at<? ORDER BY c.expires_at LIMIT 20",
                (now.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _record_resolution(self, call: dict, row: dict, *, now: datetime) -> int:
        """Require terminal provider payout and exact identity, never candle labels."""
        try:
            payouts = row["payouts"]
            if (
                row.get("condition_id") != call["condition_id"]
                or row.get("status") != "resolved"
                or row.get("extended_review") is not False
                or not isinstance(row.get("transaction_hash"), str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", row["transaction_hash"])
                or not isinstance(row.get("resolved_block"), int) or row["resolved_block"] <= 0
                or not isinstance(payouts, list) or len(payouts) != 2
                or any(isinstance(v, bool) for v in payouts)
            ):
                return 0
            up, down = map(float, payouts)
            if not all(math.isfinite(v) and v >= 0 for v in (up, down)):
                return 0
            if not ((up > 0 and down == 0) or (down > 0 and up == 0)):
                return 0  # split/cancelled payouts are not binary Up/Down labels
            resolved = datetime.fromisoformat(row["resolved_at"])
            expires = datetime.fromisoformat(call["expires_at"])
            if resolved.utcoffset() is None or not expires <= resolved <= now:
                return 0
        except (KeyError, ValueError, TypeError, OverflowError):
            return 0
        receipt = json.dumps(row, sort_keys=True, separators=(",", ":"))
        with self.store.connect() as conn:
            result = conn.execute(
                "INSERT INTO rival_polymarket_ai_outcomes "
                "(slug,condition_id,direction,resolved_at,observed_at,receipt_sha256,receipt_json) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
                (call["slug"], call["condition_id"], "UP" if up > 0 else "DOWN",
                 resolved.isoformat(), now.isoformat(),
                 hashlib.sha256(receipt.encode()).hexdigest(), receipt),
            )
        return result.rowcount

    async def settle(self, reader) -> int:
        now = datetime.now(UTC)
        calls = await asyncio.to_thread(self._unsettled, now)
        if not calls:
            return 0
        response = await reader._http().get(
            "https://data-api.polymarket.com/v2/resolutions",
            params={"condition": ",".join(dict.fromkeys(c["condition_id"] for c in calls))},
        )
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, list):
            return 0
        by_condition: dict[str, list[dict]] = {}
        for row in data:
            if isinstance(row, dict):
                by_condition.setdefault(row.get("condition_id"), []).append(row)
        total = 0
        for call in calls:
            rows = by_condition.get(call["condition_id"], [])
            if len(rows) == 1:
                total += await asyncio.to_thread(self._record_resolution, call, rows[0], now=now)
        if total:
            _LOG.info("RivaL Polymarket exact-contract AI outcomes settled=%d", total)
        return total

    def maybe_settle(self, reader) -> None:
        if self._closed:
            return
        now = datetime.now(UTC)
        if now < self._settle_after or (self._settle_task is not None and not self._settle_task.done()):
            return
        self._settle_after = now + timedelta(seconds=60)

        async def run():
            try:
                await asyncio.wait_for(self.settle(reader), timeout=10)
            except Exception:  # noqa: BLE001 - outcome outages do not block live calls
                _LOG.info("Polymarket outcome receipt check unavailable")

        self._settle_task = asyncio.create_task(run(), name="rival-polymarket-call-outcomes")

    async def aclose(self) -> None:
        self._closed = True
        pending = list(self._pending.values())
        if self._settle_task is not None:
            pending.append(self._settle_task)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()
