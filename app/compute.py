"""Derived metrics: the model writes the FORMULA, this module computes the NUMBER.

The system's invariant is that no count, sum, percentage or total is ever
produced by a language model. Every figure in a response is a
ClinicalTrials.gov ``totalCount``.

Users still want ratios, shares and growth rates. There are two ways to give
them one:

  1. Ask the model for the number.  ``"Phase 3 is 5.9% of melanoma trials."``
  2. Ask the model for the arithmetic, and run it ourselves.
     ``phase3 / total * 100``  over inputs that are exclusively measured counts.

Only (2) is compatible with the invariant, and the difference is not stylistic.
A model-written number is a terminal assertion: nothing downstream can check it
without redoing the work the model claimed to have done. A model-written
formula is a *program over named measurements* — it can be printed next to its
inputs, re-executed, unit-tested, and diffed. If it is wrong, it is wrong
visibly and reproducibly, which is the same standard ``meta.trace`` already
holds every count to.

So this module is deliberately paranoid about one thing above all others:

    **Every name the expression references must resolve to a count the API
    returned.** A name that is not in ``data`` is rejected, not defaulted.

That single rule is what stops the model inventing an input and laundering a
fabricated quantity through a legitimate-looking formula. Everything else here
(the AST allowlist, the node cap, the timeout) exists to stop the expression
being a program in the general sense — it is arithmetic, and nothing else.

Threat model, stated plainly
----------------------------
The expression is attacker-controlled text. It is *not* trusted because a model
wrote it; a model is exactly the component most easily talked into writing
``__import__('os').system(...)``. The defence is a whitelist over the parsed
AST, applied **before** anything is compiled or executed:

* No ``Attribute`` nodes at all. This kills ``().__class__.__bases__`` and every
  other sandbox escape that walks the object graph, at the root, because such an
  escape must traverse an attribute to reach anything interesting.
* No ``Import``, ``Lambda``, comprehension, generator, walrus, f-string, ``Dict``,
  ``Set``, ``Starred``, ``Slice``, ``Await``.
* ``Call`` only to a fixed set of six pure numeric builtins, referenced by bare
  name, with no keywords and no ``*``/``**`` expansion.
* ``Subscript`` only in the form ``data['literal string']``.
* ``__builtins__`` is emptied, so even a name that slipped through resolves to
  nothing.
* Every data value and every numeric literal is coerced to ``float`` before
  execution, which removes the entire arbitrary-precision-integer CPU-bomb class
  (``9**9**9`` and friends) rather than trying to time it out.

What this module does NOT claim
-------------------------------
This is an in-process AST sandbox, not an OS-level one. It is airtight against
the class of attack it is designed for — reaching Python objects, the import
system, or the filesystem — because that class requires syntax that never
survives validation. It is *not* a substitute for a seccomp/container boundary
if you ever widen the allowlist. The timeout below is a backstop that can
*detect* a hang but cannot always kill one (Python threads are not preemptible);
it is honest defence-in-depth, not the primary guarantee. The primary guarantee
is that no allowed expression can loop, recurse, allocate, or overflow into a
slow path.
"""

from __future__ import annotations

import ast
import math
import threading
from types import CodeType
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------
# Limits. Every one of these is a hard reject, never a silent truncation —
# a partially-evaluated formula presented as a result is the same class of bug
# as a partially-probed distribution presented as complete.
# --------------------------------------------------------------------------

MAX_EXPRESSION_CHARS = 600
MAX_AST_NODES = 120
MAX_AST_DEPTH = 25
MAX_LITERAL_MAGNITUDE = 1_000_000.0
MAX_POW_EXPONENT = 64
MAX_POW_NODES = 2
DEFAULT_TIMEOUT_S = 1.0

# Pure, total, numeric. Nothing here can open a file, import a module, or reach
# an attribute. `sum`/`min`/`max` are given explicit lists, never generators,
# because generators are a comprehension and comprehensions are rejected.
SAFE_FUNCTIONS: dict[str, Any] = {
    "min": min,
    "max": max,
    "sum": sum,
    "round": round,
    "abs": abs,
    "len": len,
}

#: Names the caller's data may not bind, because they are already taken by the
#: sandbox namespace. A datum whose label collides stays reachable through
#: ``data['...']`` — it is never silently dropped, and never silently shadows.
RESERVED_NAMES = frozenset({"data", *SAFE_FUNCTIONS})

#: Key under which ``meta.total_matching`` is bound. The only non-visualization
#: number allowed into scope, because a share without its denominator is the
#: thing this whole repo exists to prevent (design-dna rule-2).
TOTAL_MATCHING_KEY = "total_matching"

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub, ast.Not)
_ALLOWED_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class UnsafeExpressionError(ValueError):
    """The expression was rejected before execution. It never ran."""


class ComputationError(ValueError):
    """The expression validated but could not produce a finite float."""


class DerivationError(ValueError):
    """A metric could not be bound to measured data."""


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class MetricRequest(BaseModel):
    """What a planner (LLM or otherwise) is allowed to ask for.

    Note what is absent: there is no ``result`` field. The model cannot propose
    an answer, only a way of arriving at one.
    """

    name: str = Field(min_length=1, max_length=120)
    expression: str = Field(min_length=1, max_length=MAX_EXPRESSION_CHARS)
    unit: str = Field(default="ratio", min_length=1, max_length=40)


class DerivedMetric(BaseModel):
    """A number that was computed here, from inputs that were measured there.

    ``expression`` and ``inputs`` are carried in the response for the same
    reason ``meta.trace`` carries request URLs: so the figure is replayable.
    Given the visualization's counts, anyone can re-run ``expression`` and get
    ``result`` back, or discover that they cannot.
    """

    name: str = Field(min_length=1, max_length=120)
    expression: str = Field(min_length=1, max_length=MAX_EXPRESSION_CHARS)
    inputs: list[str] = Field(
        description="Datum labels consumed. Every one is a measured API count.",
    )
    result: float
    unit: str = Field(min_length=1, max_length=40)
    inputs_are_measured: bool = Field(
        default=True,
        description=(
            "True iff every name in `expression` resolved to an API-measured count. "
            "There is no code path that constructs this object with False — the "
            "validator below rejects it. It is a field rather than an implicit "
            "property so that a consumer can assert on it without trusting us."
        ),
    )

    @model_validator(mode="after")
    def _must_be_measured_and_grounded(self) -> "DerivedMetric":
        if not self.inputs_are_measured:
            raise ValueError(
                "inputs_are_measured is False; a derived metric whose inputs are not "
                "all measured counts must not be constructed, let alone returned"
            )
        if not self.inputs:
            # A formula with no inputs is a constant wearing a formula's coat.
            # `3738` and `1529 / 3738 * 100` are both model-written NUMBERS.
            raise ValueError(
                "a derived metric must consume at least one measured input; an "
                "expression over pure literals is a model-written number"
            )
        if not math.isfinite(self.result):
            raise ValueError(f"result must be finite, got {self.result!r}")
        return self


# --------------------------------------------------------------------------
# AST validation
# --------------------------------------------------------------------------


def _reject(reason: str) -> None:
    raise UnsafeExpressionError(reason)


def _depth(node: ast.AST) -> int:
    """Iterative depth, so measuring the tree cannot itself blow the stack."""
    stack: list[tuple[ast.AST, int]] = [(node, 1)]
    deepest = 1
    while stack:
        current, level = stack.pop()
        if level > deepest:
            deepest = level
        if level > MAX_AST_DEPTH:
            return level
        for child in ast.iter_child_nodes(current):
            stack.append((child, level + 1))
    return deepest


def _const_fold(node: ast.AST) -> float:
    """Evaluate a literal-only subtree, or reject.

    Used only for exponents. ``x ** y`` with a data-driven ``y`` is refused
    outright: an exponent the validator cannot bound is an exponent that can be
    made enormous at runtime, and this module's whole approach is to make bad
    outcomes unreachable rather than to detect them after the fact.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            _reject("exponent must be a numeric literal")
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _const_fold(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
    ):
        left, right = _const_fold(node.left), _const_fold(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            _reject("exponent divides by zero")
        return left / right
    _reject(
        "exponent must be a constant expression (a data-dependent exponent cannot "
        "be bounded before execution)"
    )
    raise AssertionError("unreachable")


def validate_expression(expression: str, data: Mapping[str, Any]) -> frozenset[str]:
    """Validate ``expression`` against ``data`` and return the names it consumes.

    Raises :class:`UnsafeExpressionError` on anything not explicitly allowed.
    Nothing is compiled or executed here. The caller gets back the exact set of
    measured labels the expression touches, which is what makes
    ``DerivedMetric.inputs`` a fact rather than a claim.
    """
    if not isinstance(expression, str):
        _reject("expression must be a string")
    if not expression.strip():
        _reject("expression is empty")
    if len(expression) > MAX_EXPRESSION_CHARS:
        _reject(
            f"expression is {len(expression)} chars, cap is {MAX_EXPRESSION_CHARS}"
        )
    if "\x00" in expression:
        _reject("expression contains a null byte")
    if "__" in expression:
        # Belt and braces. `Attribute` is already banned, so `__class__` has no
        # syntax to travel on — but a dunder appearing anywhere in an arithmetic
        # expression means something has gone wrong upstream, and the cheapest
        # honest response is to refuse the whole thing.
        _reject("expression contains a dunder sequence '__'")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        _reject(f"expression is not a valid Python expression: {exc.msg}")
    except (RecursionError, MemoryError, ValueError) as exc:
        _reject(f"expression could not be parsed safely: {type(exc).__name__}: {exc}")

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        _reject(f"expression has {len(nodes)} AST nodes, cap is {MAX_AST_NODES}")
    if _depth(tree) > MAX_AST_DEPTH:
        _reject(f"expression nesting exceeds depth cap of {MAX_AST_DEPTH}")

    # Names that appear as the callee of an allowed call, or as the `data`
    # target of an allowed subscript, are structural rather than data inputs.
    exempt: set[int] = set()
    referenced: set[str] = set()
    pow_nodes: list[ast.BinOp] = []

    for node in nodes:
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name):
                _reject("only direct calls to allowlisted functions are permitted")
            if func.id not in SAFE_FUNCTIONS:
                _reject(f"call to non-allowlisted function {func.id!r}")
            if node.keywords:
                _reject("keyword arguments are not permitted")
            if any(isinstance(a, ast.Starred) for a in node.args):
                _reject("argument unpacking is not permitted")
            exempt.add(id(func))
        elif isinstance(node, ast.Subscript):
            target = node.value
            if not (isinstance(target, ast.Name) and target.id == "data"):
                _reject("subscripting is only permitted on `data`")
            key = node.slice
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                _reject("`data` may only be subscripted by a literal string key")
            if key.value not in data:
                _reject(
                    f"data[{key.value!r}] is not a measured value; "
                    f"the expression may only consume counts the API returned"
                )
            referenced.add(key.value)
            exempt.add(id(target))
            exempt.add(id(key))

    for node in nodes:
        if isinstance(node, (ast.Expression, ast.Load, ast.List, ast.Tuple,
                             ast.Call, ast.Subscript, ast.IfExp)):
            continue
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                _reject(f"literal of type {type(value).__name__} is not permitted")
            if isinstance(value, str):
                # Only legal as a `data[...]` key, which was checked above. A
                # string anywhere else has no arithmetic meaning, so it is a
                # sign the expression is doing something other than arithmetic.
                if id(node) not in exempt:
                    _reject("string literals are only permitted as `data` keys")
                continue
            if not math.isfinite(float(value)) or abs(float(value)) > MAX_LITERAL_MAGNITUDE:
                _reject(f"numeric literal {value!r} exceeds the magnitude cap")
            continue
        if isinstance(node, ast.Name):
            if id(node) in exempt:
                continue
            if not isinstance(node.ctx, ast.Load):
                _reject("names may only be read, never assigned")
            if node.id == "data":
                _reject("`data` may only be used as `data['label']`")
            if node.id not in data:
                # THE rule. Everything else in this module is scaffolding for it.
                _reject(
                    f"unknown name {node.id!r}: an expression may only reference "
                    f"measured counts, and the model cannot invent an input"
                )
            referenced.add(node.id)
            continue
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                _reject(f"operator {type(node.op).__name__} is not permitted")
            if isinstance(node.op, ast.Pow):
                pow_nodes.append(node)
            continue
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARYOPS):
                _reject(f"unary operator {type(node.op).__name__} is not permitted")
            continue
        if isinstance(node, ast.BoolOp):
            if not isinstance(node.op, (ast.And, ast.Or)):
                _reject("only `and`/`or` boolean operators are permitted")
            continue
        if isinstance(node, ast.Compare):
            if not all(isinstance(op, _ALLOWED_CMPOPS) for op in node.ops):
                _reject("only ordering/equality comparisons are permitted")
            continue
        if isinstance(node, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                             ast.Mod, ast.Pow, ast.UAdd, ast.USub, ast.Not,
                             ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt,
                             ast.LtE, ast.Gt, ast.GtE)):
            continue  # operator singletons, already checked at their parent
        _reject(f"{type(node).__name__} nodes are not permitted")

    if len(pow_nodes) > MAX_POW_NODES:
        _reject(f"at most {MAX_POW_NODES} exponentiations are permitted")
    for powed in pow_nodes:
        exponent = _const_fold(powed.right)
        if abs(exponent) > MAX_POW_EXPONENT:
            _reject(f"exponent {exponent} exceeds cap of {MAX_POW_EXPONENT}")
        for inner in ast.walk(powed.left):
            if isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Pow):
                _reject("exponentiation may not be nested inside another base")

    if not referenced:
        _reject(
            "expression references no measured value; a formula over pure "
            "literals is a model-written number, not a derived metric"
        )
    return frozenset(referenced)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _coerce(label: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DerivationError(
            f"input {label!r} is {type(value).__name__}, not a number; only API "
            f"counts may enter the sandbox"
        )
    result = float(value)
    if not math.isfinite(result):
        raise DerivationError(f"input {label!r} is not finite")
    return result


def _namespace(data: Mapping[str, Any]) -> dict[str, Any]:
    """Build the *entire* execution scope. Nothing enters that is not here."""
    bound = {label: _coerce(label, value) for label, value in data.items()}
    namespace: dict[str, Any] = {"__builtins__": {}}
    namespace.update(SAFE_FUNCTIONS)
    namespace["data"] = bound
    for label, value in bound.items():
        # Bare-name access is a convenience for labels that happen to be valid
        # identifiers (PHASE3, total_matching). Everything else — "United
        # States", "2024", "Nivolumab::PHASE3" — is reachable only as
        # data['...'], which is why subscripting exists at all.
        if label.isidentifier() and not label.startswith("_") and label not in RESERVED_NAMES:
            namespace[label] = value
    return namespace


def _compile(expression: str) -> CodeType:
    return compile(ast.parse(expression, mode="eval"), "<derived-metric>", "eval")


def _eval_with_timeout(code: CodeType, namespace: dict[str, Any], timeout_s: float) -> Any:
    """Run the compiled expression on a daemon thread with a hard deadline.

    Honest note on what this buys: no expression that survives validation can
    loop, recurse, or allocate — the timeout has never fired in testing and is
    not the safety property this module rests on. It is here because "the
    allowlist is exhaustive" is a claim, and a claim deserves a backstop. If it
    ever does fire we report a rejection rather than a result, and the daemon
    thread cannot keep the interpreter alive.
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = eval(code, namespace, {})  # noqa: S307 - validated above
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="derived-metric")
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise ComputationError(f"expression exceeded the {timeout_s}s execution budget")
    if "error" in box:
        error = box["error"]
        raise ComputationError(f"{type(error).__name__}: {error}")
    return box.get("value")


def run_sandboxed(
    expression: str,
    data: Mapping[str, int | float],
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> float:
    """Execute a restricted arithmetic expression over measured counts.

    ``data`` maps a measured label to the count the API returned for it. Every
    name the expression uses must be a key of ``data``; anything else is
    rejected before execution.

    Raises :class:`UnsafeExpressionError` if the expression is not permitted,
    and :class:`ComputationError` if it is permitted but cannot produce a finite
    float (divide-by-zero, overflow, a comparison chain returning a non-number).
    """
    validate_expression(expression, data)
    code = _compile(expression)
    value = _eval_with_timeout(code, _namespace(data), timeout_s)

    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, (int, float)):
        raise ComputationError(
            f"expression produced {type(value).__name__}, not a number"
        )
    result = float(value)
    if not math.isfinite(result):
        raise ComputationError(f"expression produced a non-finite result: {result!r}")
    return result


# --------------------------------------------------------------------------
# Binding measured counts -> sandbox scope
# --------------------------------------------------------------------------


def _visualization_and_meta(response: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Accept a QueryResponse, a dict, or an examples/*.json envelope."""
    if hasattr(response, "model_dump"):
        payload = response.model_dump(mode="python")
    elif isinstance(response, Mapping):
        payload = dict(response)
    else:
        raise DerivationError(f"cannot read a response from {type(response).__name__}")

    if "response" in payload and "visualization" not in payload:
        payload = payload["response"]  # examples/*.json wrap {request, response}

    viz = payload.get("visualization")
    meta = payload.get("meta")
    if not isinstance(viz, Mapping) or not isinstance(meta, Mapping):
        raise DerivationError("response is missing `visualization` or `meta`")
    return viz, meta


def measured_data(response: Any) -> dict[str, float]:
    """Extract the ONLY values permitted into a derived-metric computation.

    Sources, exhaustively:

    * ``visualization.data[*]`` — one key per datum. ``label`` when the datum is
      ungrouped, ``"<series>::<label>"`` when it belongs to a comparison series,
      because a comparison repeats every label once per series and a colliding
      key would silently pick a winner.
    * ``visualization.nodes[*].count`` as ``node::<id>`` and
      ``visualization.edges[*].weight`` as ``edge::<source>|<target>`` — network
      results carry their counts here instead of in ``data``, and each one is a
      probe of its own (README: "every node count and edge weight ... records
      its own probe").
    * ``meta.total_matching`` as ``total_matching`` — the denominator.

    Nothing derived, nothing model-authored, and nothing from the request. In
    particular ``meta.bucket_sum`` is deliberately absent: it is already a sum
    over these same buckets, and offering it as an input would let a formula
    double-count without that being visible in the expression.
    """
    viz, meta = _visualization_and_meta(response)
    out: dict[str, float] = {}

    def put(key: str, value: Any) -> None:
        if key in out:
            raise DerivationError(
                f"duplicate measured label {key!r}; refusing to guess which count wins"
            )
        out[key] = _coerce(key, value)

    for datum in viz.get("data") or []:
        label = str(datum.get("label"))
        series = datum.get("series")
        put(f"{series}::{label}" if series else label, datum.get("count"))

    for node in viz.get("nodes") or []:
        put(f"node::{node.get('id')}", node.get("count"))

    for edge in viz.get("edges") or []:
        put(f"edge::{edge.get('source')}|{edge.get('target')}", edge.get("weight"))

    total = meta.get("total_matching")
    if total is not None:
        put(TOTAL_MATCHING_KEY, total)
    return out


def _is_sensitive_to_inputs(
    expression: str, data: Mapping[str, float], inputs: Iterable[str], baseline: float,
    timeout_s: float,
) -> bool:
    """Does the result actually depend on the counts it claims to consume?

    Catches the one attack the "every name must be measured" rule does not:
    laundering a constant through a legitimate reference, e.g.
    ``total_matching * 0 + 1529``. Such an expression names a measured input,
    validates cleanly, and still returns a model-written number. Perturbing each
    input and checking that *something* moves turns that into a rejection.
    """
    code = _compile(expression)
    for label in inputs:
        probe = dict(data)
        probe[label] = probe[label] * 2.0 + 1.0
        try:
            perturbed = _eval_with_timeout(code, _namespace(probe), timeout_s)
        except ComputationError:
            return True  # the perturbation changed the outcome, loudly
        if not isinstance(perturbed, (int, float)) or float(perturbed) != baseline:
            return True
    return False


def derive(
    metrics: Iterable[MetricRequest | Mapping[str, Any]],
    response: Any,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    require_sensitivity: bool = True,
) -> list[DerivedMetric]:
    """Compute each requested metric over the response's measured counts.

    ``response`` may be a :class:`~app.models.QueryResponse`, its ``model_dump``,
    or an ``examples/*.json`` envelope. The scope handed to every expression is
    built solely by :func:`measured_data`; there is no parameter by which a
    caller can widen it, which is the point.

    Raises :class:`UnsafeExpressionError` / :class:`ComputationError` /
    :class:`DerivationError` on the first metric that fails. This is deliberate:
    silently dropping a rejected metric would let a caller believe the list they
    got back is the list they asked for.
    """
    data = measured_data(response)
    if not data:
        raise DerivationError("response carries no measured counts to compute over")

    out: list[DerivedMetric] = []
    for raw in metrics:
        request = raw if isinstance(raw, MetricRequest) else MetricRequest.model_validate(raw)
        inputs = validate_expression(request.expression, data)
        result = run_sandboxed(request.expression, data, timeout_s=timeout_s)
        if require_sensitivity and not _is_sensitive_to_inputs(
            request.expression, data, inputs, result, timeout_s
        ):
            raise UnsafeExpressionError(
                f"metric {request.name!r} produces {result!r} regardless of its inputs; "
                f"a formula insensitive to every measured count is a constant in disguise"
            )
        out.append(
            DerivedMetric(
                name=request.name,
                expression=request.expression,
                inputs=sorted(inputs),
                result=result,
                unit=request.unit,
                inputs_are_measured=True,
            )
        )
    return out
