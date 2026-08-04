# Statistical-adequacy proof boundary

## Before

The review showed two TEST episodes but no explicit status for confidence
intervals, candidate-trial accounting, Deflated Sharpe, or PBO. A reader could
mistake absence for a completed clean check.

## Intended after

Render four typed `NOT_RUN` checks. The TEST confidence row carries the observed
episode count and frozen minimum; the other rows preserve missing observations
as null and name the artifacts required to run them.

## Out of scope

- No significance, Sharpe, PBO, or confidence number is estimated.
- No statistical check participates in promotion.
- The candidate-trial ledger and purged fold matrix remain separate work.
