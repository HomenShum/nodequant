# P0 cross-stream honesty proof

## Observable

An internally consistent mutation must not be able to change authorized trade
quantity and reach promotion. The review surface must add an explicit blocker
while independent execution reconciliation is not implemented.

## Before / after

| Probe | Before | After |
| --- | --- | --- |
| Unchanged packet, fills inflated 1 -> 6 | receipt acceptable; `PROMOTABLE` | two unauthorized fills; `BLOCKED` |
| Packet and fills co-mutated to 6 | receipt acceptable; `PROMOTABLE` | packet-set mismatch; `BLOCKED` |
| Cross-stream execution evidence | absent / blind residual serialized | residual absent; `NOT_RUN`; unimplemented blocker cannot be self-awarded |
| Real review panel | `BLOCKED`, 5 blockers | `BLOCKED`, 6 blockers |

The honest run's final equity remains `99,449,124,700` microdollars. The forged
run still computes `99,295,209,700`; the change is that the forgery is rejected,
not hidden or normalized away.

## Reproduction

- `before-adversarial.txt` / `after-adversarial.txt`: identical coherent fill
  mutation command before and after the first repair.
- `before-packet-comutation.txt` / `after-packet-comutation.txt`: identical
  packet-and-fill mutation command before and after verified packet-set binding.
- `full-pytest-final3.txt`: final full Python regression run: 513 passed,
  29 skipped under the same suite load that previously exposed pacing compression.
- `web-build.txt`: production frontend build.
- `web-lint.txt`: honest failing baseline; `oxfmt --check` flags 49 files because
  the repository has no formatter config. No frontend source was changed here.

## Visual evidence

- `comparison.png`: first adversarial probe, side by side.
- `binding-comparison.png`: packet-and-fill co-mutation, side by side.
- `ui-before.png` / `ui-after.png` / `ui-comparison.png`: the real
  `backtest-review` panel at 1100x1000 CSS pixels, DPR 2, light theme, same
  `review the backtest` trigger. Each run made exactly one chat request.
- `before-docs.png` / `after-docs.png`: stale and corrected reconciliation claims.
- `pacing-before.png` / `pacing-after.png`: the 152 ms suite-load failure and
  the re-checking scheduler's targeted/full-suite result.
- `malformed-fifo-comparison.png`: raw before/after proof that an incomplete
  fill changed from `KeyError` to explicit rejection, while a 50-caller burst
  changed from scrambled release order to FIFO with zero retained waiters.
- `before-malformed-fifo.txt` / `after-malformed-fifo.txt`: the measurements
  rendered in that comparison.
- `handoff-gap-comparison.png`: fixed-string before/after measurement for eight
  Stock AI contracts that were absent from the handoff and are now explicitly
  mapped as unbuilt; source counts are in `before-handoff-gap-coverage.txt` and
  `after-handoff-gap-coverage.txt`.

The UI before was captured from committed `563244d` in a detached worktree; the
after used the working tree. Both used isolated backend/frontend ports. An
earlier attempt targeted occupied port 8000, which belonged to another checkout;
that contaminated attempt is not used as proof.

## Unchanged

- Honest ledger equity and the 579-event review fixture.
- Review layout, curve, authority count, early-entry count and price mismatch
  count.
- Synthetic corpus, market-data, oracle, spread and slippage blockers.
- Existing uncommitted split/QuantStats recovery in `docs/THREAD_DECISIONS.md`.
