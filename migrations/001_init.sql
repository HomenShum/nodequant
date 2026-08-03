-- 001_init.sql — schema for the optional Postgres persistence layer.
--
-- Two tables, and deliberately only two. The rule applied: state is justified
-- when it is EXPENSIVE TO RE-DERIVE and SAFE TO REPLAY. Everything else in this
-- service (plans, rendered specs, traces) is cheap to recompute and derives
-- from numbers that must be re-measured anyway, so persisting it would only
-- create a second thing that can disagree with the first.
--
-- Idempotent: every statement is IF NOT EXISTS, so re-running is a no-op.
-- Applied automatically on first boot by docker-compose (mounted into
-- /docker-entrypoint-initdb.d), or manually — see docker-compose.yml.

-- ---------------------------------------------------------------------------
-- probe_cache
-- ---------------------------------------------------------------------------
--
-- One row per count-probe this system actually measured from the
-- ClinicalTrials.gov v2 API. This is the shared-process version of
-- app/ctgov.py's in-process `_BoundedCache`, which dies with the worker and so
-- shares nothing across instances that share an upstream rate limit.
--
-- The invariant this schema is shaped around: a cached value is legitimate only
-- if it is a number THIS SYSTEM measured and can still replay. Hence:
--
--   * `total_count` is named after the API's own `totalCount` field. It holds
--     that field verbatim. It is never a model output, an estimate, or a
--     default. There is no DEFAULT on this column on purpose — a row with no
--     measured count must be impossible to write.
--   * `params` stores the exact querystring that produced the count, so any
--     cached value remains replayable against the live API. A count you cannot
--     re-derive the request for is an unverifiable number.
--   * `measured_at` is the time of MEASUREMENT, not the time of insert, and it
--     is what TTL is computed against. `written_at` records the insert
--     separately so an upsert cannot quietly refresh the age of a stale count.
--   * `source` names who measured it, so a future second source cannot have its
--     counts silently read back as ClinicalTrials.gov counts.
CREATE TABLE IF NOT EXISTS probe_cache (
    -- Normalised filter key: "k=v&k=v" over sorted parameter names. Sorted so
    -- two logically identical filters built in different orders collide on one
    -- row instead of paying two upstream probes. Matches _BoundedCache.key.
    cache_key   text        PRIMARY KEY,

    -- The API's totalCount, verbatim. No default: see above.
    total_count bigint      NOT NULL CHECK (total_count >= 0),

    -- The exact parameters that produced total_count. Keeps the value replayable.
    params      jsonb       NOT NULL,

    -- When the API was actually asked. TTL is measured from here.
    measured_at timestamptz NOT NULL,

    -- Which upstream produced this number.
    source      text        NOT NULL,

    -- When this row was last written. Distinct from measured_at so that
    -- re-writing a row can never make an old measurement look fresh.
    written_at  timestamptz NOT NULL DEFAULT now()
);

-- Reads are `WHERE cache_key = $1 AND measured_at > now() - ttl`; the primary
-- key handles the lookup. This index serves the other access path: the bounded
-- sweep that deletes expired rows. Without it the sweep is a sequential scan
-- over a table keyed on user-controlled filters.
CREATE INDEX IF NOT EXISTS probe_cache_measured_at_idx
    ON probe_cache (measured_at);

-- Serves "what has this source measured recently" — the operational question
-- when a source is suspected of drift, and the per-source purge path.
CREATE INDEX IF NOT EXISTS probe_cache_source_measured_at_idx
    ON probe_cache (source, measured_at DESC);


-- ---------------------------------------------------------------------------
-- entity_alias
-- ---------------------------------------------------------------------------
--
-- The offline entity index the README names as "what breaks first" when this
-- generalises to a multi-source knowledge graph: `Merck Sharp & Dohme LLC`,
-- `Merck & Co.` and `MSD` are one entity; `Keytruda` and `Pembrolizumab` are
-- one drug.
--
-- This table is a PROPOSAL store, not a truth store. A row here says "this
-- string may denote this entity". It never carries a trial count, and nothing
-- in it may be presented as a measured number — resolution decisions still have
-- to be probe-verified through app/ctgov.py's count(), on the same principle as
-- every other number in this system. The absence of any count column is the
-- enforcement: there is nowhere here for an unverified number to live.
--
-- Owned by app/entities.py (built by another agent). This migration only
-- provides the table and the documented shape; nothing in app/store.py assumes
-- entities.py uses it.
CREATE TABLE IF NOT EXISTS entity_alias (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- entities.py's `Kind`. Constrained rather than free text so a typo cannot
    -- create a silent third namespace nothing ever reads back.
    kind          text        NOT NULL CHECK (kind IN ('intervention', 'sponsor')),

    -- The label this entity is canonically indexed under.
    canonical     text        NOT NULL,

    -- One surface form for that entity, as it appears in a registry or query.
    alias         text        NOT NULL,

    -- entities.py's normalize(alias, kind) output. The lookup key. Stored
    -- rather than computed in SQL because normalisation is Python-side domain
    -- logic (dose stripping, legal-suffix folding) that must not be duplicated
    -- in two languages where the two copies can drift apart.
    alias_norm    text        NOT NULL,

    -- Optional embedding: raw little-endian float32, `embedding_dim` values.
    -- bytea rather than a vector type so the schema needs no pgvector
    -- extension; this table is an index to look aliases up in, not an ANN
    -- store. NULL is a legitimate state — an alias known only lexically.
    embedding     bytea,
    embedding_dim integer,

    -- Where this alias came from: 'offline-index', 'registry', 'probe-verified'.
    -- An alias that was confirmed by a count probe is worth more than one a
    -- model proposed, and a reader must be able to tell them apart.
    source        text        NOT NULL DEFAULT 'offline-index',

    updated_at    timestamptz NOT NULL DEFAULT now(),

    -- One row per (kind, normalised alias): the upsert target, and the
    -- structural guarantee that one surface form resolves to one entity.
    CONSTRAINT entity_alias_kind_alias_norm_key UNIQUE (kind, alias_norm),

    -- An embedding without its dimension is unreadable bytes; a dimension
    -- without an embedding is a lie about what is stored.
    CONSTRAINT entity_alias_embedding_dim_paired
        CHECK ((embedding IS NULL) = (embedding_dim IS NULL)),

    -- float32 == 4 bytes. Catches a dimension mismatch at write time rather
    -- than as garbage cosine scores at query time.
    CONSTRAINT entity_alias_embedding_length
        CHECK (embedding IS NULL OR octet_length(embedding) = embedding_dim * 4)
);

-- Reverse lookup: every alias of a given canonical entity.
CREATE INDEX IF NOT EXISTS entity_alias_kind_canonical_idx
    ON entity_alias (kind, canonical);

-- Operational: "which aliases are model-proposed rather than probe-verified".
CREATE INDEX IF NOT EXISTS entity_alias_source_idx
    ON entity_alias (source);
