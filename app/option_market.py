"""Fail-closed option-chain snapshot validation without provider laundering."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.decision_packet import CheckStatus, FactReceipt

MAX_OPTION_CONTRACTS = 20_000


class OptionRight(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class OptionDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    contract_id: str = Field(min_length=1, max_length=120)
    underlying: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,19}$")
    expiration_utc: datetime
    strike_micros: int = Field(gt=0)
    right: OptionRight
    multiplier: int = Field(gt=0, le=10_000)
    known_at_utc: datetime
    receipt: FactReceipt

    @model_validator(mode="after")
    def aware_times(self):
        if any(value.tzinfo is None or value.utcoffset() is None for value in (self.expiration_utc, self.known_at_utc)):
            raise ValueError("option definition timestamps must be timezone-aware")
        return self


class OptionQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    contract_id: str = Field(min_length=1, max_length=120)
    bid_micros: int = Field(ge=0)
    ask_micros: int = Field(ge=0)
    bid_size: int = Field(ge=0)
    ask_size: int = Field(ge=0)
    quote_at_utc: datetime
    received_at_utc: datetime
    implied_vol_ppm: int | None = Field(default=None, ge=0)
    delta_ppm: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    receipt: FactReceipt

    @model_validator(mode="after")
    def aware_times(self):
        if any(value.tzinfo is None or value.utcoffset() is None for value in (self.quote_at_utc, self.received_at_utc)):
            raise ValueError("option quote timestamps must be timezone-aware")
        return self


class OptionSnapshotReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal[CheckStatus.NOT_RUN] = CheckStatus.NOT_RUN
    snapshot_hash: str | None
    definition_count: int
    quote_count: int
    eligible_quote_count: int | None
    duplicate_definition_rows: int | None
    duplicate_quote_rows: int | None
    missing_definition_quotes: int | None
    future_definition_mismatches: int | None
    expired_definition_mismatches: int | None
    future_quote_mismatches: int | None
    receive_order_mismatches: int | None
    stale_quote_mismatches: int | None
    crossed_quote_mismatches: int | None
    zero_size_mismatches: int | None
    missing_greeks_mismatches: int | None
    provenance_mismatches: int | None
    provider_replay_verified: Literal[False] = False
    point_in_time_definitions_verified: Literal[False] = False
    selector_ready: Literal[False] = False
    evidence_scope: Literal["caller_option_snapshot_diagnostic_not_option_authority"] = (
        "caller_option_snapshot_diagnostic_not_option_authority"
    )

    @property
    def acceptable(self) -> bool:
        return False


def _hash(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_provenance(receipt: FactReceipt) -> bool:
    inputs = dict(receipt.probe_inputs)
    return bool(receipt.source_hash and inputs.get("source_locator"))


def assess_option_snapshot(
    definitions: tuple[OptionDefinition, ...] | None,
    quotes: tuple[OptionQuote, ...] | None,
    *,
    decision_at_utc: datetime,
    max_quote_age_ms: int = 5_000,
) -> OptionSnapshotReceipt:
    """Measure a supplied chain, but never turn its own labels into authority."""

    if decision_at_utc.tzinfo is None or decision_at_utc.utcoffset() is None:
        raise ValueError("decision_at_utc must be timezone-aware")
    if not 1 <= max_quote_age_ms <= 60_000:
        raise ValueError("max_quote_age_ms must be 1-60000")
    if definitions is None or quotes is None:
        return OptionSnapshotReceipt(
            status=CheckStatus.NOT_RUN,
            snapshot_hash=None,
            definition_count=0 if definitions is None else len(definitions),
            quote_count=0 if quotes is None else len(quotes),
            eligible_quote_count=None,
            duplicate_definition_rows=None,
            duplicate_quote_rows=None,
            missing_definition_quotes=None,
            future_definition_mismatches=None,
            expired_definition_mismatches=None,
            future_quote_mismatches=None,
            receive_order_mismatches=None,
            stale_quote_mismatches=None,
            crossed_quote_mismatches=None,
            zero_size_mismatches=None,
            missing_greeks_mismatches=None,
            provenance_mismatches=None,
        )
    if len(definitions) > MAX_OPTION_CONTRACTS or len(quotes) > MAX_OPTION_CONTRACTS:
        raise ValueError(f"option snapshot exceeds {MAX_OPTION_CONTRACTS} contracts")
    definition_ids = [row.contract_id for row in definitions]
    quote_ids = [row.contract_id for row in quotes]
    known = set(definition_ids)
    definition_counts = Counter(definition_ids)
    quote_counts = Counter(quote_ids)
    definition_by_id = {row.contract_id: row for row in definitions}
    max_quote_age = timedelta(milliseconds=max_quote_age_ms)
    age = lambda row: decision_at_utc - row.quote_at_utc
    counters = {
        "missing": sum(row.contract_id not in known for row in quotes),
        "future_def": sum(row.known_at_utc > decision_at_utc for row in definitions),
        "expired_def": sum(row.expiration_utc <= decision_at_utc for row in definitions),
        "future_quote": sum(row.quote_at_utc > decision_at_utc for row in quotes),
        "receive_order": sum(row.received_at_utc < row.quote_at_utc or row.received_at_utc > decision_at_utc for row in quotes),
        "stale": sum(age(row) > max_quote_age for row in quotes),
        "crossed": sum(row.bid_micros >= row.ask_micros for row in quotes),
        "zero_size": sum(row.bid_size == 0 or row.ask_size == 0 for row in quotes),
        "greeks": sum(row.implied_vol_ppm is None or row.delta_ppm is None for row in quotes),
        "provenance": sum(not _has_provenance(row.receipt) for row in (*definitions, *quotes)),
    }
    eligible = sum(
        row.contract_id in known
        and definition_counts[row.contract_id] == 1
        and quote_counts[row.contract_id] == 1
        and definition_by_id[row.contract_id].known_at_utc <= decision_at_utc
        and definition_by_id[row.contract_id].expiration_utc > decision_at_utc
        and _has_provenance(definition_by_id[row.contract_id].receipt)
        and row.quote_at_utc <= decision_at_utc
        and row.received_at_utc >= row.quote_at_utc
        and row.received_at_utc <= decision_at_utc
        and timedelta(0) <= age(row) <= max_quote_age
        and row.bid_micros < row.ask_micros
        and row.bid_size > 0 and row.ask_size > 0
        and row.implied_vol_ppm is not None and row.delta_ppm is not None
        and _has_provenance(row.receipt)
        for row in quotes
    )
    payload = {
        "decision_at_utc": decision_at_utc.astimezone(timezone.utc).isoformat(),
        "max_quote_age_ms": max_quote_age_ms,
        "definitions": [row.model_dump(mode="json") for row in definitions],
        "quotes": [row.model_dump(mode="json") for row in quotes],
    }
    return OptionSnapshotReceipt(
        status=CheckStatus.NOT_RUN,
        snapshot_hash=_hash(payload),
        definition_count=len(definitions),
        quote_count=len(quotes),
        eligible_quote_count=eligible,
        duplicate_definition_rows=len(definition_ids) - len(set(definition_ids)),
        duplicate_quote_rows=len(quote_ids) - len(set(quote_ids)),
        missing_definition_quotes=counters["missing"],
        future_definition_mismatches=counters["future_def"],
        expired_definition_mismatches=counters["expired_def"],
        future_quote_mismatches=counters["future_quote"],
        receive_order_mismatches=counters["receive_order"],
        stale_quote_mismatches=counters["stale"],
        crossed_quote_mismatches=counters["crossed"],
        zero_size_mismatches=counters["zero_size"],
        missing_greeks_mismatches=counters["greeks"],
        provenance_mismatches=counters["provenance"],
    )
