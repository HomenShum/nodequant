"""Bounded ingestion for immutable market-bar CSV snapshots.

This module proves where a supplied snapshot came from and which exact bytes
were used. It deliberately does not certify exchange completeness, adjustments,
late prints, revisions, or point-in-time correctness. Those remain the separate
market-data conformance gate.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.decision_packet import EvidenceClass, FactReceipt
from app.proof_kernel import Bar, run_gate

MAX_BAR_FILE_BYTES = 16 * 1024 * 1024
MAX_BARS = 100_000
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,19}$")
_HEADERS = ["timestamp_utc", "close"]


@dataclass(frozen=True)
class BarSnapshot:
    """One byte-addressed snapshot; tuple storage prevents post-load mutation."""

    bars: tuple[Bar, ...]
    symbol: str
    source: str
    source_locator: str
    source_hash: str
    retrieved_at_utc: datetime


def run_snapshot_gate(snapshot: BarSnapshot, limit: int = 25) -> dict:
    """Exercise the verifier without claiming market-data conformance.

    Only a dedicated conformance runner may produce ``REAL_BAR_REPLAY`` or
    ``POINT_IN_TIME_MARKET_REPLAY`` evidence. A byte-addressed CSV snapshot is
    external evidence, but its provider semantics remain unverified.
    """

    return run_gate(
        limit,
        list(snapshot.bars),
        EvidenceClass.EXTERNAL_BAR_SNAPSHOT,
    )


def load_csv_snapshot(
    path: str | Path,
    *,
    symbol: str,
    source: str,
    source_locator: str,
    retrieved_at_utc: datetime,
    max_bars: int = MAX_BARS,
    max_bytes: int = MAX_BAR_FILE_BYTES,
) -> BarSnapshot:
    """Load strict ``timestamp_utc,close`` rows and attach replay provenance.

    The locator is a stable provider-side identifier or export URI, never the
    local path. The SHA-256 binds the receipt to the exact local bytes.
    """

    symbol = symbol.strip().upper()
    source = source.strip()
    source_locator = source_locator.strip()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("symbol must be 1-20 uppercase ticker characters")
    if not 1 <= len(source) <= 200:
        raise ValueError("source must contain 1-200 characters")
    if not 1 <= len(source_locator) <= 500:
        raise ValueError("source_locator must contain 1-500 characters")
    if retrieved_at_utc.tzinfo is None or retrieved_at_utc.utcoffset() is None:
        raise ValueError("retrieved_at_utc must be timezone-aware")
    if max_bars < 1 or max_bars > MAX_BARS:
        raise ValueError(f"max_bars must be between 1 and {MAX_BARS}")
    if max_bytes < 1 or max_bytes > MAX_BAR_FILE_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_BAR_FILE_BYTES}")

    with Path(path).open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"bar snapshot exceeds the {max_bytes}-byte read cap")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("bar snapshot must be UTF-8") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != _HEADERS:
        raise ValueError(f"bar snapshot headers must be exactly {_HEADERS}")

    parsed: list[tuple[datetime, float]] = []
    previous: datetime | None = None
    for row_number, row in enumerate(reader, start=2):
        if len(parsed) >= max_bars:
            raise ValueError(f"bar snapshot exceeds the {max_bars}-row cap")
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"row {row_number}: expected exactly two columns")
        try:
            ts = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"row {row_number}: invalid timestamp_utc") from exc
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError(f"row {row_number}: timestamp_utc must include an offset")
        ts = ts.astimezone(timezone.utc)
        if previous is not None and ts <= previous:
            raise ValueError(f"row {row_number}: timestamps must be strictly increasing")
        try:
            close = float(row["close"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"row {row_number}: close must be numeric") from exc
        if not math.isfinite(close) or close <= 0:
            raise ValueError(f"row {row_number}: close must be finite and positive")
        parsed.append((ts, close))
        previous = ts

    if not parsed:
        raise ValueError("bar snapshot contains no rows")

    source_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    retrieved = retrieved_at_utc.astimezone(timezone.utc)
    if retrieved < parsed[-1][0]:
        raise ValueError("retrieved_at_utc cannot precede the newest bar")
    receipt = FactReceipt(
        probe_id=f"bars.{symbol.lower()}.csv_snapshot",
        source=source,
        retrieved_at_utc=retrieved,
        probe_inputs=(
            ("format", "timestamp_utc,close"),
            ("source_locator", source_locator),
            ("symbol", symbol),
        ),
        source_hash=source_hash,
    )
    bars = tuple(
        Bar(index=index, ts=ts, close=close, receipt=receipt)
        for index, (ts, close) in enumerate(parsed)
    )
    return BarSnapshot(
        bars=bars,
        symbol=symbol,
        source=source,
        source_locator=source_locator,
        source_hash=source_hash,
        retrieved_at_utc=retrieved,
    )
