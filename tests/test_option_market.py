"""Point-in-time option snapshot scenarios without caller promotion authority."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.decision_packet import CheckStatus, FactReceipt
from app.option_market import OptionDefinition, OptionQuote, assess_option_snapshot
from app.option_market import OptionSnapshotReceipt
from pydantic import ValidationError


NOW = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)


def receipt(name: str, proven: bool = True) -> FactReceipt:
    return FactReceipt(
        probe_id=name,
        source="provider-fixture",
        retrieved_at_utc=NOW,
        source_hash="sha256:" + "a" * 64 if proven else None,
        probe_inputs=(("source_locator", f"batch/{name}"),) if proven else (),
    )


def rows():
    definition = OptionDefinition(
        contract_id="SPY-202602-C-700", underlying="SPY",
        expiration_utc=NOW + timedelta(days=30), strike_micros=700_000_000,
        right="CALL", multiplier=100, known_at_utc=NOW - timedelta(days=1),
        receipt=receipt("definition"),
    )
    quote = OptionQuote(
        contract_id=definition.contract_id, bid_micros=1_000_000, ask_micros=1_100_000,
        bid_size=10, ask_size=12, quote_at_utc=NOW - timedelta(milliseconds=100),
        received_at_utc=NOW - timedelta(milliseconds=90), implied_vol_ppm=200_000,
        delta_ppm=500_000, receipt=receipt("quote"),
    )
    return definition, quote


def test_options_reviewer_sees_measured_snapshot_but_no_selector_self_promotion():
    definition, quote = rows()
    result = assess_option_snapshot((definition,), (quote,), decision_at_utc=NOW)

    assert result.eligible_quote_count == 1
    assert result.status is CheckStatus.NOT_RUN
    assert not result.acceptable and not result.selector_ready
    assert not result.provider_replay_verified
    assert not result.point_in_time_definitions_verified
    assert "not_option_authority" in result.evidence_scope


@pytest.mark.parametrize(
    "mutation,counter",
    [
        ({"contract_id": "MISSING"}, "missing_definition_quotes"),
        ({"quote_at_utc": NOW + timedelta(milliseconds=1)}, "future_quote_mismatches"),
        ({"received_at_utc": NOW - timedelta(seconds=1)}, "receive_order_mismatches"),
        ({"quote_at_utc": NOW - timedelta(seconds=6)}, "stale_quote_mismatches"),
        ({"bid_micros": 1_200_000}, "crossed_quote_mismatches"),
        ({"bid_size": 0}, "zero_size_mismatches"),
        ({"delta_ppm": None}, "missing_greeks_mismatches"),
        ({"receipt": receipt("unproven", False)}, "provenance_mismatches"),
    ],
)
def test_bad_chain_rows_increment_the_named_failure_counter(mutation, counter):
    definition, quote = rows()
    result = assess_option_snapshot((definition,), (quote.model_copy(update=mutation),), decision_at_utc=NOW)
    assert getattr(result, counter) == 1
    assert result.eligible_quote_count == 0


def test_absence_is_null_not_a_clean_zero_and_bounds_fail_closed():
    missing = assess_option_snapshot(None, None, decision_at_utc=NOW)
    assert missing.status is CheckStatus.NOT_RUN
    assert missing.eligible_quote_count is None
    assert missing.crossed_quote_mismatches is None
    with pytest.raises(ValueError, match="1-60000"):
        assess_option_snapshot(tuple(), tuple(), decision_at_utc=NOW, max_quote_age_ms=0)


@pytest.mark.parametrize(
    "attack",
    ["duplicate_definition", "duplicate_quote", "future_definition",
     "expired_definition", "unproven_definition", "locked_quote"],
)
def test_structurally_ambiguous_or_unproven_contracts_are_never_eligible(attack):
    definition, quote = rows()
    definitions = (definition,)
    quotes = (quote,)
    if attack == "duplicate_definition":
        definitions = (definition, definition)
    elif attack == "duplicate_quote":
        quotes = (quote, quote)
    elif attack == "future_definition":
        definitions = (definition.model_copy(update={"known_at_utc": NOW + timedelta(seconds=1)}),)
    elif attack == "expired_definition":
        definitions = (definition.model_copy(update={"expiration_utc": NOW}),)
    elif attack == "unproven_definition":
        definitions = (definition.model_copy(update={"receipt": receipt("bad-definition", False)}),)
    else:
        quotes = (quote.model_copy(update={"bid_micros": quote.ask_micros}),)

    result = assess_option_snapshot(definitions, quotes, decision_at_utc=NOW)
    assert result.eligible_quote_count == 0


def test_public_receipt_cannot_be_constructed_as_ready_or_passed():
    payload = assess_option_snapshot(None, None, decision_at_utc=NOW).model_dump()
    payload.update(
        status="PASS",
        provider_replay_verified=True,
        point_in_time_definitions_verified=True,
        selector_ready=True,
        evidence_scope="forged",
    )
    with pytest.raises(ValidationError):
        OptionSnapshotReceipt.model_validate(payload)


def test_quote_age_uses_exact_time_not_truncated_milliseconds():
    definition, quote = rows()
    exactly = quote.model_copy(update={"quote_at_utc": NOW - timedelta(milliseconds=5_000)})
    at_limit = assess_option_snapshot((definition,), (exactly,), decision_at_utc=NOW)
    assert (at_limit.eligible_quote_count, at_limit.stale_quote_mismatches) == (1, 0)

    over = quote.model_copy(
        update={"quote_at_utc": NOW - timedelta(milliseconds=5_000, microseconds=1)}
    )
    beyond = assess_option_snapshot((definition,), (over,), decision_at_utc=NOW)
    assert (beyond.eligible_quote_count, beyond.stale_quote_mismatches) == (0, 1)


def test_duplicate_counters_name_extra_rows_and_exclude_all_ambiguous_quotes():
    definition, quote = rows()
    result = assess_option_snapshot(
        (definition, definition, definition),
        (quote, quote, quote),
        decision_at_utc=NOW,
    )
    assert result.duplicate_definition_rows == 2
    assert result.duplicate_quote_rows == 2
    assert result.eligible_quote_count == 0
