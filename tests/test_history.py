from parlay_bot.storage.history import collapse_repeated_history


def test_collapses_only_contiguous_duplicate_states() -> None:
    rows = [
        {
            "createdAt": "2026-09-28T18:02:00Z",
            "price": 1.91,
            "limit": 1000,
            "active": True,
            "exchangeMeta": None,
        },
        {
            "createdAt": "2026-09-28T18:00:00Z",
            "price": 1.91,
            "limit": 1000,
            "active": True,
            "exchangeMeta": None,
        },
        {
            "createdAt": "2026-09-28T18:01:00Z",
            "price": 1.91,
            "limit": 1000,
            "active": True,
            "exchangeMeta": None,
        },
        {
            "createdAt": "2026-09-28T18:03:00Z",
            "price": 1.83,
            "limit": 1000,
            "active": True,
            "exchangeMeta": None,
        },
    ]

    result = collapse_repeated_history(rows)

    assert len(result) == 2
    assert result[0]["firstSeen"] == "2026-09-28T18:00:00Z"
    assert result[0]["lastSeen"] == "2026-09-28T18:02:00Z"
    assert result[0]["occurrences"] == 3
    assert result[1]["price"] == 1.83
    assert result[1]["occurrences"] == 1


def test_repeated_price_after_intervening_change_is_not_merged() -> None:
    rows = [
        {"createdAt": "2026-09-28T18:00:00Z", "price": 1.9, "limit": 10, "active": True},
        {"createdAt": "2026-09-28T18:01:00Z", "price": 2.0, "limit": 10, "active": True},
        {"createdAt": "2026-09-28T18:02:00Z", "price": 1.9, "limit": 10, "active": True},
    ]

    assert len(collapse_repeated_history(rows)) == 3
