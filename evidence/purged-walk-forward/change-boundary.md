# Purged walk-forward proof boundary

## Before

The current batch had split results but no candidate-by-fold matrix. PBO was
`NOT_RUN` because its primary input did not exist.

## Intended after

Render all nine variants across three policy-bound evaluation folds. Every fold
ends before TEST and has 30 complete one-minute bars between training and
evaluation. The ordered matrix root is policy-bound.

## Out of scope

- The folds and variants are post-hoc and access provenance is self-attested.
- Three folds plus an incomplete global trial ledger do not establish PBO.
- The matrix is synthetic diagnostic evidence and cannot promote a strategy.
