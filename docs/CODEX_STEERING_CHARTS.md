# Steering: interactive charts — 2026-08-05

Companion to `docs/CODEX_STEERING.md`. That file says the market map becomes
panel eleven inside the chat. This file says what renders the charts, and what
must not.

Every license fact below was verified today against the repo itself, not
recalled. Version numbers and dates are exact.

---

## 0. The rule that governs every library choice

The model emits a **typed payload**. A hand-written adapter in our repo turns
that payload into chart config. The model never emits chart config, JSX, or a
series array.

    typed payload -> our adapter (unit-tested) -> library

A library that fetches its own data is banned outright. It would put numbers on
screen that no receipt covers, which breaks the product's one claim in a single
line. This rules out every embedded vendor widget, including TradingView's
hosted one.

## 1. TradingView Lightweight Charts — approved, narrow scope

`tradingview/lightweight-charts` · **v5.2.0** (2026-04-24) · **Apache-2.0** ·
16,859 stars · actively pushed 2026-07-08.

**Use it for the price/candlestick surface only.**

**Do NOT make it the general chart library.** Its x-axis is time-only, so it
structurally cannot draw an options payoff (x = underlying price) or an IV
smile (x = strike). Those need a numeric axis. Choosing it as the default
forces a second library anyway.

### The license obligation is real. Verified, not assumed.

The repo ships a `NOTICE` file next to `LICENSE`. Its full contents:

```
TradingView Lightweight Charts™
Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/
```

TradingView's own docs state it plainly: *"The Lightweight Charts™ license
**requires** specifying TradingView as the product creator."* Two things must
appear on a public page:

1. the attribution notice from the `NOTICE` file, and
2. the `https://www.tradingview.com` link.

`attributionLogo` is a real chart option (334 code hits in-repo). Turning the
in-chart logo **off does not remove the obligation** — it moves it to your
public page. Ship the attribution in the footer or an about page **in the same
commit** that adds the dependency. Do not defer it.

Also: the library is **client-side only** and explicitly not designed for
Node.js server-side rendering. Its own docs say so. It must never be imported
from a server component.

## 2. What each panel actually needs

| Surface | Renderer | Note |
|---|---|---|
| Price / candlestick + signal markers | Lightweight Charts | markers are DATA (`coord`, `time`), never code |
| Equity curve + drawdown | already hand-rolled SVG | it works, it is ours, leave it |
| Options payoff (x = price) | hand-rolled SVG | ~30 lines: `max(0, S−K)` summed over legs. No library exists worth the dependency |
| IV smile (x = strike) | hand-rolled SVG, one line per expiry | ship slices; a 3D surface is a demo toy, traders read slices |
| Blotter / holdings, 504 rows | `@tanstack/react-table` + `react-virtual` (MIT) | headless, typed column defs, virtualized |
| Treemap (market map) | already hand-rolled | it works; do not swap it for a library |

**Add at most one charting dependency in one commit, with its adapter and the
adapter's test.** Two libraries landing together makes a bisect useless.

## 3. Do not install these

- **Any embedded vendor widget** — fetches its own data. Banned by §0.
- **ECharts as a second general library** — if Lightweight Charts covers price
  and SVG covers the rest, ECharts adds ~120-170KB for panels we already draw.
  Revisit only if a panel genuinely needs it.
- **Recharts / visx / Nivo** — JSX composition invites per-chart authoring,
  which is the failure mode §0 exists to prevent. Payload-to-adapter is
  enforceable by the type system; JSX is not.
- **Plotly** — 1.39MB.
- **Highcharts** — commercial license.

## 4. InteractiveGraph — read it, do not install it

`grapheco/InteractiveGraph` · **BSD-2-Clause** · 1,120 stars · last pushed
**2025-05-24**, over a year ago.

Worth reading for its three named applications, which map onto things we
already have or want:

- **GraphNavigator** — guided traversal of a large graph
- **GraphExplorer** — expand-a-node-at-a-time exploration
- **RelFinder** — find the relationship path between two nodes

That last one is the interesting one. Our research DAG and the lineage chain
(`packet → plan → risk verdict → journal → ledger`) both want "show me how
these two things connect". RelFinder is the interaction pattern for it.

**Do not add the dependency.** Reasons in order: unmaintained for over a year;
it brings its own rendering stack and MainFrame/theme/controls opinions that
would fight our closed panel vocabulary; and the graph we would render is
small and already content-addressed. Steal the **interaction**, not the code.

If a typed graph panel is built later, use `reactflow` — already named in
`DESIGN.md` §1 — and feed it **graph data, not executable React**.

## 5. Where to look for more

`github.com/topics/interactive-visualization` and
`github.com/topics/interactive-charts` are browsing surfaces, not a
shortlist. Judge any candidate found there against §0 first: if it cannot be
driven from a typed payload through an adapter we own, it fails before its
features matter. Then license, then bundle size, then features.

## 6. Verify before claiming done

- Screenshot every chart in BOTH light and dark. The palette bug that measured
  **2.04 contrast against white** — below WCAG AA — on the verdict badge was
  invisible to `tsc` and to every test.
- Assert on chart CONTENT in the DOM, not on the component mounting. A chart
  that renders an empty canvas passes a mount check and says nothing.
- Wide content scrolls inside its own container. The page body must never
  scroll horizontally.
- Charts are trust surfaces. No motion, no easing, no animated draw-in. A
  number that animates into place reads as a number being decided.
