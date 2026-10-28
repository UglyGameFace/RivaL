"""Exact six-calendar-month 5m continuity audit, not a row-count estimate.

The audit reads stored candle openings in ascending order, checks UTC grid,
reports missing intervals and invalid observation timing, and keeps results
in a separate restart-safe history. Nothing backfilled becomes a fake live
forward prediction.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.history_inventory import six_months_prior
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_STEP = timedelta(minutes=5)
_ASSETS = ("BTC", "ETH", "SOL")


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("UTC timezone is required")
    return value.astimezone(UTC)


def _market(venue: str, asset: str) -> tuple[str, str]:
    from parlay_bot.discord_app.data_dashboard import MARKET_SYMBOLS

    template, quote = MARKET_SYMBOLS[venue]
    symbol = ("XBTUSDTM" if venue == "kucoin" and asset == "BTC"
              else template.format(asset=asset))
    return symbol, quote


class SixMonthQualityAudit:
    def __init__(self, market: PerpetualMarketStore) -> None:
        self.market = market
        self.store = market.store

    def initialize(self) -> None:
        self.market.initialize()
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_history_quality (
                    venue TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    audited_at TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    expected_bars INTEGER NOT NULL,
                    valid_bars INTEGER NOT NULL,
                    missing_bars INTEGER NOT NULL,
                    gap_segments INTEGER NOT NULL,
                    longest_gap_bars INTEGER NOT NULL,
                    off_grid_bars INTEGER NOT NULL,
                    invalid_observations INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY(venue,asset,audited_at)
                );
                CREATE INDEX IF NOT EXISTS idx_perp_history_quality_latest
                    ON perp_history_quality(asset,venue,audited_at);
                """
            )

    def audit_stream(
        self, *, venue: str, asset: str, now: datetime | None = None,
    ) -> dict:
        from parlay_bot.discord_app.data_dashboard import MARKET_SYMBOLS

        if asset not in _ASSETS or venue not in MARKET_SYMBOLS:
            raise ValueError("invalid historical audit stream")
        clock = _utc(now or datetime.now(UTC))
        end = clock.replace(minute=clock.minute // 5 * 5,
                            second=0, microsecond=0)
        start = six_months_prior(end).replace(
            minute=six_months_prior(end).minute // 5 * 5,
            second=0, microsecond=0,
        )
        expected = int((end - start).total_seconds() // 300)
        contract, quote = _market(venue, asset)
        valid = invalid = off_grid = segments = longest = 0
        cursor = start
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT opened_at,closed_at,first_observed_at
                   FROM perp_market_candles
                   WHERE venue=? AND contract=? AND quote=?
                     AND interval_minutes=5 AND opened_at>=?
                     AND opened_at<? ORDER BY opened_at ASC""",
                (venue, contract, quote, start.isoformat(), end.isoformat()),
            )
            # Process in chunks to avoid loading six months of candles at once.
            while batch := rows.fetchmany(512):
                for row in batch:
                    try:
                        opened = _utc(datetime.fromisoformat(str(row["opened_at"])))
                        closed = _utc(datetime.fromisoformat(str(row["closed_at"])))
                        observed = _utc(datetime.fromisoformat(
                            str(row["first_observed_at"])
                        ))
                    except (ValueError, TypeError, OverflowError):
                        invalid += 1
                        continue
                    offset = (opened - start).total_seconds()
                    if (
                        offset < 0 or offset % 300 != 0
                        or opened != opened.replace(second=0, microsecond=0)
                    ):
                        off_grid += 1
                        continue
                    if opened < cursor:
                        off_grid += 1
                        continue
                    if opened > cursor:
                        gap = int((opened - cursor).total_seconds() // 300)
                        segments += 1
                        longest = max(longest, gap)
                    valid += 1
                    if (
                        closed != opened + _STEP - timedelta(milliseconds=1)
                        or observed <= closed or observed > clock
                    ):
                        # These are genuine stored bars but cannot be certified
                        # as valid point-in-time historical observations.
                        invalid += 1
                    cursor = opened + _STEP
        if cursor < end:
            gap = int((end - cursor).total_seconds() // 300)
            segments += 1
            longest = max(longest, gap)
        missing = expected - valid
        # Never claim fully gap-checked history on a malformed timestamp.
        status = (
            "complete" if not (missing or invalid or off_grid)
            else "gaps_or_anomalies"
        )
        result = {
            "venue": venue, "asset": asset, "contract": contract, "quote": quote,
            "audited_at": clock.isoformat(),
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "expected_bars": expected, "valid_bars": valid,
            "missing_bars": missing, "gap_segments": segments,
            "longest_gap_bars": longest, "off_grid_bars": off_grid,
            "invalid_observations": invalid, "status": status,
        }
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_history_quality
                   (venue,asset,contract,quote,audited_at,window_start,window_end,
                    expected_bars,valid_bars,missing_bars,gap_segments,
                    longest_gap_bars,off_grid_bars,invalid_observations,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(venue,asset,audited_at) DO UPDATE SET
                    valid_bars=excluded.valid_bars,
                    missing_bars=excluded.missing_bars,
                    gap_segments=excluded.gap_segments,
                    longest_gap_bars=excluded.longest_gap_bars,
                    off_grid_bars=excluded.off_grid_bars,
                    invalid_observations=excluded.invalid_observations,
                    status=excluded.status""",
                tuple(result.values()),
            )
        return result

    def audit_asset(
        self, *, asset: str, venues: tuple[str, ...],
        now: datetime | None = None,
    ) -> list[dict]:
        return [self.audit_stream(venue=venue, asset=asset, now=now)
                for venue in dict.fromkeys(venues)]

    def latest(self, *, asset: str, venues: tuple[str, ...]) -> list[dict]:
        if asset not in _ASSETS:
            raise ValueError("invalid asset")
        self.initialize()
        with self.store.connect() as conn:
            result = []
            for venue in dict.fromkeys(venues):
                row = conn.execute(
                    """SELECT * FROM perp_history_quality
                       WHERE venue=? AND asset=?
                       ORDER BY audited_at DESC LIMIT 1""",
                    (venue, asset),
                ).fetchone()
                if row is not None:
                    result.append(dict(row))
        return result
