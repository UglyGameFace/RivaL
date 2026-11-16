# RivaL remaining backlog

Recorded from the owner's November 15, 2026 handoff. This is the full scope, not an assertion that every area was independently re-audited in one change. Engineering integration, hosted operation, independent prediction quality and executable-cost evidence are separate acceptance levels.

Verified checkpoint: `main@dc692ef` includes #603–#605. Authenticated [host receipt 43748283578](https://github.com/UglyGameFace/RivaL/actions/runs/43748283578) proves fresh collection/learning and five issuing XMarket scopes; it proves no qualified paper edge. Existing ordinary 5m models do not fill missing XMarket 5m slots. Website work stays paused.

| Area | Remaining acceptance |
|---|---|
| 1. Demonstrated prediction quality (#6) | Per exact source/artifact/horizon: enough independent genuinely future outcomes and days, calibrated baseline improvement, reliable paired quotes, positive conservative and severe after-cost results. No XMarket scope currently qualifies. |
| 2. XMarket coverage | Qualifying imported fits still missing for BTC/ETH/SOL/XRP 5m, ETH 15m/60m and SOL 60m. Preserve training/fidelity gates; a missing eligible fit is not a broken live worker. |
| 3. Usable learning winners (#299) | Demonstrate controlled improvements from pooled training/Model Lab, then verify the gated path to actual served predictions, degradation demotion and rollback. Weight updates alone do not establish improvement. |
| 4. Historical and live inputs (#295) | Verify core candles, funding, mark/index, open interest, liquidity and liquidation coverage; use controlled comparisons to determine which real inputs help. Preserve point-in-time availability and source identity. |
| 5. Exchange quote proof | OKX and Bitget now have actual production SOL 15m entry/exit pairs after #611, alongside the existing Hyperliquid/Binance pipeline. Current integration adds Gate, Deribit linear-USDC and Lighter native books, fees and review capacity; their hosted entries/exits still require acceptance. Phemex, KuCoin and dYdX remain unsupported for paired cost proof. Binance recently remained connected without books and had no XMarket issuance. Never borrow another venue's quotes. |
| 6. Polymarket and Kalshi (#138) | Independently verify each venue's exact rules, native prices/outcomes, fees, calibration and qualification. Perpetual/XMarket repairs do not establish contract-market completion. Preserve Polymarket's separate continuous qualification policy. |
| 7. Entry/stop/TP1/TP2 (#151) | Prove actual path issuance and future settlement, calibrated target-before-stop probabilities and executable-cost proof. Contract-native Polymarket/Kalshi targets remain required. #610 recovered recent close-target processing (2,240 raw side records in the November 16 00:37 UTC receipt); older/missing-source backlog and independent calibration remain unresolved. |
| 8. Unattended reliability (#347) | Obtain actual deadlock/lock-graph evidence, resolve demonstrated contention, bound slow queries/pool pressure, and verify recovery through outages, restarts and backlogs. Watchdog success is not long-term acceptance. |
| 9. Infrastructure/recovery | Verify dedicated database isolation, backup restore and any required zero-loss separation. Resolve private DNS acceptance despite a working authenticated fallback. |
| 10. User experience | Verify Android Discord selection, readable model names, target cards, refresh and alert routing. Live website receipt publication has failures; website work remains paused. |
| 11. Broader original scope | DraftKings needs verified live contract-price integration. All-sports requires its own completion audit. Whale/on-chain intelligence stays deferred. |

## Current implementation outcome

Extend the shared paired quote pipeline to Gate, Deribit linear-USDC and Lighter, preserving native size/clock/source identity and fees. Increase bounded proof-review capacity so all seven registered venues stay within the unchanged 48-hour freshness gate. Require hosted native health and genuine entries/exits after release. OKX/Bitget source proof and current path settlement have advanced; model quality, the three unsupported quote sources and all other backlog areas remain unresolved.

See [ACTIVE_TASK.md](../ACTIVE_TASK.md), issue #151 and the implementing PR for exact tests and hosted acceptance. Preserve this complete backlog when changing priorities; other conversations may work on independent areas after checking current repository overlap.
