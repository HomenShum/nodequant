# Split discipline — visual change boundary

## BEFORE authority

`before.png` is a byte-for-byte copy of the observed honest oracle AFTER image,
SHA-256 `F53D4E77733E75B2A58BD7C4D46833A0FE82330B65F5E60ED64D371556623B93`.
It shows three blockers, an absolute event-time curve with no split geometry,
and the oracle's self-attested provenance footer.

Capture contract: `POST /api/kernel/review`, 1100 × 1000 CSS pixels, DPR 2,
light theme, one request, panel `[data-testid="backtest-review"]`.

## CHANGE A · self-awardable split claims become a blocker

Before: promotion silently defaults two caller booleans to "candidate frozen"
and "optimizer did not see test".

After: the booleans are absent from the promotion boundary. A frozen manifest
defines four chronological windows and tags every displayed point, while
`SPLIT_DISCIPLINE_NOT_PROVEN` remains visible because repository metadata cannot
prove actual access control or freeze time.

## CHANGE B · split-aware curve

Before: one undifferentiated absolute-equity curve.

After: one chart retains all splits together, renders persistent DEVELOPMENT,
VALIDATION, TEST and HOLDOUT bands, gives development lower emphasis, and
defaults equity to rebased-within-split. Absolute equity remains in the payload.
The footer must say `splits tagged true · access provenance self-attested`.

## Explicitly unchanged neighbors

Strategy identity, the original three blockers, all six economic summaries,
journal PASS/mismatch zero, oracle match/provenance status, and all authoritative
absolute equity payload values remain unchanged. The policy hash must move
because the enforcement path and frozen manifest hash are sealed by it.
