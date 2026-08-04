#!/usr/bin/env python3
"""Independent, stdlib-only replay of the manifest-defined trading signal."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Iterable


AUTHOR_TASK = "/root/frozen_oracle"
ACCESS_STATEMENT = (
    "I accessed only the three named spec inputs: spec/manifest.json, "
    "spec/output-schema.json, and spec/bars.csv."
)
SIGNAL_RULE = "sma_fast > sma_slow AND close < sma_fast"
EDGE_RULE = (
    "emit only when signal_rule is true on this bar and false on the "
    "immediately previous bar"
)
EXECUTION_RULE = (
    "the next input bar timestamp; if absent, decision timestamp plus 60 seconds"
)


class ReplayError(ValueError):
    """Raised when an input or output violates the replay contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayError(f"expected a JSON object in {path}")
    return value


def parse_timestamp(text: str, row_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReplayError(f"row {row_number}: invalid timestamp_utc") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayError(f"row {row_number}: timestamp_utc must include an offset")
    if parsed.utcoffset() != timedelta(0):
        raise ReplayError(f"row {row_number}: timestamp_utc must be UTC")
    return parsed


def parse_bars(path: Path, expected_columns: list[str]) -> list[tuple[int, datetime, Decimal]]:
    try:
        handle = path.open("r", encoding="utf-8", newline="")
    except (OSError, UnicodeError) as exc:
        raise ReplayError(f"cannot read CSV {path}: {exc}") from exc
    bars: list[tuple[int, datetime, Decimal]] = []
    with handle:
        reader = csv.reader(handle, strict=True)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ReplayError("CSV is empty") from exc
        if header != expected_columns:
            raise ReplayError(f"CSV header must be exactly {expected_columns!r}")
        for row_number, row in enumerate(reader, start=2):
            if len(row) != 3:
                raise ReplayError(f"row {row_number}: expected 3 columns")
            try:
                index = int(row[0])
            except ValueError as exc:
                raise ReplayError(f"row {row_number}: index is not an integer") from exc
            if str(index) != row[0] or index < 0:
                raise ReplayError(f"row {row_number}: index must be a canonical nonnegative integer")
            if index != len(bars):
                raise ReplayError(f"row {row_number}: expected sequential index {len(bars)}")
            timestamp = parse_timestamp(row[1], row_number)
            if bars and timestamp <= bars[-1][1]:
                raise ReplayError(f"row {row_number}: timestamps must strictly increase")
            try:
                close = Decimal(row[2])
            except InvalidOperation as exc:
                raise ReplayError(f"row {row_number}: close is not numeric") from exc
            if not close.is_finite():
                raise ReplayError(f"row {row_number}: close must be finite")
            bars.append((index, timestamp, close))
    return bars


def validate_manifest(manifest: dict[str, Any]) -> None:
    required_types: dict[str, type] = {
        "input_file": str,
        "output_file": str,
        "output_schema": str,
        "input_columns": list,
        "fast_window": int,
        "slow_window": int,
        "mean_round_decimal_places": int,
        "minimum_signal_index": int,
        "decision_offset_seconds": int,
        "maximum_outputs": int,
    }
    for key, expected in required_types.items():
        if key not in manifest or type(manifest[key]) is not expected:
            raise ReplayError(f"manifest field {key!r} must be {expected.__name__}")
    if manifest["input_columns"] != ["index", "timestamp_utc", "close"]:
        raise ReplayError("unsupported input_columns")
    if not 0 < manifest["fast_window"] < manifest["slow_window"]:
        raise ReplayError("windows must satisfy 0 < fast_window < slow_window")
    for key in ("mean_round_decimal_places", "minimum_signal_index", "decision_offset_seconds"):
        if manifest[key] < 0:
            raise ReplayError(f"manifest field {key!r} cannot be negative")
    if manifest["maximum_outputs"] < 0:
        raise ReplayError("maximum_outputs cannot be negative")
    if manifest.get("signal_rule") != SIGNAL_RULE:
        raise ReplayError("unsupported signal_rule")
    if manifest.get("edge_rule") != EDGE_RULE:
        raise ReplayError("unsupported edge_rule")
    if manifest.get("execution_rule") != EXECUTION_RULE:
        raise ReplayError("unsupported execution_rule")


def json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    converted = float(value)
    if not math.isfinite(converted):
        raise ReplayError("numeric output is not finite")
    return converted


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def derive_signals(
    bars: list[tuple[int, datetime, Decimal]], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    fast_window = manifest["fast_window"]
    slow_window = manifest["slow_window"]
    precision = Decimal(1).scaleb(-manifest["mean_round_decimal_places"])
    maximum_outputs = manifest["maximum_outputs"]
    fast_values: deque[Decimal] = deque()
    slow_values: deque[Decimal] = deque()
    fast_sum = Decimal(0)
    slow_sum = Decimal(0)
    previous_signal = False
    signals: list[dict[str, Any]] = []

    for position, (index, timestamp, close) in enumerate(bars):
        fast_values.append(close)
        fast_sum += close
        if len(fast_values) > fast_window:
            fast_sum -= fast_values.popleft()
        slow_values.append(close)
        slow_sum += close
        if len(slow_values) > slow_window:
            slow_sum -= slow_values.popleft()

        current_signal = False
        rounded_fast: Decimal | None = None
        rounded_slow: Decimal | None = None
        if len(slow_values) == slow_window:
            rounded_fast = (fast_sum / fast_window).quantize(precision, rounding=ROUND_HALF_EVEN)
            rounded_slow = (slow_sum / slow_window).quantize(precision, rounding=ROUND_HALF_EVEN)
            uptrend = rounded_fast > rounded_slow
            pullback = close < rounded_fast
            current_signal = uptrend and pullback
            if (
                index >= manifest["minimum_signal_index"]
                and current_signal
                and not previous_signal
                and len(signals) < maximum_outputs
            ):
                decision = timestamp + timedelta(seconds=manifest["decision_offset_seconds"])
                execution = bars[position + 1][1] if position + 1 < len(bars) else decision + timedelta(seconds=60)
                signals.append(
                    {
                        "index": index,
                        "decision_at_utc": iso_utc(decision),
                        "earliest_execution_at_utc": iso_utc(execution),
                        "close": json_number(close),
                        "sma_fast": json_number(rounded_fast),
                        "sma_slow": json_number(rounded_slow),
                        "uptrend": uptrend,
                        "pullback": pullback,
                    }
                )
        previous_signal = current_signal
    return signals


def _matches_type(value: Any, name: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item),
        "boolean": lambda item: isinstance(item, bool),
    }
    return name in checks and checks[name](value)


def validate_schema(value: Any, schema: dict[str, Any], location: str = "$") -> None:
    expected_type = schema.get("type")
    if expected_type and not _matches_type(value, expected_type):
        raise ReplayError(f"output schema violation at {location}: expected {expected_type}")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ReplayError(f"output schema violation at {location}: missing {missing!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ReplayError(f"output schema violation at {location}: extra {extras!r}")
        for key, child in value.items():
            if key in properties:
                validate_schema(child, properties[key], f"{location}.{key}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ReplayError(f"output schema violation at {location}: too many items")
        if "items" in schema:
            for position, child in enumerate(value):
                validate_schema(child, schema["items"], f"{location}[{position}]")
    if isinstance(value, str) and "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
        raise ReplayError(f"output schema violation at {location}: pattern mismatch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ReplayError(f"output schema violation at {location}: below minimum")


def build_output(spec_dir: Path) -> tuple[dict[str, Any], Path, Path, Path]:
    manifest_path = spec_dir / "manifest.json"
    schema_path = spec_dir / "output-schema.json"
    manifest = load_json(manifest_path)
    validate_manifest(manifest)
    if Path(manifest["input_file"]).name != manifest["input_file"]:
        raise ReplayError("input_file must be a filename")
    if Path(manifest["output_schema"]).name != manifest["output_schema"]:
        raise ReplayError("output_schema must be a filename")
    bars_path = spec_dir / manifest["input_file"]
    declared_schema_path = spec_dir / manifest["output_schema"]
    if declared_schema_path.resolve() != schema_path.resolve():
        raise ReplayError("manifest must name output-schema.json")
    schema = load_json(schema_path)
    bars = parse_bars(bars_path, manifest["input_columns"])
    output = {
        "manifest_sha256": f"sha256:{sha256_file(manifest_path)}",
        "bars_sha256": f"sha256:{sha256_file(bars_path)}",
        "signals": derive_signals(bars, manifest),
    }
    validate_schema(output, schema)
    return output, manifest_path, schema_path, bars_path


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def replay(spec_dir: Path, implementation_dir: Path) -> tuple[Path, Path]:
    output, manifest_path, schema_path, bars_path = build_output(spec_dir)
    implementation_path = Path(__file__).resolve()
    output_path = implementation_dir / "oracle-output.json"
    authorship_path = implementation_dir / "authorship.json"
    write_json(output_path, output)
    authorship = {
        "canonical_task_name": AUTHOR_TASK,
        "input_hashes": {
            "spec/manifest.json": f"sha256:{sha256_file(manifest_path)}",
            "spec/output-schema.json": f"sha256:{sha256_file(schema_path)}",
            "spec/bars.csv": f"sha256:{sha256_file(bars_path)}",
        },
        "implementation_hash": f"sha256:{sha256_file(implementation_path)}",
        "output_hash": f"sha256:{sha256_file(output_path)}",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "access_statement": ACCESS_STATEMENT,
    }
    write_json(authorship_path, authorship)
    return output_path, authorship_path


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    script_dir = Path(__file__).resolve().parent
    parser.add_argument("--spec-dir", type=Path, default=script_dir.parent / "spec")
    parser.add_argument("--implementation-dir", type=Path, default=script_dir)
    args = parser.parse_args(argv)
    try:
        replay(args.spec_dir.resolve(), args.implementation_dir.resolve())
    except ReplayError as exc:
        parser.exit(2, f"replay error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
