# Post-publication FOMC drift falsifier

## Decision

The famous FOMC return pattern does **not** survive this frozen 2016-2025
post-publication daily-bar proxy after the preregistered 5 bp one-way cost. The
result is a useful contradiction, not alpha and not a replication of the
original 2 p.m.-to-2 p.m. effect.

## Frozen causal order

1. Official Federal Reserve calendar pages were frozen and parsed.
2. Policy v1 was rejected because its event-count expectation and parser were
   wrong. Policy v2 selected the correct dates but was rejected because modern
   row provenance inherited a stale heading.
3. Policy v3 froze 79 scheduled statement dates, the evaluator, IVV, the closed
   date window, costs, folds, bootstrap seed and one conjunctive gate.
4. The exact event manifest was externally anchored in Notion while IVV bytes
   remained unread.
5. A second external authority bound that manifest and IVV window; only then
   was the adjusted-close response created and evaluated once.

## Corpus proof

- 79 official scheduled statement dates: 2016-01-27 through 2025-12-10.
- Exact annual counts: 8, 8, 8, 8, 7, 8, 8, 8, 8, 8.
- Policy hash: `sha256:fbf46bac88cfee7dbca1d7f9c9b1125735b134580aef2977bee76be12dea061f`.
- Evaluator hash: `sha256:754d1c1703784e4cafadfb38e156228424dd878fc61c8c595066fa7cc9ee118e`.
- Event-manifest hash: `sha256:207a8a9739fbff734b74a1dd44a257fbe4443a3982171e97f2c016efbb3d6f76`.
- IVV response hash: `sha256:15eab395b83f29fa06b40b14acb344e891673e9b5d34cfb9e379fa6dc4a5eb99`.
- Result hash: `sha256:4c13693dbe48937e45f1a71dd565344c4241e0b50aebbbd534b0e8b3525ff7ba`.

An independent reconstruction that imported no project evaluator reproduced
the price hash, all 79 event outcomes, primary mean and Sharpe, bootstrap lower
bound, folds, remove-best statistic and result hash exactly.

## Measured result

Before cost, the event return averaged **+9.319 bp** and the year-matched active
return averaged **+3.325 bp**. At the frozen 5 bp one-way cost (10 bp round
trip), the event mean becomes **-0.681 bp** and the year-matched active mean
becomes **-6.675 bp**, with annualized active Sharpe **-0.172**.

The one-sided 95% circular-block lower bound is **-26.556 bp**. Both time folds
are negative (**-10.889 bp** for 2016-2020 and **-2.567 bp** for 2021-2025), and
removing the best five events produces event Sharpe **-0.721**. All three frozen
gate checks fail. Verdict: `POST_PUBLICATION_FOMC_DRIFT_FAILS`.

## Claim boundary

This is a daily close-to-close post-publication falsifier. It shifts and widens
the original intraday window, uses a secondary revisable adjusted-close source,
and does not model intraday spreads, slippage, taxes, capacity or cash yield. It
cannot promote capital or establish that every formulation of the historical
pre-FOMC effect has disappeared.

