# Crypto seasonality: BTC discovery to ETH replication

Status: **v2 invalidated; corrected post-hoc sensitivity remains negative**.
This branch is frozen. It does not establish alpha, valid replication, or
eligibility for promotion or live capital.

## Question

Do UTC intraday sessions or calendar partitions produce a cost-adjusted return
advantage that survives a change in both asset and time? The experiment puts a
recent positive Bitcoin overnight hypothesis against the stronger published
prior that cryptocurrency calendar effects vary across exchanges and do not
persist consistently.

## Frozen experiment

- Discovery: BTC-USD hourly Coinbase candles, 2020-2022.
- Replication: previously unread ETH-USD candles, 2023-2025.
- Discovery slate: 106 candidates across UTC hour, UTC session, weekday, and
  weekday/weekend families, including buy-and-hold.
- Replication slate: one deterministic winner from each family plus
  buy-and-hold, regardless of whether the family survived discovery.
- Costs: 10 bp one way, stressed at 5 and 25 bp.
- Daily sample: all 24 consecutive intervals beginning at UTC hours 00 through
  23 must be observed. A missing interval excludes that UTC day.
- Gate: at least 0.10 robust-Sharpe-floor improvement, two of three annual fold
  wins, positive net CAGR, and a positive paired block-bootstrap lower bound.

Policy v1 and its discovery result are preserved but invalid because they
grouped returns by interval end date and admitted incomplete days. Policy v2
was externally anchored in Notion at `2026-08-04T07:13:10.931Z` before the
first ETH request, but a later adversarial audit found a deeper provider-time
defect: Coinbase candle timestamps are bucket starts, while v2 treated them as
close instants. V2 is `INVALID_CANDLE_TIMESTAMP_SEMANTICS`.

## Result and invalidation

The stored v2 artifact reported a Wednesday-only ETH lead. Those exact metrics
and two frozen UTC candidate labels are invalid because every return was
assigned one hour early. They must not be quoted as measured performance.

A no-network corrected-timing sensitivity shifted the BTC hourly winner from
start 20 to start 21 and the session winner from start 9 to start 10. Evaluating
the four originally frozen candidates on the already-open ETH bytes still
failed the same three decisive gates. Wednesday remained the post-hoc champion
but weakened to approximately 21.63% CAGR, 0.852 Sharpe, and a 0.182 robust
Sharpe floor; the paired active bootstrap lower bound fell to -92.75%. These are
diagnostic sensitivity estimates, not replacement replication evidence.

The evaluator hash also omitted raw parsing, fold masks, bootstrap logic,
discovery loading, and authority verification. A function mutation could
therefore change gate semantics without changing the authority-visible hash.
The declared folds also omitted terminal December 31 because settlement
boundaries were not represented explicitly. Neither defect changed the negative
verdict under sensitivity, but both invalidate the claimed proof identity.

## Invalid artifact identity

The hashes below identify the preserved failed run; they do not certify its
scientific validity.

- Policy: `sha256:9748f3b15e755642192ea7595777edb1e1e949d8f5455f8d25842fa7a5499433`
- Evaluator: `sha256:58c010afc16765b774abeaba0417b813ba565074de17fbe4f9322b1b570ff549`
- BTC panel: `sha256:86605449424c1143fbc2f19d2670c336e667cc1fd6a360cfa442abd055ea4cd9`
- Discovery result: `sha256:909019a0d40824702907f418108364e862e54f439c93b6cc4eef12e806687a83`
- Selection seal: `sha256:4df483b638a9eddfc4e7a39908d2a48250f5f06489cfb3a1c23ff7faabaf654d`
- Authority file: `sha256:b0f44558da11639eda00fe50a65809f87491f751646ba0635312fa94d54c63ca`
- ETH panel: `sha256:2912f1c3f9fae5b6a69fed878d0aef98e898f6d4cb3fd423a95428ca756d6b41`
- Result: `sha256:f0155de43e8fc0d7128b1a0e6b3d91fb1f1cd83fb6cf2efb58ebf107834e593c`
- Invalidated visual: `evidence/crypto-seasonality/comparison-v2.png`, SHA-256
  `41765864845cf1ef88902894bbe027d0b2af5e168be1044412ff9a20636d24d1`

## Corrective boundary

The evaluator foundation now converts Coinbase bucket starts to derived close
instants and binds transitive semantic dependencies into its identity. Provider
contract and mutation scenarios cover both defects. The stale v2 policy is
deliberately not rebound to corrected code, and ETH cannot be reused as an
unread holdout.

Any future crypto experiment must fetch a predecessor candle, retain the
settlement endpoint, freeze one primary candidate or apply a multiplicity-aware
max-statistic gate, and externally anchor the corrected evaluator before opening
a genuinely new asset/time/venue panel.

## Decision

Do not tune or describe Wednesday as a lead. Preserve v2 as a failed experiment.
The next historical search budget belongs to an independent economic mechanism
with a stronger prior and an unread panel, while the ETF packet accumulates
prospective outcomes.
