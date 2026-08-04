# Source compiler proof boundary

## Before

The missing original strategy snapshot was described only in documentation.
There was no byte-span compiler or visible distinction between absent clauses
and a measured zero ambiguities.

## Intended after

Bind literal clauses to exact UTF-8 byte spans, preserve contradictory and
underspecified interpretations as explicit variants, and render the absent
source and human lock as `NOT_RUN`.

## Out of scope

- The original Notion strategy snapshot is not in this checkout.
- Code cannot infer which ambiguous interpretation the user intended.
- Literal, reconstructed and repaired versions remain blocked until an
  immutable snapshot and separately evidenced human lock exist.
