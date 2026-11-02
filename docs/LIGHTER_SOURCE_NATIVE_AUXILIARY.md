# Lighter native funding and mark-price source acquisition

The public Lighter API documents original funding and mark-price candles.
This collector reuses the actual active perpetual orderBooks market catalog
rather than hardcoding a numeric market ID, and retains original raw JSON
plus immutable SHA receipts in RivaL private Google Drive with readback.

Fundings return UTC epoch seconds, whereas markPriceCandles return
UTC epoch milliseconds. A single source response is not proof of a complete
month. Each calendar month may be complete for one family but incomplete for
another, and no derived signed funding feature is fabricated from the
original unsigned rate/direction until independently validated.

Archive scope starts at February 2025, since Lighter did not operate
perpetuals in 2021. Some earliest source responses lack mark-price candles.
Lighter's historical funding and mark-price API can return original rows from
before the requested start timestamp alongside in-window rows. The collector
now SHA-preserves the full response but attributes only rows inside the
requested UTC window to that month; missing periods are not filled or called
complete. Main-branch acquisition starts from the latest complete month,
while the six-hour schedule rotates recent six months and older months. Four serialized scheduled runs each day rotate
all completed native market months, with authenticated Drive bytes
reverified and original source family identities preserved.

Requester-paid historical trade S3 is never activated. Historic L2
snapshots are not derived from current orderBooks, and the existing
five-minute live collector stays unchanged.

Official docs: https://github.com/elliottech/lighter-python/blob/main/docs/CandlestickApi.md
