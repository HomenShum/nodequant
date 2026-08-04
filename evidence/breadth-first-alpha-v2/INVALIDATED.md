# Invalidated result

`result.json` is retained as failed-trial evidence, not a current measurement.

Adversarial inspection found that relative-momentum and short-reversal rules
ranked an all-zero score vector before their declared lookback existed. The
378-day candidate therefore held arbitrary assets from index 22 rather than
waiting for 378 observations. The corrected, evaluator-bound run is in
`evidence/breadth-first-alpha-v3/`.
