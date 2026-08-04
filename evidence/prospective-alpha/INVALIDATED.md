# Invalidated prospective packet v1

`packet-2026-08.json` is retained only as defect evidence. The provider returned
an August 3 timestamp with a null adjusted close. The v1 cutoff detector therefore
selected June 30 instead of the already completed July 31 month end. It also added
`created_at_utc` after computing `packet_hash`, leaving the entry-time authority
outside the sealed identity. No outcome or strategy claim may use this packet.

