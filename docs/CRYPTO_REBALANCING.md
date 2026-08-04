# Coinbase crypto rebalancing-premium experiment

## Decision

The fresh-universe monthly fixed-weight portfolio produced a large positive
economic difference from buy-and-hold, but it **fails the frozen robustness
gate**. The result is useful allocation evidence and a strong lead; it is not
robust alpha, execution evidence or promotion authority.

## V1 invalidation

Policy v1 opened SOL, LINK, LTC, BCH and ADA but produced no result. Coinbase
may return candles preceding a requested start, so adjacent chunks repeated six
boundary dates per product. All 30 repeated opens agreed exactly, but the v1
parser rejected every repetition. V1 status is
`INVALID_PROVIDER_OVERLAP_CONTRACT`.

A post-outcome diagnostic at 25 bp one-way showed 10.56% fixed-weight CAGR
versus 4.25% buy-and-hold, +3.33% annual active mean and two positive folds, but
remove-best-ten active Sharpe was -0.416. It is not registered evidence.

## Fresh v2 contract

- Products: DOGE-USD, XLM-USD, ATOM-USD, ETC-USD and ZEC-USD.
- Closed window: 2021-07-01 through 2025-12-31, 1,645 exact common UTC opens.
- Monthly reset to equal weight versus the same initial portfolio left to drift.
- Consecutive UTC open-to-open returns; bucket timestamps are never treated as closes.
- Exact repeated boundaries are deduplicated; conflicts and missing dates fail.
- 25 bp one-way primary cost on half-L1 rebalance turnover; 0/25/50 sensitivities.
- Two fixed time folds, 30-day circular block bootstrap and remove-best-ten gate.
- Corrected policy hash: `sha256:f7dc55096fbe29dc46e0e3587910aae5f4504849f7d4129632a505fc518a120b`.
- Evaluator hash: `sha256:600a94f00d54a05b236506a62d957b9b2fe9b1d701bd50ebff115ab267156b57`.

The first v2 external anchor was rejected before data because its guessed freeze
label was three seconds later than the Notion refetch. The corrected policy was
refetched after its observed freeze time and is the only authorized hash.

## Measured result

At 25 bp one-way, fixed-weight CAGR is **11.07%** versus buy-and-hold **2.69%**.
Annual active mean is **+5.17%**, active Sharpe **0.274**, annual one-way turnover
**76.17%**, and both folds are positive (**+7.61%** and **+2.12%**).

The one-sided 95% 30-day block lower bound is **-10.11%**, and removing the best
ten days yields active Sharpe **-0.379**. Two of three checks fail. Verdict:
`CRYPTO_REBALANCING_PREMIUM_FAILS`; result hash
`sha256:e82a5319893ca2015b864c5ab0cfa601f3cbdf3c6d2ae78ec4ab99cae5474ec9`.

An independent raw-file/NumPy reconstruction imported no project evaluator and
matched all raw hashes, 1,645 common dates, cost sensitivities, folds, bootstrap,
remove-best statistic and result hash exactly.

## Claim boundary

The fixed currently available universe creates survivorship and listing-selection
bias. Daily opens are first trades rather than executable quotes. Fees, spreads,
slippage, taxes, custody, staking, capacity and delistings are incomplete. The
result supports a future point-in-time-universe replication, not capital deployment.

