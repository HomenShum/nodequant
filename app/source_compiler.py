"""Byte-bound legacy strategy clauses and explicit ambiguity envelopes."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.decision_packet import CheckStatus

MAX_SOURCE_BYTES = 1_048_576
MAX_SOURCE_CLAUSES = 500


class AmbiguityKind(str, Enum):
    NONE = "NONE"
    UNDERSPECIFIED = "UNDERSPECIFIED"
    CONTRADICTORY = "CONTRADICTORY"


class Interpretation(BaseModel):
    model_config = ConfigDict(frozen=True)

    interpretation_id: str = Field(min_length=1, max_length=100)
    normalized_rule: str = Field(min_length=1, max_length=1_000)


class LiteralClause(BaseModel):
    model_config = ConfigDict(frozen=True)

    clause_id: str = Field(min_length=1, max_length=100)
    start_byte: int = Field(ge=0, le=MAX_SOURCE_BYTES)
    end_byte: int = Field(gt=0, le=MAX_SOURCE_BYTES)
    literal_text: str = Field(min_length=1, max_length=10_000)
    ambiguity: AmbiguityKind
    interpretations: tuple[Interpretation, ...] = Field(min_length=1, max_length=20)
    selected_interpretation_id: str | None = None


class SourceCompilerReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_status: Literal[CheckStatus.NOT_RUN] = CheckStatus.NOT_RUN
    source_hash: str | None
    clause_root_hash: str | None
    snapshot_root_hash: str | None
    clause_count: int | None
    unresolved_ambiguity_count: int | None
    contradictory_clause_count: int | None
    byte_spans_verified: bool
    human_lock_verified: Literal[False] = False
    literal_version_ready: Literal[False] = False
    reconstructed_version_ready: Literal[False] = False
    repaired_version_ready: Literal[False] = False
    evidence_scope: Literal["source_structure_diagnostic_human_lock_required"] = (
        "source_structure_diagnostic_human_lock_required"
    )


def _hash(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compile_source_snapshot(
    raw: bytes | None,
    clauses: tuple[LiteralClause, ...] | None,
) -> SourceCompilerReceipt:
    """Verify literal spans; never infer or self-certify a human interpretation."""

    if raw is None or clauses is None:
        return SourceCompilerReceipt(
            source_hash=None,
            clause_root_hash=None,
            snapshot_root_hash=None,
            clause_count=None,
            unresolved_ambiguity_count=None,
            contradictory_clause_count=None,
            byte_spans_verified=False,
            literal_version_ready=False,
        )
    if not 1 <= len(raw) <= MAX_SOURCE_BYTES:
        raise ValueError(f"source snapshot must contain 1-{MAX_SOURCE_BYTES} bytes")
    if not 1 <= len(clauses) <= MAX_SOURCE_CLAUSES:
        raise ValueError(f"source snapshot must contain 1-{MAX_SOURCE_CLAUSES} clauses")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source snapshot must be UTF-8") from exc
    if len({clause.clause_id for clause in clauses}) != len(clauses):
        raise ValueError("clause IDs must be unique")
    previous_end = -1
    for clause in clauses:
        if clause.start_byte < previous_end or clause.end_byte <= clause.start_byte:
            raise ValueError("clause byte spans must be ordered, positive and non-overlapping")
        if clause.end_byte > len(raw):
            raise ValueError("clause byte span exceeds source snapshot")
        try:
            observed = raw[clause.start_byte:clause.end_byte].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("clause span splits a UTF-8 code point") from exc
        if observed != clause.literal_text:
            raise ValueError("literal clause text does not match its byte span")
        ids = [row.interpretation_id for row in clause.interpretations]
        if len(ids) != len(set(ids)):
            raise ValueError("interpretation IDs must be unique within a clause")
        if clause.selected_interpretation_id is not None and clause.selected_interpretation_id not in ids:
            raise ValueError("selected interpretation is not present in its ambiguity envelope")
        if clause.ambiguity is AmbiguityKind.NONE and len(ids) != 1:
            raise ValueError("unambiguous clauses must have exactly one normalized interpretation")
        normalized = [row.normalized_rule for row in clause.interpretations]
        if clause.ambiguity is not AmbiguityKind.NONE:
            if len(normalized) < 2 or len(normalized) != len(set(normalized)):
                raise ValueError("ambiguous clauses require at least two distinct normalized rules")
        previous_end = clause.end_byte
    unresolved = sum(clause.ambiguity is not AmbiguityKind.NONE for clause in clauses)
    source_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    clause_payload = [clause.model_dump(mode="json") for clause in clauses]
    clause_root_hash = _hash(clause_payload)
    return SourceCompilerReceipt(
        source_hash=source_hash,
        clause_root_hash=clause_root_hash,
        snapshot_root_hash=_hash(
            {"source_hash": source_hash, "clauses": clause_payload}
        ),
        clause_count=len(clauses),
        unresolved_ambiguity_count=unresolved,
        contradictory_clause_count=sum(
            clause.ambiguity is AmbiguityKind.CONTRADICTORY for clause in clauses
        ),
        byte_spans_verified=True,
        literal_version_ready=False,
    )
