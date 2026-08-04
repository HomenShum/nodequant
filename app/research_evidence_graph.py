"""Compile repository research artifacts into a bounded point-in-time graph.

The projection carries references, hashes, statuses, and review priority. It
never copies performance measurements out of their authoritative artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = REPO_ROOT / "research" / "research_evidence_catalog_v1.json"
DEFAULT_OUTPUT = REPO_ROOT / "web" / "app" / "research-graph" / "data.json"
MAX_CATALOG_BYTES = 256_000


class EvidenceGraphError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _parse_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EvidenceGraphError("graph timestamps must carry an offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_artifact(relative: str, maximum_bytes: int) -> tuple[Path, bytes]:
    candidate = (REPO_ROOT / relative).resolve()
    try:
        candidate.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise EvidenceGraphError("artifact path escapes repository") from exc
    if not candidate.is_file():
        raise EvidenceGraphError(f"artifact is absent: {relative}")
    if candidate.stat().st_size > maximum_bytes:
        raise EvidenceGraphError(f"artifact exceeds byte bound: {relative}")
    data = candidate.read_bytes()
    return candidate, data


def _json_path(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise EvidenceGraphError(f"JSON path is absent: {dotted}")
        current = current[part]
    return current


def _attention(kind: str, status: str) -> dict[str, int]:
    evidence = 5 if kind in {"result", "authority", "invalidation"} else 3
    contradiction = 5 if status in {"INVALIDATED", "INVALIDATES_PRIOR_RESULT", "OPEN"} else 1
    recency = 4 if kind in {"result", "invalidation", "visual_proof", "limitation"} else 2
    return {"evidence": evidence, "contradiction": contradiction, "recency": recency}


def compile_graph(catalog_path: Path = DEFAULT_CATALOG) -> dict[str, Any]:
    if catalog_path.stat().st_size > MAX_CATALOG_BYTES:
        raise EvidenceGraphError("catalog exceeds byte bound")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    required = {"version", "claim_boundary", "maximum_nodes", "maximum_edges",
                "maximum_artifact_bytes", "items", "relations"}
    if not isinstance(catalog, dict) or set(catalog) != required:
        raise EvidenceGraphError("catalog schema is invalid")
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}

    for item in catalog["items"]:
        artifact_path, raw = _safe_artifact(item["artifact"], catalog["maximum_artifact_bytes"])
        artifact_json = None
        if artifact_path.suffix.lower() == ".json":
            artifact_json = json.loads(raw)
        if "known_at_json_field" in item:
            if not isinstance(artifact_json, dict):
                raise EvidenceGraphError("JSON timestamp authority requires a JSON artifact")
            known_at = _parse_time(str(_json_path(artifact_json, item["known_at_json_field"])))
            time_authority = f"artifact_field:{item['known_at_json_field']}"
        else:
            known_at = _parse_time(item["known_at_utc"])
            time_authority = item["time_authority"]
        artifact_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        semantic = {
            "key": item["key"], "label": item["label"], "kind": item["kind"],
            "layer": item["layer"], "status": item["status"], "known_at_utc": known_at,
            "time_authority": time_authority, "authority": item["authority"],
            "summary": item["summary"],
            "artifact": {"path": item["artifact"].replace("\\", "/"), "sha256": artifact_hash,
                         "bytes": len(raw)},
            "attention": _attention(item["kind"], item["status"]),
        }
        node = {**semantic, "id": _hash(semantic)}
        if node["key"] in by_key:
            raise EvidenceGraphError("duplicate catalog key")
        nodes.append(node); by_key[node["key"]] = node

        expand = item.get("expand_object_path")
        if expand:
            collection = _json_path(artifact_json, expand)
            if isinstance(collection, list):
                entries = [(str(index + 1), value) for index, value in enumerate(collection)]
            elif isinstance(collection, dict):
                entries = sorted(collection.items())
            else:
                raise EvidenceGraphError("expanded collection is not an object or list")
            for child_key, child in entries:
                label = child_key
                if isinstance(child, dict):
                    candidate = child.get("candidate")
                    if isinstance(candidate, dict):
                        label = str(candidate.get("id") or child_key)
                    else:
                        label = str(child.get("candidate_id") or child.get("id") or child_key)
                child_semantic = {
                    "key": f"{item['key']}::trial::{child_key}", "label": label,
                    "kind": "trial", "layer": "experiment", "status": "MEASURED",
                    "known_at_utc": known_at, "time_authority": time_authority,
                    "authority": "contained_in_hashed_artifact",
                    "summary": "Measured trial; open the parent artifact for authoritative metrics.",
                    "artifact": semantic["artifact"],
                    "attention": {"evidence": 1, "contradiction": 1, "recency": 1},
                }
                child_node = {**child_semantic, "id": _hash(child_semantic)}
                nodes.append(child_node); by_key[child_node["key"]] = child_node
                edge_semantic = {"source": node["key"], "target": child_node["key"],
                                 "relation": "CONTAINS_TRIAL", "layer": "experiment",
                                 "known_at_utc": known_at}
                edges.append({**edge_semantic, "id": _hash(edge_semantic)})

    for relation in catalog["relations"]:
        if relation["source"] not in by_key or relation["target"] not in by_key:
            raise EvidenceGraphError("relation references an absent node")
        known_at = max(by_key[relation["source"]]["known_at_utc"],
                       by_key[relation["target"]]["known_at_utc"])
        semantic = {**relation, "known_at_utc": known_at}
        edges.append({**semantic, "id": _hash(semantic)})

    if len(nodes) > catalog["maximum_nodes"] or len(edges) > catalog["maximum_edges"]:
        raise EvidenceGraphError("compiled graph exceeds catalog bounds")
    if len({node["id"] for node in nodes}) != len(nodes) or len({edge["id"] for edge in edges}) != len(edges):
        raise EvidenceGraphError("compiled graph contains duplicate content IDs")
    times = sorted({node["known_at_utc"] for node in nodes})
    projection = {
        "version": "nodequant-research-evidence-graph-v1",
        "catalog_hash": "sha256:" + hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        "claim_boundary": catalog["claim_boundary"],
        "layers": sorted({node["layer"] for node in nodes} | {edge["layer"] for edge in edges}),
        "time_cutoffs": times,
        "nodes": sorted(nodes, key=lambda node: (node["known_at_utc"], node["key"])),
        "edges": sorted(edges, key=lambda edge: (edge["known_at_utc"], edge["id"])),
    }
    projection["graph_hash"] = _hash(projection)
    return projection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile the bounded NodeQuant research evidence graph.")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        graph = compile_graph(args.catalog)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(graph, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.output)
    except (EvidenceGraphError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}))
        return 2
    print(json.dumps({"ok": True, "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
                      "graph_hash": graph["graph_hash"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
