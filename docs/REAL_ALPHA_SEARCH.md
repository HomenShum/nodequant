# Real daily-data alpha search — 2026-08-03

## Decision

No robust positive alpha was found in the two preregistered daily-ETF generations.
The strongest candidate produced a real positive test result, but it underperformed
the always-long comparator in the final holdout and therefore failed. Do not tune
another daily-price rule on this now-open 2007–2025 panel and call it out of sample.

This is a useful negative result, not a promotion result and not investment advice.

## Why the earlier work produced no measured news

The existing graph loop evaluated a synthetic 25-packet fixture. Its two sourced
alpha hypotheses required intraday session bars and quotes, so both remained
`BLOCKED_REAL_SESSION_DATA`. The architecture could prove that evidence was missing,
but it had no runnable path to measure genuine market outcomes.

The corrective increment added a separate diagnostic runner over genuine daily
adjusted closes. It did not alter the sealed proof kernel and cannot issue
`REAL_BAR_REPLAY`.

## Frozen experiment contract

- Provider: Yahoo Finance chart endpoint, classified as secondary/unofficial and
  diagnostic only.
- Frozen panel: 4,677 common rows for SPY, QQQ, IWM, TLT, GLD, and BIL from
  2007-06-01 through 2025-12-31.
- Data quality: zero null, duplicate, invalid, or non-common rows in the frozen
  six-symbol panel; dates strictly increase.
- Timing: monthly decisions from the previous month-end adjusted close, then one
  complete daily bar before exposure. Same-bar execution is prohibited.
- Costs: 10 basis points one way at baseline; 5, 20, and 25 basis-point stresses.
- Benchmark: equal-weight always-long risky basket; inactive strategy weight earns
  the BIL adjusted-close return.
- Selection: minimum of development Sharpe, validation Sharpe, combined 25-bp
  Sharpe, and combined Sharpe after removal of the best five days.
- Required test checks: beat always-long Sharpe, positive CAGR, improve drawdown,
  remain nonnegative at 25 basis points, and remain nonnegative after omitting each
  risky asset.
- Holdout requirement: positive net CAGR and excess Sharpe plus a positive lower
  bound from a 5,000-sample paired 20-day block bootstrap of active return.

The Sharpe values here are excess-return Sharpe ratios relative to the BIL cash
proxy. They are not the external papers' reported Sharpes.

## Generation 1 — absolute trend

Eight candidates and one inverted control were frozen before outcomes: three
lookback-return rules, three moving-average rules, one multi-horizon consensus,
always-long, and inverted 12-month trend.

- Selected candidate: `tsmom_252`.
- Development/validation robust-floor improvement over always-long: **+0.349**.
- Untouched 2016–2019 candidate excess Sharpe: **0.593**.
- Untouched 2016–2019 always-long excess Sharpe: **1.305**.
- Candidate CAGR: **5.5%**, versus **12.4%** always-long.
- Candidate drawdown: **−13.9%**, versus **−13.0%** always-long.
- Final status: **TEST FAIL**; 2020–2025 holdout remained unopened in this
  generation.

Mechanism: the long-cash trend filter retained positive absolute performance and
survived 25-bp costs and every leave-one-asset-out slice, but its defensive cash
allocation imposed more opportunity cost than drawdown benefit during 2016–2019.

## Generation 2 — relative and dual momentum

Generation 1 ended before the second family was frozen. Nine additional trials
tested top-one/top-two relative momentum, absolute-gated dual momentum, a
three-horizon composite, always-long, and a bottom-one falsifier. The cumulative
trial count is 18.

- Selected candidate: `composite_top2`.
- Development/validation robust-floor improvement over always-long: **+0.100**.
- 2020–2022 test excess Sharpe: **0.495**, versus **0.331** always-long.
- 2020–2022 test CAGR: **7.9%**, versus **4.9%** always-long.
- 2020–2022 drawdown: **−21.8%**, versus **−25.5%** always-long.
- All six test checks passed, including 25-bp cost stress and every
  leave-one-asset-out slice.

The final holdout reversed the result:

- 2023–2025 holdout excess Sharpe: **0.818**, versus **1.224** always-long.
- Holdout CAGR: **16.2%**, versus **20.7%** always-long.
- Holdout drawdown: **−11.1%**, versus **−12.8%** always-long.
- Paired block-bootstrap 95% lower bound on annual active return: **−13.8%**.
- Only **6 of 18** complete calendar years had positive active return.
- Final status: **HOLDOUT FAIL**.

Mechanism: cross-asset rotation improved the difficult 2020–2022 test period and
slightly reduced final-holdout drawdown, but its active return was sparse and
regime-concentrated. It did not compensate for repeatedly rotating away from the
strongest persistent risky assets.

## Data revision incident

A second provider fetch returned the same 4,677 dates but changed thousands of
adjusted-close floats by tiny amounts; the largest observed difference was
0.000244 price units. The initial Generation-2 refetch result is preserved as
invalid evidence. The runner now supports frozen-byte replay, and the valid
Generation-2 result was recomputed from Generation-1's exact raw files.

Two independent frozen-byte reruns originally produced result hash
`sha256:2eaf27a08a43e1d92943005fc487b8a1103fb7a966cbc7f8a53de2cc43aae8b7`.
The breadth-search independent recount later found that `always_long` waited for
the first month boundary instead of investing on the first usable return. That
old identity is therefore retained as superseded evidence, not the current
result.

After the baseline repair, the frozen-byte Generation-2 identity is:

`sha256:5ffddc19c9a703411debd50a1999f1302706588f027cffd336af78c321a4c257`

The rounded reported conclusions do not change: the test still passes and the
holdout still fails, with 0.818 versus 1.224 holdout excess Sharpe and a -13.8%
active-return lower bound.

The timestamp is excluded from result identity; every load-bearing result field is
included. This establishes local deterministic replay of the snapshot, not provider
conformance.

## Research synthesis

The positive intermediate finding is real but insufficient: relative momentum
passed a difficult three-year test under costs, drawdown, and asset-removal stress.
The final claim is negative because that improvement did not persist in 2023–2025.

Three daily-price families are now substantially covered on this small ETF panel:
absolute trend, moving-average trend, and cross-asset relative/dual momentum.
Further parameter search on the same opened years would increase trial count without
creating new evidence. Volatility management is also a weak next bet: published
out-of-sample work reports that in-sample gains need not translate into a better
investor opportunity set.

The next honest alpha experiments require a new information plane or future data:

1. Licensed intraday SPY/QQQ bars and executable quotes for the already frozen Gao
   and noise-area hypotheses.
2. Point-in-time ClinicalTrials.gov version events linked to public sponsors, with
   packets committed before market outcomes are revealed.
3. Prospective shadow packets after 2026-08-03. This is the only way to create a new
   untouched period without pretending the opened 2007–2025 panel is still hidden.

The final procurement check found no usable licensed intraday corpus in the
discovery catalog. Concretum's implementation requires a vendor subscription; a
Kaggle minute-bar lead lacks executable quotes and provider replay; and Databento's
official historical API requires an account and API key. The environment contains
no `DATABENTO_API_KEY` and no `.env` file. Databento advertises signup credits, but
accepting terms and creating an account are user-controlled external actions. This
is the concrete stop condition for the intraday rung.

## Primary research sources

- [Moskowitz, Ooi, and Pedersen — Time Series Momentum](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf)
- [Faber — A Quantitative Approach to Tactical Asset Allocation](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461)
- [Antonacci — Optimal Momentum: A Global Cross Asset Approach](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1833722)
- [Moreira and Muir — Volatility-Managed Portfolios](https://www.nber.org/papers/w22208)
- [Cederburg et al. — On the Performance of Volatility-Managed Portfolios](https://www.sciencedirect.com/science/article/abs/pii/S0304405X2030132X)
- [awesome-systematic-trading discovery catalog](https://github.com/paperswithbacktest/awesome-systematic-trading) — discovery only, never metric or data authority.

## Reproduction

```powershell
uv run --no-sync pytest -q tests\test_real_alpha.py
uv run --no-sync python -m app.real_alpha `
  --policy research/real_alpha_policy_g2.json `
  --output evidence/real-alpha-search/result-g2.json `
  --raw-directory evidence/real-alpha-search/raw `
  --reuse-raw
cd web
node capture-real-alpha.mjs
```

Evidence:

- `evidence/real-alpha-search/before.txt`
- `evidence/real-alpha-search/result.json`
- `evidence/real-alpha-search/result-g2.json`
- `evidence/real-alpha-search/result-g2-rerun.json`
- `evidence/real-alpha-search/result-g2-invalid-refetch.json`
- `evidence/real-alpha-search/after.png`
