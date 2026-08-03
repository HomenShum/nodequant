# Agent memory: it stores filters, and never counts

`app/memory.py` gives the service conversational continuity — "now compare that to
Opdivo", "drill into PHASE3", "same but since 2020". This document explains the one
design decision that shaped every line of it, because the decision is not obvious and
the obvious alternative is wrong in a way that is hard to detect.

---

## The rule memory is most likely to break

The whole system rests on one sentence:

> **The model chooses which questions to ask the API. It never answers them.**
> Every number in every response is a ClinicalTrials.gov `totalCount`.

Every other component is *structurally* incapable of breaking it. The planner's entire
output surface is an enum-constrained `QueryPlan` with no integer field. The executor
only ever writes numbers it read from an HTTP response. The graph planner's extra model
calls return booleans, enums, and short names.

Memory is different. Memory's *purpose* is to carry something from an earlier turn into
a later one. A cache of results is the natural thing to build, it makes follow-ups feel
instant, and it is the one component where storing an answer is a plausible engineering
decision rather than an obvious mistake.

It is also the one place where breaking the rule is invisible.

---

## Why a remembered count is worse than a hallucinated one

A hallucinated number is wrong and unsupported. Every check the system has catches it:
there is no `nct_id` behind it, no verbatim excerpt, no trace step to replay.

A *remembered* number is wrong and fully supported. Consider:

```
Turn 1  "How are melanoma trials distributed across phases?"
        -> PHASE3: 219        (measured: query.cond=melanoma AND AREA[Phase]PHASE3)

Turn 2  "now compare that to Opdivo"
        -> PHASE3: 40         (measured: query.intr=Opdivo AND query.cond=melanoma ...)
```

Those are the real numbers from a live run. Now suppose memory had cached turn 1 and a
"helpful" optimisation reused `PHASE3: 219` because the phase bucket "hadn't changed".
The response would carry:

- a real `nct_id` and a real verbatim excerpt — from a real melanoma PHASE3 trial;
- a trace step whose URL, when replayed, returns exactly `219`;
- a reconciliation panel whose arithmetic balances.

Every integrity mechanism in this repo passes. The number is genuine. It is simply the
answer to a question nobody asked — measured under `condition=melanoma`, presented under
`condition=melanoma AND intervention=Opdivo`. A reader comparing 219 against 40 would
conclude Opdivo accounts for essentially all melanoma PHASE3 activity.

That is the failure mode this module exists to prevent, and it is why the design is not
"cache carefully" but **do not store the quantity at all**.

---

## What is stored, and what is not

| Stored | Not stored |
|---|---|
| the validated `QueryPlan` | any `Datum.count` |
| the `PlanFilters` that were applied | `meta.total_matching` |
| the `Dimension` bucketed by | `bucket_sum`, `unbucketed_count` |
| the `VizType` rendered | `citation_total`, `returned`, `total` |
| the bucket **labels** | anything a count could hide inside |

The distinction is between **shape** and **answer**.

`PHASE3` is a label — it says *what was looked at*. `219` is a count — it says *what was
found*. A label is context a planner can legitimately use to avoid re-asking the same
question; a count is a claim about the world that must be re-derived, with a citation and
a trace, every single time it is shown.

This is enforced by the type, not by discipline. `Turn` is a frozen dataclass with seven
fields and no `**extra`; `ConversationMemory.record()` has no `counts` parameter. Adding
a place to put a number requires editing the dataclass, which is a code review, which is
the control. `tests/test_memory.py::test_turn_has_no_field_that_can_hold_a_count` pins
the field set so the review cannot be skipped.

`record_response()` is where a count would most naturally sneak in — it takes a whole
`QueryResponse`. It reads exactly two things off it: `datum.label` and
`visualization.type`.

---

## `summarise()`: the leak path, and its two guarantees

`summarise()` produces the recap that goes into the planner's prompt. It is the only
place stored state becomes model input, so it is the only place a stored number could
become a model-authored number.

A planner reading `"melanoma: 3,738 trials"` in its prompt cannot distinguish a
remembered measurement from an invented one. Both are just tokens. It may restate the
figure in a title or an interpretation, and now the count is in the response having
never touched an HTTP request.

So the recap has two independent guarantees:

1. **It is built without numbers.** Filters are named, not valued, where the value is
   numeric: an inherited `start_year` is described as *"an earliest-start-year
   constraint"*, never as *"since 2020"*. The actual year is carried structurally by
   `resolve_followup`, where it belongs.
2. **It is scrubbed before return.** `scrub_numbers()` deletes every run of two or more
   digits from the final string. This is deliberately stricter than the executor's
   three-digit rule — in a recap, even a two-digit figure reads as an answer.

Guarantee 2 exists because guarantee 1 depends on every future edit remembering the rule.
A genuine year like `2020` is destroyed by the scrub, and that is the correct trade: a
scrub with exceptions is a scrub with a hole, and losing "since 2020" from a prompt costs
nothing because the constraint travels as a typed field.

The recap also states its own absence explicitly:

```
Earlier in this conversation (context only — no results are carried over):
- asked about melanoma, grouped by phase; filters: condition melanoma; rendered as bar_chart
Filters still in effect unless changed: condition melanoma.
No counts, totals or findings from those turns are available. Every number must be
measured from the API for the question being planned now.
```

That last sentence is not decoration. A model shown a description of a prior chart with
no numbers may treat the numbers as an omission to be helpfully filled in. Naming the
absence makes it a constraint instead of a gap.

---

## Follow-ups inherit filters, then re-measure from scratch

`resolve_followup(text, memory)` returns a plain `QueryRequest` — the same type the HTTP
endpoint accepts from a cold caller. It goes through the same planner, the same executor,
and issues the same probes. A follow-up is not cheaper than the equivalent question typed
in full; it is only shorter to type.

That is the point. The returned object has nowhere to put a remembered number, so
re-measurement is not a convention someone has to honour — it is the only available
behaviour.

Inherited filters are attached as **explicit** `QueryRequest` fields, not merely woven
into prose, so `QueryPlan.merged_with_request()` forces them to win over whatever the
planner infers. The composed prose exists only so the planner picks a sensible dimension
and title.

Live, with a real key against the live API:

```
Turn 1  "How are melanoma trials distributed across phases?"
        filters_applied  {"query.cond": "melanoma"}
        PHASE1 1340  PHASE2 1529  PHASE3 219  ...   (7 trace steps)

user types: "now compare that to Opdivo"

Turn 2  composed query   "Opdivo melanoma trials distributed across phases"
        explicit fields  drug_name="Opdivo", condition="melanoma"
        filters_applied  {"query.intr": "Opdivo", "query.cond": "melanoma"}
        PHASE1 141   PHASE2 204   PHASE3 40   ...   (7 trace steps, 20 fresh probes)
```

`melanoma` was never restated by the user. Every number in turn 2 was measured under
turn 2's filters. No value from turn 1 appears in turn 2's response.

---

## The other dangerous direction: a false follow-up

A missed follow-up costs the user a re-typed filter. A **false** follow-up applies a
stale constraint to a genuinely new question and returns an honestly-measured count for
something nobody asked — the same class of harm as a replayed count, arriving through a
different door, and far easier to ship because the number really was measured correctly.

So `is_followup()` is asymmetric on purpose:

> A self-contained interrogative with no anaphor is **never** a follow-up, even when it
> opens with "now".

`"Now how are lung cancer trials distributed across phases?"` opens with a continuation
word but is a complete question. It must not inherit `condition=melanoma`. Detection
requires either an anaphor (*that*, *those*, *same*, *it*) or an explicitly elliptical
opener (*what about X*, *drill into X*) — and, additionally, that a concrete delta was
actually identified. No delta means nothing to apply, which means treat it as a new
question. Every ambiguous case resolves to "no memory", because the failure of not
inheriting is recoverable and the failure of wrongly inheriting is not.

---

## Bounds

An agent loop finds an unbounded store in minutes, and it does so from *inside* one
session — a planner that follows up on itself appends forever.

| Container | Cap | Eviction |
|---|---|---|
| `ConversationMemory` turns | `max_turns` (8) | oldest first |
| `ConversationMemory` bytes | `max_bytes` (16 KiB) | oldest first |
| per-turn labels / label chars / query chars | 24 / 64 / 500 | truncated at write |
| `MemoryStore` sessions | `max_sessions` (256) | least-recently-used |

Both turn-count and byte bounds exist because they fail differently: turn count caps how
much context the planner sees, bytes cap what the process holds, and a few verbose plans
can outweigh a hundred terse ones. Per-turn fields are capped *at write time*, so a single
turn can never exceed the session budget on its own — without that, byte eviction could
not converge.

The newest turn is never evicted. A memory that drops the turn a follow-up refers to is
worse than no memory: `resolve_followup` would silently return a fresh query while the
user believes their context carried.

Session bounds matter as much as turn bounds. A per-session cap does nothing if a new
session id can be minted per request, which is exactly what an unauthenticated endpoint
receives.

---

## Integration

`app/memory.py` is self-contained and imports only `app.models` and `app.planner`. It is
not yet wired into `app/main.py`; the minimal diff to do so is in the handover notes.
`resolve_followup` returns `None` for anything it is unsure about, so a wired-in
integration degrades to today's stateless behaviour rather than to a wrong answer.

## Tests

`tests/test_memory.py`, 37 tests, fully offline — no key, no network, no model.

- a follow-up inherits prior filters and applies the delta (drug, phase, year, location,
  status, regrouping);
- `summarise()` contains no multi-digit run even when the prior turns measured
  five-figure counts, and even when a model-authored plan title contained a figure;
- memory is bounded by turns and by bytes, and eviction drops the oldest;
- a fresh question never inherits stale filters — five phrasings, including the
  "Now how are ...?" trap;
- two sessions never see each other's state, in both directions;
- `Turn` has no field capable of holding a count (structural, so it fails at review time
  rather than at leak time);
- a composed follow-up routes identically through the deterministic keyword planner, so
  the feature is not silently key-only.
