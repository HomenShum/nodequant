# NodeQuant Autonomous Graph Research Program

## Mission

Research, implement, falsify, and refine systematic strategy candidates in
paper-only mode. The target is not peak backtest Sharpe. The target is the
simplest candidate whose result survives point-in-time replay, frozen costs,
purged development folds, concentration attacks, multiple-testing controls,
and independent verification.

## Governing invariant

The research agent may mutate only typed candidate strategy graphs. Every
market fact, fill, return, metric, trial count, and verdict comes from the
sealed deterministic evaluator. A candidate may not survive by changing the
evaluator, data, splits, costs, stresses, budget, objective, or trial history.

## Editable surface

- `research/hypotheses/**`
- `research/sources/**`
- future `strategies/candidates/**`
- future `features/candidates/**`
- experiment manifests that do not alter frozen evaluation policy

Everything listed in `research/policy.json::immutable_files` is outside the
optimizer's authority. An evaluator defect stops the experiment and becomes a
separate infrastructure repair; it never counts as a strategy improvement.

## Research protocol

For each experiment:

1. Select one unresolved mechanism or failure pattern from the frontier.
2. State one falsifiable hypothesis and its expected failure mode.
3. Create one atomic candidate mutation.
4. Seal the candidate, evaluator, policy, corpus, split plan, seed, and budget.
5. Run the proof kernel, fixed-budget diagnostic, purged folds, cost stress,
   best-episode removal, and mandatory adversarial checks.
6. Append the complete trial before considering any champion change.
7. Compare only with the declared parent under the same frozen policy.
8. Retain failed, invalid, timed-out, and crashed trials permanently.
9. Synthesize recurring wins, failures, contradictions, and exhausted families.

The agent may explain results. It may not author or relabel a measured score.
Published metrics remain external metadata and never enter local evaluation.

## Frozen inner objective

`robust_sharpe_floor` is the minimum of:

- the 25th percentile of episode-level net Sharpe across purged development folds;
- episode-level net Sharpe under two-times transaction costs;
- episode-level net Sharpe after removing the five best episodes.

The score is authoritative only when every eligibility gate in
`research/policy.json` passes. A diagnostic value from the synthetic fixture is
stored separately and cannot create a champion or promotion claim.

## Convergence

Atomic removals and falsifiers precede parameter tuning. Combine two ideas only
after each independently improves the same parent. Synthesize every frozen
interval and whenever the frontier is exhausted. Stop when the budget is
exhausted, no eligible candidates remain, a critical evaluator defect appears,
real-data evidence is unavailable, or the human review boundary is reached.

## Required output

- content-addressed source, mechanism, hypothesis, candidate, experiment,
  result, failure, synthesis, and promotion nodes;
- typed edges between those nodes;
- hash-chained node, edge and trial streams bound to one atomic local head;
- current frontier and stopping reason;
- authoritative champion or explicit `null`;
- promotion verdict with blockers;
- full reproduction command and limitations.

The local head detects accidental deletion and incomplete appends; it is not an
externally signed authority against a machine-level attacker who can rewrite
both logs and checkpoint. That stronger anchor remains a promotion blocker.

Never report "found Sharpe X" without the exact evaluator receipt and promotion
stage that authorize that statement.
