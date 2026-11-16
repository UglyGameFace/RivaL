# OKX native-month disagreement: original provenance diagnostic

## Verified block

- Original-source feature run [43878923264](https://github.com/UglyGameFace/RivaL/actions/runs/43878923264) ran, but all twelve XMarket inner training-only ablations were unavailable: **2 distinct UTC evaluation days** per scope versus minimum **7**.
- Real OKX post-#627 historical run [43885314333](https://github.com/UglyGameFace/RivaL/actions/runs/43885314333) still blocked BTC, ETH, SOL and XRP with `conflicting native month snapshots`, **0** recovered native months. #627 safely handles only exact SHA-identical replay manifests. It cannot select from actually different versions.

## This change: bounded read-only conflict receipt

`restore_verified_source_months` now raises a typed `NativeMonthConflictError`, which retains its ordinary `HistoryImportError` parent and original fail-closed message. On a second **different** SHA-verified manifest for the same `(venue,contract,quote,month)`, the exception carries:

- exact venue, contract, quote, year-month;
- both full manifest SHA-256 digests, each claimed history SHA-256 (`data_sha256`), the original provider response SHA-256 (`source_sha256`), and, when present, the additional source archive SHA-256;
- the validated source-integrity family and sanitized bounded source transport;
- explicit proof flags: **both manifest bytes have been SHA checked**, first manifest's original history was read back and checked; **the second manifest's original history has not been independently read back** at the point of rejection;
- `blocked_divergent_manifest_bytes` as the only outcome.

Neither ID, user token, URL, raw price, source body nor private Drive file path is included. Missing or malformed optional source hashes report `null`, never guessed identities. The true original source may be identical even when manifest byte provenance differs; **do not conflate unequal manifest hashes with unequal OHLCV bytes.**

`okx_bulk_bootstrap` and generic `archive_training.restore_verified_months` propagate this typed receipt into their *existing* per-asset failure reports, so a failed authenticated run produces a useful sanitized GitHub Actions artifact without new API calls, model creation or Drive mutation.

Exact-byte replay still succeeds, conflicting/tampered versions still fail, cross-asset provider mixing remains refused. Existing original-source readers, training thresholds, old holdout, model promotion and trade paths are unchanged.

## Next diagnosis using a real authenticated receipt

After PR-head CI and merged-main validation, inspect the output of the existing OKX bulk workflow. For every conflicting asset/month, compare the two **reported** SHA and original source provenance values. If the claimed history hashes differ, investigate actual source pages/byte evidence and producer timestamp before selecting any provider correction. If hashes match but manifest SHA differs, isolate provenance-version/publisher metadata differences; retain the unresolved conflict until an explicit verifiable policy is implemented and tested. Never choose the most recent by convenience.

Acceptance: real four-asset receipts with concrete, privacy-safe identities; identify the true publisher/root cause; verify authentic additional historical **evaluation days**, not candle counts; train again only when the causal inner research fold reaches the predeclared seven distinct UTC days. Original 150-target/21-day/80%-quote/after-cost independent qualification gates remain.

This is **a diagnostic repair, not proof of recovered OKX history or profitable XMarket predictions**.
