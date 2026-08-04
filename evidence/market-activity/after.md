# Next.js chat market-map integration proof

Final route: `http://127.0.0.1:3210/`
Desktop viewport: `1728x1117`
Mobile viewport: `390x844`
Theme: light
Session: anonymous local browser, empty thread unless otherwise named

## Observed result

- Raw production HTML returned `200` and contained `market-map-launcher`, `aria-label="Market Map"`, and `504 equities`.
- Empty-state DOM: launcher present, named header link present, `504 equities` and `11 sectors` present, exact structural claim boundary present, horizontal overflow `0`.
- Preview link navigated to `/market-map`; the destination exposed `data-testid="market-map-page"`, `504` unique `[data-ticker]` marks, and horizontal overflow `0`.
- Mobile DOM at `390x844`: launcher present at `333px` wide, named header action present, horizontal overflow `0`.
- Populated-state replay against a fresh backend reached one user message, one assistant message, and one `backtest-review` panel; the launcher correctly left the message surface while the persistent header link remained.
- Empty and populated final-browser console captures both contain `0` errors and `0` warnings.

## Before / after

| Artifact | SHA-256 |
| --- | --- |
| `before-nextjs-chat-no-market-map.png` | `44e580036c28774c53813b94e2bdb2213936f364021eb08698416ba5a94d18e3` |
| `after-nextjs-chat-market-map-final.png` | `da6e13a292b8242b84136286d03103e1b423d56c3003ab295bac21db4add7569` |
| `before-after-nextjs-chat.png` | `0645c2dccbabcd4ae5d58b312fa06edd1ebcb1c511f420bad110502753b6af1a` |
| `after-nextjs-chat-mobile.png` | `1f53bde5fc59757524a233c7638fbc29bca0158768471d83a77b72efd0d040c9` |

## Unchanged assertions

- Sidebar contents and thread controls are unchanged.
- Composer placement, attachment control, send path, message rendering, and proof-panel vocabulary are unchanged.
- Breadcrumb and theme control remain present.
- The full market map and research graph implementations are unchanged.
- The preview repeats no authored market dataset: the server derives its summary from the compiled `market-map/data.json` snapshot.

## Runtime surprise

The older Python process already occupying port `8000` does not expose `/api/kernel/review` and produced an honest `404` in the first populated-state replay. The checked-in route exists. A fresh backend on `8001` plus a fresh Next.js production process on `3209` reproduced the successful populated state with zero console errors. No process was killed or silently replaced.
