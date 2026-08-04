# Breadth-first daily alpha search

## Decision

A sealed 14-candidate replication slate produced a positive panel winner, but
no replication survivor. The current verdict is `NO_REPLICATION_SURVIVOR`.

The search now measures substantially more than the original eighteen trials:

- **100** frozen discovery candidates across **13** strategy families;
- trend, momentum, risk allocation, volatility, reversal, correlation and
  calendar mechanisms;
- **1** deterministic synthesis trial;
- **14** family/synthesis representatives sealed with the evaluator before the
  final replication universe was fetched;
- **116** total measured trials including the replication baseline.

## Three invalid results were preserved, not hidden

Independent recount and adversarial review found three evaluator defects before
the current result was accepted:

1. `always_long` remained in cash until the first month boundary while calendar
   candidates could invest earlier;
2. price-ranking rules selected arbitrary assets from an all-zero vector before
   their declared lookback existed;
3. target weights were reused in daily returns while turnover was charged only
   when targets changed, mixing daily-rebalanced returns with monthly costs.

The old JSON now embeds an invalidation status, each directory has an
`INVALIDATED.md`, and the old polished screenshot is watermarked. The current
engine drifts weights self-financing between explicit rebalance dates and charges
turnover from drifted pre-trade weights. A divergent-asset knockout scenario
locks that behavior.

## Temporal and evidence boundary

The SPY/QQQ/IWM/TLT/GLD/BIL panel is `OPENED_CORPUS_DISCOVERY_ONLY`. It ranks
family representatives but cannot prove out-of-sample performance.

The final selection seal is:

`sha256:fe6d38d21113a63d0616dad1e864e5ffe5e18a01b8efb3f8ed51f7a5199bf8c5`

It binds evaluator:

`sha256:63706d3ff8954202dceb728e555e0c6dcefd730c94096eab7d16ce5bec5962c8`

The final SPTM/EWC/BWX/DBB/ICF/BIL replication panel contains **4,529** common
rows from 2008-01-02 through 2025-12-31, with zero null, duplicate, invalid or
noncommon rows. Its normalized hash is:

`sha256:88d1757322a82dd743fcde7e670bd7895cbb4fef75907f760e2290c341c8fd7b`

Frozen-byte replay produced result identity:

`sha256:8534370821ef4537fb242c135f0bf22d4c407007f21a62408f9e5548cd7de384`

## Measured result

The three-horizon top-three composite-momentum representative ranked first on
the final replication panel:

| Metric | Composite momentum | Always-long |
|---|---:|---:|
| Robust excess-Sharpe floor | 0.376 | 0.229 |
| Full-period excess Sharpe | 0.473 | 0.336 |
| Net CAGR | 6.77% | 5.44% |
| Maximum drawdown | -31.44% | -50.39% |
| Annualized turnover | 4.07 | 0.36 |
| Replication folds beating baseline | 1 of 3 | - |

These are positive point measurements: higher risk-adjusted return, higher CAGR
and a drawdown improvement of roughly 19 percentage points under 10-bp base
costs and 25-bp stress.

They are not alpha evidence. The candidate was **not** a discovery survivor,
only one of three replication folds beat always-long, and the paired 20-day
block-bootstrap 95% lower bound on annual active return was **-4.29%**. The
uncertainty therefore prevents an alpha claim; it does not prove alpha is absent.

An independent raw-JSON/Numpy implementation reproduced every displayed metric,
fold value, robust-floor component and bootstrap bound to less than `1e-12`.

## Why this remains diagnostic

- Yahoo adjusted closes are secondary observations, not executable quotes.
- The current symbol list does not establish point-in-time universe membership.
- Cross-universe replication shares global time regimes and is weaker than a
  future-time holdout.
- Costs cover turnover at 10 and 25 basis points, not taxes, impact, borrow or
  account-specific fees.
- All 116 trials remain counted; source-reported Sharpe never enters evaluation.
- File timestamps support seal-before-fetch ordering, but the local untracked
  tree is not an immutable external chronology authority.

## Reproduction

```powershell
uv run --no-sync pytest -q tests\test_alpha_breadth.py tests\test_real_alpha.py
uv run --no-sync python -m app.alpha_breadth `
  --policy research/alpha_breadth_policy_v4.json `
  --output evidence/breadth-first-alpha-v4/result.json `
  --raw-directory evidence/real-alpha-search/raw `
  --replication-raw-directory evidence/breadth-first-alpha-v4/raw-replication `
  --replication-snapshot research/alpha_breadth_replication_snapshot_v4.json `
  --reuse-raw
```

Primary source metadata and implementation caveats are recorded in
`research/sources/catalog.json`; awesome-systematic-trading was used only as a
discovery index.
