# Trial-ledger proof boundary

## Before

Nine frozen robustness rows were visible, but there was no immutable per-trial
definition/result hash ledger. Statistical adequacy therefore reported the
candidate-trial ledger as `NOT_RUN`.

## Intended after

Preserve definition and result hashes for all nine rows in the current batch,
and compare the ordered definition universe and ordered result root against the
policy-bound experiment manifest.
Report batch completeness separately from global completeness and pre-test
registration. Because the latter two remain unverified and two trials observed
OPENED_HOLDOUT, the statistical check becomes `FAIL`, never `PASS`.

## Out of scope

- The repository cannot prove no discarded trials existed before this batch.
- The ledger does not clear Deflated Sharpe, PBO, or promotion.
