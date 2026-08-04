# Invalidated crypto seasonality replication v2

Status: `INVALID_CANDLE_TIMESTAMP_SEMANTICS`

Coinbase documents candle `time` as the bucket start. The v2 loader stored the
bucket's closing price under that start timestamp, then treated the timestamp as
the close instant. Every hourly close-to-close return was consequently assigned
one hour early. This changes two frozen BTC UTC candidate labels and materially
changes the reported ETH Wednesday point estimates.

The negative verdict is directionally unchanged under a no-network corrected
timing sensitivity: only positive CAGR passes; robust improvement, annual fold
breadth, and the bootstrap lower bound still fail. That sensitivity is post-hoc
because ETH is now open and is not a replacement replication.

The v2 evaluator identity is also incomplete: its hash omitted raw parsing,
fold masks, bootstrap logic, discovery loading, and authority verification.
December 31 coverage was misstated because the settlement boundary was not
modeled explicitly. A future experiment must bind every transitive semantic
dependency, model bucket start and close instant separately, obtain predecessor
and settlement candles, and use a genuinely unread holdout.

The raw data, policy, authority, result, and visibly watermarked screenshot are
retained as immutable failed-run evidence. None may support an alpha, replication,
or promotion claim.
