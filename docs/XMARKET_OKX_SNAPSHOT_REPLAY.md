# XMarket/OKX original native month snapshot reconciliation

## Verified original-source failure

The true GitHub Actions read-only research run
[43878923264](https://github.com/UglyGameFace/RivaL/actions/runs/43878923264)
restored six Bitget source months per asset but excluded OKX for each coin,
plus BTC reference restores, with:
`HistoryImportError: conflicting native month snapshots`.

The authoritative implementation,
`research.source_native.restore_verified_source_months`, retained a set of
`(venue, contract, quote, month)` and raised on a second manifest with that
identity **without examining whether its verified bytes were identical**.
This can block an idempotent upload replay. A source conflict may also be
genuine, however, and must not be hidden.

## Minimal source-safe repair

For each native month, verify Drive folder ancestry, object metadata, the
complete downloaded manifest SHA-256, source/schema/contract identity and
filename. Verify the first month's corresponding original history bytes
and any required source witnesses before recording its identity.

A second manifest with the **same scope and the exact same manifest SHA**
is a strict byte-identical replay, so skip it without duplicating a
training stream, modifying originals or refetching its already-verified
history. A second manifest with a different SHA continues raising
`conflicting native month snapshots`; a tampered duplicate fails SHA
verification before replay handling. No freshness heuristic, "last wins",
renaming, cross-venue borrow or silent conflict resolution.

These changes do not establish whether the actual conflicting OKX Drive
objects have equal or different hashes; only a new authenticated original
read can prove that. If errors persist, preserve both original SHA
identities and investigate the publisher producing conflicting versions.
A true conflict is not a reason to train on either copy.

## Qualification and validation

- Tests exercise two byte-identical Drive objects (one restored month),
  divergent manifest provenance (hard failure), and falsified replay SHA
  (hard failure).
- Exact-branch and merged-main Ruff, full SQLite/PostgreSQL CI and
  original source restores should pass. Production confirmation requires
  a fresh read-only OKX restore outcome for BTC/ETH/SOL/XRP.
- Even restored months cannot substitute for seven distinct UTC days in
  the *inner original-training* evaluation fold. The November 16 XMarket
  research run had only two: model improvement, XMarket 5m fitting,
  prospective superiority and positive after-cost edge remain unproved.
- No production model weights, served predictions, promotion thresholds,
  quote requirements, data records, source publishing or user orders change.
