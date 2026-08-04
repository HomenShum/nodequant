"""Closed NodeQuant panel vocabulary and research-tier type quarantine."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, TypeAdapter, model_validator

from app.decision_packet import (
    CheckStatus, DecisionPacket, ExecutionCostPolicy, Fact, FactReceipt, Rule, Signal, Strategy,
)

Ref = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")]
Hash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class StrictPanelModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PanelName(str, Enum):
    DECISION_PACKET = "decision-packet"
    EVIDENCE_PACKET = "evidence-packet"
    OPTION_STRUCTURE = "option-structure"
    TRADE_INTENT = "trade-intent"
    RISK_GATE_VERDICT = "risk-gate-verdict"
    EXECUTION_RECEIPT = "execution-receipt"
    POSITION_STATE = "position-state"
    ADJUDICATION_VERDICT = "adjudication-verdict"
    BACKTEST_REVIEW = "backtest-review"
    RESEARCH_DIAGRAM = "research-diagram"


class PanelFactReceipt(StrictPanelModel):
    probe_id: Ref
    source: Ref
    retrieved_at_utc: datetime
    source_hash: Hash
    fact_hash: Hash
    probe_inputs: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def canonical_inputs(self):
        if self.retrieved_at_utc.tzinfo is None or self.retrieved_at_utc.utcoffset() is None:
            raise ValueError("receipt retrieval time must be timezone-aware")
        if self.probe_inputs != tuple(sorted(self.probe_inputs)):
            raise ValueError("probe inputs must be sorted")
        keys = [key for key, _ in self.probe_inputs]
        if len(keys) != len(set(keys)) or "source_locator" not in keys:
            raise ValueError("probe inputs require unique keys and source_locator")
        if any(not 1 <= len(key) <= 64 or not 1 <= len(value) <= 500 for key, value in self.probe_inputs):
            raise ValueError("probe input keys/values must be bounded and nonblank")
        return self


class PanelFact(StrictPanelModel):
    fact_id: Ref
    label: str = Field(min_length=1, max_length=200)
    value: StrictInt | StrictFloat | StrictStr
    unit: str | None = Field(default=None, max_length=40)
    known_at_utc: datetime
    receipt: PanelFactReceipt

    @model_validator(mode="after")
    def bounded_value(self):
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("numeric facts must be finite")
        if isinstance(self.value, str) and not 1 <= len(self.value) <= 1_000:
            raise ValueError("text facts must be bounded and nonblank")
        return self


class EvidencePacket(StrictPanelModel):
    packet_id: Ref
    decision_at_utc: datetime
    accepted_facts: tuple[PanelFact, ...] = Field(max_length=200)
    rejected_future_fact_ids: tuple[Ref, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def evidence_integrity(self):
        if self.decision_at_utc.tzinfo is None or self.decision_at_utc.utcoffset() is None:
            raise ValueError("decision_at_utc must be timezone-aware")
        ids = [fact.fact_id for fact in self.accepted_facts]
        if len(ids) != len(set(ids)) or len(self.rejected_future_fact_ids) != len(set(self.rejected_future_fact_ids)):
            raise ValueError("fact references must be unique")
        if set(ids) & set(self.rejected_future_fact_ids):
            raise ValueError("accepted and rejected fact references must be disjoint")
        for fact in self.accepted_facts:
            if fact.known_at_utc.tzinfo is None or fact.known_at_utc.utcoffset() is None:
                raise ValueError("fact known_at_utc must be timezone-aware")
            if fact.known_at_utc > self.decision_at_utc:
                raise ValueError("future facts belong in rejected_future_fact_ids")
            if not (fact.known_at_utc <= fact.receipt.retrieved_at_utc <= self.decision_at_utc):
                raise ValueError("fact receipt must be available between knowledge and decision time")
            fact_payload = {
                "fact_id": fact.fact_id,
                "label": fact.label,
                "value": fact.value,
                "unit": fact.unit,
                "known_at_utc": fact.known_at_utc.astimezone(timezone.utc).isoformat(),
            }
            encoded = json.dumps(fact_payload, sort_keys=True, separators=(",", ":"))
            expected = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if fact.receipt.fact_hash != expected:
                raise ValueError("fact value does not match its receipt hash")
        return self


class OptionLeg(StrictPanelModel):
    contract_id: Ref
    side: Literal["LONG", "SHORT"]
    quantity: int = Field(gt=0, le=10_000)
    quote_fact_id: Ref
    definition_fact_id: Ref


class OptionStructure(StrictPanelModel):
    candidate_id: Ref
    selector_receipt_hash: Hash
    legs: tuple[OptionLeg, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_legs(self):
        contracts = [leg.contract_id for leg in self.legs]
        quote_refs = [leg.quote_fact_id for leg in self.legs]
        definition_refs = [leg.definition_fact_id for leg in self.legs]
        if any(len(values) != len(set(values)) for values in (contracts, quote_refs, definition_refs)):
            raise ValueError("option legs and their authority references must be unique")
        return self


class TradeAction(str, Enum):
    NO_TRADE = "NO_TRADE"
    WAIT = "WAIT"
    OPEN_APPROVED_STRUCTURE = "OPEN_APPROVED_STRUCTURE"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    REVIEW = "REVIEW"


class TradeIntent(StrictPanelModel):
    intent_id: Ref
    decision_packet_hash: Hash
    action: TradeAction
    option_candidate_id: Ref | None = None
    supporting_fact_ids: tuple[Ref, ...] = Field(min_length=1, max_length=100)
    counterevidence_fact_ids: tuple[Ref, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def refs_and_action(self):
        supporting = set(self.supporting_fact_ids)
        counter = set(self.counterevidence_fact_ids)
        if len(supporting) != len(self.supporting_fact_ids) or len(counter) != len(self.counterevidence_fact_ids):
            raise ValueError("fact references must be unique")
        if supporting & counter:
            raise ValueError("a fact cannot be both support and counterevidence")
        if (self.action is TradeAction.OPEN_APPROVED_STRUCTURE) != (self.option_candidate_id is not None):
            raise ValueError("only OPEN_APPROVED_STRUCTURE carries an option candidate")
        return self


class StrictCheck(StrictPanelModel):
    check_id: Ref
    label: str = Field(min_length=1, max_length=200)
    status: CheckStatus
    detail: str = Field(default="", max_length=1_000)


class RiskGateVerdict(StrictPanelModel):
    input_hash: Hash
    status: CheckStatus
    checks: tuple[StrictCheck, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def derived_status(self):
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("risk check IDs must be unique")
        statuses = {check.status for check in self.checks}
        expected = (
            CheckStatus.FAIL if CheckStatus.FAIL in statuses
            else CheckStatus.NOT_RUN if CheckStatus.NOT_RUN in statuses
            else CheckStatus.PASS
        )
        if self.status is not expected:
            raise ValueError("risk verdict status must be derived from named checks")
        return self


class ExecutionReceiptPanel(StrictPanelModel):
    intent_id: Ref
    order_journal_root_hash: Hash
    ledger_root_hash: Hash
    broker_receipt_verified: Literal[False] = False


class PositionState(StrictPanelModel):
    through_event_seq: int = Field(ge=0)
    ledger_prefix_hash: Hash
    reduction_receipt_hash: Hash
    quantity: int
    cash_micros: int
    marked_value_micros: int


class AdjudicationVerdict(StrictPanelModel):
    position_ledger_hash: Hash
    outcome: Literal["EXPECTED", "SIGNAL_FAILURE", "SELECTION_FAILURE", "EXECUTION_FAILURE", "UNRESOLVED"]
    supporting_fact_ids: tuple[Ref, ...] = Field(min_length=1, max_length=100)
    counterexample_fact_ids: tuple[Ref, ...] = Field(min_length=1, max_length=100)


class ResearchConcept(str, Enum):
    HYPOTHESIS = "HYPOTHESIS"
    SUPPORT = "SUPPORT"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"
    UNKNOWN = "UNKNOWN"
    QUESTION = "QUESTION"


class ResearchRelation(str, Enum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    TESTS = "TESTS"
    LEADS_TO = "LEADS_TO"


class ResearchNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    node_id: str = Field(pattern=r"^n_[0-9a-f]{16}$")
    concept: ResearchConcept


class ResearchEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str = Field(pattern=r"^n_[0-9a-f]{16}$")
    target_id: str = Field(pattern=r"^n_[0-9a-f]{16}$")
    relation: ResearchRelation


class ResearchDiagram(BaseModel):
    """Quarantined graph: controlled concepts only, no facts, figures or prose."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    nodes: tuple[ResearchNode, ...] = Field(min_length=1, max_length=100)
    edges: tuple[ResearchEdge, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def graph_integrity(self):
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("research node IDs must be unique")
        known = set(ids)
        if any(edge.source_id not in known or edge.target_id not in known for edge in self.edges):
            raise ValueError("research edges must reference declared nodes")
        edge_keys = [(edge.source_id, edge.target_id, edge.relation) for edge in self.edges]
        if len(edge_keys) != len(set(edge_keys)) or any(edge.source_id == edge.target_id for edge in self.edges):
            raise ValueError("research edges must be unique and cannot self-loop")
        return self


class RiskDecisionPacket(DecisionPacket):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def recursively_forbid_extras(cls, value):
        if not isinstance(value, dict):
            return value
        def exact(payload, model, label):
            if isinstance(payload, dict):
                extras = set(payload) - set(model.model_fields)
                if extras:
                    raise ValueError(f"{label} contains forbidden fields: {sorted(extras)}")
        exact(value, DecisionPacket, "decision packet")
        exact(value.get("strategy"), Strategy, "strategy")
        if isinstance(value.get("strategy"), dict):
            exact(value["strategy"].get("execution_cost_policy"), ExecutionCostPolicy, "execution policy")
        exact(value.get("signal"), Signal, "signal")
        for row in value.get("rules", []) if isinstance(value.get("rules"), (list, tuple)) else []:
            exact(row, Rule, "rule")
        for row in value.get("facts", []) if isinstance(value.get("facts"), (list, tuple)) else []:
            exact(row, Fact, "fact")
            if isinstance(row, dict) and row.get("receipt") is not None:
                exact(row["receipt"], FactReceipt, "fact receipt")
        return value


# Decision packets are deliberately revalidated through the recursively strict
# subtype. A pre-built permissive DecisionPacket instance is not authority.
RiskGateInput = Annotated[RiskDecisionPacket | TradeIntent | OptionStructure, Field(union_mode="left_to_right")]
RISK_GATE_INPUT_ADAPTER = TypeAdapter(RiskGateInput)
PANEL_VOCABULARY = tuple(item.value for item in PanelName)
