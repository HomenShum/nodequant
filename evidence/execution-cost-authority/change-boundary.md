# Frozen execution-cost slice — visual change boundary

## Capture contract

- Route: `POST /api/kernel/review`, reached through the UI's one same-origin
  `POST /api/trialscope-chat` request and isolated Playwright routing.
- Session: isolated local backend `127.0.0.1:18317` and frontend
  `127.0.0.1:18318`; no foreign listener or session was used.
- Viewport: 1100 × 1000 CSS pixels at device scale factor 2.
- Theme: light.
- Trigger: `review the backtest`.
- Data source: `proof_kernel.fixture_bars()`, the synthetic closed-form fixture;
  25 packets reduce to 579 ledger events.
- Surface: `[data-testid="backtest-review"]`.

## CHANGE A · cost blockers

The upper 3 px box is limited to `[data-testid="promotion-blockers"]`.

Before: the list contains six blockers, including `NO_SPREAD_MODEL` and
`NO_SLIPPAGE_MODEL`.

After contract: those two codes disappear only when a frozen policy is embodied
in every fill and independently verified by the receipt. The four unrelated
blockers remain: cross-stream reconciliation, synthetic corpus, market-data
conformance and oracle independence.

## CHANGE B · modeled cost metrics

The lower 3 px box is limited to the summary metric `<dl>`.

Before: final equity is $99,449.12, realised P&L is -$516.88, maximum drawdown is
-$553.40 and fees are $34.00. There are no spread or slippage rows.

After contract: add visibly non-zero `modelled spread` and `modelled slippage`
rows; final equity and realised P&L must worsen; fees remain the commission-only
$34.00. Modeled costs must never be labeled observed or provider-derived.

## Explicitly unchanged neighbors

The header, promotion badge/target, strategy identity, receipt integrity counters,
event count, surrounding chat chrome, composer and sidebar are outside both boxes
and unchanged. Event count remains 579 because costs are represented on fills
rather than new events. The policy hash is expected to change because the frozen
policy and its enforcement path change.

The equity/drawdown curve is deliberately not inside either annotation box: its
container geometry, SVG structure, styling and event timestamps are unchanged,
but its equity and drawdown y-values are expected to move when modeled costs enter
the ledger. It is a dependent data-output change, not an unchanged neighbor and
not a separate UI implementation surface.

`before.png` is the unannotated authority image. `change-boundary.png` is a
deterministic overlay on the stored BEFORE pixels. The upper outline contains the
blocker list. The lower outline is strictly limited to the summary `<dl>`; its
label sits inside right-side whitespace so no curve pixel is enclosed.
