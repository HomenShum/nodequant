"""Source reconstruction scenarios preserve ambiguity instead of guessing."""

from __future__ import annotations

import pytest

from app.decision_packet import CheckStatus
from app.source_compiler import Interpretation, LiteralClause, compile_source_snapshot


RAW = b"Buy on a pullback.\nExit soon or when momentum fails.\n"


def clause(clause_id, start, end, text, ambiguity="NONE", choices=("literal",), selected=None):
    return LiteralClause(
        clause_id=clause_id,
        start_byte=start,
        end_byte=end,
        literal_text=text,
        ambiguity=ambiguity,
        interpretations=tuple(
            Interpretation(interpretation_id=item, normalized_rule=f"rule:{item}") for item in choices
        ),
        selected_interpretation_id=selected,
    )


def test_reconstructor_preserves_unresolved_source_language_and_never_self_locks():
    clauses = (
        clause("entry", 0, 18, "Buy on a pullback."),
        clause("exit", 19, 52, "Exit soon or when momentum fails.",
               "UNDERSPECIFIED", ("time-exit", "momentum-exit")),
    )
    result = compile_source_snapshot(RAW, clauses)
    assert result.source_status is CheckStatus.NOT_RUN
    assert result.byte_spans_verified
    assert result.unresolved_ambiguity_count == 1
    assert not result.literal_version_ready
    assert not result.human_lock_verified
    assert not result.reconstructed_version_ready


@pytest.mark.parametrize("attack", ["changed-source", "overlap", "wrong-text", "missing-choice"])
def test_source_or_envelope_mutations_fail_closed(attack):
    first = clause("entry", 0, 18, "Buy on a pullback.")
    second = clause("exit", 19, 52, "Exit soon or when momentum fails.",
                    "CONTRADICTORY", ("a", "b"))
    raw, clauses = RAW, (first, second)
    if attack == "changed-source":
        raw = b"Sell" + RAW[4:]
    elif attack == "overlap":
        clauses = (first, second.model_copy(update={"start_byte": 10}))
    elif attack == "wrong-text":
        clauses = (first.model_copy(update={"literal_text": "Buy on strength."}), second)
    else:
        clauses = (first, second.model_copy(update={"selected_interpretation_id": "invented"}))
    with pytest.raises(ValueError):
        compile_source_snapshot(raw, clauses)


def test_absent_snapshot_is_not_zero_clauses_or_a_literal_pass():
    result = compile_source_snapshot(None, None)
    assert result.clause_count is None
    assert result.unresolved_ambiguity_count is None
    assert not result.byte_spans_verified
    assert not result.literal_version_ready


def test_caller_selection_never_substitutes_for_a_human_lock_or_readiness():
    selected = clause(
        "entry", 0, 18, "Buy on a pullback.", "UNDERSPECIFIED",
        ("one", "two"), selected="one",
    )
    result = compile_source_snapshot(RAW, (selected,))
    assert result.unresolved_ambiguity_count == 1
    assert not result.human_lock_verified
    assert not result.literal_version_ready


@pytest.mark.parametrize("choices", [("only",), ("same-a", "same-b")])
def test_ambiguity_envelope_requires_two_distinct_normalized_rules(choices):
    interpretations = tuple(
        Interpretation(interpretation_id=item, normalized_rule="same-rule") for item in choices
    )
    ambiguous = LiteralClause(
        clause_id="entry", start_byte=0, end_byte=18,
        literal_text="Buy on a pullback.", ambiguity="CONTRADICTORY",
        interpretations=interpretations,
    )
    with pytest.raises(ValueError, match="two distinct"):
        compile_source_snapshot(RAW, (ambiguous,))


def test_snapshot_root_binds_unquoted_source_bytes_as_well_as_clause_rows():
    entry = clause("entry", 0, 18, "Buy on a pullback.")
    original = compile_source_snapshot(RAW, (entry,))
    changed = compile_source_snapshot(RAW[:-2] + b"!\n", (entry,))
    assert original.clause_root_hash == changed.clause_root_hash
    assert original.source_hash != changed.source_hash
    assert original.snapshot_root_hash != changed.snapshot_root_hash
    assert not original.literal_version_ready and not changed.literal_version_ready


def test_public_receipt_cannot_be_forged_ready():
    from pydantic import ValidationError
    from app.source_compiler import SourceCompilerReceipt

    payload = compile_source_snapshot(None, None).model_dump()
    payload["literal_version_ready"] = True
    with pytest.raises(ValidationError):
        SourceCompilerReceipt.model_validate(payload)
