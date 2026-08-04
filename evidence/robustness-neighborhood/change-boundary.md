# Robustness-neighborhood proof boundary

## Before

The review had one strategy result and matched-entry nulls, but no frozen rule
ablation or nearby-parameter results. A reader could not tell whether the
reported behavior survived small definition changes.

## Intended after

Render nine manifest-bound variants across DEVELOPMENT, VALIDATION, TEST, and
OPENED_HOLDOUT.
Before any variant is exposed, the baseline simulator must reproduce the bound
ledger's split P&L exactly. Missing split episodes render `NOT_RUN`, never zero.

## Out of scope

- The suite was authored post-hoc, has no verified test-access provenance, and
  two variants observe OPENED_HOLDOUT; it is unusable for selection.
- No robustness score or variant selects/promotes a strategy.
- This is synthetic-fixture behavior, not market evidence.
