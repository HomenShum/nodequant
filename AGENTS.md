# AGENTS.md — driving TrialScope cold

You are an agent that has just been pointed at this repository. This file is
everything you need to be useful in the next two minutes without reading the
source.

---

## 1. What this is

**TrialScope** turns a natural-language question about clinical trials into a
**validated visualization specification** plus a **rendered chart**, backed by
exact counts from the ClinicalTrials.gov v2 API.

```
"How are melanoma trials distributed across phases?"
        │
        ▼
   plan (LLM, enums only)  →  count-probes (one per bucket)  →  spec + chart
```

## 2. The central invariant — read this before anything else

> **The model plans which questions to ask the API. It never answers them.**

Every count in every response is a ClinicalTrials.gov `totalCount` probe
(`countTotal=true&pageSize=1`). No count, sum, percentage or total is produced
by a language model — including the prose in `meta.interpretation`, which is
composed from validated enum values, not generated.

Consequences you can rely on:

* **Every number is replayable.** `meta.trace[]` carries the literal URL and
  params of each request. `trialscope replay` / `ctgov_replay` re-issues them
  and reports match / drift / error per step.
* **The model is never shown study records**, so there is no step at which it
  could be wrong about a quantity.
* **A number you cannot find in `meta.trace` does not exist.** If you are about
  to state a figure that is not a `result_count` or a `data[].count`, stop.

## 3. The finding that shaped the schema

`designModule.phases` is an **array**. Phase buckets therefore **do not
partition** the result set, and the direction of the error is not predictable:

| query | matched | bucket sum | why |
|---|--:|--:|---|
| pembrolizumab | 2,915 | **3,205** | overlap — 469 trials are both PHASE1 and PHASE2 |
| melanoma | 3,738 | **3,602** | 136 trials carry no `phases` field at all |

A naive renderer draws both as a clean pie summing to 100%, and every
proportion a reader takes from it is wrong. So the response carries
`meta.buckets_are_exclusive`, and exclusivity is a property **of the dimension,
not an arithmetic check** — overlap inflates the sum while absent values deflate
it, and the two can cancel exactly.

## 4. The rendering contract — four rules

Break any of these and the artifact misrepresents the data.

1. **`meta.buckets_are_exclusive == false` ⇒ never draw a pie, donut, treemap or
   100% stacked bar.** Use independent or grouped bars, and show a
   reconciliation strip stating `bucket_sum` vs `total_matching` plus
   `meta.warnings`.
2. **Render `data[].is_unbucketed` marks. Never drop them.** They mean "no value
   recorded", not a category. Style them in the reserved neutral grey **and**
   distinguish them without relying on colour (this repo uses a 45° hatch in
   SVG and a `▒` glyph in ASCII).
3. **`citations` is a bounded sample; `citation_total` is the truth.** Show
   "showing 3 of 219", never imply 3 is all there is. Same for
   `visualization.returned` vs `visualization.total`.
4. **Show `meta.denominator_note` next to the chart.** A percentage without its
   base is unreadable.

Plus: **every bar carries a numeric label.** Nothing is encoded by colour alone.

`app/render.py` enforces all five, so if you use `--svg` / `--md` /
`ctgov_render` you get them for free. If you draw your own chart, branch on
`render_contract.part_of_whole_marks_allowed` (MCP) or
`meta.buckets_are_exclusive` (raw JSON) — do not parse the prose.

---

## 5. Run it

```bash
uv sync --extra dev          # or: pip install -e ".[dev]"
uv run pytest -q             # offline, no credentials needed
```

`OPENAI_API_KEY` in `.env` is **optional**. Without it the service falls back to
a deterministic keyword planner and says so in `meta.assumptions`. Every number
is identical; only question interpretation is less flexible.

### Cost model — budget before you call

| surface | network | wall clock |
|---|---|---|
| `schema`, `examples`, `render` | none | instant |
| distribution (phase / status / sponsor) | ~12-17 requests | ~5-13s |
| comparison (`A vs B`), country breakdown | ~25-37 requests | ~20-26s |
| network graph | ~57 requests | ~30-40s |
| `replay` | 1 request per trace step | 3-30s |

The upstream API rate-limits with `429` and the client paces itself and retries
with full jitter. **Do not fan out queries in parallel** — they share one
throttle budget and will collectively trip the limit. `max_citations_per_datum=0`
removes roughly 40% of the requests when you only need counts.

---

## 6. CLI surface

Installed as the console script `trialscope` (`uv run trialscope ...`), or
`python -m app.cli`.

```bash
trialscope query "How are melanoma trials distributed across phases?"
trialscope query "..." --json              # raw QueryResponse on stdout
trialscope query "..." --md                # markdown + inline data-URI SVG
trialscope query "..." --svg chart.svg     # standalone SVG artifact
trialscope query "..." --no-llm            # deterministic keyword planner
trialscope query "..." --drug Pembrolizumab --condition melanoma \
                        --phase PHASE2 --status RECRUITING \
                        --start-year 2018 --end-year 2024 --max-citations 0

trialscope schema [--which request|response|both]
trialscope examples                        # list the 5 bundled runs
trialscope examples --slug 01-distribution-phases --svg out.svg
trialscope render response.json --format svg|markdown|ascii --out chart.svg
trialscope replay response.json [--json] [--allow-drift]
```

**Contract for machine use**

* **stdout is the artifact and nothing else.** Timings, planner choice, request
  counts and "wrote chart.svg" all go to **stderr**, so
  `trialscope query "..." --json | jq` is safe.
* No colour, no spinners, no cursor control, ever. Output is byte-stable for
  the same input, so you can diff two runs.
* Exit codes: `0` ok · `2` bad input or unreadable file · `3` upstream
  ClinicalTrials.gov failure · `4` unanswerable (probe cap) · `5` replay found
  drift or an error.

**Start here, not with a network call:** `trialscope examples` and
`trialscope schema` are offline and instant. The five bundled runs cover every
visualization type the system produces.

---

## 7. MCP server

stdio MCP server exposing five tools. Each tool's description states what it
returns, what it costs, and how it fails.

| tool | network | what it gives you |
|---|---|---|
| `ctgov_query` | 12-57 req, 12-40s | `{markdown, ascii, response, stats, render_contract}` |
| `ctgov_render` | none | a QueryResponse → `svg` \| `markdown` \| `ascii` |
| `ctgov_replay` | 1 req/step | per-step `match` \| `drift` \| `error` + a verdict |
| `ctgov_schema` | none | both JSON Schemas + the four contract rules |
| `ctgov_examples` | none | the 5 bundled runs, index or one in full |

Tools **return `{"ok": false, "error": ..., "remedy": ...}` instead of raising**,
so you can branch on the failure class (`invalid_request`, `upstream`,
`unanswerable`, `invalid_response`, `unknown_slug`, `no_trace`) rather than
parsing a traceback.

Run it directly:

```bash
uv run python -m app.mcp_server
```

### Claude Code

Project-scoped — drop this in `.mcp.json` at the repo root (already checked in),
then `claude` picks it up on launch:

```json
{
  "mcpServers": {
    "trialscope": {
      "command": "uv",
      "args": ["run", "--directory", ".", "python", "-m", "app.mcp_server"]
    }
  }
}
```

Or add it globally, substituting the absolute path to this checkout:

```bash
claude mcp add trialscope -- uv run --directory /abs/path/to/stock-ai-dipsea python -m app.mcp_server
```

### Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.trialscope]
command = "uv"
args = ["run", "--directory", "/abs/path/to/stock-ai-dipsea", "python", "-m", "app.mcp_server"]
```

### Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` globally:

```json
{
  "mcpServers": {
    "trialscope": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/to/stock-ai-dipsea", "python", "-m", "app.mcp_server"]
    }
  }
}
```

If `uv` is not on PATH for the client process, use the interpreter directly:
`"command": "/abs/path/to/stock-ai-dipsea/.venv/Scripts/python.exe"` (or
`.venv/bin/python` on macOS/Linux) with `"args": ["-m", "app.mcp_server"]` and
`"cwd"` set to the checkout.

---

## 8. HTTP surface

Also runnable as a service, same pipeline, no second implementation:

```bash
uv run uvicorn app.main:app --port 8000
```

| method | path | purpose |
|---|---|---|
| `POST` | `/api/query` | the documented contract: `QueryRequest` → `QueryResponse` |
| `POST` | `/api/chat` | the same answer streamed as assistant-ui panels |
| `GET` | `/api/health` | liveness + upstream request/retry counters |

---

## 9. Response shape, abridged

```jsonc
{
  "visualization": {
    "type": "bar_chart",        // | grouped_bar_chart | time_series | histogram
                                // | scatter_plot | network_graph | single_value
    "title": "Melanoma trials by phase",
    "encoding": {"x": {"field": "label"}, "y": {"field": "count"}},
    "data": [{
      "label": "PHASE2", "count": 1529,   // EXACT, from the API
      "series": null, "is_unbucketed": false,
      "citation_total": 1529,             // the truth
      "citations": [{"nct_id": "NCT…", "excerpt": "…", "field": "briefTitle"}]
    }],                                   // a bounded SAMPLE of citation_total
    "nodes": [], "edges": [],             // network_graph only
    "returned": 6, "total": 6             // truncation always declared
  },
  "meta": {
    "interpretation": "Trials grouped by trial phase, filtered to condition = melanoma.",
    "trace": [{"step": "…", "url": "…", "params": {…}, "result_count": 3738}],
    "filters_applied": {"query.cond": "melanoma"},
    "total_matching": 3738,
    "denominator_note": "of 3,738 trials matching query.cond=melanoma",
    "buckets_are_exclusive": false,       // ⚠ false ⇒ NO pie, NO 100% stacked bar
    "bucket_sum": 3602, "unbucketed_count": 136,
    "sort": "count_desc", "units": "trials",
    "assumptions": [], "warnings": ["…buckets do not partition…"]
  }
}
```

Get the full schemas with `trialscope schema` or `ctgov_schema`.

---

## 10. Repository layout

```
app/
  ctgov.py       API client — count-probes, pacing, full-jitter backoff
  models.py      request/response schemas; every field traced to a design rule
  planner.py     LLM → validated QueryPlan (+ deterministic keyword fallback)
  executor.py    plan → visualization spec; exclusivity check; networks; citations
  render.py      QueryResponse → SVG / markdown / ASCII; enforces the contract
  cli.py         the `trialscope` console script
  mcp_server.py  stdio MCP server
  main.py        FastAPI: /api/query and /api/chat
tests/           offline; regression-first
examples/        5 real runs with captured output — develop against these
design-system/   component library the renderer's visual decisions come from
design-dna/      reference observations → design rules → schema fields
```

## 11. Conventions if you change code

* Run everything with `uv run`. `uv run pytest -q` must stay green.
* Secrets live in `.env`; `set -a && . ./.env && set +a` before commands that
  need the key.
* Tests are scenario-based: a persona with a goal, driven end to end, covering
  happy path, sad paths and degraded upstream — not one assertion per flag.
* Do not add a code path that lets a model emit a quantity. That is the one
  thing this design exists to prevent.
