import inspect
import io
import zipfile

import numpy as np
import pytest

from app.factor_premia import (
    _csv_text,
    _evaluator_dependencies,
    _evaluator_hash,
    candidate_returns,
    expand_candidates,
    parse_monthly_csv,
)


def test_research_director_gets_every_preregistered_nonempty_factor_subset_once():
    candidates = expand_candidates({"candidate_factors": ["HML", "RMW", "CMA", "Mom"]})
    assert len(candidates) == 15
    assert len({row["id"] for row in candidates}) == 15
    assert candidates[0] == {"id": "equal__HML", "factors": ["HML"], "weight": 1.0}


def test_researcher_parses_monthly_percent_returns_and_stops_before_annual_table():
    text = "metadata\n,Mkt-RF,SMB,HML,RMW,CMA,RF\n196407,1,2,3,4,5,.3\n196408,2,3,4,5,6,.3\n\n Annual Factors: January-December\n1964,9,9,9,9,9,9\n"
    rows = parse_monthly_csv(text, ["HML", "RMW", "CMA"])
    assert rows == {"196407": {"HML": 0.03, "RMW": 0.04, "CMA": 0.05}, "196408": {"HML": 0.04, "RMW": 0.05, "CMA": 0.06}}


def test_momentum_narrative_cannot_be_mistaken_for_the_csv_header():
    text = "Mom is the average return on winners minus losers.\nMissing values are -99.99.\n\n,Mom\n196401,1.25\n196402,-0.50\n\n Annual Factors\n,Mom\n1964,9.00\n"
    assert parse_monthly_csv(text, ["Mom"]) == {"196401": {"Mom": 0.0125}, "196402": {"Mom": -0.005}}


def test_archive_reader_rejects_multi_member_and_oversized_zip_bombs():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", "x")
        archive.writestr("b.csv", "y")
    with pytest.raises(ValueError, match="member bounds"):
        _csv_text(buffer.getvalue(), 100)


def test_factor_allocator_applies_frozen_annual_haircut_monthly_without_lookahead():
    panel = {"columns": ["HML", "RMW", "CMA", "MOM"], "values": np.asarray([[0.02, 0.04, -0.01, 0.01], [0.03, -0.02, 0.01, 0.05]])}
    candidate = {"id": "equal__HML_RMW", "factors": ["HML", "RMW"], "weight": 0.5}
    observed = candidate_returns(panel, candidate, {"HML": 120, "RMW": 240, "CMA": 120, "MOM": 360})
    assert observed == pytest.approx([0.0285, 0.0035])


def test_evaluator_identity_changes_for_every_transitive_semantic_dependency(monkeypatch):
    baseline = _evaluator_hash()
    original = inspect.getsource
    for target in _evaluator_dependencies():
        with monkeypatch.context() as current:
            current.setattr(inspect, "getsource", lambda value, target=target: original(value) + ("\n# mutation" if value is target else ""))
            assert _evaluator_hash() != baseline
