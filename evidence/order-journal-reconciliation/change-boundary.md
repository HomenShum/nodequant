# Order-journal reconciliation — visual change boundary

## Reused BEFORE authority

`before.png` is a byte-for-byte copy of
`evidence/execution-cost-authority/after.png`; both have SHA-256
`04F0B2B021E6861A0171726FDEF9630E428A293E9067FA29C9AE28CB8804A4B5`.
No base pixels were recaptured or regenerated.

The inherited capture contract is `POST /api/kernel/review`, trigger
`review the backtest`, 1100 × 1000 CSS pixels, DPR 2, light theme, one request,
and the synthetic `proof_kernel.fixture_bars()` source. The panel is
`[data-testid="backtest-review"]`.

## CHANGE A · reconciliation blocker

The upper 3 px box contains only
`CROSS_STREAM_RECONCILIATION_NOT_IMPLEMENTED` and its detail.

Before: no independent order-lifecycle stream reconciles quantity, fills, cash,
fees, marks and IDs.

After contract: this one blocker disappears only after a verifier-backed journal
receipt proves those joins and mutation tests kill coherent ledger/journal
tampering. The remaining blockers are exactly `SYNTHETIC_CORPUS`,
`MARKET_DATA_CONFORMANCE_NOT_RUN` and `ORACLE_NOT_INDEPENDENT_ENOUGH`.

## CHANGE B · journal receipt footer

The lower 3 px box contains only the receipt footer and policy hash.

Before: the footer has curve/authority/price counters and 579 events, but no
cross-stream status.

After contract: add a visible `journal reconciliation PASS` status and zero
named reconciliation mismatches. `PASS` must come from an independent journal
receipt, never a caller boolean or the financial ledger reducing itself.

## Explicitly unchanged neighbors

The header, BLOCKED → PAPER_SHADOW badge, strategy identity, equity/drawdown
curve, complete summary—including final equity $99,447.58, realised P&L
-$518.42, maximum drawdown -$554.90, fees $34.00, modelled spread $1.03 and
modelled slippage $0.52—event count 579, current policy hash, and surrounding
chat chrome remain unchanged. The blocker list changes only at the boxed first
row; the other three blocker rows remain byte-for-byte in meaning and order.

`change-boundary.png` is a deterministic overlay on the copied BEFORE pixels;
the unboxed header, remaining blockers, curve and summary were not edited.
