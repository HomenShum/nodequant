import inspect
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from app.insider_cluster import (
    _bounded_get,
    _evaluator_dependencies,
    _evaluator_hash,
    _zip_tsv,
    compile_manifest,
    evaluate,
    fetch_prices,
)


def _archive(submissions, owners, transactions):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SUBMISSION.tsv", submissions)
        archive.writestr("REPORTINGOWNER.tsv", owners)
        archive.writestr("NONDERIV_TRANS.tsv", transactions)
    return buffer.getvalue()


def _policy():
    return {
        "sec_provider": {"maximum_uncompressed_member_bytes": 100000},
        "signal": {"allowed_symbol_regex": "[A-Z]{1,5}", "allowed_security_title_terms": ["common stock"], "cluster_calendar_days": 30, "minimum_distinct_owners": 2, "minimum_combined_purchase_usd": 250000, "issuer_cooldown_calendar_days": 90, "maximum_events": 10},
        "portfolio": {"benchmark_symbol": "SPY", "holding_sessions": 3},
        "price_data": {"maximum_rows_per_symbol": 100},
        "costs": {"one_way_bps_sensitivity": [10], "primary_one_way_bps": 10},
        "folds": [{"id": "2024", "start": "2024-01-01", "end": "2024-12-31"}],
        "bootstrap": {"seed": 7, "samples": 200, "block_days": 2},
        "gate": {"minimum_events": 1, "minimum_unique_issuers": 1, "minimum_event_coverage": 1.0, "minimum_positive_folds": 1, "remove_best_days": 1},
        "promotion_ceiling": "TEST_ONLY",
    }


def test_research_director_gets_one_earliest_nonoverlapping_cluster_from_as_filed_rows():
    submissions = "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERTRADINGSYMBOL\tISSUERNAME\nA1\t02-JAN-2024\t4\t123\tACME\tAcme\nA2\t20-JAN-2024\t4\t123\tACME\tAcme\nA3\t25-JAN-2024\t4\t123\tACME\tAcme\n"
    owners = "ACCESSION_NUMBER\tRPTOWNERCIK\nA1\t11\nA2\t22\nA3\t33\n"
    transactions = "ACCESSION_NUMBER\tSECURITY_TITLE\tTRANS_CODE\tTRANS_ACQUIRED_DISP_CD\tTRANS_SHARES\tTRANS_PRICEPERSHARE\nA1\tCommon Stock\tP\tA\t1000\t100\nA2\tCommon Stock\tP\tA\t2000\t100\nA3\tCommon Stock\tP\tA\t5000\t100\n"
    manifest = compile_manifest(_policy(), {"2024q1": _archive(submissions, owners, transactions)}, "sha256:policy")
    assert len(manifest["events"]) == 1
    event = manifest["events"][0]
    assert event["signal_date"] == "2024-01-20"
    assert event["distinct_owner_count"] == 2
    assert event["combined_purchase_usd"] == 300000
    assert event["owner_ciks"] == ["0000000011", "0000000022"]


def test_parser_rejects_oversized_members_and_fetch_guard_rejects_ssrf():
    raw = _archive("x\n" + "a" * 100, "x\n", "x\n")
    with pytest.raises(ValueError, match="member bounds"):
        _zip_tsv(raw, "SUBMISSION.tsv", 10)
    with pytest.raises(ValueError, match="outside"):
        _bounded_get("http://127.0.0.1/secret", {"host": "www.sec.gov", "user_agent": "test", "timeout_seconds": 1, "maximum_response_bytes": 100})


def _price_raw(start, changes):
    days = [start + timedelta(days=index) for index in range(len(changes))]
    prices = [100.0]
    for change in changes[1:]:
        prices.append(prices[-1] * (1 + change))
    return json.dumps({"chart": {"result": [{"timestamp": [int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()) for day in days], "indicators": {"adjclose": [{"adjclose": prices}]}}]}}).encode()


def test_calendar_time_sleeves_do_not_count_overlapping_horizons_as_independent_trials():
    start = datetime(2024, 1, 1).date()
    policy = _policy()
    policy["portfolio"]["holding_sessions"] = 21
    manifest = {"manifest_hash": "sha256:manifest", "events": [
        {"event_id": "one", "issuer_cik": "1", "symbol": "AAA", "signal_date": "2024-01-01"},
        {"event_id": "two", "issuer_cik": "2", "symbol": "BBB", "signal_date": "2024-01-02"},
    ]}
    raw = {"AAA": _price_raw(start, [0] + [.002] * 29), "BBB": _price_raw(start, [0] + [.001] * 29), "SPY": _price_raw(start, [0] * 30)}
    result = evaluate(policy, manifest, raw, {}, "sha256:policy")
    assert result["data"]["evaluated_events"] == 2
    assert result["cost_sensitivities"]["10"]["active"]["days"] < 2 * 21
    assert result["promotion_eligible"] is False


def test_evaluator_identity_covers_every_transitive_semantic_dependency(monkeypatch):
    baseline, original = _evaluator_hash(), inspect.getsource
    for target in _evaluator_dependencies():
        with monkeypatch.context() as current:
            current.setattr(inspect, "getsource", lambda value, target=target: original(value) + ("\n# mutation" if value is target else ""))
            assert _evaluator_hash() != baseline


def test_research_operator_cannot_overwrite_one_frozen_price_stream(tmp_path):
    policy = _policy()
    policy["price_data"]["maximum_symbols"] = 2
    manifest = {"events": [{"symbol": "AAA"}]}
    (tmp_path / "AAA.json").write_bytes(b"already frozen")

    with pytest.raises(ValueError, match="refusing to overwrite frozen price bytes"):
        fetch_prices(policy, manifest, tmp_path, refresh=True)
