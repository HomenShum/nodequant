# CFTC producer-hedging-pressure experiment

## Decision

The official event corpus is usable and point-in-time conservatism is explicit, but the first return result is **invalidated**. It cannot count for or against alpha because the human-readable portfolio accounting contract and the hashed evaluator disagree.

## Frozen experiment

- Official CFTC Disaggregated Futures Only dataset `72hh-3qpy`.
- Eight fixed contracts mapped to USO, UNG, GLD, SLV, CPER, CORN, SOYB and WEAT.
- Producer hedging pressure is `(producer short - producer long) / open interest`; hold the top three.
- Every Tuesday-dated report is embargoed 70 calendar days because the CFTC provides no complete historical release-timestamp series.
- One candidate, one seed, fixed costs, two folds and one conjunctive gate.
- CFTC rows were frozen before prices; the exact manifest was externally authorized before any adjusted-close byte was fetched.

## Corpus proof

- 5,424 official rows and 678 complete eight-contract reports.
- Report dates: 2013-01-08 through 2025-12-30.
- CFTC raw hash: `sha256:c510d18b02b35d3b3efc72d23635797efa451e9c5eb82710e46be91710dfa76a`.
- Manifest hash: `sha256:2da057db2fcbd93bdadc8e295cdd5b2dfadf4999d77fa0e93d871fef190ace1c`, independently rebuilt exactly.

## Measured but non-authoritative result

The registered evaluator reported active annual mean **+1.585%**, Sharpe **0.123**, block lower bound **-3.678%**, one of two folds positive and remove-best-10 Sharpe **-0.109**. The formal verdict was `CFTC_HEDGING_PRESSURE_FAILS`, result hash `sha256:2cdf0a3fb2b66d3cd332577a667d57ee839e9d08bf90afbf1d0835ed59b2a226`. Independent raw-JSON/NumPy reconstruction reproduced every raw hash, primary statistic, bootstrap quantile and result hash.

That result is not accepted. The prose says buy-and-hold equal-eight benchmark and turnover from rebalance weights; the evaluator instead applies equal weights mechanically each day and never drifts either sleeve. A post-outcome accounting sensitivity with actual weight drift also failed: active mean **-0.008%**, Sharpe **-0.001**, block lower **-4.199%**, folds **-2.953% / +4.088%**, remove-best-10 Sharpe **-0.363**. This sensitivity is diagnostic only, not a second trial.

## Claim boundary

The positioning corpus and two-stage authority pattern are retained as valid infrastructure. The return result is invalid evidence. ETF adjusted closes remain revisable proxies with roll, fund, collateral and execution differences. Nothing here can promote capital, certify a futures implementation or support investment advice.
