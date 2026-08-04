# Investable factor-proxy falsifier — 2026-08-04

## Decision

The first positive factor-persistence result **does not survive** this frozen
liquid long-only translation. Equal thirds MTUM, QUAL and VLUE trailed SPY after
explicit portfolio turnover charges from January 2014 through December 2025.
All three preregistered gates failed. This result falsifies one implementation
proxy; it does not invalidate the published long-short factor result or prove
that every factor portfolio is inferior.

## Why this was the next rational test

The developed-ex-U.S. result used reconstructed long-short research returns and
fixed stress deductions. It did not measure a security-level portfolio,
turnover, liquidations, or a long-only benchmark. Opening another factor dataset
would have repeated the same claim class. This experiment instead asks whether
the exact value/quality/momentum mechanism has useful investable expression in
three liquid U.S. ETFs.

There was no search. The candidate, benchmark, dates, costs, folds, seed,
bootstrap and conjunction gate were externally anchored in Notion before any of
the four adjusted-close responses were fetched.

## Frozen contract

- Candidate: equal thirds MTUM, QUAL and VLUE; monthly rebalance.
- Comparator: SPY.
- Window: 144 monthly returns, January 2014–December 2025; December 2013 is
  used only as the initial month-end.
- Costs: 10 basis points one way at the primary setting; 0, 5, 10, 20 and 25
  basis-point sensitivities. Initial purchases and terminal liquidations are
  charged to both portfolios.
- Gate: one-sided 95% circular 12-month moving-block lower bound on annual
  active mean above zero, at least 3/4 positive three-year folds, and positive
  active Sharpe after removing the best 12 months.
- Data: bounded Yahoo chart adjusted-close responses, classified as secondary,
  unofficial and revisable.
- Policy: `sha256:90d50e7f96ceceb791ba3f3574d7fea57a7c10393143af0a1569b80380034aa2`.
- Evaluator: `sha256:ccf8f175ea31c81a5489c1093fad55b9f2a72245b86319585add6b9be12eeb1e`.
- External authority: Notion refetch at `2026-08-04T08:56:47.511Z`; authority
  receipt `sha256:b5883eeb0b436176575e7c2af36fd42ee3a9a1d7781b84b293ea5eb96456debb`.

Adjusted-close total returns already reflect fund operating expenses. The
current issuer expense ratios are disclosures and are not subtracted again:
[MTUM](https://www.ishares.com/us/products/251614/MTUM),
[QUAL](https://www.ishares.com/us/products/256101/QUAL),
[VLUE](https://www.ishares.com/us/products/251616/VLUE), and the
[SPY fact sheet](https://www.ssga.com/library-content/products/factsheets/etfs/us/factsheet-us-en-spy.pdf).

## Measured result

| Metric | Equal MTUM/QUAL/VLUE | SPY / active |
|---|---:|---:|
| Net CAGR at 10 bp | 12.30% | SPY 13.40% |
| Annual active mean | — | −0.95% |
| Active Sharpe | — | −0.318 |
| 95% block-bootstrap lower bound | — | −2.36% |
| Positive registered folds | — | 1/4 |
| Remove-best-12 active Sharpe | — | −1.034 |
| Annualized one-way turnover | 23.79% | — |

The only positive fold was 2017–2019 (+0.51% annual active mean). The other
three were negative: −0.23%, −2.04% and −2.05%. The verdict is
`INVESTABLE_FACTOR_PROXY_FAILS`; `promotion_eligible` remains false.

Result identity:
`sha256:4ecaaee5627c556c091d58a8ff19bcde2c78efca152e406f76153ffa8537dc58`.
Normalized panel identity:
`sha256:c43c3c9126dca62a71c172fb8f631e7478ed63707abdc334f275ad8d7905150a`.

## Independent verification and visual proof

A separate raw-JSON/NumPy reconstruction that imported no project evaluator
reproduced every raw hash, the panel hash, 144 returns, both CAGRs, active mean
and Sharpe, turnover, four fold means, the seeded 10,000-sample bootstrap,
remove-best statistic and semantic result hash exactly.

- `evidence/investable-factor/after.png` — result chart, SHA-256
  `06b785c01674c8798c3d6910b8de5cf5b16b045e20b46170338bb94a9e27b36b`.
- `evidence/investable-factor/comparison.png` — captured command-absent baseline
  beside the verified result, SHA-256
  `0e602f8d6a1704b278c7e55fc1f4ba188909b8f77fefbb36e03a28589635c466`.

## Claim boundary

This is meaningful negative news. A simple liquid long-only expression of the
selected factor mix did not add value over SPY in the opened period, and the
failure was broad rather than a marginal cost artifact. It does not establish
that the factors vanished: the ETF indexes differ from French factor
definitions, the portfolio is long-only, the window overlaps the earlier
research period, and Yahoo observations are not point-in-time provider
conformance or executable quotes. Further tuning on these opened 2014–2025 ETF
returns would be research leakage, not a new holdout.

## Reproduce

```powershell
uv run nodequant-investable-factor `
  --raw-directory evidence/investable-factor/raw `
  --output evidence/investable-factor/result-replay.json `
  --chart evidence/investable-factor/after-replay.svg `
  --reuse-raw
```
