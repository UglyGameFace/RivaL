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
perpetuals in 2021. Four serialized scheduled runs each day rotate
all completed native market months, with authenticated Drive bytes
reverified and original source family identities preserved.

Requester-paid historical trade S3 is never activated. Historic L2
snapshots are not derived from current orderBooks, and the existing
five-minute live collector stays unchanged.

Official docs: https://github.com/elliottech/lighter-python/blob/main/docs/CandlestickApi.md
