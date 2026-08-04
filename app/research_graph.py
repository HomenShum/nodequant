"""Locally anchored, content-addressed research DAG and trial registry.

The atomic heads file detects deletion and uncommitted append tails.  It is a
local trust anchor, not a defence against an attacker who can rewrite both the
logs and the head; externally signed checkpoints remain a promotion blocker.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import unicodedata
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

MAX_LOCK_WAIT_SECONDS = 5.0
MAX_HEADS_BYTES = 64 * 1024
_PROCESS_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()
_PROCESS_LOCKS_GUARD = threading.Lock()
TRIAL_STATUSES = {"INELIGIBLE", "BUDGET_EXCEEDED", "CRASHED_RECOVERED"}
_CONTROLLER_CAPABILITY = object()
CONTROLLER_NODE_TYPES = {"source", "source_claim", "mechanism", "hypothesis",
                         "dataset_manifest", "evaluation_policy", "experiment", "result",
                         "synthesis", "promotion", "counterexample"}
EDGE_ENDPOINTS = {
    "SUPPORTS": {("source", "source_claim"), ("source", "mechanism")},
    "CONTRADICTS": {("counterexample", "hypothesis")},
    "DERIVED_FROM": {("mechanism", "hypothesis"), ("synthesis", "promotion")},
    "IMPLEMENTS": {("hypothesis", "strategy_candidate")},
    "MUTATES": {("strategy_candidate", "strategy_candidate")},
    "TESTED_ON": {("strategy_candidate", "experiment")},
    "EVALUATED_UNDER": {("experiment", "evaluation_policy")},
    "PRODUCED": {("experiment", "result")},
    "REFUTES": {("result", "hypothesis")},
    "SURVIVES": {("result", "hypothesis")},
    "FAILS_BECAUSE": {("strategy_candidate", "failure_pattern"), ("hypothesis", "failure_pattern")},
    "GENERALIZES": {("result", "hypothesis")},
    "SUPERSEDES": {("strategy_candidate", "strategy_candidate")},
    "COMBINES_WITH": {("strategy_candidate", "strategy_candidate")},
    "PROMOTED_AS": {("strategy_candidate", "promotion")},
}


class GraphIntegrityError(ValueError):
    """Raised when durable graph state violates its frozen contract."""


def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            normal = unicodedata.normalize("NFC", str(key))
            if normal in out:
                raise GraphIntegrityError("canonical JSON key collision after NFC normalization")
            out[normal] = _normalise(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GraphIntegrityError("non-finite numbers are not canonical JSON")
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (bool, int)):
        return value
    raise GraphIntegrityError(f"unsupported canonical value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _process_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _process_lock(path):
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + MAX_LOCK_WAIT_SECONDS
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("research graph lock budget exceeded")
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ResearchGraphStore:
    """Append-only graph with an atomic local checkpoint over every stream."""

    STREAMS = ("nodes", "edges", "trials")

    def __init__(self, root: Path, policy: dict[str, Any], policy_hash: str | None = None,
                 capability: object | None = None) -> None:
        self.root, self.policy, self.bounds = root, policy, policy["bounds"]
        self.policy_hash = policy_hash or content_hash(policy)
        self._controller = capability is _CONTROLLER_CAPABILITY
        root.mkdir(parents=True, exist_ok=True)
        self.paths = {"nodes": root / "nodes.jsonl", "edges": root / "edges.jsonl",
                      "trials": root / "trial-registry.jsonl"}
        self.nodes_path, self.edges_path, self.trials_path = (self.paths[name] for name in self.STREAMS)
        self.heads_path, self.lock_path = root / "heads.json", root / ".append.lock"
        with _exclusive_lock(self.lock_path):
            self._initialize_or_recover()

    def _empty_heads(self) -> dict[str, Any]:
        return {"version": "research-head-v1", "policy_hash": self.policy_hash,
                **{name: {"count": 0, "root": None} for name in self.STREAMS}}

    def _write_heads(self, heads: dict[str, Any]) -> None:
        tmp = self.root / ".heads.json.tmp"
        with tmp.open("wb") as handle:
            handle.write(canonical_bytes(heads) + b"\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, self.heads_path)

    def _load_heads(self) -> dict[str, Any]:
        if not self.heads_path.exists() or self.heads_path.stat().st_size > MAX_HEADS_BYTES:
            raise GraphIntegrityError("research heads file is absent or exceeds its byte bound")
        try:
            with self.heads_path.open("rb") as handle:
                raw = handle.read(MAX_HEADS_BYTES + 1)
            if len(raw) > MAX_HEADS_BYTES:
                raise GraphIntegrityError("research heads file exceeds its byte bound")
            heads = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GraphIntegrityError("research heads file is absent or invalid") from exc
        if not isinstance(heads, dict) or heads.get("version") != "research-head-v1":
            raise GraphIntegrityError("research heads schema is invalid")
        if heads.get("policy_hash") != self.policy_hash:
            raise GraphIntegrityError("campaign policy hash differs from the frozen policy")
        for name in self.STREAMS:
            head = heads.get(name)
            if not isinstance(head, dict) or not isinstance(head.get("count"), int) or head["count"] < 0:
                raise GraphIntegrityError(f"{name} head is malformed")
        return heads

    def _initialize_or_recover(self) -> None:
        if not self.heads_path.exists():
            if any(path.exists() and path.stat().st_size for path in self.paths.values()):
                raise GraphIntegrityError("unanchored research logs cannot be self-blessed")
            self._write_heads(self._empty_heads())
        heads = self._load_heads()
        for name, path in self.paths.items():
            if path.exists() and path.stat().st_size > self.bounds["maximum_jsonl_bytes"]:
                raise GraphIntegrityError(f"{path.name} exceeds the frozen byte bound before recovery")
            raw = path.read_bytes() if path.exists() else b""
            lines = raw.splitlines(keepends=True)
            anchored = heads[name]["count"]
            if len(lines) < anchored or any(not line.endswith(b"\n") for line in lines[:anchored]):
                raise GraphIntegrityError(f"{path.name} is shorter than its anchored head")
            committed = b"".join(lines[:anchored])
            if len(raw) != len(committed):
                with path.open("wb") as handle:
                    handle.write(committed); handle.flush(); os.fsync(handle.fileno())

    def _read(self, name: str) -> list[dict[str, Any]]:
        path = self.paths[name]
        if not path.exists(): return []
        if path.stat().st_size > self.bounds["maximum_jsonl_bytes"]:
            raise GraphIntegrityError(f"{path.name} exceeds the frozen byte bound")
        records = []
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.endswith("\n"): raise GraphIntegrityError(f"{path.name}:{number} is truncated")
                try: item = json.loads(line)
                except json.JSONDecodeError as exc: raise GraphIntegrityError(f"{path.name}:{number} invalid JSON") from exc
                if not isinstance(item, dict): raise GraphIntegrityError(f"{path.name}:{number} is not an object")
                records.append(item)
        return records

    def _append(self, name: str, record: dict[str, Any], heads: dict[str, Any]) -> None:
        path, encoded = self.paths[name], canonical_bytes(record) + b"\n"
        if (path.stat().st_size if path.exists() else 0) + len(encoded) > self.bounds["maximum_jsonl_bytes"]:
            raise GraphIntegrityError(f"{path.name} would exceed the frozen byte bound")
        with path.open("ab", buffering=0) as handle:
            handle.write(encoded); os.fsync(handle.fileno())
        heads[name] = {"count": record["sequence"], "root": record["record_hash"]}
        self._write_heads(heads)

    @staticmethod
    def _semantic(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key not in {"sequence", "previous_hash", "record_hash"}}

    def _chain(self, name: str, records: list[dict[str, Any]], heads: dict[str, Any]) -> None:
        previous = None
        for sequence, record in enumerate(records, 1):
            if record.get("sequence") != sequence or record.get("previous_hash") != previous:
                raise GraphIntegrityError(f"{name} hash chain is broken")
            carried = record.get("record_hash")
            if not isinstance(carried, str) or carried != content_hash({k: v for k, v in record.items() if k != "record_hash"}):
                raise GraphIntegrityError(f"{name} record hash mismatch")
            previous = carried
        if heads[name].get("count") != len(records) or heads[name].get("root") != previous:
            raise GraphIntegrityError(f"{name} differs from its anchored head")

    @staticmethod
    def _require_keys(payload: Any, exact: set[str], label: str) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != exact:
            raise GraphIntegrityError(f"{label} payload schema is invalid")
        return payload

    def _validate_node(self, node: dict[str, Any]) -> None:
        sem = self._semantic(node)
        if set(sem) != {"id", "node_type", "payload"} or sem.get("node_type") not in self.policy["node_types"]:
            raise GraphIntegrityError("research node schema or type is invalid")
        if sem.get("id") != content_hash({"node_type": sem["node_type"], "payload": sem["payload"]}):
            raise GraphIntegrityError("research node content hash mismatch")
        node_type = sem["node_type"]
        if node_type == "strategy_candidate":
            p = self._require_keys(sem["payload"], {"strategy_id", "parent_candidate_id", "family_id", "mutation", "authority"}, "strategy_candidate")
            self._require_keys(p["mutation"], {"variant_id", "use_uptrend", "use_pullback", "fast_window", "slow_window", "hold_bars"}, "strategy mutation")
            if p["authority"] != "frozen_manifest_candidate": raise GraphIntegrityError("candidate authority is invalid")
        elif node_type == "experiment":
            self._require_keys(sem["payload"], {"generation", "ordinal", "candidate_id", "policy_hash", "dataset_id", "seed", "maximum_seconds", "prediction", "expected_failure"}, "experiment")
        elif node_type == "result":
            p = self._require_keys(sem["payload"], {"candidate_id", "variant_id", "status", "authoritative_robust_sharpe_floor", "diagnostic_robust_sharpe_floor", "diagnostic_components", "blockers", "budget_outcome", "evidence_scope"}, "result")
            if p["status"] not in TRIAL_STATUSES: raise GraphIntegrityError("result status is invalid")
            if p["evidence_scope"] != "synthetic_graph_loop_diagnostic_not_promotion_evidence":
                raise GraphIntegrityError("result evidence scope is invalid")
            if self.policy["corpus_scope"] != "real_market_conformed" and p["authoritative_robust_sharpe_floor"] is not None:
                raise GraphIntegrityError("synthetic result cannot carry authoritative Sharpe")
            if p["status"] == "INELIGIBLE":
                components = self._require_keys(p["diagnostic_components"], {
                    "variant_id", "fold_sharpes", "fold_sharpe_p25", "two_times_cost_sharpe",
                    "remove_best_five_sharpe", "diagnostic_robust_sharpe_floor",
                    "accessible_episode_count", "development_episode_count",
                    "validation_episode_count", "aggregate_matches_frozen_receipt",
                    "annualization", "evidence_scope"}, "diagnostic receipt")
                if (components["variant_id"] != p["variant_id"] or
                        components["diagnostic_robust_sharpe_floor"] != p["diagnostic_robust_sharpe_floor"]):
                    raise GraphIntegrityError("result disagrees with its diagnostic receipt")
            elif p["diagnostic_components"] != {} or p["diagnostic_robust_sharpe_floor"] is not None:
                raise GraphIntegrityError("non-completed result cannot carry a diagnostic score")
        elif node_type == "evaluation_policy":
            p = self._require_keys(sem["payload"], {"policy_hash", "policy"}, "evaluation_policy")
            if p["policy_hash"] != self.policy_hash or p["policy"] != self.policy: raise GraphIntegrityError("evaluation policy node is not the frozen policy")
        elif node_type == "dataset_manifest":
            p = self._require_keys(sem["payload"], {"manifest_hash", "bars_hash", "packet_hashes", "corpus_scope", "optimizer_accessible_splits", "forbidden_optimizer_splits"}, "dataset_manifest")
            if (p["corpus_scope"] != self.policy["corpus_scope"] or
                    p["optimizer_accessible_splits"] != self.policy["optimizer_accessible_splits"] or
                    p["forbidden_optimizer_splits"] != self.policy["forbidden_optimizer_splits"]):
                raise GraphIntegrityError("dataset manifest exceeds optimizer authority")
        elif node_type == "promotion":
            p = self._require_keys(sem["payload"], {"generation", "verdict", "authoritative_champion_id", "blockers", "repository_promotion_policy_hash"}, "promotion")
            if p["verdict"] != "BLOCKED" or p["authoritative_champion_id"] is not None or not p["blockers"]:
                raise GraphIntegrityError("local synthetic research cannot claim promotion")
        else:
            schemas = {
                "source": {"source_id", "source_type", "publication_date", "title", "url", "asset_class", "holding_period", "economic_mechanism", "signal_definition", "execution_assumptions", "cost_assumptions", "reported_universe", "reported_period", "reported_metric", "known_failure_modes", "replication_code", "license", "candidate_relevance"},
                "source_claim": {"source_id", "reported_metric", "authority", "candidate_relevance"},
                "mechanism": {"source_id", "statement"},
                "hypothesis": {"source_id", "statement", "required_fields", "status"},
                "failure_pattern": {"candidate_id", "hypothesis_id", "blockers"},
                "synthesis": {"generation", "experiments_in_generation", "authoritative_champion_id", "diagnostic_leader_candidate_id", "diagnostic_leader_score", "recurring_blockers", "remaining_candidate_ids", "conclusion", "next_highest_information_experiment"},
                "counterexample": {"hypothesis_id", "falsifier", "observation", "authority"},
            }
            if node_type not in schemas: raise GraphIntegrityError(f"node type {node_type} has no implemented schema")
            p = self._require_keys(sem["payload"], schemas[node_type], node_type)
            if node_type == "source_claim" and p["authority"] != "external_metadata_never_local_evidence":
                raise GraphIntegrityError("source claim authority is invalid")
            if node_type == "synthesis" and p["authoritative_champion_id"] is not None:
                raise GraphIntegrityError("synthetic synthesis cannot name an authoritative champion")

    def _validate_trial(self, trial: dict[str, Any], by_id: dict[str, dict[str, Any]],
                        edge_tuples: set[tuple[str, str, str]]) -> None:
        p = self._semantic(trial)
        required = {"experiment_id", "candidate_id", "result_node_id", "status", "policy_hash", "generation"}
        if set(p) != required or p.get("status") not in TRIAL_STATUSES or not isinstance(p.get("generation"), int) or p["generation"] < 1:
            raise GraphIntegrityError("trial semantic schema is invalid")
        if p.get("policy_hash") != self.policy_hash:
            raise GraphIntegrityError("trial policy hash differs from frozen policy")
        candidate, experiment, result = by_id.get(p["candidate_id"]), by_id.get(p["experiment_id"]), by_id.get(p["result_node_id"])
        if not candidate or candidate["node_type"] != "strategy_candidate": raise GraphIntegrityError("trial candidate is absent or mistyped")
        if not experiment or experiment["node_type"] != "experiment": raise GraphIntegrityError("trial experiment is absent or mistyped")
        if not result or result["node_type"] != "result": raise GraphIntegrityError("trial result is absent or mistyped")
        ep, rp = experiment["payload"], result["payload"]
        dataset = by_id.get(ep["dataset_id"])
        if not dataset or dataset["node_type"] != "dataset_manifest":
            raise GraphIntegrityError("trial experiment dataset is absent or mistyped")
        if ep["candidate_id"] != p["candidate_id"] or ep["policy_hash"] != p["policy_hash"] or ep["generation"] != p["generation"]:
            raise GraphIntegrityError("trial disagrees with its experiment")
        if rp["candidate_id"] != p["candidate_id"] or rp["status"] != p["status"]:
            raise GraphIntegrityError("trial disagrees with its result")
        required_edges = {
            ("TESTED_ON", p["candidate_id"], p["experiment_id"]),
            ("PRODUCED", p["experiment_id"], p["result_node_id"]),
        }
        if not required_edges <= edge_tuples:
            raise GraphIntegrityError("trial is missing candidate/experiment/result provenance edges")
        policy_nodes = {node_id for node_id, node in by_id.items()
                        if node["node_type"] == "evaluation_policy" and
                        node["payload"]["policy_hash"] == p["policy_hash"]}
        if not any(("EVALUATED_UNDER", p["experiment_id"], node_id) in edge_tuples
                   for node_id in policy_nodes):
            raise GraphIntegrityError("trial is missing frozen-policy provenance")
        if self.policy["corpus_scope"] != "real_market_conformed" and (p["status"] == "ELIGIBLE" or rp["authoritative_robust_sharpe_floor"] is not None):
            raise GraphIntegrityError("synthetic evidence cannot be authoritative or eligible")

    def verify(self) -> dict[str, Any]:
        heads = self._load_heads(); nodes, edges, trials = (self._read(n) for n in self.STREAMS)
        for name, records in zip(self.STREAMS, (nodes, edges, trials)): self._chain(name, records, heads)
        if len(nodes) > self.bounds["maximum_nodes"] or len(edges) > self.bounds["maximum_edges"] or len(trials) > self.bounds["maximum_total_trials"]:
            raise GraphIntegrityError("research graph bound exceeded")
        by_id: dict[str, dict[str, Any]] = {}
        for node in nodes:
            self._validate_node(node); sem = self._semantic(node)
            if sem["id"] in by_id: raise GraphIntegrityError("duplicate research node")
            by_id[sem["id"]] = sem
        adjacency: dict[str, set[str]] = {node_id: set() for node_id in by_id}; edge_ids = set()
        for edge in edges:
            sem = self._semantic(edge)
            if set(sem) != {"id", "edge_type", "source", "target", "payload"} or sem.get("edge_type") not in self.policy["edge_types"]:
                raise GraphIntegrityError("research edge schema or type is invalid")
            if sem["source"] == sem["target"]: raise GraphIntegrityError("research graph self-loop")
            if sem["source"] not in by_id or sem["target"] not in by_id: raise GraphIntegrityError("research edge references absent node")
            pair = (by_id[sem["source"]]["node_type"], by_id[sem["target"]]["node_type"])
            if pair not in EDGE_ENDPOINTS.get(sem["edge_type"], set()):
                raise GraphIntegrityError("research edge endpoint types are invalid")
            if sem["id"] != content_hash({k: sem[k] for k in ("edge_type", "source", "target", "payload")}): raise GraphIntegrityError("research edge content hash mismatch")
            if sem["id"] in edge_ids: raise GraphIntegrityError("duplicate research edge")
            edge_ids.add(sem["id"]); adjacency[sem["source"]].add(sem["target"])
        visiting: set[str] = set(); visited: set[str] = set()
        def visit(node: str) -> None:
            if node in visiting: raise GraphIntegrityError("research graph contains a cycle")
            if node in visited: return
            visiting.add(node)
            for child in adjacency[node]: visit(child)
            visiting.remove(node); visited.add(node)
        for node in adjacency: visit(node)
        experiments = set()
        edge_tuples = {(edge["edge_type"], edge["source"], edge["target"]) for edge in edges}
        for trial in trials:
            self._validate_trial(trial, by_id, edge_tuples)
            if trial["experiment_id"] in experiments: raise GraphIntegrityError("duplicate experiment in trial registry")
            experiments.add(trial["experiment_id"])
        return {"nodes": len(nodes), "edges": len(edges), "trials": len(trials),
                "node_root": heads["nodes"]["root"], "edge_root": heads["edges"]["root"],
                "trial_root": heads["trials"]["root"], "local_anchor_only": True, "verified": True}

    def _append_record(self, name: str, semantic: dict[str, Any]) -> dict[str, Any]:
        records, heads = self._read(name), self._load_heads()
        record = {"sequence": len(records) + 1, "previous_hash": heads[name]["root"], **_normalise(semantic)}
        record["record_hash"] = content_hash(record)
        self._append(name, record, heads)
        return record

    def append_node(self, node_type: str, payload: dict[str, Any]) -> str:
        if node_type in CONTROLLER_NODE_TYPES and not self._controller:
            raise GraphIntegrityError(f"{node_type} nodes require controller authority")
        semantic = {"node_type": node_type, "payload": _normalise(payload)}
        semantic["id"] = content_hash(semantic)
        with _exclusive_lock(self.lock_path):
            self.verify(); nodes = self._read("nodes")
            if any(n.get("id") == semantic["id"] for n in nodes): return semantic["id"]
            if len(nodes) >= self.bounds["maximum_nodes"]: raise GraphIntegrityError("node bound reached")
            probe = {"sequence": 1, "previous_hash": None, "record_hash": "x", **semantic}; self._validate_node(probe)
            self._append_record("nodes", semantic)
        return semantic["id"]

    def append_edge(self, edge_type: str, source: str, target: str, payload: dict[str, Any] | None = None) -> str:
        semantic = {"edge_type": edge_type, "source": source, "target": target, "payload": _normalise(payload or {})}
        semantic["id"] = content_hash({k: semantic[k] for k in ("edge_type", "source", "target", "payload")})
        with _exclusive_lock(self.lock_path):
            self.verify(); edges = self._read("edges")
            if any(e.get("id") == semantic["id"] for e in edges): return semantic["id"]
            if len(edges) >= self.bounds["maximum_edges"]: raise GraphIntegrityError("edge bound reached")
            if source == target: raise GraphIntegrityError("research graph self-loop")
            nodes = {n["id"]: n["node_type"] for n in self._read("nodes")}
            if source not in nodes or target not in nodes: raise GraphIntegrityError("research edge references absent node")
            if (nodes[source], nodes[target]) not in EDGE_ENDPOINTS.get(edge_type, set()):
                raise GraphIntegrityError("research edge endpoint types are invalid")
            adjacency: dict[str, set[str]] = {node: set() for node in nodes}
            for edge in edges: adjacency[edge["source"]].add(edge["target"])
            stack = [target]; seen = set()
            while stack:
                current = stack.pop()
                if current == source: raise GraphIntegrityError("research edge would create a cycle")
                if current not in seen: seen.add(current); stack.extend(adjacency[current])
            self._append_record("edges", semantic)
        return semantic["id"]

    def append_trial(self, payload: dict[str, Any]) -> dict[str, Any]:
        normal = _normalise(payload)
        with _exclusive_lock(self.lock_path):
            self.verify(); trials = self._read("trials")
            for trial in trials:
                if trial.get("experiment_id") == normal.get("experiment_id"):
                    if self._semantic(trial) == normal: return trial
                    raise GraphIntegrityError("conflicting replay for an existing experiment id")
            if len(trials) >= self.bounds["maximum_total_trials"]: raise GraphIntegrityError("trial bound reached")
            nodes = {n["id"]: self._semantic(n) for n in self._read("nodes")}
            probe = {"sequence": len(trials)+1, "previous_hash": None, "record_hash": "x", **normal}
            edge_tuples = {(edge["edge_type"], edge["source"], edge["target"])
                           for edge in self._read("edges")}
            self._validate_trial(probe, nodes, edge_tuples)
            return self._append_record("trials", normal)

    def trial_records(self) -> list[dict[str, Any]]: return self._read("trials")
    def node_records(self) -> list[dict[str, Any]]: return self._read("nodes")

    def write_projection(self, filename: str, payload: dict[str, Any]) -> Path:
        target, tmp = self.root / filename, self.root / f".{filename}.tmp"
        encoded = canonical_bytes(payload) + b"\n"
        if len(encoded) > self.bounds["maximum_jsonl_bytes"]: raise GraphIntegrityError("projection exceeds bound")
        if target.exists() and target.stat().st_size <= self.bounds["maximum_jsonl_bytes"] and target.read_bytes() == encoded:
            return target
        with tmp.open("wb") as handle: handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, target); return target
