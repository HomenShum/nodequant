# Agent playbook — how this was built, and how to rebuild it

This system was built by a human directing Claude Code, with OpenAI Codex as an
adversarial reviewer and four background subagents building isolated modules. This
document is the reproduction recipe: the setup, the prompt sequence, and — most
usefully — the prompts that *caught things*.

It is written so a different engineer with a different task can run the same loop.

---

## 1. Environment

```bash
# Claude Code, plus the plugins actually used here
claude plugin marketplace add DietrichGebert/ponytail   # minimal-code discipline
claude plugin install ponytail@ponytail

# Codex CLI as the adversarial reviewer (a second model, deliberately)
codex --version        # 0.145.0 here
```

MCP servers that earned their place: **Context7** (current library docs — the AI SDK v7
frame contract was read from it, not recalled), **Mobbin** (UI reference capture),
**claude-in-chrome** (driving a real browser), and the project's own MCP server once it
existed, so the agent could exercise its own tool surface.

Two rules that mattered more than any plugin:

- **`uv run --no-sync`** once background agents are running. A dev server holding a file
  will make `uv sync` fail repo-wide and take every parallel agent down with it.
- **Strict file ownership per subagent.** Every background agent was told *"you own these
  files only; if integration requires touching another, STOP and report the diff instead
  of making it."* Four agents ran concurrently on one repo with zero merge conflicts
  because of this line.

---

## 2. The loop that produced the work

Not a waterfall. Each phase produced something falsifiable before the next began.

| Phase | What it produced | The thing that made it work |
|---|---|---|
| **Probe** | API behaviour, before any design | Found that `/stats/field/values` rejects filters — which killed the obvious architecture on day zero |
| **Invariant** | One falsifiable sentence | *"The model plans which questions to ask. It never answers them."* Everything else fell out of defending it |
| **Build** | The deterministic core | No LLM in the executor at all, so it is unit-testable |
| **Attack** | Codex review, adversarial tests | Found 7 P0s the author missed |
| **Measure** | Golden eval, generated ground truth | Found that 17 "wrong counts" were a scoring artifact |
| **Integrate** | Wiring isolated modules | Found bugs that unit tests structurally cannot see |

---

## 3. The prompts that actually caught things

Reproduced close to verbatim. These are the ones with a hit rate.

### 3.1 Probe before designing

> Before proposing anything: probe the API and tell me what is actually true. Specifically
> — does the aggregation endpoint accept query filters? What is the max page size? Is
> there a rate limit? Report measurements, not expectations.

**Caught:** `/stats/field/values` rejects `query.*` prefixes entirely. The obvious design
was dead before a line was written.

### 3.2 Name the invariant, then defend it

> State the ONE rule this system must never break, as a single sentence that can be
> violated. Then tell me the test that fails loudly when it is.

**Produced:** *every number is a `totalCount`* → trace replay. That test later verified
339/339 steps and is the strongest evidence this repo carries.

### 3.3 Adversarial review with a stated hypothesis

Generic "review this" produces generic findings. Naming the failure class does not.

> Review for CORRECTNESS BUGS ONLY. The central invariant is X. Two bugs of this exact
> class were already found and fixed: [describe both]. Look for more of the same family.
> Specifically hunt for: [five concrete hypotheses]. For each finding give file:line, a
> concrete failing input, and the minimal fix. If a category is clean, say so explicitly
> rather than inventing findings.

**Caught:** 7 P0s including two paths that produced *plausible, well-formed, completely
wrong numbers*.

### 3.4 Verify, do not assert

Attached to every build task:

> VERIFY, DO NOT ASSERT: actually run it and paste the real output. If you cannot get it
> working, say exactly where it breaks and what you tried. Do not claim a working demo you
> have not observed.

**Caught:** an agent that could not start Postgres reported *skipped tests with a printed
reason* instead of mocking a pass.

### 3.5 Report the diff instead of making it

> You own these files only: [list]. Do NOT edit [list]. If integration requires touching
> one, STOP and report the exact minimal diff you would need instead of making it.

**Caught:** four agents on one repo, no conflicts — and every integration was reviewed by a
human before it landed. One diff was applied incompletely (a dropped `try/finally`),
which a later review caught. The pattern still won: a bad merge is visible; a silent
cross-file edit is not.

### 3.6 Falsify your own fix

> You fixed X. Now measure whether the fix worked, and whether it caused anything new.

**Caught:** the first rate-limit fix *created* a retry storm — six requests backing off in
lockstep and re-tripping the limit.

---

## 4. Prompts for senior-level concerns

These are the ones to add to any agent-built backend. Each is written to produce a
*measurement*, not an opinion.

### 4.1 Memory and unbounded growth

> Audit every in-memory collection in this codebase. For each: what bounds its size, what
> evicts from it, and what happens under a sustained agent loop issuing thousands of
> distinct queries? An unbounded dict keyed on user-controlled input is a leak — find them
> all. Then write a test that fills each past its cap and asserts it evicted rather than
> grew. Report actual memory growth over 10,000 inserts, measured with `tracemalloc`, not
> estimated.

Applied here: the probe cache, the session store, and the entity index each carry a MAX
and an eviction path. The session store is LRU-bounded at 256 sessions with per-turn field
caps, so byte-eviction converges instead of thrashing.

### 4.2 Concurrency and shared state

> Trace every piece of state shared between concurrent requests. For each: is it mutated?
> Under what lock? What happens if two requests interleave at the worst possible point?
> Specifically check: cache read-then-write races, whether a shared rate limiter is
> actually shared or per-instance, and whether one request's failure can corrupt another's
> state. Write a test that runs N concurrent requests against the same key and asserts the
> result is correct, not merely that nothing raised.

Applied here: a 429 is treated as information about a **shared** budget — a cooldown moves
the schedule for every pending request, not just the one that was throttled. Reacting
per-request is how the retry storm formed.

### 4.3 Backpressure and the real bottleneck

> Where does this system actually saturate? Not where you think — measure it. Then answer:
> does horizontal scaling help, or make it worse? If the bottleneck is a shared external
> quota, more workers is a regression, and you should say so plainly rather than describing
> a scaling story that does not hold.

Applied here: a network query costs 40–57 upstream requests against a rate limit every
instance shares. Scaling out makes it worse. The fix is a shared cache and a distributed
token bucket — documented as such, not papered over.

### 4.4 Failure modes and degradation

> For every external dependency: what happens when it is slow, when it is down, when it
> returns garbage, and when it returns *plausible but wrong* data? Which of those does the
> system currently detect? Optional dependencies must be genuinely optional — write a test
> that runs the whole service with the dependency absent and asserts identical behaviour.

Applied here: no API key → deterministic fallback planner, disclosed in the response. No
database → every store call is a no-op. A failed cache read → falls through to the
network, because a cache must never be able to *fail* a request.

### 4.5 The seams between modules

> These modules each have green unit tests and are about to be wired together. Unit tests
> cannot see composition bugs. Review the SEAMS: lifecycle ordering, whether a failure in
> one path leaves another inconsistent, and whether anything wired in has no test at all.

Applied here: found `store.connect()` with no matching `close()` — a pool leak on every
graceful shutdown, invisible to every unit test because it lived entirely in the wiring.

### 4.6 Data-model adversarialism

> Look at your own schema and ask what a hostile or careless caller could put in each
> field that would still validate but produce a wrong answer. Then ask what the model
> could put there.

Applied here: `MetricRequest` was given **no `result` field at all** — the model has
nowhere to put a number, so it isn't asked not to, it *can't*. And `bucket_sum` was
deliberately excluded from sandbox scope because it is already a sum over the same
buckets, so exposing it would let a formula double-count invisibly.

---

## 5. What to steal wholesale

1. **One falsifiable invariant, chosen early.** Not "be correct" — something a test can
   break. The architecture follows from defending it.
2. **Generated ground truth.** The eval re-probes the source through a client sharing no
   code with the system, so the golden set cannot inherit the system's bugs.
3. **A second model as adversary.** Codex found 7 P0s in code that already had a green
   suite. Use a different model, and give it a hypothesis.
4. **A measurement log.** [`MEASUREMENTS.md`](../MEASUREMENTS.md) — every belief held,
   measured, and found false. A reviewer cannot otherwise distinguish work that went
   smoothly from work whose problems were never looked for.
5. **Research the audience before the architecture.** Done last here, and it should have
   been first: the stack was in a job description sitting in an already-accessible inbox
   while the conclusion "stack unknown" was being written. The defect was not bad
   searching; it was searching instead of asking.
