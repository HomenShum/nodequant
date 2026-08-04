"""Sealed post-publication FOMC announcement-day drift falsifier."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import re
import sys
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx
import numpy as np

from .real_alpha import _canonical_hash


REPO_ROOT = Path(__file__).parents[1]
DEFAULT_POLICY = REPO_ROOT / "research" / "fomc_drift_policy_v3.json"
DEFAULT_AUTHORITY = REPO_ROOT / "research" / "fomc_drift_price_authority_v1.json"
EXPECTED_POLICY_SHA256 = "fbf46bac88cfee7dbca1d7f9c9b1125735b134580aef2977bee76be12dea061f"
EXPECTED_AUTHORITY_SHA256 = "56e67d8cfc47512aec862b437ef04296a729a20a00b56e76f15eb8c778010a1f"


def _bounded_get(url: str, provider: dict[str, Any]) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != provider["host"] or parsed.fragment:
        raise ValueError("provider URL is outside the frozen HTTPS host")
    with httpx.Client(timeout=httpx.Timeout(provider["timeout_seconds"]), follow_redirects=False) as client:
        with client.stream("GET", url, headers={"User-Agent": provider.get("user_agent", "nodequant-research/0.1")}) as response:
            response.raise_for_status(); chunks, size = [], 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > provider["maximum_response_bytes"]:
                    raise ValueError("provider response exceeds the frozen byte cap")
                chunks.append(chunk)
    return b"".join(chunks)


class _MeetingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True); self.heading: list[str] | None = None; self.current_heading = ""; self.links: list[tuple[str, str, str, str]] = []; self._href: str | None = None; self._link_text: list[str] = []; self._row_depth = 0; self._row_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "h5": self.heading = []
        if tag.lower() == "div":
            classes = (dict(attrs).get("class") or "").split()
            if self._row_depth: self._row_depth += 1
            elif "fomc-meeting" in classes: self._row_depth = 1; self._row_text = []
        if tag.lower() == "a": self._href = dict(attrs).get("href"); self._link_text = []

    def handle_data(self, data: str) -> None:
        if self.heading is not None: self.heading.append(data)
        if self._row_depth: self._row_text.append(data)
        if self._href is not None: self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "h5" and self.heading is not None:
            self.current_heading = " ".join("".join(self.heading).split()); self.heading = None
        if tag.lower() == "a" and self._href is not None:
            row = " ".join(" ".join(self._row_text).split()) if self._row_depth else ""
            self.links.append((self.current_heading, row, self._href, " ".join("".join(self._link_text).split())))
            self._href = None; self._link_text = []
        if tag.lower() == "div" and self._row_depth:
            self._row_depth -= 1
            if not self._row_depth: self._row_text = []


def fetch_event_pages(policy: dict[str, Any], directory: Path, *, refresh: bool) -> dict[str, bytes]:
    directory.mkdir(parents=True, exist_ok=True); output = {}
    for source in policy["event_data"]["sources"]:
        path = directory / source["filename"]
        if refresh and path.exists(): raise ValueError(f"refusing to overwrite frozen Federal Reserve bytes: {path.name}")
        if refresh:
            raw = _bounded_get(source["url"], policy["event_provider"])
            try:
                with path.open("xb") as handle: handle.write(raw)
            except FileExistsError as exc: raise ValueError(f"refusing to overwrite frozen Federal Reserve bytes: {path.name}") from exc
        else: raw = path.read_bytes()
        output[source["id"]] = raw
    return output


def compile_manifest(policy: dict[str, Any], pages: dict[str, bytes], policy_hash: str) -> dict[str, Any]:
    events, raw_hashes = {}, {}
    pattern = re.compile(r"monetary(20\d{6})a\.htm$", re.I)
    for source in policy["event_data"]["sources"]:
        raw = pages[source["id"]]; raw_hashes[source["id"]] = "sha256:" + hashlib.sha256(raw).hexdigest()
        parser = _MeetingParser()
        try: parser.feed(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc: raise ValueError(f"invalid Federal Reserve HTML: {source['id']}") from exc
        for heading, row_context, href, text in parser.links:
            match = pattern.search(href)
            historical_scheduled = "Meeting" in heading and "unscheduled" not in heading.lower()
            modern_scheduled = bool(row_context) and "notation vote" not in row_context.lower()
            if match is None or not (historical_scheduled or modern_scheduled): continue
            day = date.fromisoformat(f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}")
            if not policy["event_data"]["start_date"] <= day.isoformat() <= policy["event_data"]["end_date"]: continue
            if day in events: raise ValueError(f"duplicate scheduled FOMC statement date: {day}")
            events[day] = {"decision_date": day.isoformat(), "meeting_heading": row_context if modern_scheduled else heading, "statement_url": urljoin(source["url"], href), "source_id": source["id"]}
    rows = [events[key] for key in sorted(events)]
    expected_years = range(int(policy["event_data"]["start_date"][:4]), int(policy["event_data"]["end_date"][:4]) + 1)
    counts = {str(year): sum(row["decision_date"].startswith(str(year)) for row in rows) for year in expected_years}
    if counts != policy["event_data"]["expected_events_by_year"] or len(rows) != policy["event_data"]["expected_events"]:
        raise ValueError(f"scheduled FOMC event counts differ from frozen contract: {counts}")
    manifest = {"version": "nodequant-fomc-drift-manifest-v1", "policy_hash": policy_hash, "raw_hashes": raw_hashes, "events": rows, "quality": {"events": len(rows), "events_by_year": counts, "first_event": rows[0]["decision_date"], "last_event": rows[-1]["decision_date"]}, "limitations": ["Statement links prove realized scheduled decision dates, not the date on which each schedule was first announced.", "Notation votes and non-meeting actions are excluded by the registered heading rule."]}
    manifest["manifest_hash"] = _canonical_hash(manifest); manifest["created_at_utc"] = datetime.now(timezone.utc).isoformat(); return manifest


def _load_manifest(path: Path, policy_hash: str) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if value.get("policy_hash") != policy_hash: raise ValueError("FOMC manifest belongs to another policy")
    body = {key: item for key, item in value.items() if key not in {"manifest_hash", "created_at_utc"}}
    if value.get("manifest_hash") != _canonical_hash(body): raise ValueError("FOMC manifest body differs from its declared hash")
    return value


def _load_authority(path: Path, policy_hash: str, manifest_hash: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_AUTHORITY_SHA256: raise ValueError("FOMC authority differs from the external trust root")
    value = json.loads(raw)
    if value.get("policy_hash") != policy_hash or value.get("manifest_hash") != manifest_hash: raise ValueError("FOMC authority does not bind this policy and manifest")
    return value


def _price_url(policy: dict[str, Any]) -> str:
    data = policy["price_data"]; start = int(datetime.fromisoformat(data["download_start"] + "T00:00:00+00:00").timestamp()); end = int(datetime.fromisoformat(data["download_end_exclusive"] + "T00:00:00+00:00").timestamp())
    query = urlencode({"period1": start, "period2": end, "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"})
    return f"https://{policy['price_provider']['host']}/v8/finance/chart/{data['symbol']}?{query}"


def fetch_price(policy: dict[str, Any], path: Path, *, refresh: bool) -> bytes:
    if refresh and path.exists(): raise ValueError(f"refusing to overwrite frozen FOMC price bytes: {path.name}")
    if refresh:
        raw = _bounded_get(_price_url(policy), policy["price_provider"]); path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle: handle.write(raw)
        except FileExistsError as exc: raise ValueError(f"refusing to overwrite frozen FOMC price bytes: {path.name}") from exc
        return raw
    return path.read_bytes()


def _parse_prices(raw: bytes, policy: dict[str, Any]) -> dict[date, float]:
    chart = json.loads(raw).get("chart", {}); result = chart.get("result")
    if chart.get("error") is not None or not isinstance(result, list) or len(result) != 1: raise ValueError("price provider returned no single IVV result")
    stamps = result[0].get("timestamp"); arrays = result[0].get("indicators", {}).get("adjclose")
    if not isinstance(stamps, list) or not isinstance(arrays, list) or len(arrays) != 1 or len(stamps) > policy["price_data"]["maximum_rows"]: raise ValueError("IVV adjusted-close arrays are invalid or excessive")
    values = arrays[0].get("adjclose")
    if not isinstance(values, list) or len(values) != len(stamps): raise ValueError("IVV price lengths differ")
    rows = {}
    for stamp, value in zip(stamps, values, strict=True):
        if value is None: continue
        day, price = datetime.fromtimestamp(stamp, timezone.utc).date(), float(value)
        if day in rows or not math.isfinite(price) or price <= 0: raise ValueError("IVV contains duplicate or invalid adjusted closes")
        rows[day] = price
    return rows


def _summary(values: np.ndarray, events_per_year: float) -> dict[str, Any]:
    if len(values) < 10: raise ValueError("FOMC event sample is too short")
    deviation = float(np.std(values, ddof=1))
    return {"events": len(values), "mean": float(np.mean(values)), "volatility": deviation, "event_sharpe_annualized": float(np.mean(values) / deviation * math.sqrt(events_per_year)) if deviation else None, "positive_fraction": float(np.mean(values > 0))}


def _bootstrap_lower(values: np.ndarray, policy: dict[str, Any]) -> float:
    config, length = policy["bootstrap"], len(values); indexes = np.arange(length); rng = np.random.default_rng(config["seed"]); stats = np.empty(config["samples"]); blocks = math.ceil(length / config["block_events"])
    for sample in range(config["samples"]):
        starts = rng.integers(0, length, size=blocks); chosen = np.concatenate([indexes[(start + np.arange(config["block_events"])) % length] for start in starts])[:length]; stats[sample] = float(np.mean(values[chosen]))
    return float(np.quantile(stats, 0.05))


def evaluate(policy: dict[str, Any], manifest: dict[str, Any], raw: bytes, policy_hash: str) -> dict[str, Any]:
    prices = _parse_prices(raw, policy); days = sorted(prices); events = {date.fromisoformat(row["decision_date"]) for row in manifest["events"]}
    event_gross, event_dates, non_event_by_year = [], [], {}
    for index in range(1, len(days)):
        right = days[index]; value = prices[right] / prices[days[index - 1]] - 1
        if right in events: event_gross.append(value); event_dates.append(right)
        elif policy["event_data"]["start_date"] <= right.isoformat() <= policy["event_data"]["end_date"]: non_event_by_year.setdefault(right.year, []).append(value)
    if len(event_dates) != len(events): raise ValueError(f"only {len(event_dates)} of {len(events)} scheduled dates have exact close-to-close outcomes")
    baselines = {year: float(np.mean(values)) for year, values in non_event_by_year.items() if values}
    sensitivities, primary = {}, None
    for cost_bps in policy["costs"]["one_way_bps_sensitivity"]:
        net = np.asarray(event_gross) - 2 * cost_bps / 10_000; active = np.asarray([value - baselines[day.year] for value, day in zip(net, event_dates, strict=True)])
        sensitivities[str(cost_bps)] = {"event_net": _summary(net, policy["evaluation"]["events_per_year"]), "year_matched_active": _summary(active, policy["evaluation"]["events_per_year"])}
        if cost_bps == policy["costs"]["primary_one_way_bps"]: primary = active
    if primary is None: raise ValueError("primary FOMC cost was not evaluated")
    folds = {}
    for fold in policy["folds"]:
        mask = np.asarray([date.fromisoformat(fold["start"]) <= day <= date.fromisoformat(fold["end"]) for day in event_dates]); folds[fold["id"]] = _summary(primary[mask], policy["evaluation"]["events_per_year"])
    lower = _bootstrap_lower(primary, policy); count = policy["gate"]["remove_best_events"]; keep = np.ones(len(primary), dtype=bool); keep[np.argpartition(primary, -count)[-count:]] = False; reduced = _summary(primary[keep], policy["evaluation"]["events_per_year"])["event_sharpe_annualized"]
    positive_folds = sum(row["mean"] > 0 for row in folds.values()); checks = {"bootstrap_lower_positive": lower > 0, "minimum_positive_folds": positive_folds >= policy["gate"]["minimum_positive_folds"], "remove_best_event_sharpe_positive": reduced is not None and reduced > 0}
    result = {"version": "nodequant-fomc-drift-result-v1", "policy_hash": policy_hash, "evaluator_hash": _evaluator_hash(), "manifest_hash": manifest["manifest_hash"], "data": {"price_raw_hash": "sha256:" + hashlib.sha256(raw).hexdigest(), "price_rows": len(prices), "event_outcomes": len(event_dates), "first_event": event_dates[0].isoformat(), "last_event": event_dates[-1].isoformat()}, "cost_sensitivities": sensitivities, "primary": {"active_mean_circular_block_onesided_95pct_lower": lower, "folds": folds, "positive_folds": positive_folds, "remove_best_5_event_sharpe": reduced, "checks": checks}, "verdict": "POST_PUBLICATION_FOMC_DRIFT_SURVIVES" if all(checks.values()) else "POST_PUBLICATION_FOMC_DRIFT_FAILS", "promotion_eligible": False, "promotion_ceiling": policy["promotion_ceiling"], "limitations": ["Daily close-to-close returns shift and widen the original 2 p.m.-to-2 p.m. measurement window and include post-announcement trading.", "The year-matched non-event mean is an evaluation baseline, not a live hedge.", "Yahoo adjusted closes are secondary and revisable.", "This event study does not model intraday spreads, slippage, taxes, capacity or cash yield."]}
    result["result_hash"] = _canonical_hash(result); result["created_at_utc"] = datetime.now(timezone.utc).isoformat(); return result


def _evaluator_dependencies() -> tuple[Any, ...]: return (_bounded_get, _MeetingParser, fetch_event_pages, compile_manifest, _load_manifest, _load_authority, _price_url, fetch_price, _parse_prices, _summary, _bootstrap_lower, evaluate, _canonical_hash)


def _evaluator_hash() -> str: return "sha256:" + hashlib.sha256("\n".join(inspect.getsource(value) for value in _evaluator_dependencies()).encode()).hexdigest()


def load_policy(path: Path = DEFAULT_POLICY) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_POLICY_SHA256: raise ValueError("FOMC policy differs from the preregistered trust root")
    value = json.loads(raw)
    if value.get("evaluator_hash") != _evaluator_hash() or value["trial_accounting"]["candidate_count"] != 1: raise ValueError("FOMC policy belongs to another evaluator or trial slate")
    return value, "sha256:" + digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile or evaluate the sealed post-publication FOMC drift falsifier."); parser.add_argument("--stage", required=True, choices=("manifest", "evaluate")); parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY); parser.add_argument("--event-raw-directory", type=Path, required=True); parser.add_argument("--price-raw", type=Path); parser.add_argument("--manifest", type=Path); parser.add_argument("--authority", type=Path, default=DEFAULT_AUTHORITY); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--reuse-raw", action="store_true"); args = parser.parse_args(argv)
    try:
        policy, policy_hash = load_policy(args.policy)
        if args.stage == "manifest": result = compile_manifest(policy, fetch_event_pages(policy, args.event_raw_directory, refresh=not args.reuse_raw), policy_hash); status = {"stage": "manifest", "events": len(result["events"]), "manifest_hash": result["manifest_hash"]}
        else:
            if args.manifest is None or args.price_raw is None: raise ValueError("evaluate requires --manifest and --price-raw")
            manifest = _load_manifest(args.manifest, policy_hash); _load_authority(args.authority, policy_hash, manifest["manifest_hash"]); result = evaluate(policy, manifest, fetch_price(policy, args.price_raw, refresh=not args.reuse_raw), policy_hash); status = {"stage": "evaluate", "verdict": result["verdict"], "result_hash": result["result_hash"]}
        args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"); print(json.dumps(status, sort_keys=True)); return 0 if args.stage == "manifest" or status["verdict"].endswith("SURVIVES") else 5
    except (OSError, ValueError, KeyError, json.JSONDecodeError, httpx.HTTPError) as exc: print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
