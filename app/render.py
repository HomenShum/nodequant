"""Visual artifacts for a QueryResponse.

Pure functions. No browser, no headless Chrome, no network, no external
services — a string in, a string out — so an agent can call these in-process
and paste the result straight into a transcript.

The whole point of this project is that the response carries enough metadata to
be rendered *honestly*. A renderer that ignores that metadata is the failure
mode the schema exists to prevent, so the four rendering-contract rules are
enforced here rather than left to the caller:

  R1  meta.buckets_are_exclusive is False  ->  never a pie, never a 100%
      stacked bar. Independent/grouped bars only, plus a visible reconciliation
      strip stating total_matching vs bucket_sum and the warnings.
  R2  is_unbucketed data is rendered, in the reserved null grey AND with a
      hatch, so it is distinguishable without colour. Never dropped.
  R3  "showing N of M" appears wherever returned < total or
      len(citations) < citation_total.
  R4  meta.denominator_note sits next to the chart, always.

Plus one that is not negotiable in any of the three formats: every bar carries
its numeric label. Nothing is encoded by colour alone.

Design tokens are lifted from design-system/foundations/tokens.html; the null
grey (#b8b7b2 light / #6a6963 dark) is reserved for unbucketed data and used
for nothing else.
"""

from __future__ import annotations

import base64
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from .models import Datum, QueryResponse, VizType

__all__ = [
    "render_svg",
    "render_markdown",
    "render_ascii",
    "PART_OF_WHOLE_TYPES",
]

# Marks that assert "these slices are the whole". Forbidden when buckets do not
# partition the result set. This renderer never emits any of them, for any
# input — the constant exists so a test can assert the absence by name.
PART_OF_WHOLE_TYPES = ("pie", "donut", "stacked_100", "treemap", "sunburst")

# ---------------------------------------------------------------------------
# tokens (design-system/foundations/tokens.html)
# ---------------------------------------------------------------------------

_TOKENS_LIGHT = {
    "bg": "#fbfbfa", "card": "#ffffff", "ink": "#1a1a18", "muted": "#6b6b66",
    "line": "#e6e5e1", "accent": "#1f6f6b", "accent2": "#7fb3b0", "null": "#b8b7b2",
    "warn": "#b4750f", "warnbg": "#fdf6e9", "okbg": "#eff5f4", "track": "#f2f1ed",
}
_TOKENS_DARK = {
    "bg": "#161614", "card": "#1e1e1b", "ink": "#f0efeb", "muted": "#a3a29c",
    "line": "#33322e", "accent": "#5ec6c0", "accent2": "#2f7a76", "null": "#6a6963",
    "warn": "#e0a44a", "warnbg": "#2a2318", "okbg": "#1b2624", "track": "#26251f",
}

_FONT = 'ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif'
_MONO = 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace'

W = 900
PAD = 24
ROW_H = 30
LABEL_W = 176
RIGHT_W = 104


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _n(value: int | float | None) -> str:
    """Thousands separators. Every number a reader sees goes through here."""
    if value is None:
        return "—"
    return f"{value:,}"


def _e(text: Any) -> str:
    return _xml_escape(str(text))


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _series_of(data: list[Datum]) -> list[str]:
    """Distinct series in first-appearance order. Deterministic, never sorted
    by count — reordering a legend between runs makes two renders of the same
    question look like different answers."""
    seen: list[str] = []
    for d in data:
        if d.series and d.series not in seen:
            seen.append(d.series)
    return seen


def showing_notes(response: QueryResponse) -> list[str]:
    """R3. Every place the payload is a bounded sample of a larger truth."""
    viz = response.visualization
    notes: list[str] = []
    unit = "nodes" if viz.type is VizType.NETWORK_GRAPH else "marks"
    if viz.total > viz.returned:
        notes.append(f"showing {_n(viz.returned)} of {_n(viz.total)} {unit}")
    for datum in viz.data:
        got, total = len(datum.citations), datum.citation_total
        if total > got and got > 0:
            notes.append(f"{datum.label}: showing {_n(got)} of {_n(total)} citations")
    for edge in viz.edges:
        got, total = len(edge.citations), edge.citation_total
        if total > got and got > 0:
            notes.append(
                f"{edge.source}→{edge.target}: showing {_n(got)} of {_n(total)} citations"
            )
    return notes


def reconciliation(response: QueryResponse) -> dict[str, Any]:
    """R1. The exclusivity verdict, as data, so all three formats agree.

    `render_part_of_whole` is the single authority: no format may draw a
    part-of-whole mark unless it is True, and it is never True here when the
    executor said the buckets overlap.
    """
    meta = response.meta
    exclusive = meta.buckets_are_exclusive
    delta = None
    if meta.bucket_sum is not None:
        delta = meta.bucket_sum - meta.total_matching
    return {
        "exclusive": exclusive,
        "render_part_of_whole": bool(exclusive),
        "forbidden_marks": list(PART_OF_WHOLE_TYPES),
        "total_matching": meta.total_matching,
        "bucket_sum": meta.bucket_sum,
        "unbucketed_count": meta.unbucketed_count,
        "delta": delta,
        # The headline has to be true for the shape of the answer. A network
        # graph has no buckets at all, so "these buckets overlap" would be
        # gibberish attached to a warning strip that is otherwise correct.
        "headline": (
            "Buckets are mutually exclusive — shares of the total are safe to read."
            if exclusive
            else (
                "These buckets overlap and do not sum to the total. "
                "Not a part-of-whole chart."
                if meta.bucket_sum is not None
                else "These counts do not partition the matching set. "
                "Do not read them as shares of a whole."
            )
        ),
        "warnings": list(meta.warnings),
    }


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------


def _svg_style() -> str:
    def block(selector: str, tokens: dict[str, str]) -> str:
        return selector + "{" + "".join(f"--{k}:{v};" for k, v in tokens.items()) + "}"

    light = block(":root,svg", _TOKENS_LIGHT)
    dark_media = "@media (prefers-color-scheme:dark){" + block(":root,svg", _TOKENS_DARK) + "}"
    dark_attr = block('svg[data-theme="dark"]', _TOKENS_DARK)
    light_attr = block('svg[data-theme="light"]', _TOKENS_LIGHT)
    return (
        "<style><![CDATA["
        + light
        + dark_media
        + dark_attr
        + light_attr
        + f"text{{font-family:{_FONT};font-variant-numeric:tabular-nums;fill:var(--ink);}}"
        ".t-title{font-size:16px;font-weight:600;}"
        ".t-denom{font-size:12px;fill:var(--muted);}"
        ".t-pill{font-size:11.5px;fill:var(--muted);font-family:" + _MONO + ";}"
        ".t-lab{font-size:12.5px;font-family:" + _MONO + ";}"
        ".t-sub{font-size:10.5px;fill:var(--muted);}"
        ".t-val{font-size:12px;font-weight:600;font-family:" + _MONO + ";}"
        ".t-val-in{fill:#ffffff;}"
        # Dark ink for labels sitting on the lighter fills (accent-weak, the
        # reserved grey hatch), with a halo so the hatch texture cannot break
        # up the glyphs.
        ".t-val-ink{fill:var(--ink);stroke:var(--card);stroke-width:2.5px;"
        "paint-order:stroke fill;}"
        ".t-val-out{fill:var(--muted);}"
        ".t-warn{font-size:12px;fill:var(--warn);}"
        ".t-note{font-size:11.5px;fill:var(--muted);}"
        # A halo, so an edge weight stays readable where curves bundle.
        ".t-edge{fill:var(--ink);stroke:var(--card);stroke-width:3px;"
        "paint-order:stroke fill;font-weight:600;}"
        ".bar{fill:var(--accent);}"
        ".bar2{fill:var(--accent2);}"
        ".track{fill:var(--track);}"
        ".nullbar{fill:var(--null);}"
        ".card{fill:var(--card);stroke:var(--line);}"
        ".page{fill:var(--bg);}"
        ".edge{stroke:var(--accent);fill:none;}"
        "]]></style>"
    )


def _defs() -> str:
    """R2's hatch. Two patterns so unbucketed data reads as unbucketed on a
    monochrome printer and for a colour-blind reader."""
    return (
        "<defs>"
        + _svg_style()
        + '<pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)">'
        '<rect width="6" height="6" fill="var(--null)"/>'
        '<rect width="3" height="6" fill="rgba(0,0,0,0.16)"/>'
        "</pattern>"
        '<pattern id="hatchkey" width="4" height="4" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)">'
        '<rect width="4" height="4" fill="var(--null)"/>'
        '<rect width="2" height="4" fill="rgba(0,0,0,0.16)"/>'
        "</pattern>"
        "</defs>"
    )


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        candidate = f"{cur} {word}".strip()
        if len(candidate) > width and cur:
            lines.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    return lines


def _header(response: QueryResponse, y: int) -> tuple[list[str], int]:
    """Title, denominator note (R4), truncation pill (R3)."""
    viz, meta = response.visualization, response.meta
    out = [f'<text class="t-title" x="{PAD}" y="{y + 16}">{_e(viz.title)}</text>']

    if viz.type is VizType.NETWORK_GRAPH:
        pill = f"{_n(viz.returned)} of {_n(viz.total)} nodes · {_n(len(viz.edges))} edges"
    else:
        pill = f"{_n(viz.returned)} of {_n(viz.total)} buckets"
    out.append(
        f'<text class="t-pill" x="{W - PAD}" y="{y + 16}" text-anchor="end">{_e(pill)}</text>'
    )

    # The denominator note is a filter expression and can be long — it wraps
    # down the left column rather than running under the pill. A collision here
    # is not cosmetic: the note is the base for every number on the chart, and
    # R4 says it must be legible next to the mark.
    cursor = y + 34
    for line in _wrap(meta.denominator_note, 96)[:3]:
        out.append(f'<text class="t-denom" x="{PAD}" y="{cursor}">{_e(line)}</text>')
        cursor += 15
    out.append(
        f'<text class="t-note" x="{PAD}" y="{cursor}">{_e(_clip(meta.interpretation, 118))}</text>'
    )
    return out, cursor + 16


def _strip(response: QueryResponse, y: int) -> tuple[list[str], int]:
    """R1. The reconciliation strip. Warning-coloured when the buckets overlap,
    confirmation-coloured when they genuinely partition."""
    rec = reconciliation(response)
    if rec["bucket_sum"] is None and rec["exclusive"]:
        return [], y

    lines = [rec["headline"]]
    if rec["bucket_sum"] is not None:
        sign = "≠" if not rec["exclusive"] else "="
        math = (
            f"bucket sum {_n(rec['bucket_sum'])}  {sign}  "
            f"trials matching {_n(rec['total_matching'])}"
        )
        if rec["delta"]:
            math += f"   (delta {rec['delta']:+,})"
        if rec["unbucketed_count"]:
            math += f"   unbucketed {_n(rec['unbucketed_count'])}"
        lines.append(math)
    for warning in rec["warnings"]:
        lines.extend("· " + part for part in _wrap(warning, 108)[:1])
        for extra in _wrap(warning, 108)[1:]:
            lines.append("  " + extra)

    height = 16 + 15 * len(lines)
    fill = "var(--warnbg)" if not rec["exclusive"] else "var(--okbg)"
    stroke = "var(--warn)" if not rec["exclusive"] else "var(--accent)"
    out = [
        f'<rect x="{PAD}" y="{y}" width="{W - 2 * PAD}" height="{height}" rx="7" '
        f'fill="{fill}" stroke="{stroke}"/>',
        f'<rect x="{PAD}" y="{y}" width="3" height="{height}" fill="{stroke}"/>',
        f'<text class="t-val" x="{PAD + 14}" y="{y + 19}" fill="{stroke}">'
        f'{"≠" if not rec["exclusive"] else "="}</text>',
    ]
    for i, line in enumerate(lines):
        cls = "t-warn" if i == 0 and not rec["exclusive"] else "t-note"
        out.append(
            f'<text class="{cls}" x="{PAD + 34}" y="{y + 19 + 15 * i}">{_e(line)}</text>'
        )
    return out, y + height + 14


def _bar_rows(data: list[Datum], y: int, series_colours: dict[str, str]) -> tuple[list[str], int]:
    """Horizontal bars. Every bar carries its number; unbucketed bars are hatched."""
    out: list[str] = []
    x0 = PAD + LABEL_W + 12
    track_w = W - PAD - RIGHT_W - x0 - 12
    peak = max((d.count for d in data), default=0) or 1

    for datum in data:
        width = max(2, int(track_w * datum.count / peak)) if datum.count else 2
        fill = "url(#hatch)" if datum.is_unbucketed else series_colours.get(datum.series or "", "")
        cls = "" if fill else "bar"
        fill_attr = f' fill="{fill}"' if fill else ""
        label = datum.label + (f"  · {datum.series}" if datum.series else "")
        out.append(
            f'<text class="t-lab" x="{PAD + LABEL_W}" y="{y + 18}" text-anchor="end">'
            f"{_e(_clip(label, 26))}</text>"
        )
        if datum.is_unbucketed:
            out.append(
                f'<text class="t-sub" x="{PAD + LABEL_W}" y="{y + 29}" text-anchor="end">'
                "no value recorded</text>"
            )
        out.append(
            f'<rect class="track" x="{x0}" y="{y + 3}" width="{track_w}" height="22" rx="4"/>'
        )
        out.append(
            f'<rect class="{cls}" x="{x0}" y="{y + 3}" width="{width}"{fill_attr} '
            'height="22" rx="4"/>'
        )
        # Numeric label: inside when the bar can hold it, outside when it
        # cannot — a long bar cannot put its number outside without colliding
        # with the citation column, and a short bar cannot put it inside.
        #
        # The ink of an inside label is chosen from the fill, not fixed: white
        # is legible on the primary accent but sits at ~2.2:1 on accent-weak
        # and ~1.9:1 on the reserved grey. The label *is* the number, so it is
        # the last thing on the chart allowed to be hard to read.
        text = _n(datum.count)
        on_primary = cls == "bar" or fill == "var(--accent)"
        if width > 9 * len(text) + 18:
            ink = "t-val-in" if on_primary else "t-val-ink"
            out.append(
                f'<text class="t-val {ink}" x="{x0 + width - 9}" y="{y + 19}" '
                f"text-anchor=\"end\">{_e(text)}</text>"
            )
        else:
            out.append(
                f'<text class="t-val t-val-out" x="{x0 + width + 8}" y="{y + 19}">{_e(text)}</text>'
            )
        support = (
            f"{_n(len(datum.citations))} of {_n(datum.citation_total)} cited"
            if datum.citation_total > len(datum.citations) and datum.citations
            else (f"{_n(datum.citation_total)} sources" if datum.citation_total else "")
        )
        if support:
            out.append(
                f'<text class="t-note" x="{W - PAD}" y="{y + 19}" text-anchor="end">'
                f"{_e(support)}</text>"
            )
        y += ROW_H + (6 if datum.is_unbucketed else 0)
    return out, y


def _legend(response: QueryResponse, y: int) -> tuple[list[str], int]:
    viz, meta = response.visualization, response.meta
    out = [
        f'<line x1="{PAD}" y1="{y}" x2="{W - PAD}" y2="{y}" stroke="var(--line)"/>',
    ]
    y += 18
    x = PAD
    entries: list[tuple[str, str]] = []
    series = _series_of(viz.data)
    if series:
        palette = _palette(series)
        entries.extend((palette[s], s) for s in series)
    else:
        entries.append(("var(--accent)", "measured"))
    if any(d.is_unbucketed for d in viz.data):
        entries.append(("url(#hatchkey)", "no value recorded"))
    for fill, label in entries:
        out.append(f'<rect x="{x}" y="{y - 9}" width="10" height="10" rx="2" fill="{fill}"/>')
        out.append(f'<text class="t-note" x="{x + 15}" y="{y}">{_e(label)}</text>')
        x += 22 + int(7.0 * len(label))
    tail = " · ".join(
        part
        for part in [
            f"sorted by {meta.sort}" if meta.sort else "",
            f"units: {meta.units}" if meta.units else "",
            f"granularity: {meta.time_granularity}" if meta.time_granularity else "",
            f"source: {meta.source}",
        ]
        if part
    )
    out.append(f'<text class="t-note" x="{W - PAD}" y="{y}" text-anchor="end">{_e(tail)}</text>')
    y += 12

    for note in showing_notes(response)[:6]:
        y += 14
        out.append(f'<text class="t-note" x="{PAD}" y="{y}">· {_e(note)}</text>')
    return out, y + 10


def _palette(series: list[str]) -> dict[str, str]:
    """Accent, then accent-weak, then muted. Never the null grey — that is
    reserved for unbucketed data and must not mean 'series 3'."""
    ramp = ["var(--accent)", "var(--accent2)", "var(--muted)", "var(--warn)"]
    return {name: ramp[i % len(ramp)] for i, name in enumerate(series)}


def _timeseries_body(response: QueryResponse, y: int) -> tuple[list[str], int]:
    """Vertical columns. Same rules: numeric label on every column."""
    data = response.visualization.data
    out: list[str] = []
    plot_h = 190
    x0, plot_w = PAD + 8, W - 2 * PAD - 16
    peak = max((d.count for d in data), default=0) or 1
    slot = plot_w / max(1, len(data))
    bar_w = max(6, min(48, slot * 0.66))
    base = y + plot_h

    out.append(f'<line x1="{x0}" y1="{base}" x2="{x0 + plot_w}" y2="{base}" stroke="var(--line)"/>')
    for i, datum in enumerate(data):
        height = max(2, int((plot_h - 28) * datum.count / peak))
        cx = x0 + slot * i + slot / 2
        bx = cx - bar_w / 2
        fill = "url(#hatch)" if datum.is_unbucketed else "var(--accent)"
        out.append(
            f'<rect x="{bx:.1f}" y="{base - height}" width="{bar_w:.1f}" height="{height}" '
            f'rx="3" fill="{fill}"/>'
        )
        out.append(
            f'<text class="t-val t-val-out" x="{cx:.1f}" y="{base - height - 6}" '
            f'text-anchor="middle">{_e(_n(datum.count))}</text>'
        )
        out.append(
            f'<text class="t-lab" x="{cx:.1f}" y="{base + 16}" text-anchor="middle">'
            f"{_e(_clip(datum.label, 10))}</text>"
        )
        if datum.is_unbucketed:
            out.append(
                f'<text class="t-sub" x="{cx:.1f}" y="{base + 28}" text-anchor="middle">'
                "no value</text>"
            )
    return out, base + 40


def _network_body(response: QueryResponse, y: int) -> tuple[list[str], int]:
    """Deterministic bipartite layout — no force simulation, so the same
    response always produces byte-identical SVG."""
    viz = response.visualization
    nodes = list(viz.nodes)
    out: list[str] = []
    if not nodes:
        out.append(f'<text class="t-note" x="{PAD}" y="{y + 14}">No nodes returned.</text>')
        return out, y + 30

    types = []
    for node in nodes:
        if node.type not in types:
            types.append(node.type)
    left_type = types[0]
    left = [n for n in nodes if n.type == left_type]
    right = [n for n in nodes if n.type != left_type]
    if not right:
        right, left = left[len(left) // 2 :], left[: len(left) // 2]

    rows = max(len(left), len(right))
    step = 34
    lx, rx = PAD + 250, W - PAD - 250
    pos: dict[str, tuple[float, float]] = {}
    for i, node in enumerate(left):
        pos[node.id] = (lx, y + 16 + i * step)
    for i, node in enumerate(right):
        pos[node.id] = (rx, y + 16 + i * step)

    peak_w = max((e.weight for e in viz.edges), default=1) or 1
    drawable = [e for e in viz.edges if e.source in pos and e.target in pos]
    labels: list[str] = []
    for index, edge in enumerate(drawable):
        (x1, y1), (x2, y2) = pos[edge.source], pos[edge.target]
        stroke = 1 + 4.0 * edge.weight / peak_w
        mid = (x1 + x2) / 2
        out.append(
            f'<path class="edge" d="M{x1},{y1} C{mid},{y1} {mid},{y2} {x2},{y2}" '
            f'stroke-width="{stroke:.2f}" stroke-opacity="0.45"/>'
        )
        # Weight labels ride the curve at a staggered parameter rather than all
        # sitting at the midpoint, where they pile into an unreadable stack —
        # the failure mode of every naive bipartite render. Deterministic in
        # edge order, so the layout never shifts between runs.
        span = 0.55 / max(1, len(drawable) - 1) if len(drawable) > 1 else 0.0
        t = 0.24 + span * index
        u = 1 - t
        bx = u**3 * x1 + 3 * u**2 * t * mid + 3 * u * t**2 * mid + t**3 * x2
        by = u**3 * y1 + 3 * u**2 * t * y1 + 3 * u * t**2 * y2 + t**3 * y2
        labels.append(
            f'<text class="t-note t-edge" x="{bx:.0f}" y="{by - 4:.0f}" '
            f'text-anchor="middle">{_e(_n(edge.weight))}</text>'
        )
    # Drawn after every curve so no line crosses a number.
    out.extend(labels)

    peak_n = max((n.count for n in nodes), default=1) or 1
    for node in nodes:
        x, ny = pos[node.id]
        radius = 4 + 7.0 * (node.count / peak_n) ** 0.5
        anchor, tx = ("end", x - 12) if x == lx else ("start", x + 12)
        out.append(f'<circle cx="{x}" cy="{ny}" r="{radius:.1f}" fill="var(--accent)"/>')
        out.append(
            f'<text class="t-lab" x="{tx}" y="{ny + 4}" text-anchor="{anchor}">'
            f"{_e(_clip(node.label, 28))}</text>"
        )
        out.append(
            f'<text class="t-note" x="{tx + (-8 if anchor == "end" else 8)}" y="{ny + 16}" '
            f'text-anchor="{anchor}">{_e(node.type)} · {_e(_n(node.count))}</text>'
        )
    return out, y + 16 + rows * step + 20


def _single_value_body(response: QueryResponse, y: int) -> tuple[list[str], int]:
    datum = response.visualization.data[0]
    out = [
        f'<text x="{PAD}" y="{y + 54}" font-size="52" font-weight="640" '
        f'font-family="{_MONO}" fill="var(--accent)">{_e(_n(datum.count))}</text>',
        f'<text class="t-lab" x="{PAD}" y="{y + 78}">{_e(datum.label)}</text>',
    ]
    return out, y + 96


def render_svg(response: QueryResponse, theme: str | None = None) -> str:
    """Standalone SVG for any QueryResponse.

    `theme` is None (follow the viewer's colour scheme), "light", or "dark".
    Never emits a pie, donut, treemap or 100% stacked bar for any input.
    """
    viz = response.visualization
    body: list[str] = []
    y = PAD

    head, y = _header(response, y)
    body += head
    strip, y = _strip(response, y)
    body += strip

    if viz.type is VizType.NETWORK_GRAPH:
        chart, y = _network_body(response, y)
    elif viz.type is VizType.SINGLE_VALUE:
        chart, y = _single_value_body(response, y)
    elif viz.type is VizType.TIME_SERIES:
        chart, y = _timeseries_body(response, y)
    else:
        colours = _palette(_series_of(viz.data))
        chart, y = _bar_rows(viz.data, y, colours)
    body += chart

    legend, y = _legend(response, y + 6)
    body += legend

    height = int(y + PAD)
    theme_attr = f' data-theme="{theme}"' if theme in ("light", "dark") else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" '
        f'viewBox="0 0 {W} {height}" role="img"{theme_attr} '
        f'aria-label="{_e(viz.title)} — {_e(response.meta.denominator_note)}">'
        f"<title>{_e(viz.title)}</title>"
        f"<desc>{_e(response.meta.interpretation)} "
        f"Part-of-whole marks are {'permitted' if response.meta.buckets_are_exclusive else 'FORBIDDEN'}: "
        f"buckets_are_exclusive={str(response.meta.buckets_are_exclusive).lower()}.</desc>"
        + _defs()
        + f'<rect class="page" x="0" y="0" width="{W}" height="{height}"/>'
        + "".join(body)
        + "</svg>"
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def svg_data_uri(svg: str) -> str:
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def render_markdown(response: QueryResponse, embed_svg: bool = True) -> str:
    """Markdown suitable for pasting into an agent transcript.

    Table first (readable everywhere, including places that strip images), then
    the same chart as an inline data-URI SVG so a human reading the transcript
    sees the picture without fetching anything.
    """
    viz, meta = response.visualization, response.meta
    rec = reconciliation(response)
    out: list[str] = [f"### {viz.title}", "", f"*{meta.denominator_note}*", ""]
    out.append(f"> {meta.interpretation}")
    out.append("")

    if not rec["exclusive"]:
        out.append("> **≠ Buckets overlap — not a part-of-whole chart.**  ")
        if rec["bucket_sum"] is not None:
            out.append(
                f"> bucket sum `{_n(rec['bucket_sum'])}` ≠ trials matching "
                f"`{_n(rec['total_matching'])}`"
                + (f" · unbucketed `{_n(rec['unbucketed_count'])}`" if rec["unbucketed_count"] else "")
                + "  "
            )
        out.append("> Do not render as a pie, donut, treemap or 100% stacked bar.")
        for warning in rec["warnings"]:
            out.append(f"> - {warning}")
        out.append("")
    elif rec["bucket_sum"] is not None:
        out.append(
            f"> **= Buckets are mutually exclusive.** bucket sum `{_n(rec['bucket_sum'])}` "
            f"= trials matching `{_n(rec['total_matching'])}` — shares are safe to read."
        )
        out.append("")

    if viz.type is VizType.NETWORK_GRAPH:
        out += ["| node | type | count |", "|---|---|---:|"]
        for node in viz.nodes:
            out.append(f"| {node.label} | {node.type} | {_n(node.count)} |")
        out.append("")
        if viz.edges:
            out += ["| source | target | weight |", "|---|---|---:|"]
            for edge in viz.edges:
                left = next((n.label for n in viz.nodes if n.id == edge.source), edge.source)
                right = next((n.label for n in viz.nodes if n.id == edge.target), edge.target)
                out.append(f"| {left} | {right} | {_n(edge.weight)} |")
            out.append("")
    else:
        has_series = bool(_series_of(viz.data))
        header = "| label |" + (" series |" if has_series else "") + " count | citations |"
        rule = "|---|" + ("---|" if has_series else "") + "---:|---|"
        out += [header, rule]
        for datum in viz.data:
            label = datum.label + (" *(no value recorded)*" if datum.is_unbucketed else "")
            cite = (
                f"showing {_n(len(datum.citations))} of {_n(datum.citation_total)}"
                if datum.citation_total > len(datum.citations) and datum.citations
                else (_n(datum.citation_total) if datum.citation_total else "—")
            )
            row = f"| {label} |"
            if has_series:
                row += f" {datum.series or '—'} |"
            row += f" {_n(datum.count)} | {cite} |"
            out.append(row)
        out.append("")

    for note in showing_notes(response):
        out.append(f"- {note}")
    if showing_notes(response):
        out.append("")

    if embed_svg:
        out.append(f"![{viz.title}]({svg_data_uri(render_svg(response))})")
        out.append("")

    if meta.assumptions:
        out.append("**Assumptions**")
        out += [f"- {a}" for a in meta.assumptions]
        out.append("")

    out.append("<details><summary>Trace — every number is replayable</summary>")
    out.append("")
    out += ["| step | result | params |", "|---|---:|---|"]
    for step in meta.trace:
        params = " ".join(f"`{k}={v}`" for k, v in step.params.items())
        out.append(f"| {step.step} | {_n(step.result_count)} | {params} |")
    out += ["", f"Source: {meta.source}", "</details>", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# ASCII
# ---------------------------------------------------------------------------

_FULL = "█"   # measured
_HATCH = "▒"  # unbucketed — a different glyph, not a different colour


def render_ascii(response: QueryResponse, width: int = 88) -> str:
    """Terminal output. Deterministic, no colour, no cursor control — safe to
    capture into a log or an agent transcript."""
    viz, meta = response.visualization, response.meta
    rec = reconciliation(response)
    lines: list[str] = [viz.title, meta.denominator_note, f"~ {meta.interpretation}", ""]

    if not rec["exclusive"]:
        lines.append("!= BUCKETS OVERLAP - not a part-of-whole chart (no pie, no 100% stack)")
        if rec["bucket_sum"] is not None:
            detail = (
                f"   bucket sum {_n(rec['bucket_sum'])} != "
                f"trials matching {_n(rec['total_matching'])}"
            )
            if rec["delta"]:
                detail += f"  (delta {rec['delta']:+,})"
            if rec["unbucketed_count"]:
                detail += f"  unbucketed {_n(rec['unbucketed_count'])}"
            lines.append(detail)
        for warning in rec["warnings"]:
            for i, part in enumerate(_wrap(warning, width - 6)):
                lines.append(("   - " if i == 0 else "     ") + part)
        lines.append("")
    elif rec["bucket_sum"] is not None:
        lines.append(
            f"=  buckets are exclusive: bucket sum {_n(rec['bucket_sum'])} = "
            f"trials matching {_n(rec['total_matching'])}"
        )
        lines.append("")

    if viz.type is VizType.NETWORK_GRAPH:
        lines.append(f"NODES ({_n(viz.returned)} of {_n(viz.total)})")
        peak = max((n.count for n in viz.nodes), default=1) or 1
        for node in viz.nodes:
            bar = _FULL * max(1, int(28 * node.count / peak))
            lines.append(f"  {_clip(node.label, 30):<30} {bar:<28} {_n(node.count):>7}  {node.type}")
        lines.append("")
        if viz.edges:
            lines.append(f"EDGES ({_n(len(viz.edges))})")
            labels = {n.id: n.label for n in viz.nodes}
            peak_e = max((e.weight for e in viz.edges), default=1) or 1
            for edge in viz.edges:
                left = _clip(labels.get(edge.source, edge.source), 24)
                right = _clip(labels.get(edge.target, edge.target), 24)
                bar = _FULL * max(1, int(16 * edge.weight / peak_e))
                lines.append(f"  {left:>24} -> {right:<24} {bar:<16} {_n(edge.weight):>6}")
    else:
        label_w = min(24, max((len(d.label) for d in viz.data), default=6) + 2)
        if any(d.series for d in viz.data):
            label_w = min(34, label_w + 12)
        bar_w = max(12, width - label_w - 22)
        peak = max((d.count for d in viz.data), default=0) or 1
        for datum in viz.data:
            name = datum.label + (f"/{datum.series}" if datum.series else "")
            glyph = _HATCH if datum.is_unbucketed else _FULL
            filled = max(1, int(bar_w * datum.count / peak)) if datum.count else 0
            bar = glyph * filled
            tail = "  (no value recorded)" if datum.is_unbucketed else ""
            lines.append(
                f"  {_clip(name, label_w):<{label_w}} {bar:<{bar_w}} {_n(datum.count):>9}{tail}"
            )
        lines.append("")
        lines.append(f"  legend: {_FULL} measured   {_HATCH} no value recorded")

    lines.append("")
    tail = " | ".join(
        part
        for part in [
            f"sort={meta.sort}" if meta.sort else "",
            f"units={meta.units}" if meta.units else "",
            f"granularity={meta.time_granularity}" if meta.time_granularity else "",
            f"source={meta.source}",
            f"api calls={len(meta.trace)}",
        ]
        if part
    )
    lines.append("  " + tail)
    for note in showing_notes(response):
        lines.append(f"  * {note}")
    for assumption in meta.assumptions:
        for i, part in enumerate(_wrap(assumption, width - 6)):
            lines.append(("  ~ " if i == 0 else "    ") + part)
    return "\n".join(lines)
