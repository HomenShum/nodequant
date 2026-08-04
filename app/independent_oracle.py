"""Verify the no-source-access oracle bundle against current kernel artifacts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.decision_packet import DecisionPacket, OracleIndependence
from app.proof_kernel import Bar

MAX_ORACLE_FILE_BYTES = 2 * 1024 * 1024
EXPECTED_TASK = "/root/frozen_oracle"
EXPECTED_ACCESS = (
    "I accessed only the three named spec inputs: spec/manifest.json, "
    "spec/output-schema.json, and spec/bars.csv."
)
FROZEN_HASHES = {
    "manifest": "sha256:14438e51f6a3774f21a7c07925c4bc71162bc88a87212daf28989aa5c5d8b38a",
    "schema": "sha256:530597ac59d5325ca3a16bab6fbcabad43c5456ec5156e8418cbfb8fd231e630",
    "bars": "sha256:7ecd94bbeb1505b556bc58a6a37ff9871c82258c1d8daf3c31a6372f261bb9ac",
    "implementation": "sha256:28ed1fa98f26fdfaca9bfa32141467d7c6a45fdbaed707f9916fe56d6a1052bd",
    "output": "sha256:98efaddc2218f3b2e01c636ef3d16e35b4a82dea3c00b1d19125c0c99ecb7ff3",
    "authorship": "sha256:272cd819c0a1140132cd56406c36b415a84585b889f0f8ebc245c6c4954be279",
}


class FrozenOracleReceipt(BaseModel):
    independence: OracleIndependence
    claimed_independence: OracleIndependence
    provenance_verified: bool
    author_task: str
    manifest_hash: str
    schema_hash: str
    bars_file_hash: str
    implementation_hash: str
    output_hash: str
    signal_count: int
    signal_mismatches: int
    input_hash_mismatches: int
    authorship_mismatches: int
    forbidden_imports: int
    matched: bool

    @property
    def acceptable(self) -> bool:
        return (
            self.independence is OracleIndependence.SEPARATE_AGENT_FROM_FROZEN_SPEC
            and self.provenance_verified
            and self.signal_count > 0
            and self.signal_mismatches == 0
            and self.input_hash_mismatches == 0
            and self.authorship_mismatches == 0
            and self.forbidden_imports == 0
            and self.matched
        )


def _read(path: Path) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(MAX_ORACLE_FILE_BYTES + 1)
    if len(raw) > MAX_ORACLE_FILE_BYTES:
        raise ValueError(f"oracle artifact exceeds {MAX_ORACLE_FILE_BYTES} bytes: {path.name}")
    return raw


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid oracle {label} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"oracle {label} must be an object")
    return value


def _bars_csv_hash(bars: list[Bar]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(("index", "timestamp_utc", "close"))
    for bar in bars:
        writer.writerow((bar.index, bar.ts.isoformat(), str(bar.close)))
    return _digest(stream.getvalue().encode("utf-8"))


def _primary_signal(packet: DecisionPacket) -> dict[str, Any]:
    facts = {fact.fact_id: fact.value for fact in packet.facts}
    rules = {rule.rule_id: rule.result for rule in packet.rules}
    try:
        index = int(packet.packet_id.rsplit("-", 1)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"packet id does not end in a bar index: {packet.packet_id}") from exc
    return {
        "index": index,
        "decision_at_utc": packet.signal.decision_at_utc.isoformat(),
        "earliest_execution_at_utc": packet.signal.earliest_execution_at_utc.isoformat(),
        "close": facts.get("close"),
        "sma_fast": facts.get("sma_fast"),
        "sma_slow": facts.get("sma_slow"),
        "uptrend": rules.get("uptrend"),
        "pullback": rules.get("pullback"),
    }


def verify_frozen_oracle(
    bars: list[Bar],
    packets: list[DecisionPacket],
    root: Path | None = None,
) -> FrozenOracleReceipt:
    root = root or Path(__file__).parents[1] / "independent_oracle"
    spec = root / "spec"
    implementation = root / "implementation"
    paths = {
        "manifest": spec / "manifest.json",
        "schema": spec / "output-schema.json",
        "bars": spec / "bars.csv",
        "implementation": implementation / "replay.py",
        "output": implementation / "oracle-output.json",
        "authorship": implementation / "authorship.json",
    }
    raw = {name: _read(path) for name, path in paths.items()}
    hashes = {name: _digest(value) for name, value in raw.items()}
    output = _json(raw["output"], "output")
    authorship = _json(raw["authorship"], "authorship")

    input_hashes = authorship.get("input_hashes")
    expected_inputs = {
        "spec/manifest.json": hashes["manifest"],
        "spec/output-schema.json": hashes["schema"],
        "spec/bars.csv": hashes["bars"],
    }
    input_bad = sum(hashes[name] != FROZEN_HASHES[name] for name in FROZEN_HASHES)
    input_bad += _bars_csv_hash(bars) != hashes["bars"]
    input_bad += output.get("manifest_sha256") != hashes["manifest"]
    input_bad += output.get("bars_sha256") != hashes["bars"]
    input_bad += input_hashes != expected_inputs
    input_bad += authorship.get("implementation_hash") != hashes["implementation"]
    input_bad += authorship.get("output_hash") != hashes["output"]

    author_bad = int(authorship.get("canonical_task_name") != EXPECTED_TASK)
    author_bad += int(authorship.get("access_statement") != EXPECTED_ACCESS)
    source = raw["implementation"].decode("utf-8", errors="replace")
    forbidden = sum(token in source for token in ("from app", "import app", "proof_kernel"))

    oracle_signals = output.get("signals")
    if not isinstance(oracle_signals, list):
        raise ValueError("oracle output signals must be an array")
    primary = [_primary_signal(packet) for packet in packets]
    signal_bad = abs(len(primary) - len(oracle_signals))
    signal_bad += sum(left != right for left, right in zip(primary, oracle_signals))
    matched = input_bad == author_bad == forbidden == signal_bad == 0
    return FrozenOracleReceipt(
        # The separate task and its output are real, but authorship/access are
        # self-attested. Without an immutable orchestrator-issued execution
        # record, the strongest proven rung remains implementation diversity.
        independence=OracleIndependence.SAME_AUTHOR_DIVERSE_IMPLEMENTATION,
        claimed_independence=OracleIndependence.SEPARATE_AGENT_FROM_FROZEN_SPEC,
        provenance_verified=False,
        author_task=str(authorship.get("canonical_task_name", "")),
        manifest_hash=hashes["manifest"],
        schema_hash=hashes["schema"],
        bars_file_hash=hashes["bars"],
        implementation_hash=hashes["implementation"],
        output_hash=hashes["output"],
        signal_count=len(oracle_signals),
        signal_mismatches=signal_bad,
        input_hash_mismatches=input_bad,
        authorship_mismatches=author_bad,
        forbidden_imports=forbidden,
        matched=matched,
    )
