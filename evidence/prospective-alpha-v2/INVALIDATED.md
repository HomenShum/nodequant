# Invalidated prospective packet v2

The weights and external Notion anchor were numerically correct, but v2 cannot
act as prospective authority. `write_once` trusted the declared hash of an
existing body and used an exists-then-write sequence. Its completed-month rule
also failed when month end fell on a weekend, and the policy's declared freeze
time followed the packet's internal seal time. V3 supersedes this artifact.

