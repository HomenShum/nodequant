"""Real-user scenarios for the market-bar snapshot boundary.

The loader is intentionally tested as an ingestion boundary, not as proof that
a caller-supplied file is genuine market data. Market conformance remains a
separate NOT_RUN gate after successful ingestion.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app import ledger as lg
from app import market_data as md
from app import promotion as pm
from app import proof_kernel as pk
from app.market_conformance import assess_market_data
from app.order_journal import build_order_journal
from app.decision_packet import (
    CheckStatus,
    EvidenceClass,
    verify,
)


RETRIEVED = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)


def _csv_from_fixture(count: int) -> str:
    rows = ["timestamp_utc,close"]
    rows.extend(
        f"{bar.ts.isoformat().replace('+00:00', 'Z')},{bar.close}"
        for bar in pk.fixture_bars(count)
    )
    return "\n".join(rows) + "\n"


def test_researcher_can_replay_a_snapshot_without_laundering_conformance(tmp_path):
    """A researcher exports enough bars to exercise the full packet gate.

    Exact bytes and replay inputs must reach every displayed fact, the kernel
    must still detect 25 signals, and market-data conformance must remain
    NOT_RUN because CSV shape validation cannot certify exchange semantics.
    """

    export = tmp_path / "provider-export.csv"
    export.write_text(_csv_from_fixture(2400), encoding="utf-8")

    snapshot = md.load_csv_snapshot(
        export,
        symbol="SPY",
        source="scenario-provider",
        source_locator="exports/SPY/2026-08-03T18:00:00Z",
        retrieved_at_utc=RETRIEVED,
    )
    bars = list(snapshot.bars)
    gate = md.run_snapshot_gate(snapshot, 25)
    signal_index = pk.detect(bars)[0]
    packet = pk.build_packet(bars, signal_index)
    panel = pk.verified_panel(
        signal_index, bars, EvidenceClass.BYTE_ADDRESSED_BAR_SNAPSHOT
    )
    result = verify(
        packet,
        pk.oracle(bars, signal_index),
        pk.mutation_evidence(bars, signal_index),
        EvidenceClass.BYTE_ADDRESSED_BAR_SNAPSHOT,
    )
    packets = list(pk.packets(25, bars))
    events = lg.build_ledger(bars, packets)
    promotion = pm.evaluate(
        kernel_gate=gate,
        events=events,
        bars=bars,
        packets=packets,
        journal=build_order_journal(bars, packets),
        snapshot=snapshot,
    )

    assert gate["status"] == "PASS"
    assert gate["packetsPassed"] == 25
    assert result.status is CheckStatus.PASS
    assert result.assurance.evidence_class is EvidenceClass.BYTE_ADDRESSED_BAR_SNAPSHOT
    assert result.assurance.market_data_conformance is CheckStatus.NOT_RUN
    assert (
        panel["data"]["verification"]["assurance"]["evidence_class"]
        == "BYTE_ADDRESSED_BAR_SNAPSHOT"
    )
    assert {fact.receipt.source_hash for fact in packet.facts} == {snapshot.source_hash}
    assert all(dict(fact.receipt.probe_inputs)["symbol"] == "SPY" for fact in packet.facts)
    assert str(export) not in packet.model_dump_json(), "local paths are not replay IDs"
    blocker_codes = {blocker.code for blocker in promotion.blockers}
    assert "CORPUS_NOT_CONFORMED" in blocker_codes
    assert "SYNTHETIC_CORPUS" not in blocker_codes


def test_corrupt_or_ambiguous_exports_fail_closed_before_reaching_the_kernel(tmp_path):
    """Degraded and adversarial provider exports never become partial bar sets."""

    cases = {
        "wrong-header": "time,price\n2026-01-05T14:30:00Z,600\n",
        "naive-time": "timestamp_utc,close\n2026-01-05T14:30:00,600\n",
        "duplicate-time": (
            "timestamp_utc,close\n"
            "2026-01-05T14:30:00Z,600\n"
            "2026-01-05T14:30:00Z,601\n"
        ),
        "non-finite": "timestamp_utc,close\n2026-01-05T14:30:00Z,nan\n",
        "non-positive": "timestamp_utc,close\n2026-01-05T14:30:00Z,0\n",
        "ragged": "timestamp_utc,close\n2026-01-05T14:30:00Z,600,unexpected\n",
        "empty": "timestamp_utc,close\n",
    }
    for name, payload in cases.items():
        export = tmp_path / f"{name}.csv"
        export.write_text(payload, encoding="utf-8")
        with pytest.raises(
            ValueError,
            match="headers|offset|increasing|finite|positive|two columns|no rows",
        ):
            md.load_csv_snapshot(
                export,
                symbol="SPY",
                source="scenario-provider",
                source_locator=f"exports/{name}",
                retrieved_at_utc=RETRIEVED,
            )


def test_snapshot_ingestion_is_bounded_and_deterministic_under_burst_and_sustained_use(
    tmp_path,
):
    """One analyst burst-loads a shared snapshot while a long export hits caps."""

    export = tmp_path / "shared.csv"
    export.write_text(_csv_from_fixture(5000), encoding="utf-8")

    def load_once():
        return md.load_csv_snapshot(
            export,
            symbol="SPY",
            source="scenario-provider",
            source_locator="exports/shared",
            retrieved_at_utc=RETRIEVED,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(lambda _: load_once(), range(16)))

    assert len({snapshot.source_hash for snapshot in snapshots}) == 1
    assert all(len(snapshot.bars) == 5000 for snapshot in snapshots)

    with pytest.raises(ValueError, match="row cap"):
        md.load_csv_snapshot(
            export,
            symbol="SPY",
            source="scenario-provider",
            source_locator="exports/shared",
            retrieved_at_utc=RETRIEVED,
            max_bars=4999,
        )
    with pytest.raises(ValueError, match="read cap"):
        md.load_csv_snapshot(
            export,
            symbol="SPY",
            source="scenario-provider",
            source_locator="exports/shared",
            retrieved_at_utc=RETRIEVED,
            max_bytes=64,
        )


def test_snapshot_assurance_rejects_legacy_receipts_and_cannot_self_promote():
    """Neither a label nor generic gate call can upgrade fixture evidence."""

    bars = pk.fixture_bars()
    index = pk.detect(bars)[0]
    result = verify(
        pk.build_packet(bars, index),
        pk.oracle(bars, index),
        pk.mutation_evidence(bars, index),
        EvidenceClass.BYTE_ADDRESSED_BAR_SNAPSHOT,
    )

    assert result.status is CheckStatus.FAIL
    receipt_check = next(c for c in result.checks if c.check_id == "fact_receipts")
    assert receipt_check.status is CheckStatus.FAIL
    assert "source hash" in receipt_check.detail

    with pytest.raises(ValueError, match="cannot self-award promotable evidence"):
        pk.run_gate(25, bars, EvidenceClass.REAL_BAR_REPLAY)


def test_caller_typed_external_label_without_byte_provenance_stays_unproven():
    """A plausible source name is metadata, not evidence of an external corpus."""

    bars = pk.fixture_bars()
    forged = [
        replace(
            bar,
            receipt=bar.receipt.model_copy(
                update={"source": "caller-typed-external"}
            ),
        )
        for bar in bars
    ]

    receipt = assess_market_data(forged)
    assert receipt.evidence_class is EvidenceClass.UNPROVEN_BAR_INPUT
    assert receipt.source_hash_count == 0
    assert receipt.replay_locator_count == 0
    assert "not byte-addressed" in receipt.limitation
    assert not receipt.acceptable


def test_fabricated_hash_and_locator_without_ingested_bytes_stay_unproven():
    bars = pk.fixture_bars()
    forged_receipt = bars[0].receipt.model_copy(
        update={
            "source": "caller-typed-external",
            "source_hash": "sha256:" + "0" * 64,
            "probe_inputs": (("source_locator", "made-up"),),
        }
    )
    forged = [replace(bar, receipt=forged_receipt) for bar in bars]

    receipt = assess_market_data(forged)
    assert receipt.evidence_class is EvidenceClass.UNPROVEN_BAR_INPUT
    assert not receipt.acceptable


def test_unrelated_raw_bytes_cannot_bind_a_fabricated_snapshot():
    bars = pk.fixture_bars()
    raw = b"timestamp_utc,close\n2026-01-05T14:30:00Z,1\n"
    digest = "sha256:" + __import__("hashlib").sha256(raw).hexdigest()
    receipt = bars[0].receipt.model_copy(
        update={
            "source": "scenario-provider",
            "source_hash": digest,
            "probe_inputs": (("source_locator", "made-up"),),
        }
    )
    forged_bars = tuple(replace(bar, receipt=receipt) for bar in bars)
    snapshot = md.BarSnapshot(
        bars=forged_bars,
        symbol="SPY",
        source="scenario-provider",
        source_locator="made-up",
        source_hash=digest,
        retrieved_at_utc=RETRIEVED,
        raw_bytes=raw,
    )
    result = assess_market_data(list(forged_bars), snapshot)
    assert result.evidence_class is EvidenceClass.UNPROVEN_BAR_INPUT
    assert not result.acceptable
