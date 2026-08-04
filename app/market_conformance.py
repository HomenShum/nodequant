"""Derive market-data assurance from bar artifacts, never caller labels."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from app.decision_packet import CheckStatus, EvidenceClass
from app.proof_kernel import Bar, bars_hash
from app.market_data import BarSnapshot, MAX_BAR_FILE_BYTES, parse_snapshot_bytes


class MarketDataConformanceReceipt(BaseModel):
    evidence_class: EvidenceClass
    status: CheckStatus
    bars_hash: str | None
    source_count: int
    source_hash_count: int
    replay_locator_count: int
    gap_mismatches: int | None = None
    halt_mismatches: int | None = None
    split_mismatches: int | None = None
    revision_mismatches: int | None = None
    late_print_mismatches: int | None = None
    provider_refetch_mismatches: int | None = None
    limitation: str

    @property
    def acceptable(self) -> bool:
        counters = (
            self.gap_mismatches,
            self.halt_mismatches,
            self.split_mismatches,
            self.revision_mismatches,
            self.late_print_mismatches,
            self.provider_refetch_mismatches,
        )
        return (
            self.evidence_class
            in {EvidenceClass.REAL_BAR_REPLAY, EvidenceClass.POINT_IN_TIME_MARKET_REPLAY}
            and self.status is CheckStatus.PASS
            and all(value == 0 for value in counters)
        )


def assess_market_data(
    bars: list[Bar] | None, snapshot: BarSnapshot | None = None
) -> MarketDataConformanceReceipt:
    """Classify only what the supplied receipts prove.

    A CSV hash and provider locator prove byte provenance, not gaps, halts,
    splits, revisions, late prints, or provider re-fetch. Those counters remain
    ``None`` until a licensed raw-event conformance runner supplies them.
    """

    if not bars:
        return MarketDataConformanceReceipt(
            evidence_class=EvidenceClass.SYNTHETIC_KERNEL,
            status=CheckStatus.NOT_RUN,
            bars_hash=None,
            source_count=0,
            source_hash_count=0,
            replay_locator_count=0,
            limitation="no bars were supplied; conformance did not run.",
        )
    receipts = [bar.receipt for bar in bars]
    sources = {receipt.source for receipt in receipts}
    source_hashes = {receipt.source_hash for receipt in receipts if receipt.source_hash}
    locators = {
        dict(receipt.probe_inputs).get("source_locator")
        for receipt in receipts
        if dict(receipt.probe_inputs).get("source_locator")
    }
    synthetic = sources == {"fixture"} and not source_hashes and not locators
    try:
        parsed_snapshot = parse_snapshot_bytes(snapshot.raw_bytes) if snapshot else []
    except ValueError:
        parsed_snapshot = []
    parsed_matches_bars = (
        len(parsed_snapshot) == len(bars)
        and all(
            index == bar.index and ts == bar.ts and close == bar.close
            for index, ((ts, close), bar) in enumerate(zip(parsed_snapshot, bars))
        )
    )
    snapshot_bound = (
        snapshot is not None
        and len(snapshot.raw_bytes) <= MAX_BAR_FILE_BYTES
        and tuple(bars) == snapshot.bars
        and parsed_matches_bars
        and snapshot.source_hash
        == "sha256:" + hashlib.sha256(snapshot.raw_bytes).hexdigest()
        and all(receipt.source == snapshot.source for receipt in receipts)
        and all(receipt.source_hash == snapshot.source_hash for receipt in receipts)
        and all(
            dict(receipt.probe_inputs).get("source_locator") == snapshot.source_locator
            for receipt in receipts
        )
    )
    byte_addressed = (
        not synthetic
        and snapshot_bound
        and all(receipt.source != "fixture" for receipt in receipts)
        and all(
            bool(receipt.source_hash)
            and bool(dict(receipt.probe_inputs).get("source_locator"))
            for receipt in receipts
        )
    )
    return MarketDataConformanceReceipt(
        evidence_class=(
            EvidenceClass.SYNTHETIC_KERNEL
            if synthetic
            else (
                EvidenceClass.BYTE_ADDRESSED_BAR_SNAPSHOT
                if byte_addressed
                else EvidenceClass.UNPROVEN_BAR_INPUT
            )
        ),
        status=CheckStatus.NOT_RUN,
        bars_hash=bars_hash(bars),
        source_count=len(sources),
        source_hash_count=len(source_hashes),
        replay_locator_count=len(locators),
        limitation=(
            "closed-form fixture; proves the verifier, not market behavior."
            if synthetic
            else (
                "byte provenance exists, but provider semantics and replay conformance are unverified."
                if byte_addressed
                else "bar origin is not byte-addressed and replayable; market provenance is unproven."
            )
        ),
    )
