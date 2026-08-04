"""Scenario and knockout tests for the bounded graph research controller."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from app import proof_kernel as pk
from app.experiment import SplitId, load_manifest, split_for_timestamp
from app.research_graph import (_CONTROLLER_CAPABILITY, GraphIntegrityError, ResearchGraphStore,
                                canonical_bytes, content_hash)
from app.research_loop import (DEFAULT_POLICY, _build_frozen_batch, _diagnostic_arguments,
                               _optimizer_bars, load_policy, run_generation)


def _results(root: Path):
    store = ResearchGraphStore(root, load_policy()[0], load_policy()[1])
    return [node["payload"] for node in store.node_records() if node["node_type"] == "result"]


def _bytes(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file() and path.name != ".append.lock"}


def test_operator_exhausts_frontier_without_promoting_synthetic_sharpe_and_resume_is_byte_stable(tmp_path):
    """A research director can resume all nine trials; a third run is a true no-op."""

    root = tmp_path / "campaign"
    first = run_generation(output=root, max_experiments=4, seed=240803)
    assert first["prior_trial_count"] == 0 and first["trial_count"] == 4
    second = run_generation(output=root, max_experiments=32, seed=240803)
    assert second["prior_trial_count"] == 4 and second["trial_count"] == 9
    assert second["frontier"]["stop_reason"] == "FRONTIER_EXHAUSTED"
    assert second["authoritative_champion"] is None
    assert second["promotion"]["repository_gate_blockers"] == [
        "SYNTHETIC_CORPUS", "MARKET_DATA_CONFORMANCE_NOT_RUN",
        "ORACLE_NOT_INDEPENDENT_ENOUGH", "SPLIT_DISCIPLINE_NOT_PROVEN"]
    before = _bytes(root)
    third = run_generation(output=root, max_experiments=32, seed=240803)
    assert third == second
    assert _bytes(root) == before
    forged = json.loads((root / "latest-summary.json").read_text(encoding="utf-8"))
    forged["authoritative_champion"] = "FORGED"
    forged["promotion"]["verdict"] = "PASS"
    forged["graph"]["verified"] = False
    (root / "latest-summary.json").write_text(json.dumps(forged), encoding="utf-8")
    repaired = run_generation(output=root, max_experiments=32, seed=240803)
    assert repaired == second
    assert repaired["authoritative_champion"] is None
    assert repaired["promotion"]["verdict"] == "BLOCKED" and repaired["graph"]["verified"] is True
    assert all(result["authoritative_robust_sharpe_floor"] is None for result in _results(root))


def test_policy_substitution_and_self_supplied_hashes_fail_before_graph_mutation(tmp_path):
    """A hostile optimizer cannot relabel the synthetic corpus or bless its own evaluator."""

    policy = json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))
    policy["corpus_scope"] = "real_market_conformed"
    policy["immutable_files"] = {}
    hostile = tmp_path / "hostile.json"
    hostile.write_text(json.dumps(policy), encoding="utf-8")
    output = tmp_path / "hostile-campaign"
    with pytest.raises(GraphIntegrityError, match="compiled trust root"):
        run_generation(output=output, max_experiments=1, seed=240803, policy_path=hostile)
    assert not output.exists()


def test_clean_deletion_reorder_and_full_rehash_without_head_authority_are_rejected(tmp_path):
    """A compliance reviewer catches history surgery even when each JSON record is plausible."""

    root = tmp_path / "history"
    run_generation(output=root, max_experiments=3, seed=240803)
    policy, policy_hash = load_policy()
    path = root / "trial-registry.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:-1]), encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="shorter|anchored"):
        ResearchGraphStore(root, policy, policy_hash)
    path.write_text("".join([lines[1], lines[0], lines[2]]), encoding="utf-8")
    with pytest.raises(GraphIntegrityError, match="chain|record hash"):
        ResearchGraphStore(root, policy, policy_hash).verify()


def test_semantic_forgery_self_loop_cycle_and_absent_candidate_fail_closed(tmp_path):
    """An adversarial runner cannot turn labels and hashes into a fabricated successful trial."""

    root = tmp_path / "semantic"
    run_generation(output=root, max_experiments=1, seed=240803)
    policy, policy_hash = load_policy(); store = ResearchGraphStore(root, policy, policy_hash,
                                                                    _CONTROLLER_CAPABILITY)
    candidate = next(node for node in store.node_records() if node["node_type"] == "strategy_candidate")
    with pytest.raises(GraphIntegrityError, match="payload schema"):
        store.append_node("strategy_candidate", {"ordinal": 1})
    with pytest.raises(GraphIntegrityError, match="self-loop"):
        store.append_edge("MUTATES", candidate["id"], candidate["id"])
    with pytest.raises(GraphIntegrityError, match="promotion"):
        store.append_node("promotion", {
            "generation": 1, "verdict": "PROMOTABLE",
            "authoritative_champion_id": candidate["id"], "blockers": [],
            "repository_promotion_policy_hash": "sha256:" + "9" * 64})
    with pytest.raises(GraphIntegrityError, match="payload schema"):
        store.append_node("promotion", {
            "verdict": "PROMOTABLE", "authoritative_champion_id": candidate["id"],
            "authoritative_sharpe": 999.0, "blockers": []})
    untrusted = ResearchGraphStore(root, policy, policy_hash)
    with pytest.raises(GraphIntegrityError, match="controller authority"):
        untrusted.append_node("experiment", {
            "generation": 99, "ordinal": 1, "candidate_id": candidate["id"],
            "policy_hash": policy_hash, "dataset_id": "sha256:" + "d" * 64,
            "seed": 240803, "maximum_seconds": 300, "prediction": "forged",
            "expected_failure": "none"})
    trial = store.trial_records()[0]
    with pytest.raises(GraphIntegrityError, match="candidate"):
        store.append_trial({**{k: v for k, v in trial.items() if k not in {"sequence", "previous_hash", "record_hash"}},
                            "experiment_id": "sha256:" + "e" * 64,
                            "candidate_id": "sha256:" + "f" * 64})
    assert store.verify()["verified"] is True
    nodes = {node["id"]: node for node in store.node_records()}
    assert any(edge["edge_type"] == "FAILS_BECAUSE" and
               nodes[edge["source"]]["node_type"] == "hypothesis"
               for edge in store._read("edges"))


def test_optimizer_subprocess_never_receives_test_or_opened_holdout_and_timeout_is_hard(tmp_path):
    """A hostile strategy cannot inspect forbidden bars, and a hung trial cannot exceed its budget."""

    bars = pk.fixture_bars(); manifest, _ = load_manifest()
    accessible = _optimizer_bars(bars, manifest)
    tags = {split_for_timestamp(bar.ts, manifest) for bar in accessible}
    assert tags == {SplitId.DEVELOPMENT, SplitId.VALIDATION}
    assert all(split_for_timestamp(bar.ts, manifest) not in {SplitId.TEST, SplitId.OPENED_HOLDOUT} for bar in accessible)
    full = _build_frozen_batch(); row = full[4].rows[0]
    arguments = _diagnostic_arguments(full[0], full[2].robustness_variants[0], full[2],
                                      full[1][0].strategy, row, load_policy()[0])
    assert len(arguments) == 7
    assert {type(item).__name__ for item in arguments} & {"ExperimentManifest", "RobustnessRow"} == set()
    serialized_plan = arguments[2].model_dump_json()
    assert '"TEST"' not in serialized_plan and "OPENED_HOLDOUT" not in serialized_plan
    started = time.monotonic()
    result = run_generation(output=tmp_path / "timeout", max_experiments=1, seed=240803,
                            experiment_seconds=0.001)
    assert time.monotonic() - started < 15
    assert result["trial_count"] == 1
    assert _results(tmp_path / "timeout")[0]["status"] == "BUDGET_EXCEEDED"


def test_fresh_runs_have_identical_authoritative_roots_and_canonical_json_has_a_golden_vector(tmp_path):
    """Two clean controllers produce byte-identical authority despite different wall clocks and paths."""

    left = run_generation(output=tmp_path / "left", max_experiments=2, seed=240803)
    right = run_generation(output=tmp_path / "right", max_experiments=2, seed=240803)
    assert left["graph"] == right["graph"]
    for name in ("nodes.jsonl", "edges.jsonl", "trial-registry.jsonl", "heads.json"):
        assert (tmp_path / "left" / name).read_bytes() == (tmp_path / "right" / name).read_bytes()
    expected = b'{"a":1,"b":2}'
    assert canonical_bytes({"b": 2, "a": 1}) == expected
    assert content_hash({"b": 2, "a": 1}) == "sha256:" + hashlib.sha256(expected).hexdigest()
    with pytest.raises(GraphIntegrityError, match="key collision"):
        canonical_bytes({"é": 1, "e\u0301": 2})


def test_uncommitted_tail_is_truncated_but_committed_orphan_becomes_permanent_crashed_trial(tmp_path):
    """Power-loss tails recover safely while an anchored experiment is never silently forgotten."""

    root = tmp_path / "recovery"
    run_generation(output=root, max_experiments=1, seed=240803)
    policy, policy_hash = load_policy(); store = ResearchGraphStore(root, policy, policy_hash,
                                                                    _CONTROLLER_CAPABILITY)
    with (root / "nodes.jsonl").open("ab") as handle: handle.write(b'{"partial":')
    assert ResearchGraphStore(root, policy, policy_hash).verify()["trials"] == 1
    nodes = store.node_records()
    candidate = next(node for node in nodes if node["node_type"] == "strategy_candidate" and
                     node["id"] not in {trial["candidate_id"] for trial in store.trial_records()})
    dataset = next(node for node in nodes if node["node_type"] == "dataset_manifest")
    experiment = store.append_node("experiment", {
        "generation": 2, "ordinal": 1, "candidate_id": candidate["id"], "policy_hash": policy_hash,
        "dataset_id": dataset["id"], "seed": 240803, "maximum_seconds": 300,
        "prediction": "simulated crash", "expected_failure": "controller interruption"})
    store.append_edge("TESTED_ON", candidate["id"], experiment)
    resumed = run_generation(output=root, max_experiments=1, seed=240803)
    assert resumed["recovered_trial_count"] == 1
    assert any(trial["status"] == "CRASHED_RECOVERED" for trial in ResearchGraphStore(root, policy, policy_hash).trial_records())


def test_malformed_records_report_structured_cli_error_without_traceback(tmp_path, capsys):
    """A damaged campaign gives an operator a bounded error, not a raw KeyError traceback."""

    from app.research_loop import main
    root = tmp_path / "malformed"
    run_generation(output=root, max_experiments=1, seed=240803)
    path = root / "nodes.jsonl"; lines = path.read_text(encoding="utf-8").splitlines()
    item = json.loads(lines[0]); item.pop("payload"); lines[0] = json.dumps(item)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main(["--output", str(root), "--max-experiments", "1"]) == 2
    captured = capsys.readouterr()
    assert '"ok": false' in captured.err.lower()
    assert "traceback" not in captured.err.lower()

    oversized = tmp_path / "oversized-heads"
    run_generation(output=oversized, max_experiments=1, seed=240803)
    (oversized / "heads.json").write_bytes(b"x" * (64 * 1024 + 1))
    assert main(["--output", str(oversized), "--max-experiments", "1"]) == 2
    bounded = capsys.readouterr()
    assert "byte bound" in bounded.err and "traceback" not in bounded.err.lower()
