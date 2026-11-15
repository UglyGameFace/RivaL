# RivaL remaining backlog

Recorded from the owner's November 15, 2026 handoff. This is the full scope, not an assertion that every area was independently re-audited in one change. Engineering integration, hosted operation, independent prediction quality and executable-cost evidence are separate acceptance levels.

Verified checkpoint: `main@dc692ef` includes #603–#605. Authenticated [host receipt 43748283578](https://github.com/UglyGameFace/RivaL/actions/runs/43748283578) proves fresh collection/learning and five issuing XMarket scopes; it proves no qualified paper edge. Existing ordinary 5m models do not fill missing XMarket 5m slots. Website work stays paused.

| Area | Remaining acceptance |
|---|---|
| 1. Demonstrated prediction quality (#6) | Per exact source/artifact/horizon: enough independent genuinely future outcomes and days, calibrated baseline improvement, reliable paired quotes, positive conservative and severe after-cost results. No XMarket scope currently qualifies. |
| 2. XMarket coverage | Qualifying imported fits still missing for BTC/ETH/SOL/XRP 5m, ETH 15m/60m and SOL 60m. Preserve training/fidelity gates; a missing eligible fit is not a broken live worker. |
| 3. Usable learning winners (#299) | Demonstrate controlled improvements from pooled training/Model Lab, then verify the gated path to actual served predictions, degradation demotion and rollback. Weight updates alone do not establish improvement. |
| 4. Historical and live inputs (#295) | Verify core candles, funding, mark/index, open interest, liquidity and liquidation coverage; use controlled comparisons to determine which real inputs help. Preserve point-in-time availability and source identity. |
| 5. Exchange quote proof | Hyperliquid and Binance have matched quote observers. Eight other perpetual venues need equivalent source-matched evidence; Binance recently had no XMarket issuance. Never borrow another venue's quotes. |
| 6. Polymarket and Kalshi (#138) | Independently verify each venue's exact rules, native prices/outcomes, fees, calibration and qualification. Perpetual/XMarket repairs do not establish contract-market completion. Preserve Polymarket's separate continuous qualification policy. |
| 7. Entry/stop/TP1/TP2 (#151) | Prove actual path issuance and future settlement, calibrated target-before-stop probabilities and executable-cost proof. Contract-native Polymarket/Kalshi targets remain required. Latest baseline host receipt had zero settled close-target rows. |
| 8. Unattended reliability (#347) | Obtain actual deadlock/lock-graph evidence, resolve demonstrated contention, bound slow queries/pool pressure, and verify recovery through outages, restarts and backlogs. Watchdog success is not long-term acceptance. |
| 9. Infrastructure/recovery | Verify dedicated database isolation, backup restore and any required zero-loss separation. Resolve private DNS acceptance despite a working authenticated fallback. |
| 10. User experience | Verify Android Discord selection, readable model names, target cards, refresh and alert routing. Live website receipt publication has failures; website work remains paused. |
| 11. Broader original scope | DraftKings needs verified live contract-price integration. All-sports requires its own completion audit. Whale/on-chain intelligence stays deferred. |

## Current implementation outcome

Repair the demonstrated path-settlement starvation defect and expose actual path lifecycle counts in the existing authenticated host receipt. This advances area 7 and backlog handling in area 8. It does not establish path-model calibration, executable fills, profitable targets, additional fitted XMarket scopes, or completion of the other markets.

See [ACTIVE_TASK.md](../ACTIVE_TASK.md) and the implementing PR for exact test and hosted acceptance. Preserve this complete backlog when changing priorities; other conversations may work on independent areas after checking current repository overlap.
