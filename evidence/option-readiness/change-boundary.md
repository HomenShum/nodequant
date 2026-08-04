# Option readiness proof boundary

## Before

Options requirements existed only in prose. There was no typed snapshot schema,
named conformance counters or visible distinction between absent evidence and a
measured zero.

## Intended after

Validate bounded point-in-time contract definitions and quotes, measure each
named failure class, and render absent inputs as `NOT_RUN`.

## Out of scope

- No OPRA/OCC corpus or redistribution right is present locally.
- Caller-supplied quotes can be diagnosed but never become option authority.
- Candidate selection, Greeks validation, exercise and assignment remain
  blocked until provider replay and point-in-time definitions are verified.
