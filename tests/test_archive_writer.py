from datetime import date

import duckdb

from parlay_bot.archive.models import HistoryArchiveBatch
from parlay_bot.archive.writer import ParquetArchiveWriter


def _row(row_id: int) -> dict:
    return {
        "id": row_id,
        "selection_key": f"selection-{row_id}",
        "provider": "sportsgameodds",
        "fixture_id": "event-1",
        "bookmaker": "draftkings",
        "sport_name": "Basketball",
        "tournament_name": "NBA",
        "market_id": "spread",
        "market_name": "Spread",
        "outcome_id": "home",
        "outcome_name": "Home",
        "player_id": "0",
        "player_name": None,
        "line_value": -5.5,
        "line_group_value": 5.5,
        "deeplink": None,
        "active": 1,
        "main_line": 1,
        "price_decimal": 1.91,
        "price_american": "-110",
        "price_fractional": None,
        "bet_limit": None,
        "provider_changed_at": "2026-09-28T20:00:00+00:00",
        "bookmaker_changed_at": "2026-09-28T20:00:00+00:00",
        "exchange_meta_json": None,
        "state_fingerprint": f"fingerprint-{row_id}",
        "first_seen": "2026-09-28T20:00:00+00:00",
        "last_seen": "2026-09-28T20:10:00+00:00",
        "occurrences": 2,
    }


def test_writer_creates_readable_zstd_parquet_and_manifest(tmp_path) -> None:
    batch = HistoryArchiveBatch(
        batch_id="batch-123",
        provider="sportsgameodds",
        bookmaker="draftkings",
        sport="nba",
        partition_date=date(2026, 9, 28),
        row_ids=(1, 2),
        rows=(_row(1), _row(2)),
    )
    writer = ParquetArchiveWriter(tmp_path)

    history = writer.write_history(batch)

    assert history.size_bytes > 0
    assert history.sha256
    assert history.md5
    assert history.row_count == 2
    assert history.file_name.endswith(".parquet")

    with duckdb.connect(database=":memory:") as connection:
        count = connection.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(history.path)],
        ).fetchone()[0]
        compressions = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT compression FROM parquet_metadata(?)",
                [str(history.path)],
            ).fetchall()
        }

    assert count == 2
    assert compressions == {"ZSTD"}

    manifest = writer.write_manifest(
        batch=batch,
        history=history,
        history_drive_file_id="drive-history-1",
    )
    body = manifest.path.read_text(encoding="utf-8")

    assert '"batch_id": "batch-123"' in body
    assert '"drive_file_id": "drive-history-1"' in body
    assert f'"sha256": "{history.sha256}"' in body
    assert '"row_count": 2' in body
