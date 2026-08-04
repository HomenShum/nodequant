# Split-local performance — visual change boundary

## BEFORE authority

`before.png` is a byte-for-byte copy of the approved split-discipline AFTER,
SHA-256 `83974739B809DF6A99AC03492A6082D465F35A2ACA64DB580DE3198F5E36F54C`.
It proves four bands and point tags but shows only one blended summary.

Capture contract remains `POST /api/kernel/review`, 1100 × 1000 CSS pixels,
DPR 2, light theme, one request, panel `[data-testid="backtest-review"]`.

## CHANGE · metrics and worst drawdown are split-local

After: add a table with one row for DEVELOPMENT, VALIDATION, TEST and HOLDOUT.
Populated rows show net P&L, maximum drawdown, time underwater, episodes whose
fills fall inside the drawdown interval, and recovery state. Each split carries
in the last pre-split equity so its first event cannot disappear, then resets
its peak logic at the boundary. HOLDOUT must read `NOT_RUN — no observations`,
never zero. An unrecovered row is explicitly observation-censored.

The table must explicitly disclose event-time sampling. It may not blend a
development and test metric or infer observations for the empty holdout band.

## Explicitly unchanged neighbors

All four blockers, split bands, rebased curve, six absolute summary values,
579-point absolute payload hash, journal/oracle/split provenance footer, and
policy hash remain unchanged.
