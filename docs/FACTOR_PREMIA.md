# Cross-geography factor-premium persistence

## Decision

The frozen `equal__HML_RMW_MOM` primary **passed** its single confirmatory
developed-ex-U.S. gate. This is NodeQuant's first positive confirmatory research
result. It is evidence that a stressed blend of published value, profitability,
and momentum factor returns persisted across a disjoint geography and later
period. It is **not** an investable return, an execution result, or permission to
promote capital.

## Why the result is admissible

- The policy searched all 15 nonempty equal-weight subsets of HML, RMW, CMA and
  momentum on U.S. returns from January 1964 through December 1990.
- The discovery rule selected one primary only: equal thirds HML, RMW and
  momentum. It survived all three 108-month discovery folds and removal of its
  best 12 months.
- A timestamped external authority was recorded before the developed-ex-U.S.
  ZIP bytes were read. The holdout therefore could not choose the candidate.
- Replication used exactly 420 contiguous months from January 1991 through
  December 2025, with zero missing, duplicate or invalid months.
- The only confirmatory statistic was frozen in advance: a one-sided 95%
  studentized circular 12-month moving-block-bootstrap lower bound on the
  annualized mean after the declared stress haircuts. It had to exceed zero.
- A separate reconstruction parsed the four frozen ZIPs without importing the
  project evaluator and reproduced the selection, panel hashes, result hashes,
  folds, bootstrap quantile and final lower bound.

## Measured result

| Measure | U.S. discovery | Developed ex-U.S. replication |
|---|---:|---:|
| Annualized mean after stress haircut | 4.10% | 3.73% |
| Sharpe | 0.898 | 0.837 |
| Robust Sharpe floor | 0.657 | 0.571 |
| Positive registered folds | 3 / 3 | 5 / 5 |

The confirmatory lower bound was **+2.249187% annualized**, above the frozen zero
threshold. The replication point estimate was not the gate.

The stress assumptions deduct 100 annual basis points from HML, RMW and CMA and
300 from momentum before combining sleeves. They are conservative scenario
haircuts, not measured trading costs.

Independent adversarial reconstruction also kept the conclusion positive under
block-percentile, block-basic, and HAC sensitivity intervals. Four durability
caveats remain: the frozen studentizer uses an IID standard error inside the
moving-block procedure; the replication ZIP hashes need a post-fetch immutable
receipt and atomic first-write verifier; one-primary isolation is metric-level
rather than byte-level because the five-factor ZIP necessarily exposes CMA; and
the external 07:52:44.452Z correction anchor, not the stale embedded policy
timestamp, is the authoritative v2 freeze time.

## Claim boundary

The Kenneth French library publishes reconstructed zero-cost research factor
returns. Those series do not provide constituent-level turnover, executable
prices, borrow availability, financing, taxes or market impact, and historical
values can change with source-data revisions. U.S. and developed-ex-U.S. panels
also share a producer, USD conversion and global shocks. Therefore:

- `verdict = FACTOR_PREMIUM_PERSISTS`
- `promotion_eligible = false`
- no CAGR or terminal-wealth claim is made for the zero-cost spread
- the next rung is an independently specified, point-in-time implementable
  portfolio with measured turnover, borrow, financing and execution costs

## Reproduce

```powershell
uv run --no-sync python -m app.factor_premia `
  --stage discovery `
  --policy research\factor_premia_policy_v2.json `
  --raw-directory evidence\factor-premia\raw-us-discovery `
  --output evidence\factor-premia\discovery-result.json `
  --reuse-raw

uv run --no-sync python -m app.factor_premia `
  --stage replication `
  --policy research\factor_premia_policy_v2.json `
  --raw-directory evidence\factor-premia\raw-developed-ex-us-replication `
  --discovery-result evidence\factor-premia\discovery-result.json `
  --authority research\factor_premia_replication_authority_v1.json `
  --output evidence\factor-premia\result.json `
  --reuse-raw
```

Primary artifacts:

- `research/factor_premia_policy_v2.json`
- `research/factor_premia_replication_authority_v1.json`
- `evidence/factor-premia/discovery-result.json`
- `evidence/factor-premia/result.json`
- `evidence/factor-premia/comparison.png` — SHA-256
  `5dcabf9a6132656e279ebf7a02ac03dd2a53aa5112c5cda332060193f2c39d54`

The invalid v1 parser contract is retained in
`evidence/factor-premia/INVALIDATED_V1.md`; it produced no strategy metric or
discovery result.

## Sources

- [Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html)
- [Fama and French, *A five-factor asset pricing model*](https://doi.org/10.1016/j.jfineco.2014.10.010)
- [Asness, Moskowitz and Pedersen, *Value and Momentum Everywhere*](https://doi.org/10.1111/jofi.12021)
