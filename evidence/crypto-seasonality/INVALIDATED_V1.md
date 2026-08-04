# Invalidated crypto seasonality discovery v1

Status: `INVALID_INCOMPLETE_DAY_ACCOUNTING`

The v1 evaluator admitted UTC dates even when Coinbase omitted one of the
hourly candles needed to calculate that date's returns. It converted the
unobserved interval to a zero return and grouped returns by their ending date
even though each strategy rule is defined by the interval's starting hour.

The BTC raw responses remain immutable evidence. The v1 policy and discovery
result remain preserved, but neither may authorize replication or support an
investment conclusion. Policy v2 reuses the frozen BTC bytes and excludes any
date without all 24 consecutive intervals starting at UTC hours 00 through 23. ETH replication
data had not been fetched when this defect was found.
