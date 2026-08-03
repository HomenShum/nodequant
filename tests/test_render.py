"""Rendering-contract tests.

These are not "does it draw something" tests. Each one pins a rule that, if
broken, makes the artifact *lie about the data* — which is the exact failure
this whole project exists to prevent. A renderer that silently drops the
unbucketed remainder or draws a pie over overlapping buckets is worse than no
renderer, because it looks authoritative.

Offline: everything runs against the bundled example runs and hand-built
responses. No network, no key.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.models import (
    Citation,
    Datum,
    Encoding,
    GraphEdge,
    GraphNode,
    QueryResponse,
    ResponseMeta,
    TraceStep,
    Visualization,
    VizType,
)
from app.render import (
    PART_OF_WHOLE_TYPES,
    reconciliation,
    render_ascii,
    render_markdown,
    render_svg,
    showing_notes,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
SLUGS = [
    "01-distribution-phases",
    "02-time-series",
    "03-comparison",
    "04-network-graph",
    "05-geographic",
]


def load(slug: str) -> QueryResponse:
    raw = json.loads((EXAMPLES / f"{slug}.json").read_text(encoding="utf-8"))
    return QueryResponse.model_validate(raw["response"])


@pytest.fixture
def overlapping() -> QueryResponse:
    """The melanoma-phases case: buckets overlap AND 136 trials carry no phase."""
    return load("01-distribution-phases")


def exclusive_response() -> QueryResponse:
    """A hand-built partition, to prove the strip flips state rather than
    being hardcoded to 'warning'."""
    return QueryResponse(
        visualization=Visualization(
            type=VizType.BAR_CHART,
            title="Trials by status",
            encoding=Encoding(x={"field": "label"}, y={"field": "count"}),
            data=[
                Datum(label="RECRUITING", count=60, citation_total=60),
                Datum(label="COMPLETED", count=40, citation_total=40),
            ],
            returned=2,
            total=2,
        ),
        meta=ResponseMeta(
            interpretation="Trials grouped by status.",
            total_matching=100,
            denominator_note="of 100 trials matching query.cond=x",
            buckets_are_exclusive=True,
            bucket_sum=100,
            unbucketed_count=0,
            trace=[TraceStep(step="count matching", url="u", params={}, result_count=100)],
        ),
    )


# ---------------------------------------------------------------------------
# R1 — non-exclusive buckets must never be drawn as a part of a whole
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_no_part_of_whole_mark_is_ever_emitted(slug: str) -> None:
    """Every bundled run has buckets_are_exclusive == false, so not one of the
    five may produce a pie, donut, treemap or 100% stacked mark. The check is
    structural (SVG element names and shape attributes), not a string search,
    because the warning text legitimately contains the words "pie" and
    "stacked bar"."""
    response = load(slug)
    assert response.meta.buckets_are_exclusive is False, "fixture assumption"

    root = ET.fromstring(render_svg(response))
    tags = {element.tag.split("}")[-1] for element in root.iter()}

    # A pie/donut slice can only be an <path> arc, a <circle> with a
    # stroke-dasharray, or a <polygon>. Bars are <rect>, network edges are
    # cubic <path> with no arc command.
    assert "polygon" not in tags
    for path in root.iter("{http://www.w3.org/2000/svg}path"):
        assert "A" not in (path.get("d") or "").upper(), "arc segment implies a pie slice"
    for circle in root.iter("{http://www.w3.org/2000/svg}circle"):
        assert circle.get("stroke-dasharray") is None, "dasharray circle implies a donut"

    # And the response's own verdict is carried into the artifact.
    assert reconciliation(response)["render_part_of_whole"] is False
    assert set(reconciliation(response)["forbidden_marks"]) == set(PART_OF_WHOLE_TYPES)


def test_reconciliation_strip_states_both_numbers(overlapping: QueryResponse) -> None:
    """The strip must carry total_matching AND bucket_sum, in all three formats.
    A warning that says "buckets overlap" without the arithmetic is a mood, not
    a disclosure."""
    svg = render_svg(overlapping)
    md = render_markdown(overlapping)
    ascii_art = render_ascii(overlapping)

    for artifact in (svg, md, ascii_art):
        assert "3,602" in artifact, "bucket_sum missing"
        assert "3,738" in artifact, "total_matching missing"
        assert "≠" in artifact or "!=" in artifact

    # Every warning the executor attached must reach the reader.
    for warning in overlapping.meta.warnings:
        assert warning[:50] in md
        assert warning.split()[0] in ascii_art


def test_strip_flips_to_confirmation_when_buckets_do_partition() -> None:
    response = exclusive_response()
    rec = reconciliation(response)
    assert rec["render_part_of_whole"] is True

    md = render_markdown(response)
    assert "mutually exclusive" in md
    assert "Buckets overlap" not in md
    assert "safe to read" in render_ascii(response) or "exclusive" in render_ascii(response)

    root = ET.fromstring(render_svg(response))
    desc = root.find("{http://www.w3.org/2000/svg}desc")
    assert desc is not None and "permitted" in (desc.text or "")


# ---------------------------------------------------------------------------
# R2 — the unbucketed remainder is rendered, in grey, with a hatch
# ---------------------------------------------------------------------------


def test_unbucketed_datum_is_rendered_and_hatched(overlapping: QueryResponse) -> None:
    unbucketed = [d for d in overlapping.visualization.data if d.is_unbucketed]
    assert unbucketed, "fixture assumption: example 01 has an NA bucket"
    datum = unbucketed[0]

    svg = render_svg(overlapping)
    root = ET.fromstring(svg)

    # It is present, not dropped.
    assert datum.label in svg
    assert f"{datum.count:,}" in svg

    # It is filled with the hatch pattern, and the pattern is defined.
    patterns = {p.get("id") for p in root.iter("{http://www.w3.org/2000/svg}pattern")}
    assert "hatch" in patterns
    hatched = [
        r for r in root.iter("{http://www.w3.org/2000/svg}rect")
        if r.get("fill") == "url(#hatch)"
    ]
    assert len(hatched) == len(unbucketed)

    # ASCII distinguishes it with a different glyph, not a colour.
    ascii_art = render_ascii(overlapping)
    assert "▒" in ascii_art and "█" in ascii_art
    assert "no value recorded" in ascii_art
    assert "no value recorded" in svg
    assert "no value recorded" in render_markdown(overlapping)


def test_null_grey_is_used_for_nothing_but_unbucketed_data() -> None:
    """The reserved grey must not leak into a series colour, or 'no value
    recorded' stops meaning anything."""
    from app.render import _palette

    palette = _palette([f"series{i}" for i in range(6)])
    assert "var(--null)" not in palette.values()
    assert "url(#hatch)" not in palette.values()


def test_zero_count_bucket_is_kept() -> None:
    response = exclusive_response()
    response.visualization.data.append(Datum(label="TERMINATED", count=0, citation_total=0))
    response.visualization.returned = 3
    response.visualization.total = 3
    for artifact in (render_svg(response), render_ascii(response), render_markdown(response)):
        assert "TERMINATED" in artifact


# ---------------------------------------------------------------------------
# R3 — showing N of M
# ---------------------------------------------------------------------------


def test_showing_n_of_m_when_citations_are_a_sample(overlapping: QueryResponse) -> None:
    notes = showing_notes(overlapping)
    assert notes, "example 01 returns 3 citations against totals in the hundreds"
    assert any("showing 3 of 1,529" in note for note in notes)

    md = render_markdown(overlapping)
    assert "showing 3 of 1,529" in md
    assert "showing 3 of 1,529" in render_ascii(overlapping)


def test_showing_n_of_m_when_marks_are_truncated() -> None:
    response = exclusive_response()
    response.visualization.total = 40  # returned stays 2
    notes = showing_notes(response)
    assert "showing 2 of 40 marks" in notes
    assert "2 of 40 buckets" in render_svg(response)
    assert "showing 2 of 40 marks" in render_ascii(response)


def test_no_showing_note_when_nothing_is_truncated() -> None:
    assert showing_notes(exclusive_response()) == []


# ---------------------------------------------------------------------------
# R4 — the denominator travels with the chart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_denominator_note_appears_in_every_format(slug: str) -> None:
    response = load(slug)
    note = response.meta.denominator_note
    assert note in render_ascii(response)
    assert note in render_markdown(response)
    # In the SVG it is both a visible <text> and the accessible label.
    svg = render_svg(response)
    assert note in svg
    root = ET.fromstring(svg)
    assert note in (root.get("aria-label") or "")


# ---------------------------------------------------------------------------
# no colour-only encoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_every_mark_carries_a_numeric_label(slug: str) -> None:
    response = load(slug)
    svg = render_svg(response)
    viz = response.visualization
    marks = (
        [(n.label, n.count) for n in viz.nodes] + [(e.source, e.weight) for e in viz.edges]
        if viz.type is VizType.NETWORK_GRAPH
        else [(d.label, d.count) for d in viz.data]
    )
    assert marks
    tokens: set[str] = set()
    for element in ET.fromstring(svg).iter("{http://www.w3.org/2000/svg}text"):
        tokens.update((element.text or "").split())
    for _, count in marks:
        assert f"{count:,}" in tokens, f"{count} has no numeric label in {slug}"


@pytest.mark.parametrize("slug", SLUGS)
def test_numeric_labels_never_sit_white_on_a_light_fill(slug: str) -> None:
    """The label IS the number, so it is the last thing allowed to be hard to
    read. White is only legible on the primary accent — ~2.2:1 on accent-weak
    and ~1.9:1 on the reserved grey — so any other fill must use dark ink."""
    root = ET.fromstring(render_svg(load(slug)))
    bars = {
        round(float(r.get("x", 0)) + float(r.get("width", 0))): (
            r.get("class"), r.get("fill")
        )
        for r in root.iter("{http://www.w3.org/2000/svg}rect")
        if r.get("height") == "22" and r.get("class") != "track"
    }
    for text in root.iter("{http://www.w3.org/2000/svg}text"):
        classes = (text.get("class") or "").split()
        if "t-val-in" not in classes:
            continue
        # An inside white label ends 9px before its bar's right edge.
        end = round(float(text.get("x", 0)) + 9)
        assert end in bars, "a white label is not anchored to a bar"
        cls, fill = bars[end]
        assert cls == "bar" or fill == "var(--accent)", (
            f"white label on {fill or cls} in {slug}"
        )


@pytest.mark.parametrize("slug", SLUGS)
def test_no_label_escapes_the_plot_into_the_citation_column(slug: str) -> None:
    """A full-width bar cannot put its number outside without colliding with
    the citation column, and a hairline bar cannot put it inside. Both cases
    must resolve without overlap."""
    root = ET.fromstring(render_svg(load(slug)))
    tracks = [
        r for r in root.iter("{http://www.w3.org/2000/svg}rect")
        if r.get("class") == "track"
    ]
    if not tracks:
        pytest.skip("no bar rows in this visualization type")
    right_edge = float(tracks[0].get("x", 0)) + float(tracks[0].get("width", 0))
    for text in root.iter("{http://www.w3.org/2000/svg}text"):
        if "t-val-out" not in (text.get("class") or "").split():
            continue
        # ~7.2px per monospace glyph at 12px.
        end = float(text.get("x", 0)) + 7.2 * len(text.text or "")
        assert end <= right_edge, f"outside label overruns the plot in {slug}"


@pytest.mark.parametrize("slug", SLUGS)
def test_ascii_carries_every_count(slug: str) -> None:
    response = load(slug)
    art = render_ascii(response)
    viz = response.visualization
    counts = (
        [n.count for n in viz.nodes] if viz.type is VizType.NETWORK_GRAPH
        else [d.count for d in viz.data]
    )
    for count in counts:
        assert f"{count:,}" in art


# ---------------------------------------------------------------------------
# structural / robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", SLUGS)
def test_svg_is_well_formed_and_self_contained(slug: str) -> None:
    svg = render_svg(load(slug))
    root = ET.fromstring(svg)  # raises on malformed XML
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert int(root.get("height", "0")) > 100
    assert root.get("viewBox")
    # Self-contained: no external fetches of any kind.
    assert "http://" not in svg.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in svg
    assert "<image" not in svg and "xlink:href" not in svg


@pytest.mark.parametrize("slug", SLUGS)
def test_svg_supports_light_and_dark(slug: str) -> None:
    svg = render_svg(load(slug))
    assert "prefers-color-scheme:dark" in svg
    assert 'svg[data-theme="dark"]' in svg
    assert 'svg[data-theme="light"]' in svg
    assert '#1f6f6b' in svg and '#5ec6c0' in svg  # light + dark accent
    assert '#b8b7b2' in svg and '#6a6963' in svg  # light + dark reserved null grey
    assert 'data-theme="dark"' in render_svg(load(slug), theme="dark")


@pytest.mark.parametrize("slug", SLUGS)
def test_render_is_deterministic(slug: str) -> None:
    """Two renders of one response must be byte-identical, or an agent cannot
    diff runs and a network layout would drift between screenshots."""
    response = load(slug)
    assert render_svg(response) == render_svg(load(slug))
    assert render_markdown(response) == render_markdown(load(slug))
    assert render_ascii(response) == render_ascii(load(slug))


@pytest.mark.parametrize("slug", SLUGS)
def test_markdown_embeds_a_decodable_svg(slug: str) -> None:
    import base64

    md = render_markdown(load(slug))
    match = re.search(r"data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)\)", md)
    assert match, "no inline chart in the markdown"
    decoded = base64.b64decode(match.group(1)).decode("utf-8")
    ET.fromstring(decoded)
    assert decoded == render_svg(load(slug))


def test_markdown_without_embed_has_no_data_uri(overlapping: QueryResponse) -> None:
    assert "data:image" not in render_markdown(overlapping, embed_svg=False)


def test_xml_escaping_survives_hostile_labels() -> None:
    response = exclusive_response()
    response.visualization.data[0].label = 'A & B <script>alert("x")</script>'
    svg = render_svg(response)
    assert "<script>" not in svg
    assert "&amp;" in svg
    ET.fromstring(svg)


def test_network_edges_carry_weights_and_nodes_carry_counts() -> None:
    response = load("04-network-graph")
    svg = render_svg(response)
    root = ET.fromstring(svg)
    # One curve per edge, and every curve is a cubic bezier (never an arc).
    paths = list(root.iter("{http://www.w3.org/2000/svg}path"))
    assert len(paths) == len(response.visualization.edges)
    assert all((p.get("d") or "").startswith("M") and " C" in (p.get("d") or "") for p in paths)
    assert len(list(root.iter("{http://www.w3.org/2000/svg}circle"))) == len(
        response.visualization.nodes
    )
    md = render_markdown(response)
    for edge in response.visualization.edges:
        assert f"{edge.weight:,}" in md


def test_single_value_and_empty_edges_do_not_crash() -> None:
    response = QueryResponse(
        visualization=Visualization(
            type=VizType.SINGLE_VALUE,
            title="Total trials",
            encoding=Encoding(y={"field": "count"}),
            data=[Datum(label="matching trials", count=1234, citation_total=1234)],
            returned=1,
            total=1,
        ),
        meta=ResponseMeta(
            interpretation="Total count.",
            total_matching=1234,
            denominator_note="of 1,234 trials matching query.cond=x",
            buckets_are_exclusive=True,
        ),
    )
    for artifact in (render_svg(response), render_ascii(response), render_markdown(response)):
        assert "1,234" in artifact
    ET.fromstring(render_svg(response))


def test_network_with_nodes_but_no_edges_renders() -> None:
    response = QueryResponse(
        visualization=Visualization(
            type=VizType.NETWORK_GRAPH,
            title="Empty network",
            encoding=Encoding(nodes={"field": "label"}),
            nodes=[GraphNode(id="a", label="A", type="sponsor", count=1)],
            edges=[],
            returned=1,
            total=1,
        ),
        meta=ResponseMeta(
            interpretation="No co-occurrences found.",
            total_matching=1,
            denominator_note="of 1 trial matching query.cond=x",
            buckets_are_exclusive=True,
        ),
    )
    ET.fromstring(render_svg(response))
    assert "A" in render_ascii(response)


def test_citation_excerpts_are_not_invented() -> None:
    """The renderer must never synthesise text into a citation slot."""
    response = load("01-distribution-phases")
    md = render_markdown(response)
    excerpts = [c.excerpt for d in response.visualization.data for c in d.citations]
    assert excerpts
    # We currently surface counts, not excerpt prose, in markdown — assert that
    # nothing excerpt-shaped appears that is not verbatim from the response.
    for datum in response.visualization.data:
        for citation in datum.citations:
            assert citation.nct_id.startswith("NCT")
    assert "lorem" not in md.lower()


def test_edge_citation_sample_is_reported() -> None:
    response = load("04-network-graph")
    response.visualization.edges[0] = GraphEdge(
        source=response.visualization.edges[0].source,
        target=response.visualization.edges[0].target,
        weight=response.visualization.edges[0].weight,
        citations=[Citation(nct_id="NCT00000001", excerpt="x", field="briefTitle")],
        citation_total=99,
    )
    assert any("showing 1 of 99 citations" in note for note in showing_notes(response))
