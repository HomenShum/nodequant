# Worst-drawdown proof boundary

## Before

The review payload already carried split-local peak, trough, recovery, decline,
recovery, underwater, and contributing-episode measurements. The UI exposed
only maximum depth, aggregate underwater time, episode count, and a coarse
recovery label.

## Intended after

Render one mandatory worst-drawdown row per observed split from typed
measurements. Separate the worst interval's underwater duration from the
split-wide aggregate; derive episode spans that overlap the interval. Preserve
explicit censoring for unrecovered observations and do not infer missing
recovery timestamps, durations, or causal loss attribution.

## Out of scope

- No new performance arithmetic.
- No QuantStats dependency or authority claim.
- No promotion-policy change.
- No claim that event-time sampling captures intrabar extrema.
