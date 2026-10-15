# Evidence index

## Market Map compilation recovery — 2026-10-15

A developer opening the Market Map needs the page to compile before reviewing
its snapshot. The component ended mid-expression, so the frontend could not
parse it. The bounded repair closes the existing layout and function.

- Surface: `web/app/market-map/market-map-view.tsx`, served at `/market-map`.
- Before: source at `b36918f970927c9f082997ad833654aa96b104a2` produced
  11 syntax diagnostics with the captured TypeScript parser.
- After: the same parser produced zero diagnostics. Locked installation,
  no-emit types, the money self-check and the production build passed.
- Served HTML: one bounded GET to an owned local production frontend returned
  HTTP 200 with `market-map-page` and `market-treemap`. The owned server and
  child were stopped afterward. This is local semantic evidence.
- Visual status: **NOT VERIFIED**. The first browser inventory failed because
  its configured Chrome profile was already in use. No screenshot, mobile
  viewport, browser interaction or responsiveness claim is made.
- Other open gaps: dependency findings from installation and the separately
  failing backend timing checks. No production or live-data claim is made.

Reproduce the existing frontend gates from `web/`:

```sh
npm ci
npx tsc --noEmit
node lib/money.mjs
npm run build
```

See [the component changelog](CHANGELOG/components/market-map-view.md).
A screenshot and its regeneration recipe still need to be added after the
browser session is available; this surface has not completed visual QA.
