# Responsive-agent demo reference — 2026-08-05

Source: Cheiron's two public demo videos, broken down by `gemini-3.6-flash`
(video input, both runs 2026-08-05; raw breakdowns in the session log).
Purpose: the interaction bar for NodeQuant's demo path — rent execution,
own verification, ship the responsive surface.

## The pattern both videos share

Dual-pane workbench. Generation streams left. Evidence lives right,
persistent. Citation pills bind them: click `[3]`, the claim highlights on
the left, the source opens on the right at the exact page with the exact
sentence pre-highlighted. Agent work is visible while it runs — status pill,
streamed sub-queries, URL chips — then collapses into a "View Steps"
accordion when done.

## Steal list, mapped to what NodeQuant already has

| Their pattern | Our analog | Status |
|---|---|---|
| Dual-pane: generation left, evidence right | DESIGN.md §4 — decision chain left, review rail right | Specified, not built |
| Citation pill → highlight claim + open source at page | Fact → receipt: click a fact, right pane shows probe, params, bar window, hash | Receipts exist; the *bidirectional click* is the missing interaction |
| Collapsible "View Steps" trace | TrialScope trace panel (URL + params per step, replayable) | Built, needs the auto-collapse |
| Pre-flight context modal (structured params + prompt) | Workspace-context binding — the temporal kernel already REQUIRES it (400 without) | Stronger than theirs: ours is enforced, theirs is optional |
| Live tool pills while generating | SSE `data-` frames already stream panels | Built |
| References count badge (`72`) | Receipts count per packet | Trivial |
| Skeletons in the right pane | Not built | Small |
| Source filter pills under the input | Symbol/strategy pills bound to the workspace epoch | Fits the transactional-linking rule |

## Their weaknesses are our differentiators — lead the demo with these

1. **They index static PDFs.** Our receipts replay live: every trace step
   carries the literal URL and params, and `ctgov_replay` re-issues them.
   Theirs cite; ours re-measure.
2. **Their tool logs hide parameters and retries.** Ours ARE the parameters —
   the trace panel shows the exact probe, and the packet hash pins it.
3. **Their demo never shows a failure state.** We render FAIL and NOT_RUN as
   first-class states, dashed and labelled, and the promotion gate leads with
   BLOCKED. A demo that shows its failure states honestly is the one a quant
   reviewer trusts.

## Demo build order (path B, confirmed)

1. Bidirectional fact-to-receipt click — the one interaction we lack that
   carries their entire feel.
2. Right-pane evidence rail with skeletons + receipts count.
3. Auto-collapse on the trace after stream completion.
4. Symbol pills bound to workspace epoch (transactional, per the thread).
5. External paper-platform fills flowing in as receipts — the independent
   execution evidence the design already prefers.
