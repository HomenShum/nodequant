import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.cftc_flow import _evaluator_dependencies, _evaluator_hash, compile_manifest, fetch_cftc


def _policy():
    return {
        "universe": [{"contract_market_code": code, "etf_symbol": symbol} for code, symbol in [("1", "AAA"), ("2", "BBB"), ("3", "CCC")]],
        "cftc_data": {"maximum_rows": 20},
        "signal": {"top_count": 1, "publication_embargo_calendar_days": 70, "minimum_complete_reports": 1},
    }


def _row(day, code, long, short, oi=100):
    return {"report_date_as_yyyy_mm_dd": day, "cftc_contract_market_code": code, "contract_market_name": code, "open_interest_all": str(oi), "prod_merc_positions_long": str(long), "prod_merc_positions_short": str(short)}


def test_research_director_gets_one_complete_ranked_signal_with_conservative_knowability():
    rows = [_row("2024-01-02T00:00:00.000", "1", 10, 70), _row("2024-01-02T00:00:00.000", "2", 20, 40), _row("2024-01-02T00:00:00.000", "3", 30, 20)]
    manifest = compile_manifest(_policy(), json.dumps(rows).encode(), "sha256:policy")
    assert manifest["signals"][0]["selected_symbols"] == ["AAA"]
    assert manifest["signals"][0]["available_after_date"] == "2024-03-12"
    assert manifest["quality"]["complete_reports"] == 1


def test_data_steward_rejects_duplicates_and_incomplete_panels_do_not_become_signals():
    policy = _policy()
    duplicate = [_row("2024-01-02", "1", 1, 2), _row("2024-01-02", "1", 1, 2)]
    with pytest.raises(ValueError, match="duplicates"):
        compile_manifest(policy, json.dumps(duplicate).encode(), "sha256:policy")
    incomplete = [_row("2024-01-02", "1", 1, 2), _row("2024-01-02", "2", 1, 2)]
    with pytest.raises(ValueError, match="minimum not met"):
        compile_manifest(policy, json.dumps(incomplete).encode(), "sha256:policy")


def test_research_operator_cannot_overwrite_frozen_cftc_bytes(tmp_path):
    path = tmp_path / "cftc.json"; path.write_bytes(b"frozen")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        fetch_cftc({}, path, refresh=True)


def test_evaluator_identity_covers_every_transitive_semantic_dependency(monkeypatch):
    baseline, original = _evaluator_hash(), inspect.getsource
    for target in _evaluator_dependencies():
        with monkeypatch.context() as current:
            current.setattr(inspect, "getsource", lambda value, target=target: original(value) + ("\n# mutation" if value is target else ""))
            assert _evaluator_hash() != baseline
