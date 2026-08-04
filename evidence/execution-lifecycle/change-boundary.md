# Execution lifecycle proof boundary

## Before

The canonical journal only exercised created, approved, submitted and complete
fill events. Partial fills, cancel, reject, expiry and latency failure were
named event types but had no executed state-machine scenarios.

## Intended after

Render a bounded six-scenario diagnostic suite with quantity conservation,
transition validation and explicit terminal outcomes.

## Out of scope

- Synthetic scenarios are not broker receipts or fill-quality evidence.
- No partial-fill policy is applied to the canonical backtest without quotes,
  displayed size and venue/broker testimony.
- This suite cannot clear a promotion blocker.
