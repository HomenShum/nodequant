import inspect
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.fomc_drift import _MeetingParser, _evaluator_dependencies, _evaluator_hash, compile_manifest, evaluate, fetch_price


def _policy():
    return {"event_data": {"sources": [{"id": "x", "url": "https://www.federalreserve.gov/x"}], "start_date": "2024-01-01", "end_date": "2024-12-31", "expected_events_by_year": {"2024": 1}, "expected_events": 1}}


def test_event_study_researcher_gets_scheduled_meeting_statement_but_not_notation_vote():
    html = b'''<h5>January 30-31 Meeting - 2024</h5><a href="/newsevents/pressreleases/monetary20240131a.htm">Statement</a><h5>August 22 Notation Vote</h5><a href="/newsevents/pressreleases/monetary20240822a.htm">Statement</a>'''
    manifest = compile_manifest(_policy(), {"x": html}, "sha256:policy")
    assert [row["decision_date"] for row in manifest["events"]] == ["2024-01-31"]
    assert "monetary20240131a.htm" in manifest["events"][0]["statement_url"]


def test_modern_calendar_reader_uses_meeting_rows_and_excludes_implementation_notes():
    html = b'''<div class="row fomc-meeting"><div>January</div><div>30-31</div><a href="/newsevents/pressreleases/monetary20240131a.htm">HTML</a><a href="/newsevents/pressreleases/monetary20240131a1.htm">Implementation Note</a></div>'''
    manifest = compile_manifest(_policy(), {"x": html}, "sha256:policy")
    assert [row["decision_date"] for row in manifest["events"]] == ["2024-01-31"]
    assert manifest["events"][0]["meeting_heading"] == "January 30-31 HTML"


def test_event_data_steward_rejects_duplicate_statement_dates():
    html = b'''<h5>January Meeting</h5><a href="/newsevents/pressreleases/monetary20240131a.htm">Statement</a><h5>Duplicate Meeting</h5><a href="/newsevents/pressreleases/monetary20240131a.htm">Statement</a>'''
    with pytest.raises(ValueError, match="duplicate"):
        compile_manifest(_policy(), {"x": html}, "sha256:policy")


def test_research_operator_cannot_overwrite_frozen_ivv_bytes(tmp_path):
    path = tmp_path / "IVV.json"; path.write_bytes(b"frozen")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        fetch_price({}, path, refresh=True)


def test_evaluator_identity_covers_every_transitive_dependency(monkeypatch):
    baseline, original = _evaluator_hash(), inspect.getsource
    for target in _evaluator_dependencies():
        with monkeypatch.context() as current:
            current.setattr(inspect, "getsource", lambda value, target=target: original(value) + ("\n# mutation" if value is target else ""))
            assert _evaluator_hash() != baseline


def test_macro_researcher_measures_two_sustained_time_folds_and_fails_closed_on_a_missing_event():
    event_days, days, closes, price = [], [], [], 100.0
    for year in (2024, 2025):
        start = date(year, 1, 1)
        for offset in range(60):
            day = start + timedelta(days=offset)
            is_event = offset % 3 == 2
            gross = (0.003 if (offset // 3) % 2 == 0 else 0.001) if is_event else 0.0005
            price *= 1 + gross
            days.append(day); closes.append(price)
            if is_event: event_days.append(day)
    raw = json.dumps({"chart": {"error": None, "result": [{
        "timestamp": [int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()) for day in days],
        "indicators": {"adjclose": [{"adjclose": closes}]},
    }]}}).encode()
    policy = {
        "event_data": {"start_date": "2024-01-01", "end_date": "2025-12-31"},
        "price_data": {"maximum_rows": 200},
        "costs": {"one_way_bps_sensitivity": [0, 5], "primary_one_way_bps": 5},
        "evaluation": {"events_per_year": 20},
        "bootstrap": {"seed": 7, "samples": 200, "block_events": 4},
        "folds": [
            {"id": "2024", "start": "2024-01-01", "end": "2024-12-31"},
            {"id": "2025", "start": "2025-01-01", "end": "2025-12-31"},
        ],
        "gate": {"remove_best_events": 5, "minimum_positive_folds": 2},
        "promotion_ceiling": "DIAGNOSTIC_ONLY",
    }
    manifest = {"manifest_hash": "sha256:manifest", "events": [
        {"decision_date": day.isoformat()} for day in event_days
    ]}

    result = evaluate(policy, manifest, raw, "sha256:policy")

    assert result["data"]["event_outcomes"] == 40
    assert result["primary"]["folds"]["2024"]["events"] == 20
    assert result["primary"]["folds"]["2025"]["events"] == 20
    zero_cost = result["cost_sensitivities"]["0"]["year_matched_active"]["mean"]
    primary = result["cost_sensitivities"]["5"]["year_matched_active"]["mean"]
    assert zero_cost - primary == pytest.approx(0.001)
    assert result["promotion_eligible"] is False

    missing = {**manifest, "events": manifest["events"] + [{"decision_date": "2025-12-31"}]}
    with pytest.raises(ValueError, match="exact close-to-close outcomes"):
        evaluate(policy, missing, raw, "sha256:policy")
