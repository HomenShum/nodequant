"""Real-user scenarios for the market-bar snapshot boundary.

The loader is intentionally tested as an ingestion boundary, not as proof that
a caller-supplied file is genuine market data. Market conformance remains a
separate NOT_RUN gate after successful ingestion.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app import ledger as lg
from app import market_data as md
from app import promotion as pm
from app import proof_kernel as pk
from app.decision_packet import (
    CheckStatus,
    EvidenceClass,
    OracleIndependence,
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
        signal_index, bars, EvidenceClass.EXTERNAL_BAR_SNAPSHOT
    )
    result = verify(
        packet,
        pk.oracle(bars, signal_index),
        pk.mutation_evidence(bars, signal_index),
        EvidenceClass.EXTERNAL_BAR_SNAPSHOT,
    )
    packets = list(pk.packets(25, bars))
    promotion = pm.evaluate(
        kernel_gate=gate,
        receipt=lg.curve_receipt(lg.build_ledger(bars, packets), bars, packets),
        evidence_class=EvidenceClass.EXTERNAL_BAR_SNAPSHOT,
        oracle_independence=OracleIndependence.SAME_AUTHOR_DIVERSE_IMPLEMENTATION,
        market_data_conformance=CheckStatus.NOT_RUN,
    )

    assert gate["status"] == "PASS"
    assert gate["packetsPassed"] == 25
    assert result.status is CheckStatus.PASS
    assert result.assurance.evidence_class is EvidenceClass.EXTERNAL_BAR_SNAPSHOT
    assert result.assurance.market_data_conformance is CheckStatus.NOT_RUN
    assert (
        panel["data"]["verification"]["assurance"]["evidence_class"]
        == "EXTERNAL_BAR_SNAPSHOT"
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
        EvidenceClass.EXTERNAL_BAR_SNAPSHOT,
    )

    assert result.status is CheckStatus.FAIL
    receipt_check = next(c for c in result.checks if c.check_id == "fact_receipts")
    assert receipt_check.status is CheckStatus.FAIL
    assert "source hash" in receipt_check.detail

    with pytest.raises(ValueError, match="cannot self-award promotable evidence"):
        pk.run_gate(25, bars, EvidenceClass.REAL_BAR_REPLAY)
