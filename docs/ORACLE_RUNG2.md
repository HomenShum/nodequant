# Frozen-spec replay: fresh-agent rung

## Decision

A fresh agent given only the frozen manifest, raw fixture and output schema
produced a separate PowerShell/.NET implementation whose 25 signals match the
existing replay semantically. This materially strengthens implementation
diversity, but it does **not** clear the promotion blocker because local
authorship metadata cannot prove read isolation cryptographically.

## Bounded assignment and result

- Canonical task: `/root/oracle_rung2`, created without parent-turn context.
- Allowed reads: `independent_oracle/spec/manifest.json`, `bars.csv`, and
  `output-schema.json` only.
- Allowed write root: `independent_oracle/rung2/implementation` only.
- Implementation: PowerShell 7 and .NET standard library; no project import.
- Five scenario groups passed: cold start, rising edges/execution fallback,
  sustained bounds, malformed rows, and full-corpus determinism/schema.
- Output: 25 bounded signals; exact semantic equality to the prior replay.
- Parent verification reran the agent tests, searched the implementation for
  forbidden project references, and reran promotion/oracle tests.

The implementation and output hashes live in
`evidence/oracle-rung2/receipt.json`; the agent-authored statement remains in
`independent_oracle/rung2/implementation/authorship.json`.

## Honest provenance ceiling

The current orchestrator observed a fresh task with no forked conversation and
enforced the bounded instruction, but the filesystem artifact contains no OS
audit log, sandbox attestation, signed identity or external build receipt that
can independently prove which files were read. Therefore the correct status is
`SEPARATE_AGENT_REPLAY_MATCHED_PROVENANCE_NOT_MACHINE_VERIFIABLE`.

`EXTERNAL_BAR_SNAPSHOT` remains absent from promotable evidence, and this local
receipt must not be relabelled `REAL_BAR_REPLAY` or
`separate_agent_from_frozen_spec` by caller assertion alone.

