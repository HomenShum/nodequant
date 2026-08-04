"""Closed-vocabulary and generated-tier authority attacks."""

from __future__ import annotations

import pytest
import re
import hashlib
import json
from pathlib import Path
from pydantic import ValidationError

from app.authority_panels import (
    PANEL_VOCABULARY, RISK_GATE_INPUT_ADAPTER, PanelName,
    EvidencePacket, OptionStructure, ResearchDiagram, RiskGateVerdict, TradeIntent,
)


def test_panel_vocabulary_is_exactly_the_thread_decision():
    assert PANEL_VOCABULARY == (
        "decision-packet", "evidence-packet", "option-structure", "trade-intent",
        "risk-gate-verdict", "execution-receipt", "position-state",
        "adjudication-verdict", "backtest-review", "research-diagram",
    )
    assert len(PanelName) == 10


def test_frontend_registers_each_closed_panel_exactly_once():
    source = (Path(__file__).parents[1] / "web" / "components" / "nodequant-panels.tsx").read_text(encoding="utf-8")
    registered = re.findall(r'name:\s*"([a-z-]+)"', source)
    assert len(registered) == len(PANEL_VOCABULARY)
    assert set(registered) == set(PANEL_VOCABULARY)
    assistant = (Path(__file__).parents[1] / "web" / "app" / "assistant.tsx").read_text(encoding="utf-8")
    assert "<NodeQuantPanels />" in assistant
    assert "TrialScopePanels" not in assistant
    assert '"/api/nodequant-chat"' in assistant


@pytest.mark.parametrize(
    "authoritative",
    [
        "SPY was 0.8% above VWAP", "Sharpe improved from 0.7 to 1.4",
        "BUY 10 contracts", "Risk approved", "Expected loss: $230",
        "Source published tomorrow confirms the thesis",
    ],
)
def test_research_diagram_has_no_slot_for_authoritative_content(authoritative):
    with pytest.raises(ValidationError):
        ResearchDiagram.model_validate(
            {"nodes": [{"node_id": "n", "concept": authoritative}], "edges": []}
        )
    with pytest.raises(ValidationError):
        ResearchDiagram.model_validate(
            {"nodes": [{"node_id": "n", "concept": "QUESTION", "label": authoritative}], "edges": []}
        )


def test_research_diagram_is_not_a_risk_gate_input():
    diagram = ResearchDiagram.model_validate(
        {"nodes": [{"node_id": "n_0000000000000001", "concept": "QUESTION"}], "edges": []}
    )
    with pytest.raises(ValidationError):
        RISK_GATE_INPUT_ADAPTER.validate_python(diagram)


def test_trade_intent_requires_disjoint_fact_refs_and_candidate_only_for_open():
    base = {
        "intent_id": "i", "decision_packet_hash": "sha256:" + "a" * 64,
        "action": "WAIT", "supporting_fact_ids": ("support",),
        "counterevidence_fact_ids": ("support",),
    }
    with pytest.raises(ValidationError, match="both support"):
        TradeIntent.model_validate(base)
    with pytest.raises(ValidationError, match="only OPEN"):
        TradeIntent.model_validate({**base, "counterevidence_fact_ids": ("counter",), "option_candidate_id": "c"})


def test_hybrid_research_payload_cannot_launder_into_risk_input():
    payload = {
        "intent_id": "i", "decision_packet_hash": "sha256:" + "a" * 64,
        "action": "WAIT", "supporting_fact_ids": ("support",),
        "counterevidence_fact_ids": ("counter",),
        "nodes": [{"node_id": "n_0000000000000001", "concept": "QUESTION"}],
        "edges": [], "loss_micros": 230_000_000,
    }
    with pytest.raises(ValidationError):
        RISK_GATE_INPUT_ADAPTER.validate_python(payload)


def test_numeric_evidence_requires_aware_boundary_and_strict_receipt():
    fact_payload = {
        "fact_id": "price", "label": "price", "value": 123.45, "unit": "USD",
        "known_at_utc": "2026-01-01T11:59:00+00:00",
    }
    fact_hash = "sha256:" + hashlib.sha256(
        json.dumps(fact_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    base = {
        "packet_id": "p", "decision_at_utc": "2026-01-01T12:00:00Z",
        "accepted_facts": [{
            "fact_id": "price", "label": "price", "value": 123.45, "unit": "USD",
            "known_at_utc": "2026-01-01T11:59:00Z",
            "receipt": {"probe_id": "probe", "source": "provider",
                        "retrieved_at_utc": "2026-01-01T12:00:00Z",
                        "source_hash": "sha256:" + "b" * 64,
                        "fact_hash": fact_hash,
                        "probe_inputs": (("source_locator", "batch/1"),)},
        }], "rejected_future_fact_ids": (),
    }
    assert EvidencePacket.model_validate(base).accepted_facts[0].receipt.source_hash
    with pytest.raises(ValidationError):
        EvidencePacket.model_validate({**base, "decision_at_utc": "not-a-time"})
    unreceipted = {**base, "accepted_facts": [{**base["accepted_facts"][0], "receipt": None}]}
    with pytest.raises(ValidationError):
        EvidencePacket.model_validate(unreceipted)
    changed = {**base, "accepted_facts": [{**base["accepted_facts"][0], "value": 999.0}]}
    with pytest.raises(ValidationError, match="receipt hash"):
        EvidencePacket.model_validate(changed)
    overlap = {**base, "rejected_future_fact_ids": ("price",)}
    with pytest.raises(ValidationError, match="disjoint"):
        EvidencePacket.model_validate(overlap)
    equivalent = {**base, "accepted_facts": [{
        **base["accepted_facts"][0], "known_at_utc": "2026-01-01T06:59:00-05:00"
    }]}
    assert EvidencePacket.model_validate(equivalent).accepted_facts[0].known_at_utc.utcoffset()
    boolean = {**base, "accepted_facts": [{**base["accepted_facts"][0], "value": True}]}
    with pytest.raises(ValidationError):
        EvidencePacket.model_validate(boolean)


def test_duplicate_legs_edges_and_extra_authority_fields_reject():
    leg = {"contract_id": "c", "side": "LONG", "quantity": 1,
           "quote_fact_id": "q", "definition_fact_id": "d"}
    with pytest.raises(ValidationError, match="unique"):
        OptionStructure.model_validate({"candidate_id": "candidate", "selector_receipt_hash": "sha256:" + "c" * 64, "legs": [leg, leg]})
    node = {"node_id": "n_0000000000000001", "concept": "QUESTION"}
    edge = {"source_id": "n_0000000000000001", "target_id": "n_0000000000000001", "relation": "TESTS"}
    with pytest.raises(ValidationError, match="self-loop"):
        ResearchDiagram.model_validate({"nodes": [node], "edges": [edge]})
    with pytest.raises(ValidationError):
        RiskGateVerdict.model_validate({
            "input_hash": "sha256:" + "d" * 64, "status": "PASS",
            "checks": [{"check_id": "c", "label": "check", "status": "PASS", "BUY": 10}],
        })


def test_risk_status_is_derived_and_decision_children_are_recursively_strict():
    check = {"check_id": "c", "label": "check", "status": "FAIL"}
    with pytest.raises(ValidationError, match="derived"):
        RiskGateVerdict.model_validate({
            "input_hash": "sha256:" + "d" * 64, "status": "PASS", "checks": [check]
        })

    from app import proof_kernel as pk
    packet = next(pk.packets(1, pk.fixture_bars())).model_dump(mode="json")
    packet["strategy"]["BUY_10"] = 230
    with pytest.raises(ValidationError, match="strategy contains forbidden"):
        RISK_GATE_INPUT_ADAPTER.validate_python(packet)
