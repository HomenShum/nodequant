# Invalidated result

`result.json`, `after.txt`, and `comparison.png` are retained as failed-trial
evidence, not current measurements.

Adversarial review found that the engine reused target weights in the daily
return equation while charging turnover only when targets changed. The exact
metrics therefore mixed daily rebalancing returns with monthly turnover. The
drift-aware corrected generation is `evidence/breadth-first-alpha-v4/`.
