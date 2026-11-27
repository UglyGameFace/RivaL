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
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store
        self.learner = PolymarketOutcomeLearner(store)
        self.oracle_challenger = PolymarketOracleChallenger(store)
        self._learning_tasks: dict[str, asyncio.Task[bool]] = {}
        # Thread workers must finish before the bot closes the database pool.
        self._study_workers: set[asyncio.Task] = set()
        self._math_calls: dict[str, ContractCall] = {}
        self._initialized = False
        self._closed = False
        self._init_lock = threading.Lock()
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
                    -- The settlement sweep's per-branch window scan reads
                    -- exactly (expires_at, slug, condition_id); covering
                    -- indexes on all three source tables keep that scan
                    -- index-only instead of hauling the wide evidence
                    -- heap on every 60 s pass (see _unsettled).
                    CREATE INDEX IF NOT EXISTS idx_rival_polymarket_ai_calls_expiry_cover
                        ON rival_polymarket_ai_calls(expires_at,slug,condition_id);
                    CREATE TABLE IF NOT EXISTS rival_polymarket_resolution_attempts (
                        slug TEXT PRIMARY KEY, checked_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS rival_polymarket_ai_outcomes (
                        slug TEXT PRIMARY KEY, condition_id TEXT NOT NULL,
                        direction TEXT NOT NULL, resolved_at TEXT NOT NULL,
                        observed_at TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
                        receipt_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_rival_polymarket_ai_outcomes_observed
                        ON rival_polymarket_ai_outcomes(observed_at);
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
        # The book-model call is the scored, immutable forecast.
        call = self._math_calls.get(slug)
        now = datetime.now(UTC)
        return call if call is not None and call.issued_at <= now < call.expires else None

    def status(self, slug: str) -> str:
        saved = self._status.get(slug)
        return saved[1] if saved else "No contract call has been issued yet."

    def refresh_note(self, slug: str) -> str | None:
        return self.status(slug) if slug in self._status else None

    def observe(self, notice) -> ContractCall | None:
        """Learn mathematically from the notice and return any live call.

        Background market boards and previews only ever use the book
        model; no external review is requested.
        """
        if self._closed:
            return None
        now = datetime.now(UTC)
        for slug in list(self._math_calls):
            if self._math_calls[slug].expires <= now:
                self._math_calls.pop(slug, None)
        if len(self._status) > 48:
            for slug in list(self._status):
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
        return self.peek(notice.slug)

    async def wait(self, slug: str) -> ContractCall | None:
        return self.peek(slug)

    def _unsettled(self, now: datetime) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT c.slug,c.condition_id,c.expires_at FROM ("
                "SELECT slug,condition_id,expires_at FROM rival_polymarket_ai_calls "
                "WHERE expires_at<? AND expires_at>=? AND NOT EXISTS ("
                "SELECT 1 FROM rival_polymarket_ai_outcomes o "
                "WHERE o.slug=rival_polymarket_ai_calls.slug) UNION ALL "
                "SELECT slug,condition_id,expires_at FROM rival_polymarket_observations "
                "WHERE expires_at<? AND expires_at>=? AND NOT EXISTS ("
                "SELECT 1 FROM rival_polymarket_ai_outcomes o "
                "WHERE o.slug=rival_polymarket_observations.slug) UNION ALL "
                "SELECT slug,condition_id,expires_at FROM rival_poly_contest_rounds "
                "WHERE expires_at<? AND expires_at>=? AND NOT EXISTS ("
                "SELECT 1 FROM rival_polymarket_ai_outcomes o "
                "WHERE o.slug=rival_poly_contest_rounds.slug)) c "
                "LEFT JOIN rival_polymarket_resolution_attempts a ON a.slug=c.slug "
                "ORDER BY COALESCE(a.checked_at,''),c.expires_at LIMIT 200",
                # Contracts resolve within hours; one still unresolved after
                # 30 days will not be. Unbounded, the UNION of every call and
                # observation ever grew to 5 s (84 slow warnings a day, Nov 11).
                # UNION ALL + the Python dedupe below replaces the SQL UNION:
                # its dedupe node hashed the whole 30-day window of all three
                # tables on every 60 s call. The same slug legitimately sits
                # in several sources (and in ai_calls once per revision), so
                # fetch a bounded 200 rows, drop duplicate
                # (slug, condition_id, expires_at) triples keeping the first
                # in ORDER BY order, and keep the first 20 — exactly the
                # UNION result unless more than 180 duplicate triples sort
                # ahead of the 20th distinct slug.
                # The outcome anti-join sits inside each branch (Nov 21): as
                # an outer LEFT JOIN it forced every resolved contract in the
                # window (~45k rows) through the ORDER BY sort before the
                # filter could drop it, which put this read back on the
                # slow-operation ledger at up to 25 s under load. Filtered
                # per branch, only the unresolved remainder is sorted; the
                # returned rows and their (checked_at, expires_at) order are
                # unchanged (verified row-for-row on a production-scale seed).
                # The branches still hauled the wide evidence heap for
                # every window row (up to 273 s under load, 2026-11-23);
                # the (expires_at, slug, condition_id) covering indexes
                # declared in the three initialize() methods make each
                # branch an index-only scan — same rows, same order,
                # verified row-for-row on a production-shaped seed
                # (ai_calls empty, observations/contest at production
                # volume and row width). A UNION-first single anti-join
                # was measured 10x worse and rejected.
                (now.isoformat(), (now - timedelta(days=30)).isoformat()) * 3,
            ).fetchall()
            seen = set()
            unique = []
            for row in rows:
                key = (row["slug"], row["condition_id"], row["expires_at"])
                if key not in seen:
                    seen.add(key)
                    unique.append(row)
                    if len(unique) == 20:
                        break
            rows = unique
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
            # On-chain provenance: a full transaction hash, as most
            # resolutions carry -- or, for the Chainlink-reported
            # resolutions Polymarket began returning for the SOL/XRP
            # 15-minute series on 2026-11-23, an explicitly empty hash
            # together with the positive resolved block (the live row
            # shape: transaction_hash "", resolved_block 97906248,
            # reporter CHAINLINK). The empty-hash shape with no block,
            # a missing hash, or a malformed non-empty hash is rejected
            # exactly as before. Before this, every SOL/XRP 15m outcome
            # since 2026-11-23T13:30Z was retried forever and never
            # recorded (fair-value PM settlement_unavailable gap).
            tx_hash = row.get("transaction_hash")
            block = row.get("resolved_block")
            has_block = (isinstance(block, int) and not isinstance(block, bool)
                         and block > 0)
            has_provenance = has_block and (
                (isinstance(tx_hash, str)
                 and re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash) is not None)
                or tx_hash == "")
            if (
                row.get("condition_id") != call["condition_id"]
                or row.get("status") != "resolved"
                or row.get("extended_review") is not False
                or not has_provenance
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
        pending = list(self._learning_tasks.values())
        if self._settle_task is not None:
            pending.append(self._settle_task)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # asyncio.to_thread cannot stop an already-running DB operation. Wait
        # for the owned workers before the caller shuts down the store.
        if self._study_workers:
            await asyncio.gather(*tuple(self._study_workers), return_exceptions=True)
        self._learning_tasks.clear()
