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

from parlay_bot.perpetuals.ai_types import ResearchUnavailable
from parlay_bot.perpetuals.polymarket_learning import PolymarketOutcomeLearner
from parlay_bot.perpetuals.polymarket_oracle import (
    OracleReference,
    PolymarketOracleChallenger,
)
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)


UMA_UP = "1000000000000000000"  # 1e18: first outcome ("Up")
UMA_DOWN = "0"


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
    kind: str = "AI"
    # Original probability and contract identity for a frozen model call.
    probability_up: float | None = None
    settlement_schema: str | None = None
    condition_id: str | None = None
    # None for legacy research; new contest cards persist their initial status.
    qualified_at_issue: bool | None = None


class PolymarketResearch:
    def __init__(self, store: RelationalHotStore, *, ai=None) -> None:
        self.store = store
        self.ai = ai
        self.learner = PolymarketOutcomeLearner(store)
        self.oracle_challenger = PolymarketOracleChallenger(store)
        self._learning_tasks: dict[str, asyncio.Task[bool]] = {}
        # Thread workers must finish before the bot closes the database pool.
        self._study_workers: set[asyncio.Task] = set()
        self._math_calls: dict[str, ContractCall] = {}
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
                    CREATE TABLE IF NOT EXISTS rival_polymarket_resolution_attempts (
                        slug TEXT PRIMARY KEY, checked_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS rival_polymarket_ai_outcomes (
                        slug TEXT PRIMARY KEY, condition_id TEXT NOT NULL,
                        direction TEXT NOT NULL, resolved_at TEXT NOT NULL,
                        observed_at TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
                        receipt_json TEXT NOT NULL
                    );
                """)
            self.learner.initialize()
            self.oracle_challenger.initialize()
            from parlay_bot.perpetuals.model_league import Contest
            Contest(self.store).initialize()
            self._initialized = True

    def study(self, notice):
        """Continue book learning silently; oracle learning needs exact evidence.

        Do not derive Chainlink settlement inputs from futures or Binance
        exchange quotes. When no verified oracle adapter has supplied a
        matching reference, v2 correctly remains untrained.
        """
        self.initialize()
        result = self.learner.observe(notice)
        reference = getattr(notice, "oracle_reference", None)
        if isinstance(reference, OracleReference):
            try:
                self.oracle_challenger.observe(notice, reference)
            except ValueError:
                _LOG.info(
                    "RivaL oracle challenger input rejected asset=%s horizon=%s",
                    notice.asset, notice.horizon,
                )
        return result

    def schedule_study(self, notice, *, limit: int = 4,
                       cache_call: bool = True) -> asyncio.Task[bool] | None:
        """Share one worker per contract until its database work actually finishes."""
        if self._closed:
            return None
        pending = self._learning_tasks.get(notice.slug)
        if pending is not None:
            return pending
        if len(self._learning_tasks) >= limit:
            return None

        async def learn() -> bool:
            try:
                worker = asyncio.create_task(
                    asyncio.to_thread(self.study, notice),
                    name="rival-polymarket-study-db-worker",
                )
                self._study_workers.add(worker)
                def finished_study(finished: asyncio.Task) -> None:
                    self._study_workers.discard(finished)
                    if not finished.cancelled():
                        # A cancelled coordinator may no longer consume this.
                        finished.exception()

                worker.add_done_callback(finished_study)
                # Cancelling the coordinator must not orphan its database work.
                result = await asyncio.shield(worker)
                if cache_call and result is not None and not self._closed:
                    self._math_calls[notice.slug] = ContractCall(
                        slug=notice.slug, asset=notice.asset, horizon=notice.horizon,
                        issued_at=result["issued_at"], expires=notice.expires,
                        direction=result["direction"], model=result["model"],
                        thesis="First model forecast, frozen for this contract; trained on verified Polymarket outcomes.",
                        risk=("Research only. A near-even model probability yields WAIT; "
                              "calibrated accuracy and after-fee edge are unverified."),
                        evidence_sha256=result["evidence_sha256"], input_up_ask=notice.up_ask,
                        input_down_ask=notice.down_ask, kind="model",
                        probability_up=result.get("probability_up"),
                    )
                return True
            except Exception:  # noqa: BLE001 - learning failures never block live quotes
                _LOG.exception("Polymarket contract learning unavailable asset=%s horizon=%s",
                               notice.asset, notice.horizon)
                return False

        task = asyncio.create_task(learn(), name="rival-polymarket-outcome-learning")
        self._learning_tasks[notice.slug] = task

        def learned(finished):
            if self._learning_tasks.get(notice.slug) is finished:
                self._learning_tasks.pop(notice.slug, None)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(learned)
        return task

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
        # The first book-model call is the scored, immutable forecast.
        # A newer optional AI review must not replace or rewrite it.
        call = self._math_calls.get(slug) or self._calls.get(slug)
        now = datetime.now(UTC)
        return call if call is not None and call.issued_at <= now < call.expires else None

    def status(self, slug: str) -> str:
        if slug in self._pending:
            return "Analyzing this Polymarket contract with free AI…"
        saved = self._status.get(slug)
        return saved[1] if saved else "No contract-specific AI call has been issued yet."

    def refresh_note(self, slug: str) -> str | None:
        return self.status(slug) if slug in self._pending or slug in self._status else None

    def observe(self, notice, *, request_ai: bool = False) -> ContractCall | None:
        """Learn mathematically without spending AI quota unless explicitly requested.

        Background market boards and previews must never trigger OpenRouter
        calls. A deliberate user action can opt into experimental AI review.
        """
        if self._closed:
            return None
        now = datetime.now(UTC)
        for slug in list(self._calls):
            if self._calls[slug].expires <= now:
                self._calls.pop(slug, None)
        for slug in list(self._math_calls):
            if self._math_calls[slug].expires <= now:
                self._math_calls.pop(slug, None)
        if len(self._status) > 48:
            for slug in list(self._status):
                if slug not in self._pending:
                    self._status.pop(slug, None)
                if len(self._status) <= 24:
                    break
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
        self.schedule_study(notice)
        if not request_ai:
            # Ordinary board refreshes and silent training use zero LLM tokens.
            # Existing validated model research still runs above.
            return self.peek(notice.slug)
        cached = self._calls.get(notice.slug)
        if cached is not None and not self._needs_refresh(cached, notice, now):
            return cached or self.peek(notice.slug)
        if notice.slug in self._pending:
            return cached or self.peek(notice.slug)
        previous = self._status.get(notice.slug)
        if previous is not None and now < previous[0]:
            return cached or self.peek(notice.slug)
        if len(self._pending) >= 12:
            return None
        task = asyncio.create_task(self._produce(notice), name="rival-polymarket-ai-contract")
        self._pending[notice.slug] = task

        def done(finished: asyncio.Task) -> None:
            if self._pending.get(notice.slug) is finished:
                self._pending.pop(notice.slug, None)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(done)
        return cached or self.peek(notice.slug)

    @staticmethod
    def _needs_refresh(call: ContractCall, notice, now: datetime) -> bool:
        """Optional independent AI research may publish a labeled revision.

        Revisions do not alter the book-model's frozen, scored first forecast.
        """
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
                raise ResearchUnavailable("Contract-specific LFM3 analysis is not configured.")
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
                "SELECT c.slug,c.condition_id,c.expires_at FROM ("
                "SELECT slug,condition_id,expires_at FROM rival_polymarket_ai_calls "
                "WHERE expires_at<? AND expires_at>=? UNION "
                "SELECT slug,condition_id,expires_at FROM rival_polymarket_observations "
                "WHERE expires_at<? AND expires_at>=? UNION "
                "SELECT slug,condition_id,expires_at FROM rival_poly_contest_rounds "
                "WHERE expires_at<? AND expires_at>=?) c "
                "LEFT JOIN rival_polymarket_ai_outcomes o ON o.slug=c.slug "
                "LEFT JOIN rival_polymarket_resolution_attempts a ON a.slug=c.slug "
                "WHERE o.slug IS NULL "
                "ORDER BY COALESCE(a.checked_at,''),c.expires_at LIMIT 20",
                # Contracts resolve within hours; one still unresolved after
                # 30 days will not be. Unbounded, the UNION of every call and
                # observation ever grew to 5 s (84 slow warnings a day, Nov 11).
                (now.isoformat(), (now - timedelta(days=30)).isoformat()) * 3,
            ).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT INTO rival_polymarket_resolution_attempts (slug,checked_at) VALUES (?,?) "
                    "ON CONFLICT(slug) DO UPDATE SET checked_at=excluded.checked_at",
                    (row['slug'], now.isoformat()),
                )
        return [dict(row) for row in rows]

    def _record_resolution(self, call: dict, row: dict, *, now: datetime) -> int:
        """Require terminal provider payout and exact identity, never candle labels."""
        if "payouts" not in row and "price" in row:
            return self._record_uma_resolution(call, row, now=now)
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

    def _record_uma_resolution(self, call: dict, row: dict, *, now: datetime) -> int:
        """Hourly "Up or Down" markets resolve through UMA, not payout vectors.

        The data API reports the settled UMA price instead: 1e18 is the first
        outcome (Up) and 0 the second (Down). Checked against 12 live hourly
        BTC contracts and the matching exchange candles: all agreed. A 50/50
        price, a dispute, an extended review or a missing transaction is
        never turned into a label.
        """
        try:
            price = str(row["price"])
            if (
                row.get("condition_id") != call["condition_id"]
                or row.get("status") != "resolved"
                or row.get("extended_review") is not False
                or row.get("was_disputed") is not False
                or not isinstance(row.get("transaction_hash"), str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{64}", row["transaction_hash"])
                or price not in (UMA_UP, UMA_DOWN)
            ):
                return 0
            resolved = datetime.fromtimestamp(int(row["last_update_timestamp"]), UTC)
            expires = datetime.fromisoformat(call["expires_at"])
            if expires.utcoffset() is None or not expires <= resolved <= now:
                return 0
        except (KeyError, ValueError, TypeError, OverflowError):
            return 0
        receipt = json.dumps(row, sort_keys=True, separators=(",", ":"))
        with self.store.connect() as conn:
            result = conn.execute(
                "INSERT INTO rival_polymarket_ai_outcomes "
                "(slug,condition_id,direction,resolved_at,observed_at,receipt_sha256,receipt_json) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
                (call["slug"], call["condition_id"], "UP" if price == UMA_UP else "DOWN",
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
        pending = list(self._pending.values()) + list(self._learning_tasks.values())
        if self._settle_task is not None:
            pending.append(self._settle_task)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # asyncio.to_thread cannot stop an already-running DB operation. Wait
        # for the owned workers before the caller shuts down the store.
        if self._study_workers:
            await asyncio.gather(*tuple(self._study_workers), return_exceptions=True)
        self._pending.clear()
        self._learning_tasks.clear()
