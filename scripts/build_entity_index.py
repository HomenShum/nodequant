"""Build the committed entity index. Runs OFFLINE, never at request time.

    uv run --no-sync python scripts/build_entity_index.py

What it does:

  1. Pages ClinicalTrials.gov for ``InterventionName``, ``InterventionOtherName``
     and ``LeadSponsorName``.
  2. Normalises and deduplicates surface forms into entities, keeping the
     registry's own other-names as an alias list. Those aliases are REGISTRY
     DATA — they come off real study records, not from a model asserting that
     Keytruda is Pembrolizumab.
  3. Caps the corpus by document frequency and REPORTS what was dropped.
  4. Writes one SQLite file, ``app/vendor/entity_index/entities.db``:
       * ``entities``        — canonical, kind, other_names, normalised forms
       * ``entity_aliases``  — normalised alias -> entity, for exact lookup
       * ``entities_fts``    — EXTERNAL CONTENT FTS5 index over canonical +
                               other_names, kept in sync by triggers
       * ``entities.vector`` — optional static embedding BLOB (model2vec)
  5. Writes a manifest recording the cap report and which backend was used.

Nothing built here is a count. Every number that reaches a user still comes
from a count probe issued at request time; see app.entities.verify_candidates.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.entities import KINDS, fts5_available, normalize  # noqa: E402

BASE_URL = "https://clinicaltrials.gov/api/v2"
FIELDS = "NCTId,InterventionName,InterventionOtherName,LeadSponsorName"
OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "vendor" / "entity_index"

# Static, distilled sentence-transformer. CPU-only, no API, and numpy-only at
# query time because the vectors are baked into the DB. The model itself is a
# BUILD dependency: the service never loads it.
SEMANTIC_MODEL = "minishlab/potion-base-8M"
EMBED_BATCH = 512

# Corpus bounds. A few thousand entities per kind, ranked by how many studies
# mention them. The head is what a shortlist question actually needs, and the
# tail is where retrieval would be proposing names on the strength of one
# record anyway. The cap is REPORTED, not silent.
DEFAULT_PAGES = 40
DEFAULT_PAGE_SIZE = 1000
DEFAULT_CAP = 2500

# An entity seen on a single study is usually a typo, a site-local label or a
# one-off arm description. Requiring two sightings removes most of that noise
# without touching anything a user would plausibly ask about.
MIN_DOC_FREQ = 2

# Aliases kept per entity. Pembrolizumab has 22 registry other-names; carrying
# all of them makes the indexed document mostly biosimilar part numbers.
MAX_ALIASES = 8

# EXTERNAL CONTENT FTS5: content='entities' means the FTS structure stores
# only the inverted index and reads document text back from `entities` on
# demand, so the strings are not duplicated on disk. The triggers are not
# optional bookkeeping — with external content they are the only thing that
# keeps the index correct when the source table changes.
SCHEMA = """
CREATE TABLE entities (
    rowid          INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,
    canonical      TEXT NOT NULL,
    other_names    TEXT NOT NULL DEFAULT '',
    norm_canonical TEXT NOT NULL,
    norm_all       TEXT NOT NULL,
    doc_freq       INTEGER NOT NULL DEFAULT 0,
    vector         BLOB
);
CREATE INDEX entities_kind_norm ON entities(kind, norm_canonical);
CREATE INDEX entities_kind_canon ON entities(kind, canonical);

CREATE TABLE entity_aliases (
    entity_rowid INTEGER NOT NULL REFERENCES entities(rowid),
    kind         TEXT NOT NULL,
    norm_alias   TEXT NOT NULL
);
CREATE INDEX entity_aliases_lookup ON entity_aliases(kind, norm_alias);

CREATE VIRTUAL TABLE entities_fts USING fts5(
    canonical,
    other_names,
    content='entities',
    content_rowid='rowid'
);

CREATE TRIGGER entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, canonical, other_names)
    VALUES (new.rowid, new.canonical, new.other_names);
END;
CREATE TRIGGER entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, canonical, other_names)
    VALUES('delete', old.rowid, old.canonical, old.other_names);
END;
CREATE TRIGGER entities_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, canonical, other_names)
    VALUES('delete', old.rowid, old.canonical, old.other_names);
    INSERT INTO entities_fts(rowid, canonical, other_names)
    VALUES (new.rowid, new.canonical, new.other_names);
END;
"""


def page_studies(pages: int, page_size: int, timeout: float = 60.0):
    """Yield study records, paging the registry with its own pageToken."""
    token = None
    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}) as client:
        for page in range(pages):
            params = {"pageSize": str(page_size), "fields": FIELDS, "countTotal": "true"}
            if token:
                params["pageToken"] = token
            for attempt in range(5):
                response = client.get(f"{BASE_URL}/studies", params=params)
                if response.status_code == 200:
                    break
                if response.status_code not in (429, 500, 502, 503, 504):
                    response.raise_for_status()
                time.sleep(1.5 * (attempt + 1))
            else:
                raise RuntimeError(f"page {page} failed after 5 attempts")
            payload = response.json()
            studies = payload.get("studies") or []
            if page == 0:
                print(f"  registry reports {payload.get('totalCount'):,} studies in total")
            print(f"  page {page + 1}/{pages}: {len(studies)} studies", flush=True)
            yield from studies
            token = payload.get("nextPageToken")
            if not token:
                print("  registry returned no nextPageToken; stopping early")
                return


def harvest(studies) -> dict[str, dict[str, dict]]:
    """Group surface forms into entities keyed by normalised form."""
    buckets: dict[str, dict[str, dict]] = {k: {} for k in KINDS}
    seen_studies = 0
    for study in studies:
        seen_studies += 1
        protocol = study.get("protocolSection", {})

        sponsor = (protocol.get("sponsorCollaboratorsModule", {})
                   .get("leadSponsor", {}).get("name"))
        if sponsor:
            _add(buckets["sponsor"], "sponsor", sponsor, ())

        arms = protocol.get("armsInterventionsModule", {}).get("interventions") or []
        for arm in arms:
            name = arm.get("name")
            if not name:
                continue
            # The registry sometimes packs several synonyms into one string.
            raw_aliases: list[str] = []
            for other in arm.get("otherNames") or []:
                raw_aliases.extend(part.strip() for part in str(other).split(","))
            _add(buckets["intervention"], "intervention", name,
                 tuple(a for a in raw_aliases if a))
    print(f"  scanned {seen_studies:,} studies")
    return buckets


def _add(bucket: dict[str, dict], kind: str, surface: str, aliases: tuple[str, ...]) -> None:
    surface = surface.strip()
    key = normalize(surface, kind)  # type: ignore[arg-type]
    if not key or len(key) < 2:
        return
    entry = bucket.setdefault(key, {"surfaces": Counter(), "aliases": Counter(), "docs": 0})
    entry["surfaces"][surface] += 1
    entry["docs"] += 1
    for alias in aliases:
        alias = alias.strip()
        # An "other name" identical to the label carries no bridging value.
        if alias and normalize(alias, kind) != key:  # type: ignore[arg-type]
            entry["aliases"][alias] += 1


def build_entities(bucket: dict[str, dict], cap: int) -> tuple[list[dict], dict]:
    """Rank by document frequency, apply the cap, and report the cut."""
    ranked = sorted(bucket.items(), key=lambda kv: (-kv[1]["docs"], kv[0]))
    eligible = [(k, v) for k, v in ranked if v["docs"] >= MIN_DOC_FREQ]
    kept = eligible[:cap]
    entities = [
        {
            # Canonical label is the most frequent surface form the registry
            # actually uses, not a prettier string we invented.
            "label": entry["surfaces"].most_common(1)[0][0],
            "aliases": [a for a, _ in entry["aliases"].most_common(MAX_ALIASES)],
            "doc_freq": entry["docs"],
        }
        for _key, entry in kept
    ]
    report = {
        "distinct_seen": len(ranked),
        "below_min_doc_freq": len(ranked) - len(eligible),
        "min_doc_freq": MIN_DOC_FREQ,
        "eligible": len(eligible),
        "cap": cap,
        "kept": len(kept),
        "dropped_to_cap": max(0, len(eligible) - len(kept)),
        "capped": len(eligible) > cap,
        "min_doc_freq_kept": kept[-1][1]["docs"] if kept else 0,
    }
    return entities, report


def document(entity: dict) -> str:
    """Text indexed/embedded for an entity: canonical label plus registry aliases.

    Including the aliases is what makes brand-to-generic resolution work on
    evidence instead of recall — the Pembrolizumab document literally contains
    "Keytruda" because thirty-odd registered studies say it does.
    """
    aliases = ", ".join(entity["aliases"])
    return f"{entity['label']} | {aliases}" if aliases else entity["label"]


def embed(texts: list[str], model_name: str):
    """Static embeddings via model2vec. Build-time only; returns None on failure.

    Degrading to FTS-only is a supported outcome, not an error: the lexical
    retriever is the load-bearing one, and the service must run with no
    network and no model.
    """
    try:
        import numpy as np
        from model2vec import StaticModel
    except ImportError as exc:
        print(f"  model2vec unavailable ({exc}); building FTS-only")
        return None
    try:
        model = StaticModel.from_pretrained(model_name)
    except Exception as exc:  # noqa: BLE001
        print(f"  could not fetch {model_name} ({type(exc).__name__}: "
              f"{str(exc)[:120]}); building FTS-only")
        return None
    out = []
    for start in range(0, len(texts), EMBED_BATCH):
        out.append(model.encode(texts[start : start + EMBED_BATCH]))
        print(f"    embedded {min(start + EMBED_BATCH, len(texts))}/{len(texts)}", flush=True)
    matrix = np.vstack(out).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    # float16 on disk: halves the BLOB column for ~3e-4 of cosine precision,
    # which cannot move a top-5 ranking.
    return (matrix / norms).astype(np.float16)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP,
                        help="max entities kept per kind")
    parser.add_argument("--model", default=SEMANTIC_MODEL)
    parser.add_argument("--no-semantic", action="store_true",
                        help="build the FTS5 index only")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    if not fts5_available():
        raise SystemExit(
            "FATAL: this Python's SQLite was compiled WITHOUT FTS5, so the entity "
            "index cannot be built. Verify with:\n"
            "  python -c \"import sqlite3; sqlite3.connect(':memory:')"
            ".execute('CREATE VIRTUAL TABLE t USING fts5(a)')\"\n"
            "Install a Python whose sqlite3 has FTS5 enabled and re-run."
        )

    print(f"Paging ClinicalTrials.gov: up to {args.pages} x {args.page_size} studies")
    buckets = harvest(page_studies(args.pages, args.page_size))

    args.out.mkdir(parents=True, exist_ok=True)
    db_path = args.out / "entities.db"
    db_path.unlink(missing_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)

    caps: dict[str, int] = {}
    cap_report: dict[str, dict] = {}
    semantic_ok = not args.no_semantic
    rowid = 0

    for kind in KINDS:
        entities, report = build_entities(buckets[kind], args.cap)
        caps[kind] = args.cap
        cap_report[kind] = report
        print(
            f"\n{kind}: saw {report['distinct_seen']:,} distinct normalised forms; "
            f"{report['below_min_doc_freq']:,} appeared on <{MIN_DOC_FREQ} studies; "
            f"{report['eligible']:,} eligible; CAP={args.cap} -> kept {report['kept']:,}, "
            f"dropped {report['dropped_to_cap']:,} "
            f"(tail cut at doc_freq={report['min_doc_freq_kept']})"
        )
        if not entities:
            raise SystemExit(f"no {kind} entities harvested; refusing to write an empty index")

        vectors = None
        if semantic_ok:
            print(f"  embedding {len(entities):,} {kind} documents with {args.model}")
            vectors = embed([document(e) for e in entities], args.model)
            if vectors is None:
                semantic_ok = False

        for position, entity in enumerate(entities):
            rowid += 1
            norm_canonical = normalize(entity["label"], kind)  # type: ignore[arg-type]
            norm_aliases = [normalize(a, kind) for a in entity["aliases"]]  # type: ignore[arg-type]
            norm_all = " ".join([norm_canonical, *norm_aliases])
            connection.execute(
                "INSERT INTO entities (rowid, kind, canonical, other_names, "
                "norm_canonical, norm_all, doc_freq, vector) VALUES (?,?,?,?,?,?,?,?)",
                (
                    rowid, kind, entity["label"], " | ".join(entity["aliases"]),
                    norm_canonical, norm_all, entity["doc_freq"],
                    vectors[position].tobytes() if vectors is not None else None,
                ),
            )
            for norm_alias in {a for a in norm_aliases if a and a != norm_canonical}:
                connection.execute(
                    "INSERT INTO entity_aliases (entity_rowid, kind, norm_alias) "
                    "VALUES (?,?,?)",
                    (rowid, kind, norm_alias),
                )

    connection.commit()
    connection.execute("VACUUM")
    connection.commit()

    # Prove the triggers actually populated the external-content index rather
    # than trusting that they did.
    fts_rows = connection.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
    src_rows = connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    if fts_rows != src_rows:
        raise SystemExit(f"FTS index out of sync: {fts_rows} indexed vs {src_rows} rows")
    print(f"\nFTS5 index in sync: {fts_rows:,} rows")
    connection.close()

    manifest = {
        "source": "clinicaltrials.gov/api/v2",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": "fts+semantic" if semantic_ok else "fts-only",
        "retrieval": "sqlite fts5 + bm25 (external content)"
                     + (" | model2vec static embeddings" if semantic_ok else ""),
        "semantic_model": args.model if semantic_ok else None,
        "pages": args.pages,
        "page_size": args.page_size,
        "caps": caps,
        "cap_report": cap_report,
        "note": (
            "Retrieval PROPOSES entities. It never carries a count. Every "
            "number reaching a response comes from a count probe issued at "
            "request time by app.entities.verify_candidates."
        ),
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    size_mb = db_path.stat().st_size / 1e6
    print(f"\nWrote {db_path} ({size_mb:.2f} MB)")
    print(json.dumps({k: v for k, v in manifest.items() if k != "cap_report"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
