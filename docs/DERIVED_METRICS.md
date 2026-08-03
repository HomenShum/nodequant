# Derived metrics — the model writes the formula, never the number

`app/compute.py` lets a user ask for a ratio, a share, or a growth rate without
breaking the rule the rest of this system is built on:

> The model chooses which questions to ask the API. It never answers them.
> Every number is a ClinicalTrials.gov `totalCount`.

The extension is one sentence: **the model writes an arithmetic expression, a
sandbox executes it, and the only values in scope are counts the API returned.**

```
"what share of melanoma trials are Phase 3?"
        │
        ▼
  model emits a MetricRequest ── no `result` field exists to put an answer in
        │   name:       "Phase 3 share"
        │   expression: "PHASE3 / total_matching * 100"
        │   unit:       "percent"
        ▼
  validate_expression()  ── AST allowlist; every name must be a measured count
        ▼
  run_sandboxed()        ── no builtins, no attributes, float-only arithmetic
        ▼
  DerivedMetric(result=5.858747993579454, inputs=["PHASE3","total_matching"])
```

---

## Why a model-written FORMULA is safe where a model-written NUMBER is not

This is not a stylistic preference. The two artifacts have different
epistemic properties, and only one of them is checkable.

**A number is a terminal assertion.** `"Phase 3 is 5.9% of melanoma trials"` has
no structure. Nothing downstream can evaluate it without independently redoing
the work the model claimed to have done — and if you can do that, you did not
need the model's number. It cannot be unit-tested, because there is nothing to
run. It cannot be diffed, because a wrong number and a right number look
identical. It cannot be replayed. A reviewer's only option is to trust it, and
in a clinical or regulatory context an unsourced number is not a UX defect, it
is a claim someone may act on.

**A formula is a program over named measurements.** `PHASE3 / total_matching * 100`
is checkable in five distinct ways that a number is not:

| Property | `5.9%` | `PHASE3 / total_matching * 100` |
|---|---|---|
| Names its inputs | no | yes — `["PHASE3", "total_matching"]` |
| Inputs traceable to a probe | no | yes — each is a `meta.trace` step |
| Re-executable | no | yes — same inputs, same result, deterministically |
| Reviewable before it runs | no | yes — a human reads six tokens, not a spreadsheet |
| Wrong in a *visible* way | no | yes — a wrong formula is wrong for every input |

That last row is the important one. A model that hallucinates a number is wrong
once, quietly, in a way that looks exactly like being right. A model that writes
the wrong formula is wrong *structurally* — it will be wrong on the example
data, wrong in the test, and wrong in a way a reviewer can see by reading it.
This is the same reason `meta.trace` carries request URLs rather than a
reassurance that the counts are correct: replayability is what makes a claim
falsifiable, and falsifiability is the only thing that makes trust rational.

And the arithmetic itself is not where models fail. Division is not a
hallucination-prone step; *recalling a quantity* is. So the split puts the model
where it is reliable (deciding that "share" means `part / whole * 100`) and
keeps it away from where it is not (knowing that melanoma has 3,738 trials).

**The formula is also bounded in a way prose is not.** The README already notes
that free-text model output had to be discarded because nothing stopped a plan
arriving with `"There are 999 matching trials."` An expression cannot smuggle a
figure the same way, because the validator counts its inputs: an expression that
references no measured value is rejected outright as "a model-written number,
not a derived metric."

---

## The rule that does the actual work

Everything in `compute.py` is scaffolding for one line:

```python
if node.id not in data:
    _reject(f"unknown name {node.id!r}: an expression may only reference "
            f"measured counts, and the model cannot invent an input")
```

An unknown name is **rejected, never defaulted**. Defaulting a missing input to
zero — the ordinary, friendly thing to do — would convert a fabrication into a
plausible-looking number, which is precisely the failure mode this repo exists
to prevent. Observed rejections, run live against `examples/01`:

```
REJECTED  'phase3 / total * 100'
          -> unknown name 'phase3': an expression may only reference measured
             counts, and the model cannot invent an input
REJECTED  "data['PHASE9'] / total_matching"
          -> data['PHASE9'] is not a measured value; the expression may only
             consume counts the API returned
REJECTED  '1529 / 3738 * 100'
          -> expression references no measured value; a formula over pure
             literals is a model-written number, not a derived metric
```

The third is worth dwelling on. `1529 / 3738 * 100` uses the *right* numbers —
they happen to match the response — and it is still refused, because a formula
whose inputs are typed in rather than bound is indistinguishable from one whose
inputs were invented. The provenance has to come from the binding, not from the
digits looking familiar.

### The gap the name rule leaves, and how it is closed

`total_matching * 0 + 5.9` passes the name check. It references a real measured
count, validates cleanly, and returns a number the API never produced. So
`derive()` perturbs every input and re-runs: if the result never moves, the
expression is a constant in disguise and is rejected.

```
UnsafeExpressionError: metric 'fake share' produces 5.9 regardless of its
inputs; a formula insensitive to every measured count is a constant in disguise
```

---

## What enters scope, exhaustively

`measured_data(response)` builds the *entire* namespace. There is no parameter
by which a caller can widen it.

| Source | Bound as | Example |
|---|---|---|
| `visualization.data[*].count` (ungrouped) | `label` | `PHASE3` → 219 |
| `visualization.data[*].count` (comparison) | `series::label` | `Pembrolizumab::PHASE3` |
| `visualization.nodes[*].count` | `node::id` | `node::Nivolumab` |
| `visualization.edges[*].weight` | `edge::src\|dst` | `edge::Carboplatin\|Pemetrexed` |
| `meta.total_matching` | `total_matching` | 3738 |

Deliberately **absent**: `meta.bucket_sum` and `meta.unbucketed_count`. Both are
measured, but `bucket_sum` is already a sum over the same buckets — offering it
would let a formula double-count without that being visible in the expression.
Also absent: anything from the request, the citations, the prose, or another
response. Cross-response arithmetic is refused, because two counts measured
under different filters were never co-measured.

Comparison series are namespaced because `"A vs B by phase"` repeats every label
once per series; a bare `PHASE3` would have to silently pick a winner, so it is
never bound at all. Labels that are not Python identifiers (`United States`,
`2024`, `Nivolumab::PHASE3`) are reachable only as `data['...']` — which is the
only reason `Subscript` is in the allowlist.

---

## The sandbox, and what it does and does not claim

The expression is attacker-controlled text. It is *not* trusted because a model
wrote it — a model is the component most easily talked into writing
`__import__('os').system(...)`. Validation happens on the parsed AST **before
anything is compiled or executed**.

**Allowed:** numeric literals (|v| ≤ 1e6), names bound in `data`,
`data['literal']`, `+ - * / // % **`, unary `+ - not`, `and/or`, comparisons,
conditional expressions, list/tuple literals, and calls to exactly six pure
numeric builtins — `min max sum round abs len`.

**Rejected:** `Attribute` (all of it), `Import`, `Lambda`, every comprehension
and generator, walrus, f-strings, `Dict`, `Set`, `Starred`, `Slice`, `Await`,
keyword arguments, indirect callees, dynamic subscripts, and subscripting
anything other than `data`.

Banning `Attribute` outright is the load-bearing decision. Every classic `eval`
escape — `().__class__.__bases__[0].__subclasses__()` — has to traverse an
attribute to reach anything interesting, and there is no syntax for that here.
The cheap `'__' in expression` scan fires first in practice, but it is
*redundant*, not the guarantee; `tests/test_compute.py` proves this by rejecting
dunder-free attribute access (`PHASE1.real`, `().imag`) at the AST layer with
the same error the traversal would hit.

Resource limits: 600 chars, 120 AST nodes, depth 25, at most 2 exponentiations
with a constant-foldable exponent ≤ 64 and no nesting inside a base. Every data
value and numeric literal is coerced to `float` before execution, which removes
the entire arbitrary-precision-integer CPU-bomb class (`9**9**9`) by
construction rather than trying to time it out. Overflow and division by zero
surface as `ComputationError`, never as a returned value.

**What is honestly not claimed.** This is an in-process AST sandbox, not an
OS-level one. It is airtight against the class of attack it is designed for —
reaching Python objects, the import system, or the filesystem — because that
class requires syntax that never survives validation. It is *not* a substitute
for a seccomp or container boundary if the allowlist is ever widened, and
widening the allowlist (especially to `Attribute`, comprehensions, or
non-constant exponents) invalidates the argument above. The 1-second timeout is
a daemon-thread backstop that can *detect* a hang but cannot always kill one,
since Python threads are not preemptible; it has never fired in testing, and it
is not the property the module rests on. The property it rests on is that no
expression which survives validation can loop, recurse, allocate, or overflow
into a slow path.

One residual limitation, stated plainly: an expression can still be *arithmetically*
wrong (`PHASE3 / bucket_sum` when the question wanted `total_matching`). The
sandbox guarantees the number was computed from measured counts, not that the
formula answers the question asked. That is what makes carrying `expression` and
`inputs` in the output non-optional — a reader can see the formula and disagree
with it, which is exactly the review a bare number denies them.

---

## Verified, not asserted

Run live against `examples/01-distribution-phases.json`:

```
bound counts: PHASE2 1529 · PHASE1 1340 · PHASE3 219 · EARLY_PHASE1 65
              PHASE4 37 · NA 412 · total_matching 3738

Phase 3 share of melanoma trials
   expression : PHASE3 / total_matching * 100
   inputs     : ['PHASE3', 'total_matching']   inputs_are_measured=True
   result     : 5.858747993579454 percent

hand-computed from the JSON file : 219 / 3738 * 100 = 5.858747993579454
sandbox result                   :                    5.858747993579454
identical                        : True
```

`examples/02` (time series) and `examples/03` (comparison) bind and compute the
same way; all five shipped examples are covered by a test that asserts every
bound value is a finite float.

`tests/test_compute.py` — 83 tests, offline, no credentials. Three adversarial
angles: **escape** (imports, dunder traversal, attribute access, builtins,
lambdas, comprehensions, indirect callees, dynamic subscripts), **fabrication**
(unknown names, invented labels, pure-literal formulas, laundered constants,
cross-response references), and **resource** (length, node count, nesting,
exponent bombs, magnitude caps, divide-by-zero, overflow). Plus the boring
load-bearing case: a legitimate formula equals the hand computation exactly.

Cost: 1000 full validate → compile → eval cycles in **386 ms** (~0.39 ms each).

---

## Integration status

`app/compute.py` and `tests/test_compute.py` are standalone and import nothing
that imports them — the full suite is green at **279 passed, 2 skipped**
(baseline was 196/2). No existing file was modified.

To surface derived metrics in the API response, three files would need to
change; each is a small, additive diff, listed here rather than made:

1. **`app/models.py`** — add `derived: list[DerivedMetric] = Field(default_factory=list)`
   to `ResponseMeta`, with an import from `app.compute`. (Or invert the
   dependency and move `DerivedMetric` into `models.py`; `compute.py` currently
   owns it so that `models.py` stays untouched.)
2. **`app/planner.py`** — add an optional `metrics: list[MetricRequest]` to
   `QueryPlan` and the corresponding `PLAN_JSON_SCHEMA` block, plus a system-prompt
   clause: *"you may request derived metrics as expressions over bucket labels;
   you may never state a value."*
3. **`app/executor.py`** — after the distribution is measured, call
   `derive(plan.metrics, response)` and attach the result to `meta`. It must run
   **after** the counts exist, and a rejection must fail the metric loudly rather
   than drop it silently.

**Merge this file into `README.md`** — it belongs next to "The central problem:
aggregation without a usable aggregate endpoint", as the section explaining how
the system answers derived questions without ever relaxing the invariant.
