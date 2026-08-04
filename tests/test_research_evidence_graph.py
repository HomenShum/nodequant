import json
from pathlib import Path

import pytest

from app.research_evidence_graph import EvidenceGraphError, compile_graph


def test_research_director_can_replay_the_full_point_in_time_evidence_route():
    graph = compile_graph()
    keys = {node["key"] for node in graph["nodes"]}
    assert {"program", "real-alpha-g2", "breadth-v4", "prospective-v3", "crypto-v2",
            "crypto-invalidation", "factor-policy-v2", "factor-discovery",
            "factor-authority", "factor-result", "factor-visual", "execution-gap",
            "investable-factor-policy", "investable-factor-authority",
            "investable-factor-result", "investable-factor-visual",
            "oracle-rung2-spec", "oracle-rung2-result", "oracle-rung2-provenance-gap",
            "fomc-drift-policy-v3", "fomc-drift-manifest", "fomc-drift-authority",
            "fomc-drift-result", "fomc-drift-visual", "market-activity-contract",
            "market-activity-structural-manifest",
            "crypto-rebalancing-v1-invalidation", "crypto-rebalancing-v2-policy",
            "crypto-rebalancing-v2-result", "crypto-rebalancing-v2-visual"} <= keys
    assert sum("::trial::" in key for key in keys) >= 15
    assert all(node["artifact"]["sha256"].startswith("sha256:") for node in graph["nodes"])
    assert all(node["known_at_utc"].endswith("Z") for node in graph["nodes"])
    assert graph["graph_hash"].startswith("sha256:")

    at_authority = graph["nodes"]
    authority_time = next(node["known_at_utc"] for node in at_authority if node["key"] == "factor-authority")
    visible = {node["key"] for node in at_authority if node["known_at_utc"] <= authority_time}
    assert "factor-authority" in visible
    assert "factor-result" not in visible
    investable_authority_time = next(node["known_at_utc"] for node in at_authority if node["key"] == "investable-factor-authority")
    investable_visible = {node["key"] for node in at_authority if node["known_at_utc"] <= investable_authority_time}
    assert "investable-factor-authority" in investable_visible
    assert "investable-factor-result" not in investable_visible
    fomc_authority_time = next(node["known_at_utc"] for node in at_authority if node["key"] == "fomc-drift-authority")
    fomc_visible = {node["key"] for node in at_authority if node["known_at_utc"] <= fomc_authority_time}
    assert "fomc-drift-authority" in fomc_visible
    assert "fomc-drift-result" not in fomc_visible


def test_graph_keeps_numbers_in_authoritative_artifacts_and_exposes_failures():
    graph = compile_graph()
    serialized = json.dumps(graph)
    assert "0.02249187343983656" not in serialized
    assert "0.8367225784856682" not in serialized
    invalidation = next(node for node in graph["nodes"] if node["key"] == "crypto-invalidation")
    assert invalidation["attention"]["contradiction"] == 5
    assert invalidation["artifact"]["path"].endswith("INVALIDATED_V2.md")
    oracle_gap = next(node for node in graph["nodes"] if node["key"] == "oracle-rung2-provenance-gap")
    assert oracle_gap["attention"]["contradiction"] == 5
    fomc = next(node for node in graph["nodes"] if node["key"] == "fomc-drift-result")
    assert fomc["status"] == "POST_PUBLICATION_FOMC_DRIFT_FAILS"


def test_catalog_cannot_escape_repo_or_silently_overflow(tmp_path: Path):
    source = json.loads(Path("research/research_evidence_catalog_v1.json").read_text(encoding="utf-8"))
    source["items"][0]["artifact"] = "../outside.txt"
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(EvidenceGraphError, match="escapes repository"):
        compile_graph(bad)

    source = json.loads(Path("research/research_evidence_catalog_v1.json").read_text(encoding="utf-8"))
    source["maximum_nodes"] = 1
    bounded = tmp_path / "bounded.json"; bounded.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(EvidenceGraphError, match="exceeds catalog bounds"):
        compile_graph(bounded)
