"""Bounded graph-engineered research loop over the frozen NodeQuant diagnostics."""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import queue
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app import ledger as lg
from app import proof_kernel as pk
from app.decision_packet import canonical_hash
from app.experiment import load_manifest
from app.experiment import SplitId, split_for_timestamp
from app.order_journal import build_order_journal
from app.promotion import evaluate as evaluate_promotion
from app.research_evaluator import AccessibleDiagnosticPlan, DiagnosticScore, evaluate_diagnostic_score
from app.research_graph import (_CONTROLLER_CAPABILITY, GraphIntegrityError, ResearchGraphStore,
                                canonical_bytes, content_hash)
from app.research_trust import EXPECTED_IMMUTABLE_FILES, EXPECTED_POLICY_HASH
from app.robustness import evaluate_robustness
from app.walk_forward import evaluate_walk_forward

REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "policy.json"
DEFAULT_SOURCES = REPO_ROOT / "research" / "sources" / "catalog.json"

ALPHA_HYPOTHESES = {
    "gao-intraday-momentum": {
        "mechanism": "delayed reaction links the opening return to the final half-hour return",
        "hypothesis": "The literal opening-sign rule beats its sign-inverted control after delayed executable costs on real SPY and QQQ sessions.",
        "required_fields": ["session_calendar", "official_previous_close", "one_minute_bars", "quotes"],
        "status": "BLOCKED_REAL_SESSION_DATA",
    },
    "zarattini-noise-area": {
        "mechanism": "moves outside a point-in-time minute-of-day noise envelope identify directional information",
        "hypothesis": "The literal noise-area breakout survives 7/14/28-session neighbors, delayed fills, and an inverted-rule falsifier.",
        "required_fields": ["session_calendar", "one_minute_ohlcv", "previous_close", "quotes"],
        "status": "BLOCKED_REAL_SESSION_DATA",
    },
}


def _bounded_json(path: Path, maximum_bytes: int) -> Any:
    with path.open("rb") as handle:
        raw = handle.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise GraphIntegrityError(f"{path.name} exceeds {maximum_bytes} bytes")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GraphIntegrityError(f"{path.name} is invalid JSON") from exc


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    policy = _bounded_json(path, 256 * 1024)
    if not isinstance(policy, dict):
        raise GraphIntegrityError("research policy must be a JSON object")
    policy_hash = content_hash(policy)
    if policy_hash != EXPECTED_POLICY_HASH:
        raise GraphIntegrityError("research policy differs from the compiled trust root")
    if set(policy.get("immutable_files", {})) != EXPECTED_IMMUTABLE_FILES:
        raise GraphIntegrityError("research policy immutable-file set is incomplete")
    if policy.get("corpus_scope") != "synthetic_fixture_diagnostic_not_promotion_evidence":
        raise GraphIntegrityError("research policy corpus scope differs from the compiled contract")
    if policy.get("optimizer_accessible_splits") != ["DEVELOPMENT", "VALIDATION"]:
        raise GraphIntegrityError("research policy accessible splits differ from the compiled contract")
    if policy.get("forbidden_optimizer_splits") != ["TEST", "OPENED_HOLDOUT"]:
        raise GraphIntegrityError("research policy forbidden splits differ from the compiled contract")
    return policy, policy_hash


def verify_immutable_files(policy: dict[str, Any], repo_root: Path = REPO_ROOT) -> None:
    for relative, expected in policy["immutable_files"].items():
        path = repo_root / relative
        if not path.is_file():
            raise GraphIntegrityError(f"sealed evaluator file is absent: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        actual = "sha256:" + digest.hexdigest()
        if actual != expected:
            raise GraphIntegrityError(f"sealed evaluator file changed: {relative}")


def _build_frozen_batch():
    bars = pk.fixture_bars()
    packets = list(pk.packets(25, bars))
    gate = pk.run_gate(25, bars)
    events = lg.build_ledger(bars, packets)
    journal = build_order_journal(bars, packets)
    manifest, manifest_hash = load_manifest()
    packet_hashes = [canonical_hash(packet) for packet in packets]
    robustness = evaluate_robustness(
        bars,
        packets,
        events,
        journal,
        manifest,
        expected_bars_hash=pk.bars_hash(bars),
        expected_packet_hashes=packet_hashes,
    )
    walk_forward = evaluate_walk_forward(
        bars,
        packets,
        manifest,
        robustness,
        expected_bars_hash=pk.bars_hash(bars),
        expected_packet_hashes=packet_hashes,
    )
    promotion = evaluate_promotion(gate, events, bars, packets, journal)
    return bars, packets, manifest, manifest_hash, robustness, walk_forward, gate, promotion


def _diagnostic_worker(result_queue, arguments: tuple[Any, ...]) -> None:
    """Spawn-safe evaluator boundary; the parent can terminate this process."""

    try:
        score = evaluate_diagnostic_score(*arguments[:-3],
            minimum_episodes_per_fold=arguments[-3],
            cost_multiplier=arguments[-2],
            remove_best_episode_count=arguments[-1])
        result_queue.put({"ok": True, "score": score.model_dump(mode="json")})
    except BaseException as exc:  # child failures are data, never an unrecorded controller crash
        result_queue.put({"ok": False, "error": type(exc).__name__, "detail": str(exc)})


def _evaluate_with_timeout(arguments: tuple[Any, ...], seconds: float) -> tuple[DiagnosticScore | None, str]:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=_diagnostic_worker, args=(result_queue, arguments))
    process.start()
    process.join(seconds)
    if process.is_alive():
        process.terminate(); process.join(min(seconds, 1.0))
        result_queue.close()
        return None, "BUDGET_EXCEEDED"
    try:
        outcome = result_queue.get_nowait()
    except queue.Empty as exc:
        raise GraphIntegrityError("diagnostic subprocess exited without a result") from exc
    finally:
        result_queue.close()
    if not outcome.get("ok"):
        raise GraphIntegrityError(f"diagnostic subprocess failed: {outcome.get('error')}: {outcome.get('detail')}")
    return DiagnosticScore.model_validate(outcome["score"]), "COMPLETED_WITHIN_BUDGET"


def _optimizer_bars(bars, manifest):
    """Materialize the only timestamps the diagnostic subprocess is allowed to receive."""

    allowed = {SplitId.DEVELOPMENT, SplitId.VALIDATION}
    return [bar for bar in bars if split_for_timestamp(bar.ts, manifest) in allowed]


def _diagnostic_arguments(bars, variant, manifest, strategy, row, policy) -> tuple[Any, ...]:
    """Build the exact bounded object graph crossing into the optimizer process."""

    plan = AccessibleDiagnosticPlan(
        split_windows=tuple(window for window in manifest.split_windows
                            if window.split_id in {SplitId.DEVELOPMENT, SplitId.VALIDATION}),
        walk_forward_folds=manifest.walk_forward_folds,
        expected_development_net_pnl_micros=row.development_net_pnl_micros,
        expected_validation_net_pnl_micros=row.validation_net_pnl_micros,
        expected_development_episode_count=row.development_episode_count,
        expected_validation_episode_count=row.validation_episode_count,
    )
    return (_optimizer_bars(bars, manifest), variant, plan, strategy,
            policy["eligibility"]["minimum_episodes_per_fold"],
            policy["metric"]["cost_multiplier"], policy["metric"]["remove_best_episode_count"])


def _seed_research_knowledge(
    store: ResearchGraphStore,
    policy: dict[str, Any],
    policy_hash: str,
    sources_path: Path,
) -> tuple[str, dict[str, str]]:
    sources = _bounded_json(sources_path, policy["bounds"]["maximum_source_catalog_bytes"])
    if not isinstance(sources, list) or len(sources) > 1000:
        raise GraphIntegrityError("research source catalog must be a bounded list")
    policy_node = store.append_node(
        "evaluation_policy", {"policy_hash": policy_hash, "policy": policy}
    )
    hypothesis_nodes: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, dict) or not source.get("source_id"):
            raise GraphIntegrityError("research source record is malformed")
        source_node = store.append_node("source", source)
        claim_node = store.append_node(
            "source_claim",
            {
                "source_id": source["source_id"],
                "reported_metric": source.get("reported_metric"),
                "authority": "external_metadata_never_local_evidence",
                "candidate_relevance": source.get("candidate_relevance"),
            },
        )
        store.append_edge("SUPPORTS", source_node, claim_node)
        if source["source_id"] not in ALPHA_HYPOTHESES:
            continue
        item = ALPHA_HYPOTHESES[source["source_id"]]
        mechanism = store.append_node(
            "mechanism", {"source_id": source["source_id"], "statement": item["mechanism"]}
        )
        hypothesis = store.append_node(
            "hypothesis",
            {
                "source_id": source["source_id"],
                "statement": item["hypothesis"],
                "required_fields": item["required_fields"],
                "status": item["status"],
            },
        )
        store.append_edge("SUPPORTS", source_node, mechanism)
        store.append_edge("DERIVED_FROM", mechanism, hypothesis)
        missing = store.append_node(
            "failure_pattern",
            {"candidate_id": None, "hypothesis_id": hypothesis,
             "blockers": ["REAL_SESSION_BARS_ABSENT", "EXECUTABLE_QUOTES_ABSENT"]},
        )
        store.append_edge("FAILS_BECAUSE", hypothesis, missing)
        hypothesis_nodes[source["source_id"]] = hypothesis
    return policy_node, hypothesis_nodes


def _candidate_family(variant_id: str) -> str:
    if variant_id == "baseline":
        return "baseline"
    if variant_id.startswith("without-"):
        return "simplification"
    if variant_id.startswith(("fast-", "slow-")):
        return "parameter_neighbor"
    if variant_id.startswith("hold-"):
        return "exit_horizon"
    return "unclassified"


def _candidate_nodes(store: ResearchGraphStore, manifest) -> list[tuple[Any, str]]:
    out: list[tuple[Any, str]] = []
    baseline_id: str | None = None
    for variant in manifest.robustness_variants:
        payload = {
            "strategy_id": manifest.strategy_id,
            "parent_candidate_id": baseline_id if variant.variant_id != "baseline" else None,
            "family_id": _candidate_family(variant.variant_id),
            "mutation": variant.model_dump(mode="json"),
            "authority": "frozen_manifest_candidate",
        }
        candidate_id = store.append_node("strategy_candidate", payload)
        if baseline_id is None:
            baseline_id = candidate_id
        elif candidate_id != baseline_id:
            store.append_edge("MUTATES", candidate_id, baseline_id)
        out.append((variant, candidate_id))
    return out


def _eligibility_blockers(
    policy: dict[str, Any],
    gate: dict[str, Any],
    score: DiagnosticScore,
    walk_forward,
) -> list[str]:
    blockers: list[str] = []
    if gate["status"] != "PASS":
        blockers.append("PROOF_KERNEL_FAILED")
    if policy["corpus_scope"] != "real_market_conformed":
        blockers.extend(["SYNTHETIC_CORPUS", "REAL_MARKET_CONFORMANCE_NOT_RUN"])
    if policy["eligibility"]["split_access_provenance_required"] and not walk_forward.access_provenance_verified:
        blockers.append("SPLIT_ACCESS_PROVENANCE_UNVERIFIED")
    if not score.aggregate_matches_frozen_receipt:
        blockers.append("DIAGNOSTIC_AGGREGATE_MISMATCH")
    if score.accessible_episode_count < policy["eligibility"]["minimum_total_episodes"]:
        blockers.append("INSUFFICIENT_EPISODES")
    if score.diagnostic_robust_sharpe_floor is None:
        blockers.append("ROBUST_SHARPE_COMPONENT_NOT_RUN")
    return blockers


def _synthesis(
    store: ResearchGraphStore,
    generation: int,
    generation_results: list[dict[str, Any]],
    remaining: list[str],
) -> str:
    blockers = Counter(
        blocker for result in generation_results for blocker in result.get("blockers", [])
    )
    scored = [
        result
        for result in generation_results
        if result.get("diagnostic_robust_sharpe_floor") is not None
    ]
    diagnostic_leader = (
        max(scored, key=lambda result: result["diagnostic_robust_sharpe_floor"])
        if scored
        else None
    )
    payload = {
        "generation": generation,
        "experiments_in_generation": len(generation_results),
        "authoritative_champion_id": None,
        "diagnostic_leader_candidate_id": (
            diagnostic_leader["candidate_id"] if diagnostic_leader else None
        ),
        "diagnostic_leader_score": (
            diagnostic_leader["diagnostic_robust_sharpe_floor"] if diagnostic_leader else None
        ),
        "recurring_blockers": dict(sorted(blockers.items())),
        "remaining_candidate_ids": remaining,
        "conclusion": (
            "Synthetic diagnostics may order follow-up work but cannot select an authoritative champion."
        ),
        "next_highest_information_experiment": (
            "provider-backed Gao opening-to-close-half-hour literal versus inverted replay"
        ),
    }
    return store.append_node("synthesis", payload)


def _recover_incomplete_experiments(store: ResearchGraphStore, policy_hash: str) -> int:
    """Close experiments whose node checkpoint committed before their trial did."""

    nodes = {node["id"]: node for node in store.node_records()}
    completed = {trial["experiment_id"] for trial in store.trial_records()}
    policy_nodes = [node_id for node_id, node in nodes.items()
                    if node["node_type"] == "evaluation_policy" and
                    node["payload"]["policy_hash"] == policy_hash]
    if len(policy_nodes) != 1:
        raise GraphIntegrityError("campaign lacks one frozen evaluation-policy node")
    recovered = 0
    for experiment_id, experiment in nodes.items():
        if experiment["node_type"] != "experiment" or experiment_id in completed:
            continue
        payload = experiment["payload"]
        candidate_id = payload["candidate_id"]
        candidate = nodes.get(candidate_id)
        if not candidate or candidate["node_type"] != "strategy_candidate":
            raise GraphIntegrityError("orphan experiment references an absent candidate")
        result_payload = {
            "candidate_id": candidate_id,
            "variant_id": candidate["payload"]["mutation"]["variant_id"],
            "status": "CRASHED_RECOVERED",
            "authoritative_robust_sharpe_floor": None,
            "diagnostic_robust_sharpe_floor": None,
            "diagnostic_components": {},
            "blockers": ["INCOMPLETE_PREVIOUS_ATTEMPT"],
            "budget_outcome": "CONTROLLER_CRASH_RECOVERED",
            "evidence_scope": "synthetic_graph_loop_diagnostic_not_promotion_evidence",
        }
        result_id = store.append_node("result", result_payload)
        store.append_edge("TESTED_ON", candidate_id, experiment_id)
        store.append_edge("EVALUATED_UNDER", experiment_id, policy_nodes[0])
        store.append_edge("PRODUCED", experiment_id, result_id)
        store.append_trial({"experiment_id": experiment_id, "candidate_id": candidate_id,
                            "result_node_id": result_id, "status": "CRASHED_RECOVERED",
                            "policy_hash": policy_hash, "generation": payload["generation"]})
        recovered += 1
    return recovered


def _read_existing_summary(output: Path, maximum_bytes: int) -> dict[str, Any] | None:
    path = output / "latest-summary.json"
    if not path.exists(): return None
    value = _bounded_json(path, maximum_bytes)
    if not isinstance(value, dict): raise GraphIntegrityError("latest summary is malformed")
    return value


def run_generation(
    *,
    output: Path,
    max_experiments: int,
    seed: int,
    policy_path: Path = DEFAULT_POLICY,
    sources_path: Path = DEFAULT_SOURCES,
    generation_seconds: float | None = None,
    experiment_seconds: float | None = None,
) -> dict[str, Any]:
    policy, policy_hash = load_policy(policy_path)
    verify_immutable_files(policy)
    bounds = policy["bounds"]
    if not 1 <= max_experiments <= bounds["maximum_experiments_per_generation"]:
        raise GraphIntegrityError("max_experiments violates the frozen generation bound")
    if seed not in policy["seed_set"]:
        raise GraphIntegrityError("seed is outside the frozen seed set")
    generation_seconds = (
        bounds["maximum_seconds_per_generation"]
        if generation_seconds is None
        else generation_seconds
    )
    experiment_seconds = (
        bounds["maximum_seconds_per_experiment"]
        if experiment_seconds is None
        else experiment_seconds
    )
    if generation_seconds <= 0 or generation_seconds > bounds["maximum_seconds_per_generation"]:
        raise GraphIntegrityError("generation time budget is outside policy")
    if experiment_seconds <= 0 or experiment_seconds > bounds["maximum_seconds_per_experiment"]:
        raise GraphIntegrityError("experiment time budget is outside policy")

    store = ResearchGraphStore(output, policy, policy_hash, _CONTROLLER_CAPABILITY)
    store.verify()
    prior_trial_count = len(store.trial_records())
    policy_node, hypothesis_nodes = _seed_research_knowledge(store, policy, policy_hash, sources_path)
    bars, packets, manifest, manifest_hash, robustness, walk_forward, gate, promotion_gate = _build_frozen_batch()
    dataset_node = store.append_node(
        "dataset_manifest",
        {
            "manifest_hash": manifest_hash,
            "bars_hash": pk.bars_hash(bars),
            "packet_hashes": [canonical_hash(packet) for packet in packets],
            "corpus_scope": policy["corpus_scope"],
            "optimizer_accessible_splits": policy["optimizer_accessible_splits"],
            "forbidden_optimizer_splits": policy["forbidden_optimizer_splits"],
        },
    )
    candidate_nodes = _candidate_nodes(store, manifest)
    recovered_trials = _recover_incomplete_experiments(store, policy_hash)
    existing_trials = store.trial_records()
    tried = {trial["candidate_id"] for trial in existing_trials}
    remaining_pairs = [(variant, node_id) for variant, node_id in candidate_nodes if node_id not in tried]
    if not remaining_pairs:
        audit = store.verify()
        generation = max(trial["generation"] for trial in existing_trials)
        generation_trials = [trial for trial in existing_trials if trial["generation"] == generation]
        nodes = store.node_records()
        syntheses = [node for node in nodes if node["node_type"] == "synthesis" and
                     node["payload"]["generation"] == generation]
        promotions = [node for node in nodes if node["node_type"] == "promotion" and
                      node["payload"]["generation"] == generation]
        if len(syntheses) != 1 or len(promotions) != 1:
            raise GraphIntegrityError("exhausted graph lacks one authoritative synthesis and promotion")
        repository_blockers = [blocker.code for blocker in promotion_gate.blockers]
        research_blockers = promotions[0]["payload"]["blockers"]
        frontier = {"generation": generation, "status": "BLOCKED_EXTERNAL_EVIDENCE",
                    "remaining_candidate_ids": [], "authoritative_champion_id": None,
                    "stop_reason": "FRONTIER_EXHAUSTED", "synthesis_id": syntheses[0]["id"],
                    "promotion_id": promotions[0]["id"]}
        output_payload = {
            "research_generation": generation, "experiments_run": len(generation_trials),
            "prior_trial_count": audit["trials"] - len(generation_trials),
            "trial_count": audit["trials"],
            "recovered_trial_count": sum(t["status"] == "CRASHED_RECOVERED" for t in generation_trials),
            "graph": audit, "authoritative_champion": None,
            "promotion": {"verdict": "BLOCKED", "stage": "RESEARCH_DIAGNOSTIC",
                          "repository_gate_blockers": repository_blockers,
                          "research_blockers": research_blockers},
            "frontier": frontier, "policy_hash": policy_hash,
            "reproduction": ["uv", "run", "--no-sync", "python", "-m", "app.research_loop",
                             "--max-experiments", str(max_experiments), "--seed", str(seed),
                             "--output", str(output), "--generation-seconds", str(generation_seconds),
                             "--experiment-seconds", str(experiment_seconds)],
            "limitations": ["synthetic corpus cannot validate either researched alpha family",
                            "local heads are not an externally signed trial-history anchor",
                            "external reported Sharpe values are discovery metadata only",
                            "DSR and PBO remain unrun until complete synchronized real return samples exist",
                            "research hypotheses remain deliberately unimplemented until conformant session bars and quotes exist"],
            "researched_hypothesis_ids": dict(sorted(hypothesis_nodes.items())),
        }
        store.write_projection("frontier.json", frontier)
        store.write_projection("latest-summary.json", output_payload)
        return output_payload
    generation = max((trial["generation"] for trial in existing_trials), default=0) + 1
    generation_deadline = time.monotonic() + generation_seconds
    rows = {row.variant_id: row for row in robustness.rows}
    generation_results: list[dict[str, Any]] = []

    for ordinal, (variant, candidate_id) in enumerate(remaining_pairs[:max_experiments], 1):
        if time.monotonic() >= generation_deadline:
            break
        experiment_payload = {
            "generation": generation,
            "ordinal": ordinal,
            "candidate_id": candidate_id,
            "policy_hash": policy_hash,
            "dataset_id": dataset_node,
            "seed": seed,
            "maximum_seconds": experiment_seconds,
            "prediction": "atomic candidate may improve the frozen diagnostic floor",
            "expected_failure": "real-data, access-provenance, or minimum-sample gate",
        }
        experiment_id = store.append_node("experiment", experiment_payload)
        store.append_edge("TESTED_ON", candidate_id, experiment_id)
        store.append_edge("EVALUATED_UNDER", experiment_id, policy_node)
        row = rows[variant.variant_id]
        arguments = _diagnostic_arguments(bars, variant, manifest, packets[0].strategy, row, policy)
        score, budget_outcome = _evaluate_with_timeout(arguments, min(experiment_seconds, max(generation_deadline - time.monotonic(), 0.000001)))
        blockers: list[str]
        if score is None:
            status = "BUDGET_EXCEEDED"
            blockers = ["EXPERIMENT_BUDGET_EXCEEDED", "SYNTHETIC_CORPUS", "REAL_MARKET_CONFORMANCE_NOT_RUN"]
        else:
            blockers = _eligibility_blockers(policy, gate, score, walk_forward)
            status = "INELIGIBLE" if blockers else "ELIGIBLE"
        result_payload = {
            "candidate_id": candidate_id,
            "variant_id": variant.variant_id,
            "status": status,
            "authoritative_robust_sharpe_floor": (
                score.diagnostic_robust_sharpe_floor if score is not None and not blockers else None
            ),
            "diagnostic_robust_sharpe_floor": score.diagnostic_robust_sharpe_floor if score else None,
            "diagnostic_components": score.model_dump(mode="json") if score else {},
            "blockers": sorted(set(blockers)),
            "budget_outcome": budget_outcome,
            "evidence_scope": "synthetic_graph_loop_diagnostic_not_promotion_evidence",
        }
        result_node = store.append_node("result", result_payload)
        store.append_edge("PRODUCED", experiment_id, result_node)
        if blockers:
            failure = store.append_node(
                "failure_pattern",
                {"candidate_id": candidate_id, "hypothesis_id": None,
                 "blockers": sorted(set(blockers))},
            )
            store.append_edge("FAILS_BECAUSE", candidate_id, failure)
        store.append_trial(
            {
                "experiment_id": experiment_id,
                "candidate_id": candidate_id,
                "result_node_id": result_node,
                "status": status,
                "policy_hash": policy_hash,
                "generation": generation,
            }
        )
        generation_results.append(result_payload)

    tried_after = {trial["candidate_id"] for trial in store.trial_records()}
    remaining = [node_id for _, node_id in candidate_nodes if node_id not in tried_after]
    synthesis_id = _synthesis(store, generation, generation_results, remaining)
    repository_blockers = [blocker.code for blocker in promotion_gate.blockers]
    research_blockers = sorted(set(repository_blockers + [
        "GLOBAL_TRIAL_COMPLETENESS_NOT_EXTERNALLY_ANCHORED", "DEFLATED_SHARPE_NOT_RUN", "PBO_NOT_RUN"
    ]))
    promotion_id = store.append_node(
        "promotion",
        {
            "generation": generation,
            "verdict": "BLOCKED",
            "authoritative_champion_id": None,
            "blockers": research_blockers,
            "repository_promotion_policy_hash": promotion_gate.policy_hash,
        },
    )
    store.append_edge("DERIVED_FROM", synthesis_id, promotion_id)
    audit = store.verify()
    stop_reason = (
        "FRONTIER_EXHAUSTED" if not remaining else "GENERATION_EXPERIMENT_BUDGET_REACHED"
    )
    frontier = {
        "generation": generation,
        "status": "BLOCKED_EXTERNAL_EVIDENCE",
        "remaining_candidate_ids": remaining,
        "authoritative_champion_id": None,
        "stop_reason": stop_reason,
        "synthesis_id": synthesis_id,
        "promotion_id": promotion_id,
    }
    store.write_projection("frontier.json", frontier)
    output_payload = {
        "research_generation": generation,
        "experiments_run": len(generation_results),
        "prior_trial_count": prior_trial_count,
        "trial_count": audit["trials"],
        "recovered_trial_count": recovered_trials,
        "graph": audit,
        "authoritative_champion": None,
        "promotion": {"verdict": "BLOCKED", "stage": "RESEARCH_DIAGNOSTIC",
                      "repository_gate_blockers": repository_blockers,
                      "research_blockers": research_blockers},
        "frontier": frontier,
        "policy_hash": policy_hash,
        "reproduction": ["uv", "run", "--no-sync", "python", "-m", "app.research_loop",
                         "--max-experiments", str(max_experiments), "--seed", str(seed),
                         "--output", str(output), "--generation-seconds", str(generation_seconds),
                         "--experiment-seconds", str(experiment_seconds)],
        "limitations": [
            "synthetic corpus cannot validate either researched alpha family",
            "local heads are not an externally signed trial-history anchor",
            "external reported Sharpe values are discovery metadata only",
            "DSR and PBO remain unrun until complete synchronized real return samples exist",
            "research hypotheses remain deliberately unimplemented until conformant session bars and quotes exist",
        ],
        "researched_hypothesis_ids": dict(sorted(hypothesis_nodes.items())),
    }
    store.write_projection("latest-summary.json", output_payload)
    return output_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-experiments", type=int, default=4)
    parser.add_argument("--seed", type=int, default=240803)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "artifacts" / "research-graph-v4")
    parser.add_argument("--generation-seconds", type=float)
    parser.add_argument("--experiment-seconds", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_generation(
            output=args.output,
            max_experiments=args.max_experiments,
            seed=args.seed,
            generation_seconds=args.generation_seconds,
            experiment_seconds=args.experiment_seconds,
        )
    except (GraphIntegrityError, OSError, ValueError, TimeoutError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        return 2
    print(canonical_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
