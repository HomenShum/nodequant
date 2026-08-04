# Prospective alpha evidence

## Current decision

The August 2026 v3 packet is **frozen and awaiting future outcomes**. It is not
an alpha result. Its purpose is to make the next return window genuinely future
relative to a content-addressed forecast, rather than opening another historical
universe and calling it out of sample.

The primary forecast uses the v4 panel winner,
`composite_momentum__top_n-3`. From the July 31 signal cutoff it assigns one
third each to SPTM, EWC, and ICF, and zero to BWX and DBB. The benchmark is an
equal-weight allocation across all five risky ETFs. Entry is the first common
close strictly after the packet seal; measured returns begin with the following
close-to-close interval.

## Frozen receipt

- Policy: `sha256:d163fe7a4d68e9a91fea32c1497081595f3dc4086ed7217db9bada35d235c451`
- Packet: `sha256:2eaa1e24d1501e205efcdb8fcce5aa6c0df09d512fcd803bb9ae12042143a5df`
- Source v4 result: `sha256:8534370821ef4537fb242c135f0bf22d4c407007f21a62408f9e5548cd7de384`
- Signal cutoff: 2026-07-31
- Sealed at: 2026-08-04T06:33:44.182299+00:00
- External Notion anchor: 2026-08-04T06:34:02.985Z
- Settlement policy: `sha256:9b231159cc7a1afb1071fd754d05ee3e084fc0b2f050d12c03194f6e41d65acf`
- Settlement evaluator: `sha256:3ab1114c5f9857fc37d2c457fab441fd0a808aab27f9dc748efd8b01264138a6`
- Settlement external anchor: 2026-08-04T06:42:55.962Z
- Promotion ceiling: `PROSPECTIVE_DIAGNOSTIC_ONLY_NO_LIVE_CAPITAL`

The three risk-weighted comparison candidates currently collapse to the same
weights. This is recorded as redundancy, not three independent confirmations.

## Gate

No evaluation is eligible before 24 complete packets and 504 future sessions.
The primary strategy must beat the benchmark in at least 14 packets and the
paired 20-day block-bootstrap 95% lower bound on annual active return must be
positive. Every packet remains registered regardless of outcome.

The settlement equation is also frozen before entry. It selects the first common
20:00 UTC close after the later of packet seal and settlement-policy freeze,
starts measurement with the following close-to-close interval, and ends at the
first common close in the next calendar month. Entry and exit each charge 10 bp
on risky notional, cash accrues from BIL, and weights do not rebalance inside the
packet. The runner currently exits 5 with `AWAITING_FUTURE_OUTCOMES` and writes
no result because no eligible future close is present.

## Invalidated v1

The first two packets are preserved but invalidated. Yahoo returned an August 3 row
with a null adjusted close. The first implementation therefore chose June 30 as
the completed cutoff and added the packet timestamp after hashing. V2 recognizes
July 31 from the calendar boundary and binds the seal time into packet identity.
V2 then failed adversarial write-once and chronology checks: it trusted a stored
declared hash without rehashing the body, used a non-exclusive create, inferred
weekend month ends incorrectly, and declared the policy freeze after packet seal.
V3 freezes the exact cutoff, verifies stored bytes, creates exclusively, and
enforces policy-freeze-before-packet ordering.

## Reproduction

```powershell
uv run --no-sync pytest -q tests\test_prospective_alpha.py tests\test_alpha_breadth.py tests\test_real_alpha.py
uv run --no-sync pytest -q tests\test_prospective_settlement.py
uv run --no-sync python -m app.prospective_alpha `
  --policy research\prospective_alpha_policy_v3.json `
  --raw-directory evidence\prospective-alpha\raw `
  --output evidence\prospective-alpha-v3\packet-2026-08.json `
  --reuse-raw
uv run --no-sync python -m app.prospective_settlement `
  --policy research\prospective_settlement_policy_v1.json `
  --raw-directory evidence\prospective-alpha\raw `
  --reuse-raw
```
