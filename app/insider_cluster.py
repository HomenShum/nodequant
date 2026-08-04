"""Sealed SEC Form 4 cluster-purchase manifest and calendar-time falsifier."""

from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import io
import json
import math
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import numpy as np

from .real_alpha import _canonical_hash

REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "insider_cluster_policy_v1.json"
DEFAULT_AUTHORITY = REPO_ROOT / "research" / "insider_cluster_price_authority_v1.json"
EXPECTED_POLICY_SHA256 = "365b6de904b7b69fb4880482381b75c0d043613df3c84021b6d7715649318b33"
EXPECTED_AUTHORITY_SHA256 = "AUTHORITY_SHA256_PENDING"


def _bounded_get(url: str, provider: dict[str, Any]) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != provider["host"] or parsed.fragment:
        raise ValueError("provider URL is outside the frozen HTTPS host")
    headers = {"User-Agent": provider["user_agent"]}
    with httpx.Client(timeout=httpx.Timeout(provider["timeout_seconds"]), follow_redirects=False) as client:
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > provider["maximum_response_bytes"]:
                    raise ValueError("provider response exceeds the frozen byte cap")
                chunks.append(chunk)
    return b"".join(chunks)


def fetch_sec_archives(policy: dict[str, Any], raw_directory: Path, *, refresh: bool) -> dict[str, bytes]:
    raw_directory.mkdir(parents=True, exist_ok=True)
    rows = {}
    for source in policy["sec_data"]["sources"]:
        path = raw_directory / source["filename"]
        raw = _bounded_get(source["url"], policy["sec_provider"]) if refresh else path.read_bytes()
        if refresh:
            try:
                with path.open("xb") as handle:
                    handle.write(raw)
            except FileExistsError as exc:
                raise ValueError(f"refusing to overwrite frozen SEC bytes: {path.name}") from exc
        rows[source["id"]] = raw
    return rows


def _zip_tsv(raw: bytes, name: str, maximum_bytes: int) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        matches = [row for row in archive.infolist() if not row.is_dir() and Path(row.filename).name.upper() == name.upper()]
        if len(matches) != 1 or matches[0].file_size > maximum_bytes:
            raise ValueError(f"SEC ZIP violates frozen {name} member bounds")
        text = archive.read(matches[0]).decode("utf-8-sig", errors="strict")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def _sec_date(value: str) -> date:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"invalid SEC date: {value!r}")


def compile_manifest(policy: dict[str, Any], archives: dict[str, bytes], policy_hash: str) -> dict[str, Any]:
    submissions, owners, purchases = {}, defaultdict(set), defaultdict(list)
    maximum = policy["sec_provider"]["maximum_uncompressed_member_bytes"]
    raw_hashes = {key: "sha256:" + hashlib.sha256(raw).hexdigest() for key, raw in sorted(archives.items())}
    for key, raw in sorted(archives.items()):
        for row in _zip_tsv(raw, "SUBMISSION.tsv", maximum):
            if row["DOCUMENT_TYPE"].strip() != "4":
                continue
            accession = row["ACCESSION_NUMBER"].strip()
            symbol = row["ISSUERTRADINGSYMBOL"].strip().upper()
            if not re.fullmatch(policy["signal"]["allowed_symbol_regex"], symbol):
                continue
            submissions[accession] = {
                "filing_date": _sec_date(row["FILING_DATE"]),
                "issuer_cik": row["ISSUERCIK"].strip().zfill(10),
                "symbol": symbol,
                "issuer_name": row["ISSUERNAME"].strip(),
            }
        for row in _zip_tsv(raw, "REPORTINGOWNER.tsv", maximum):
            owners[row["ACCESSION_NUMBER"].strip()].add(row["RPTOWNERCIK"].strip().zfill(10))
        for row in _zip_tsv(raw, "NONDERIV_TRANS.tsv", maximum):
            accession = row["ACCESSION_NUMBER"].strip()
            if accession not in submissions or row["TRANS_CODE"].strip() != "P" or row["TRANS_ACQUIRED_DISP_CD"].strip() != "A":
                continue
            title = row["SECURITY_TITLE"].strip().lower()
            if not any(term in title for term in policy["signal"]["allowed_security_title_terms"]):
                continue
            try:
                shares, price = float(row["TRANS_SHARES"]), float(row["TRANS_PRICEPERSHARE"])
            except ValueError:
                continue
            if math.isfinite(shares) and math.isfinite(price) and shares > 0 and price > 0:
                purchases[accession].append(shares * price)
    filing_rows = []
    for accession, values in submissions.items():
        if accession not in purchases or not owners[accession]:
            continue
        filing_rows.append(values | {"accession": accession, "owner_ciks": sorted(owners[accession]), "purchase_value": sum(purchases[accession])})
    by_issuer = defaultdict(list)
    for row in filing_rows:
        by_issuer[row["issuer_cik"]].append(row)
    events = []
    for issuer_cik, rows in sorted(by_issuer.items()):
        rows.sort(key=lambda row: (row["filing_date"], row["accession"]))
        cooldown_until = date.min
        for index, current in enumerate(rows):
            if current["filing_date"] <= cooldown_until:
                continue
            start = current["filing_date"] - timedelta(days=policy["signal"]["cluster_calendar_days"])
            window = [row for row in rows[: index + 1] if start <= row["filing_date"] <= current["filing_date"]]
            distinct = sorted({owner for row in window for owner in row["owner_ciks"]})
            value = sum(row["purchase_value"] for row in window)
            if len(distinct) < policy["signal"]["minimum_distinct_owners"] or value < policy["signal"]["minimum_combined_purchase_usd"]:
                continue
            events.append({
                "event_id": _canonical_hash({"issuer_cik": issuer_cik, "signal_date": current["filing_date"].isoformat(), "accessions": [row["accession"] for row in window]}),
                "issuer_cik": issuer_cik,
                "issuer_name": current["issuer_name"],
                "symbol": current["symbol"],
                "signal_date": current["filing_date"].isoformat(),
                "distinct_owner_count": len(distinct),
                "combined_purchase_usd": value,
                "accessions": [row["accession"] for row in window],
                "owner_ciks": distinct,
            })
            cooldown_until = current["filing_date"] + timedelta(days=policy["signal"]["issuer_cooldown_calendar_days"])
    if len(events) > policy["signal"]["maximum_events"]:
        raise ValueError("SEC cluster manifest exceeds frozen event bound")
    manifest = {
        "version": "nodequant-insider-cluster-manifest-v1",
        "policy_hash": policy_hash,
        "evaluator_hash": _evaluator_hash(),
        "data": {"raw_hashes": raw_hashes, "archives": len(archives), "eligible_filings": len(filing_rows)},
        "events": sorted(events, key=lambda row: (row["signal_date"], row["issuer_cik"], row["event_id"])),
        "trial_accounting": {"candidate_count": 1, "parameter_search": False},
        "promotion_eligible": False,
    }
    manifest["manifest_hash"] = _canonical_hash(manifest)
    manifest["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return manifest


def _load_manifest(path: Path, policy_hash: str) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    body = {key: value for key, value in manifest.items() if key not in {"manifest_hash", "created_at_utc"}}
    if manifest.get("policy_hash") != policy_hash or manifest.get("evaluator_hash") != _evaluator_hash() or manifest.get("manifest_hash") != _canonical_hash(body):
        raise ValueError("insider cluster manifest differs from its frozen identity")
    return manifest


def _load_authority(path: Path, policy_hash: str, manifest: dict[str, Any]) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_AUTHORITY_SHA256:
        raise ValueError("insider price authority differs from the frozen trust root")
    authority = json.loads(raw)
    if authority.get("policy_hash") != policy_hash or authority.get("evaluator_hash") != _evaluator_hash() or authority.get("manifest_hash") != manifest["manifest_hash"] or authority.get("price_bytes_state") != "UNREAD_AT_AUTHORITY_FREEZE":
        raise ValueError("insider price authority is not bound to this unopened manifest")
    return authority


def _price_url(symbol: str, policy: dict[str, Any]) -> str:
    data = policy["price_data"]
    start = int(datetime.fromisoformat(data["start"] + "T00:00:00+00:00").timestamp())
    end = int(datetime.fromisoformat(data["end_exclusive"] + "T00:00:00+00:00").timestamp())
    query = urlencode({"period1": start, "period2": end, "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"})
    return f"https://{policy['price_provider']['host']}/v8/finance/chart/{symbol}?{query}"


def _parse_prices(raw: bytes, symbol: str, maximum_rows: int) -> dict[date, float]:
    result = json.loads(raw).get("chart", {}).get("result")
    if not isinstance(result, list) or len(result) != 1:
        raise ValueError(f"price provider has no single result for {symbol}")
    timestamps = result[0].get("timestamp")
    arrays = result[0].get("indicators", {}).get("adjclose")
    if not isinstance(timestamps, list) or not isinstance(arrays, list) or len(arrays) != 1 or len(timestamps) > maximum_rows:
        raise ValueError(f"price provider arrays are invalid for {symbol}")
    prices = arrays[0].get("adjclose")
    if not isinstance(prices, list) or len(prices) != len(timestamps):
        raise ValueError(f"price provider lengths differ for {symbol}")
    rows = {}
    for stamp, value in zip(timestamps, prices, strict=True):
        if value is None:
            continue
        day, price = datetime.fromtimestamp(stamp, timezone.utc).date(), float(value)
        if day in rows or not math.isfinite(price) or price <= 0:
            raise ValueError(f"price provider contains duplicate or invalid data for {symbol}")
        rows[day] = price
    return rows


def fetch_prices(policy: dict[str, Any], manifest: dict[str, Any], raw_directory: Path, *, refresh: bool) -> tuple[dict[str, bytes], dict[str, str]]:
    symbols = sorted({row["symbol"] for row in manifest["events"]} | {policy["portfolio"]["benchmark_symbol"]})
    if len(symbols) > policy["price_data"]["maximum_symbols"]:
        raise ValueError("insider cluster price universe exceeds frozen symbol bound")
    raw_directory.mkdir(parents=True, exist_ok=True)
    rows, failures = {}, {}
    for symbol in symbols:
        path = raw_directory / f"{symbol.replace('.', '_')}.json"
        if refresh and path.exists():
            raise ValueError(f"refusing to overwrite frozen price bytes: {path.name}")
        try:
            raw = _bounded_get(_price_url(symbol, policy), policy["price_provider"]) if refresh else path.read_bytes()
            if refresh:
                with path.open("xb") as handle:
                    handle.write(raw)
            _parse_prices(raw, symbol, policy["price_data"]["maximum_rows_per_symbol"])
            rows[symbol] = raw
        except FileExistsError as exc:
            raise ValueError(f"refusing to overwrite frozen price bytes: {path.name}") from exc
        except (OSError, ValueError, httpx.HTTPError) as exc:
            failures[symbol] = f"{type(exc).__name__}: {exc}"
    return rows, failures


def _metrics(values: np.ndarray, annualization: int = 252) -> dict[str, Any]:
    if len(values) < 20:
        raise ValueError("insider metric window is too short")
    deviation = float(np.std(values, ddof=1))
    return {"days": len(values), "annualized_mean": float(np.mean(values) * annualization), "annualized_volatility": deviation * math.sqrt(annualization), "sharpe": float(np.mean(values) / deviation * math.sqrt(annualization)) if deviation else None}


def _bootstrap_lower(values: np.ndarray, policy: dict[str, Any]) -> float:
    config, length = policy["bootstrap"], len(values)
    rng = np.random.default_rng(config["seed"]); block = config["block_days"]
    stats = np.empty(config["samples"]); count = math.ceil(length / block)
    for sample in range(config["samples"]):
        starts = rng.integers(0, length, size=count)
        indexes = ((starts[:, None] + np.arange(block)) % length).reshape(-1)[:length]
        stats[sample] = float(np.mean(values[indexes]))
    return float(np.quantile(stats, 0.05) * 252)


def evaluate(policy: dict[str, Any], manifest: dict[str, Any], raw: dict[str, bytes], failures: dict[str, str], policy_hash: str) -> dict[str, Any]:
    benchmark_symbol = policy["portfolio"]["benchmark_symbol"]
    if benchmark_symbol not in raw:
        raise ValueError("benchmark price stream is unavailable")
    series = {symbol: _parse_prices(value, symbol, policy["price_data"]["maximum_rows_per_symbol"]) for symbol, value in raw.items()}
    sensitivities, evaluated_primary = {}, None
    for cost_bps in policy["costs"]["one_way_bps_sensitivity"]:
        contributions = defaultdict(list); evaluated = []
        cost = cost_bps / 10_000.0
        for event in manifest["events"]:
            if event["symbol"] not in series:
                continue
            common = sorted(set(series[event["symbol"]]) & set(series[benchmark_symbol]))
            eligible = [day for day in common if day > date.fromisoformat(event["signal_date"])]
            holding = policy["portfolio"]["holding_sessions"]
            if len(eligible) <= holding:
                continue
            entry = common.index(eligible[0]); exit_index = entry + holding
            if exit_index >= len(common):
                continue
            for offset in range(1, holding + 1):
                left, right = common[entry + offset - 1], common[entry + offset]
                stock = series[event["symbol"]][right] / series[event["symbol"]][left] - 1.0
                bench = series[benchmark_symbol][right] / series[benchmark_symbol][left] - 1.0
                if offset == 1:
                    stock = (1 - cost) * (1 + stock) - 1; bench = (1 - cost) * (1 + bench) - 1
                if offset == holding:
                    stock = (1 + stock) * (1 - cost) - 1; bench = (1 + bench) * (1 - cost) - 1
                contributions[right].append((stock, bench))
            evaluated.append(event["event_id"])
        if not contributions:
            raise ValueError("no insider events have complete price outcomes")
        calendar = [day for day in sorted(series[benchmark_symbol]) if min(contributions) <= day <= max(contributions)]
        stock_returns, benchmark_returns = [], []
        for day in calendar:
            rows = contributions.get(day, [])
            stock_returns.append(float(np.mean([row[0] for row in rows])) if rows else 0.0)
            benchmark_returns.append(float(np.mean([row[1] for row in rows])) if rows else 0.0)
        stock_array, benchmark_array = np.asarray(stock_returns), np.asarray(benchmark_returns)
        active = stock_array - benchmark_array
        sensitivities[str(cost_bps)] = {"strategy": _metrics(stock_array), "benchmark": _metrics(benchmark_array), "active": _metrics(active), "evaluated_events": len(evaluated)}
        if cost_bps == policy["costs"]["primary_one_way_bps"]:
            evaluated_primary = (calendar, active, evaluated)
    if evaluated_primary is None:
        raise ValueError("primary insider cost setting was not evaluated")
    calendar, active, evaluated = evaluated_primary
    folds = {}
    for fold in policy["folds"]:
        mask = np.asarray([date.fromisoformat(fold["start"]) <= day <= date.fromisoformat(fold["end"]) for day in calendar])
        folds[fold["id"]] = _metrics(active[mask])
    positive_folds = sum(row["annualized_mean"] > 0 for row in folds.values())
    lower = _bootstrap_lower(active, policy)
    keep = np.ones(len(active), dtype=bool); count = policy["gate"]["remove_best_days"]
    keep[np.argpartition(active, -count)[-count:]] = False
    reduced = _metrics(active[keep])["sharpe"]
    coverage = len(evaluated) / len(manifest["events"]) if manifest["events"] else 0.0
    unique_issuers = len({row["issuer_cik"] for row in manifest["events"] if row["event_id"] in set(evaluated)})
    checks = {
        "minimum_events": len(evaluated) >= policy["gate"]["minimum_events"],
        "minimum_unique_issuers": unique_issuers >= policy["gate"]["minimum_unique_issuers"],
        "minimum_event_coverage": coverage >= policy["gate"]["minimum_event_coverage"],
        "bootstrap_lower_positive": lower > 0,
        "minimum_positive_folds": positive_folds >= policy["gate"]["minimum_positive_folds"],
        "remove_best_days_sharpe_positive": reduced is not None and reduced > 0,
    }
    result = {
        "version": "nodequant-insider-cluster-result-v1", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "manifest_hash": manifest["manifest_hash"],
        "data": {"raw_hashes": {key: "sha256:" + hashlib.sha256(value).hexdigest() for key, value in sorted(raw.items())}, "price_failures": failures, "manifest_events": len(manifest["events"]), "evaluated_events": len(evaluated), "event_coverage": coverage, "unique_issuers": unique_issuers},
        "cost_sensitivities": sensitivities,
        "primary": {"active_annualized_mean_block_bootstrap_onesided_95pct_lower": lower, "positive_folds": positive_folds, "folds": folds, "remove_best_days_active_sharpe": reduced, "checks": checks},
        "verdict": "INSIDER_CLUSTER_SURVIVES" if all(checks.values()) else "INSIDER_CLUSTER_FAILS", "promotion_eligible": False, "promotion_ceiling": policy["promotion_ceiling"],
        "limitations": ["SEC quarterly files expose filing date but not acceptance time; entry is conservatively delayed to the next common session.", "SEC structured data are as filed and may contain filer or extraction errors.", "Yahoo adjusted closes are secondary and revisable rather than exchange-grade point-in-time prices.", "The calendar-time sleeve portfolio controls overlapping-outcome inference but is not a capacity or execution simulation.", "Historical evidence cannot replace the separately frozen prospective packet."],
    }
    result["result_hash"] = _canonical_hash(result); result["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def _evaluator_dependencies() -> tuple[Any, ...]:
    return (_bounded_get, fetch_sec_archives, _zip_tsv, _sec_date, compile_manifest, _load_manifest, _load_authority, _price_url, _parse_prices, fetch_prices, _metrics, _bootstrap_lower, evaluate, _canonical_hash)


def _evaluator_hash() -> str:
    return "sha256:" + hashlib.sha256("\n".join(inspect.getsource(value) for value in _evaluator_dependencies()).encode()).hexdigest()


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_POLICY_SHA256:
        raise ValueError("insider cluster policy differs from the preregistered trust root")
    policy = json.loads(raw)
    if policy.get("evaluator_hash") != _evaluator_hash() or policy["trial_accounting"]["candidate_count"] != 1:
        raise ValueError("insider cluster policy is bound to another evaluator or trial slate")
    return policy, "sha256:" + digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile or evaluate the sealed SEC insider-cluster falsifier.")
    parser.add_argument("--stage", required=True, choices=("manifest", "evaluate")); parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--raw-directory", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--manifest", type=Path); parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY); parser.add_argument("--reuse-raw", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        if args.stage == "manifest":
            result = compile_manifest(policy, fetch_sec_archives(policy, args.raw_directory, refresh=not args.reuse_raw), policy_hash)
            status = {"stage": "manifest", "events": len(result["events"]), "manifest_hash": result["manifest_hash"]}
        else:
            if args.manifest is None:
                raise ValueError("evaluate stage requires --manifest")
            manifest = _load_manifest(args.manifest, policy_hash); _load_authority(args.authority, policy_hash, manifest)
            raw, failures = fetch_prices(policy, manifest, args.raw_directory, refresh=not args.reuse_raw)
            result = evaluate(policy, manifest, raw, failures, policy_hash)
            status = {"stage": "evaluate", "verdict": result["verdict"], "result_hash": result["result_hash"]}
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(status, sort_keys=True)); return 0 if status.get("verdict", "").endswith("SURVIVES") or args.stage == "manifest" else 5
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile, httpx.HTTPError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
