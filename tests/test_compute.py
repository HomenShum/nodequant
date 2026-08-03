"""Adversarial tests for the derived-metric sandbox.

This is a sandbox over attacker-controlled text, so it is tested like one. The
persona behind these tests is not a curious analyst — it is a model that has
been prompt-injected into trying to reach the filesystem, and a model that has
been asked for a number it does not have and would rather fabricate than admit.

Three angles, all of them adversarial:

  1. **Escape** — can the expression reach Python? (imports, dunder traversal,
     attribute access, builtins, comprehensions, lambdas)
  2. **Fabrication** — can the expression produce a number that was not measured?
     (unknown names, invented labels, constants laundered through a real input)
  3. **Resource** — can the expression outlast or outgrow its budget?
     (length, node count, nesting, exponent bombs, division by zero)

Plus the boring-but-load-bearing case: a legitimate formula computes the exact
value a human computes by hand.

Offline. No network, no credentials, no model.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.compute import (
    MAX_AST_NODES,
    MAX_EXPRESSION_CHARS,
    ComputationError,
    DerivationError,
    DerivedMetric,
    MetricRequest,
    UnsafeExpressionError,
    derive,
    measured_data,
    run_sandboxed,
    validate_expression,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# A stand-in for a measured phase distribution. Every value here is what the
# executor would have written after a count probe.
MEASURED = {
    "PHASE1": 1340.0,
    "PHASE2": 1529.0,
    "PHASE3": 219.0,
    "PHASE4": 37.0,
    "EARLY_PHASE1": 65.0,
    "NA": 412.0,
    "total_matching": 3738.0,
}


# ---------------------------------------------------------------- 1. escape


@pytest.mark.parametrize(
    "expression",
    [
        # the canonical one
        "__import__('os').system('rm -rf /')",
        "__import__('subprocess')",
        # dunder traversal to the object graph — the classic eval escape
        "().__class__.__bases__",
        "().__class__.__bases__[0].__subclasses__()",
        "(1).__class__",
        "PHASE1.__class__.__mro__",
        "data.__class__",
        # attribute access at all, dunder or not
        "data.keys()",
        "PHASE1.real",
        "PHASE1.bit_length()",
        # builtins that were never allowlisted
        "open('x')",
        "open('/etc/passwd').read()",
        "eval('1+1')",
        "exec('x=1')",
        "getattr(PHASE1, 'real')",
        "globals()",
        "vars()",
        "compile('1', 'x', 'eval')",
        "input()",
        # callables that are not bare names
        "[min][0](PHASE1, PHASE2)",
        "(min if PHASE1 else max)(PHASE1, PHASE2)",
        "min(*[PHASE1, PHASE2])",
        "round(PHASE1, ndigits=2)",
        # lambdas, comprehensions, generators
        "(lambda: 1)()",
        "sum([x for x in [PHASE1, PHASE2]])",
        "sum(v for v in [PHASE1])",
        "{k: 1 for k in [PHASE1]}",
        "{x for x in [PHASE1]}",
        # statements and binding
        "(x := PHASE1)",
        # f-strings can evaluate arbitrary expressions
        "f'{PHASE1}'",
        # containers we never allowed
        "{'a': PHASE1}['a']",
        "{PHASE1}",
        # subscripting anything but `data`
        "[PHASE1, PHASE2][0]",
        "'abcd'[0]",
        "data['PHASE1'][0]",
        # dynamic keys defeat the "must be measured" check, so they are refused
        "data[PHASE1]",
        "data['PHA' + 'SE1']",
    ],
)
def test_escape_attempts_are_rejected_before_execution(expression: str) -> None:
    with pytest.raises(UnsafeExpressionError):
        run_sandboxed(expression, MEASURED)


def test_rejection_happens_at_validation_not_at_runtime() -> None:
    """The malicious expression must never reach `eval`.

    `validate_expression` compiles nothing and executes nothing, so proving the
    rejection happens there proves the payload never ran.
    """
    with pytest.raises(UnsafeExpressionError):
        validate_expression("__import__('os').system('echo pwned')", MEASURED)


def test_attribute_access_is_rejected_by_the_ast_layer_not_the_dunder_scan() -> None:
    """The dunder string scan is redundant, and this proves it.

    `().__class__.__bases__` trips the cheap `'__' in expression` check first,
    which would be worrying if that check were the only thing standing in the
    way — a payload without literal dunders would then walk straight through.
    It cannot: `Attribute` is not an allowlisted node type at all, so every
    object-graph traversal dies at its first `.`, dunder or not.
    """
    for expression in ("PHASE1.real", "().imag", "data['PHASE1'].real"):
        with pytest.raises(UnsafeExpressionError, match="Attribute nodes are not permitted"):
            run_sandboxed(expression, MEASURED)


def test_builtins_are_empty_even_for_a_name_that_slipped_through() -> None:
    """Defence in depth: the namespace itself has no builtins.

    If a future edit widened the allowlist by mistake, an escape would still
    have to find a callable, and there is none in scope.
    """
    from app.compute import _namespace

    namespace = _namespace(MEASURED)
    assert namespace["__builtins__"] == {}
    assert set(namespace) - {"__builtins__", "data", *MEASURED} == {
        "min", "max", "sum", "round", "abs", "len",
    }


# ----------------------------------------------------------- 2. fabrication


@pytest.mark.parametrize(
    "expression",
    [
        "phase3 / total * 100",          # plausible, but neither name was measured
        "PHASE5 / total_matching",       # a phase that does not exist
        "PHASE3 / prior_year * 100",     # the model inventing a denominator
        "glioblastoma_2023 - glioblastoma_2022",
        "PHASE3 + unmeasured_bucket",
    ],
)
def test_an_undefined_name_is_rejected(expression: str) -> None:
    """The model cannot invent an input.

    This is the rule the whole module exists to enforce: a name that is not a
    measured count has no value to resolve to, and defaulting it to zero would
    turn a fabrication into a plausible-looking number.
    """
    with pytest.raises(UnsafeExpressionError, match="unknown name|not a measured value"):
        run_sandboxed(expression, MEASURED)


def test_a_label_not_in_the_measured_data_is_rejected_via_subscript_too() -> None:
    with pytest.raises(UnsafeExpressionError, match="not a measured value"):
        run_sandboxed("data['PHASE9'] / data['total_matching']", MEASURED)


def test_a_formula_over_pure_literals_is_rejected() -> None:
    """`1529 / 3738 * 100` is a model-written NUMBER wearing a formula's coat."""
    with pytest.raises(UnsafeExpressionError, match="references no measured value"):
        run_sandboxed("1529 / 3738 * 100", MEASURED)


def test_a_constant_laundered_through_a_real_input_is_rejected() -> None:
    """The one gap the name check alone leaves.

    `total_matching * 0 + 5.9` names a measured input and validates cleanly, yet
    returns a number the API never produced. `derive` perturbs each input and
    refuses a result that never moves.
    """
    response = json.loads((EXAMPLES / "01-distribution-phases.json").read_text())
    with pytest.raises(UnsafeExpressionError, match="regardless of its inputs"):
        derive(
            [MetricRequest(name="fake share", expression="total_matching * 0 + 5.9",
                           unit="percent")],
            response,
        )


def test_derived_metric_cannot_be_constructed_with_unmeasured_inputs() -> None:
    with pytest.raises(ValueError, match="inputs_are_measured"):
        DerivedMetric(name="n", expression="a / b", inputs=["a", "b"], result=1.0,
                      unit="ratio", inputs_are_measured=False)


def test_derived_metric_requires_at_least_one_input() -> None:
    with pytest.raises(ValueError, match="at least one measured input"):
        DerivedMetric(name="n", expression="42", inputs=[], result=42.0, unit="trials")


def test_derived_metric_rejects_a_non_finite_result() -> None:
    with pytest.raises(ValueError, match="finite"):
        DerivedMetric(name="n", expression="a", inputs=["a"], result=math.inf, unit="ratio")


def test_a_measured_input_may_not_be_a_string_or_bool() -> None:
    with pytest.raises(DerivationError):
        run_sandboxed("a + 1", {"a": "1529"})  # type: ignore[dict-item]
    with pytest.raises(DerivationError):
        run_sandboxed("a + 1", {"a": True})  # type: ignore[dict-item]


# -------------------------------------------------------------- 3. resource


def test_an_over_long_expression_is_rejected() -> None:
    expression = "PHASE1 + " * 200 + "PHASE1"
    assert len(expression) > MAX_EXPRESSION_CHARS
    with pytest.raises(UnsafeExpressionError, match="cap is"):
        run_sandboxed(expression, MEASURED)


def test_an_enormous_expression_is_rejected_by_the_node_cap() -> None:
    """Under the length cap, over the node cap — so the node cap is what fires."""
    expression = "+".join(["NA"] * 180)
    assert len(expression) <= MAX_EXPRESSION_CHARS
    with pytest.raises(UnsafeExpressionError, match="AST nodes, cap is"):
        run_sandboxed(expression, MEASURED)
    assert len(expression.split("+")) > MAX_AST_NODES // 2


def test_a_deeply_nested_expression_is_rejected() -> None:
    """Parenthesis bombs are refused, whether by the depth cap or by the parser."""
    expression = "(" * 250 + "NA" + ")" * 250
    with pytest.raises(UnsafeExpressionError):
        run_sandboxed(expression, MEASURED)


@pytest.mark.parametrize(
    "expression",
    [
        "PHASE1 ** PHASE2",                 # data-driven exponent cannot be bounded
        "PHASE1 ** 9999",                   # bounded, but over the cap
        "(PHASE1 ** 64) ** 64",             # nested exponentiation
        "PHASE1 ** 2 ** 2 ** 2",            # right-assoc chain -> too many pow nodes
        "PHASE1 * 99999999999999",          # literal magnitude cap
    ],
)
def test_exponent_and_magnitude_bombs_are_rejected(expression: str) -> None:
    with pytest.raises(UnsafeExpressionError):
        run_sandboxed(expression, MEASURED)


def test_a_bounded_exponent_is_allowed_because_cagr_needs_one() -> None:
    """The caps are tight, not arbitrary: compound growth still works."""
    data = {"y2018": 120.0, "y2026": 106.0}
    result = run_sandboxed("(y2026 / y2018) ** (1 / 8) - 1", data)
    assert result == pytest.approx((106 / 120) ** (1 / 8) - 1)


def test_division_by_zero_is_a_computation_error_not_a_crash() -> None:
    with pytest.raises(ComputationError, match="ZeroDivisionError"):
        run_sandboxed("PHASE3 / empty", {**MEASURED, "empty": 0.0})


def test_overflow_is_reported_rather_than_returned() -> None:
    with pytest.raises(ComputationError):
        run_sandboxed("big ** 64", {"big": 1e300})


# ------------------------------------------------------- legitimate formulas


def test_a_legitimate_share_expression_computes_correctly() -> None:
    """The hand-computed value, to the digit."""
    data = {"phase3": 219.0, "total": 3738.0}
    result = run_sandboxed("phase3 / total * 100", data)
    assert result == 219 / 3738 * 100
    assert result == pytest.approx(5.8587480, abs=1e-7)


def test_year_over_year_growth_matches_a_hand_computation() -> None:
    data = {"y2022": 116.0, "y2023": 115.0}
    result = run_sandboxed("(y2023 - y2022) / y2022 * 100", data)
    assert result == (115 - 116) / 116 * 100
    assert result == pytest.approx(-0.862069, abs=1e-6)


def test_allowlisted_functions_work_over_explicit_lists() -> None:
    assert run_sandboxed("max([PHASE1, PHASE2, PHASE3])", MEASURED) == 1529.0
    assert run_sandboxed("min([PHASE1, PHASE2, PHASE3])", MEASURED) == 219.0
    assert run_sandboxed("sum([PHASE1, PHASE2, PHASE3])", MEASURED) == 3088.0
    assert run_sandboxed("round(PHASE3 / total_matching * 100, 2)", MEASURED) == 5.86
    assert run_sandboxed("abs(PHASE3 - PHASE2)", MEASURED) == 1310.0
    assert run_sandboxed("len([PHASE1, PHASE2]) + PHASE3 * 0.0", MEASURED) == 2.0


def test_parentheses_around_a_callee_are_syntax_not_indirection() -> None:
    """`(min)(a, b)` is still a direct call to a bare name, so it is allowed.

    Recorded explicitly because it looks like indirection and is not — the
    parser produces the same `Call(func=Name('min'))`. Genuine indirection
    (`[min][0](...)`, a conditional callee) is rejected above.
    """
    assert run_sandboxed("(min)(PHASE1, PHASE2)", MEASURED) == 1340.0


def test_a_conditional_guards_a_zero_denominator_without_lying() -> None:
    data = {"num": 5.0, "den": 0.0}
    assert run_sandboxed("num / den if den > 0 else 0", data) == 0.0


def test_labels_that_are_not_identifiers_are_reachable_only_via_data() -> None:
    data = {"United States": 900.0, "China": 400.0, "total_matching": 2000.0}
    assert run_sandboxed("data['United States'] / total_matching * 100", data) == 45.0
    with pytest.raises(UnsafeExpressionError):
        run_sandboxed("United States / total_matching", data)


def test_a_label_may_not_shadow_a_sandbox_function() -> None:
    """A datum literally labelled `sum` stays reachable, but does not shadow."""
    data = {"sum": 10.0, "other": 5.0}
    assert run_sandboxed("data['sum'] + other", data) == 15.0
    assert run_sandboxed("sum([data['sum'], other])", data) == 15.0


def test_validate_expression_reports_exactly_the_inputs_consumed() -> None:
    consumed = validate_expression(
        "(PHASE3 + data['NA']) / total_matching", MEASURED
    )
    assert consumed == {"PHASE3", "NA", "total_matching"}


# --------------------------------------------------- binding to real responses


def test_measured_data_binds_only_counts_from_a_real_example() -> None:
    response = json.loads((EXAMPLES / "01-distribution-phases.json").read_text())
    data = measured_data(response)
    assert data == {
        "PHASE2": 1529.0, "PHASE1": 1340.0, "PHASE3": 219.0,
        "EARLY_PHASE1": 65.0, "PHASE4": 37.0, "NA": 412.0,
        "total_matching": 3738.0,
    }
    # bucket_sum is measured, but it is a SUM over these same buckets. Offering
    # it would let a formula double-count invisibly.
    assert "bucket_sum" not in data
    assert "unbucketed_count" not in data
    # nothing from the request, the citations, or the prose leaks in
    assert not any("melanoma" in key for key in data)


def test_comparison_series_are_namespaced_so_labels_cannot_collide() -> None:
    response = json.loads((EXAMPLES / "03-comparison.json").read_text())
    data = measured_data(response)
    assert "Nivolumab::PHASE3" in data
    assert "Pembrolizumab::PHASE3" in data
    assert data["Nivolumab::PHASE3"] != data["Pembrolizumab::PHASE3"]
    assert "PHASE3" not in data  # the ambiguous bare label is never bound


def test_network_node_counts_and_edge_weights_are_available() -> None:
    response = json.loads((EXAMPLES / "04-network-graph.json").read_text())
    data = measured_data(response)
    assert any(key.startswith("node::") for key in data)
    assert any(key.startswith("edge::") for key in data)
    assert all(value >= 0 for value in data.values())


def test_derive_over_a_real_example_produces_an_auditable_metric() -> None:
    response = json.loads((EXAMPLES / "01-distribution-phases.json").read_text())
    [metric] = derive(
        [MetricRequest(name="Phase 3 share of melanoma trials",
                       expression="PHASE3 / total_matching * 100",
                       unit="percent")],
        response,
    )
    assert metric.inputs == ["PHASE3", "total_matching"]
    assert metric.inputs_are_measured is True
    assert metric.result == 219 / 3738 * 100
    # replayable: the formula plus the response reproduce the number exactly
    assert run_sandboxed(metric.expression, measured_data(response)) == metric.result


def test_derive_over_a_time_series_computes_year_over_year_growth() -> None:
    response = json.loads((EXAMPLES / "02-time-series.json").read_text())
    data = measured_data(response)
    [metric] = derive(
        [MetricRequest(name="2018 to 2024 growth",
                       expression="(data['2024'] - data['2018']) / data['2018'] * 100",
                       unit="percent")],
        response,
    )
    expected = (data["2024"] - data["2018"]) / data["2018"] * 100
    assert metric.result == expected
    assert sorted(metric.inputs) == ["2018", "2024"]


def test_derive_rejects_a_metric_that_references_another_response() -> None:
    """Cross-response arithmetic is a fabrication: the counts were never co-measured."""
    response = json.loads((EXAMPLES / "01-distribution-phases.json").read_text())
    with pytest.raises(UnsafeExpressionError, match="unknown name|not a measured value"):
        derive([{"name": "cross", "expression": "PHASE3 / glioblastoma_total",
                 "unit": "ratio"}], response)


def test_derive_fails_the_whole_batch_rather_than_dropping_a_bad_metric() -> None:
    response = json.loads((EXAMPLES / "01-distribution-phases.json").read_text())
    with pytest.raises(UnsafeExpressionError):
        derive(
            [
                {"name": "good", "expression": "PHASE3 / total_matching", "unit": "ratio"},
                {"name": "bad", "expression": "__import__('os')", "unit": "ratio"},
            ],
            response,
        )


def test_derive_accepts_a_query_response_model_not_just_a_dict() -> None:
    from app.models import QueryResponse

    envelope = json.loads((EXAMPLES / "01-distribution-phases.json").read_text())
    response = QueryResponse.model_validate(envelope["response"])
    [metric] = derive(
        [{"name": "phase3 share", "expression": "PHASE3 / total_matching",
          "unit": "ratio"}],
        response,
    )
    assert metric.result == pytest.approx(219 / 3738)


def test_every_shipped_example_binds_without_error() -> None:
    """Sustained-shape check: the binder must not assume one response shape."""
    files = sorted(EXAMPLES.glob("0*.json"))
    assert len(files) == 5
    for path in files:
        data = measured_data(json.loads(path.read_text()))
        assert data, f"{path.name} bound no measured counts"
        assert all(isinstance(v, float) and math.isfinite(v) for v in data.values())


def test_derive_refuses_a_response_with_nothing_measured() -> None:
    empty = {"visualization": {"data": [], "nodes": [], "edges": []}, "meta": {}}
    with pytest.raises(DerivationError, match="no measured counts"):
        derive([{"name": "n", "expression": "x", "unit": "ratio"}], empty)


def test_derive_refuses_something_that_is_not_a_response() -> None:
    with pytest.raises(DerivationError):
        derive([{"name": "n", "expression": "x", "unit": "ratio"}], "not a response")


def test_metric_request_has_no_field_for_a_model_supplied_result() -> None:
    """Structural guarantee: the plan surface cannot carry a number.

    Same principle as QueryPlan — if the model has nowhere to put an answer, it
    cannot supply one, and no downstream code can be tempted to trust it.
    """
    assert set(MetricRequest.model_fields) == {"name", "expression", "unit"}
    assert "result" not in MetricRequest.model_fields
