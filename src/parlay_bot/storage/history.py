from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def collapse_repeated_history(rows: Iterable[dict]) -> list[dict]:
    """Collapse contiguous duplicate OddsPapi history observations.

    Every actual price/limit/active/exchange state change is retained. Repeated
    observations of the same state become one interval with firstSeen, lastSeen,
    and occurrences so duration information is not lost.
    """

    ordered = sorted(rows, key=lambda row: _timestamp(str(row["createdAt"])))
    collapsed: list[dict] = []

    for row in ordered:
        state = {
            "price": row.get("price"),
            "limit": row.get("limit"),
            "active": row.get("active"),
            "exchangeMeta": row.get("exchangeMeta"),
        }
        seen_at = str(row["createdAt"])

        if collapsed and all(collapsed[-1].get(key) == value for key, value in state.items()):
            collapsed[-1]["lastSeen"] = seen_at
            collapsed[-1]["occurrences"] += 1
            continue

        collapsed.append(
            {
                **state,
                "firstSeen": seen_at,
                "lastSeen": seen_at,
                "occurrences": 1,
            }
        )

    return collapsed
