from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ArchivePartition:
    sport: str
    day: date
    bookmaker: str

    @property
    def relative_path(self) -> str:
        sport = self.sport.strip().lower().replace(" ", "-")
        bookmaker = self.bookmaker.strip().lower().replace(" ", "-")
        return (
            f"history/{sport}/{self.day:%Y}/{self.day:%m}/"
            f"{self.day:%Y-%m-%d}/{bookmaker}.parquet"
        )
