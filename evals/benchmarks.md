## External benchmarks: what was checked, and why none of it is used

The obvious question about a self-generated golden set is whether an existing
public benchmark would have been better. Four candidate families were checked
by opening the actual track pages, topic files and dataset cards rather than
recalling them. None fits, and the reason is the same in every case: they label
*which documents are relevant*, and this system reports *how many documents
exist per bucket*.

### TREC Clinical Trials Track (2021, 2022, 2023) — not usable

Pages read: `trec-cds.org` track index and the 2021/2022/2023 pages;
`trec.nist.gov/data/trials2022.html` and `trials2023.html`; the raw topic files
`trec.nist.gov/data/trials/topics2021.xml` and `topics2023.xml`;
`ir-datasets.com/clinicaltrials.html`.

* **The task is patient-to-trial matching, not question answering.** A topic is
  a patient — a synthetic admission note in 2021/2022, a structured
  questionnaire in 2023 — and a run is a ranked list of up to 1000 NCT IDs
  scored with NDCG.
* **Topic counts, read from the files:** 75 (2021), 50 (2022), 40 (2023, five
  each across eight disorders).
* **The labels are per-document relevance**, three-level: "Judgment of 0 is
  non-relevant, 1 is excluded, and 2 is eligible." There is no expected count,
  no expected grouping dimension, and no chart target anywhere in the qrels.
* **Qrels are pooled**, so a topic's judged set is a biased sample of the
  top-ranked documents, not a complete census. Any count derived from them
  would be systematically low — the exact failure mode this system is built to
  avoid.
* **The corpus is a frozen snapshot** (27 Apr 2021 for the 2021/2022 editions,
  8 May 2023 for 2023). Counts derived from a snapshot can never match a live
  v2 `totalCount`, so even a hypothetical count label would not be checkable
  against what this service returns.

Partially usable in one narrow way, not exercised here: the 2023 questionnaire
topics are a good source of realistic clinical vocabulary for *authoring* count
questions, and the frozen corpus would be a defensible fixture if reproducibility
mattered more than currency. The gold counts would still have to be computed,
which is what `evals/golden.py` does anyway.

### BioASQ — not usable

Pages read: `bioasq.org/participate/challenges_year_13`,
`participants-area.bioasq.org/datasets/`, the BioASQ-QA corpus paper
(PMC10042099), and the BioASQ 2024 overview (arXiv 2508.20532).

Wrong corpus and wrong answer type. The document collection is the PubMed
annual baseline — ClinicalTrials.gov is not part of BioASQ at all. The four
question types are yes/no, factoid, list, and summary; "list" answers are sets
of named entities, not counts, and the published typology admits no numeric
answer type. There is no adaptation path; this is not a near miss.

### nvBench / nvBench 2.0 — partially usable, wrong domain

The closest thing to an NL→visualization gold standard that exists: 25,750
(NL, VIS) pairs over 780 tables in v1, and 7,878 NL queries → 24,076
visualizations with deliberate ambiguity in v2. Its label form — a spec with an
embedded aggregate query — is structurally the same shape as this system's
output. But its databases are the general-domain Spider tables; there is no
clinical-trial domain, so it can inform *metric design* and nothing about count
correctness. nvBench 2.0's F1@k over multiple valid interpretations is the
right idea for grading ambiguous routing, and is a reasonable next step for the
routing metric here.

### Spider / BIRD / BiomedSQL — methodology only

BIRD and Spider contribute the pattern this harness already follows: grade by
*execution*, comparing result sets rather than query strings. BiomedSQL is the
nearest in task shape — text-to-SQL with numeric aggregate gold answers — but
over Open Targets and ChEMBL, not ClinicalTrials.gov.

### Why generated ground truth is the better fit

For a count question the correct answer is not a matter of opinion: it is
definitionally what the API returns for the equivalent filter. That removes the
annotation step entirely — only the *question → filter and dimension* mapping
needs a human, and that is what `evals/cases.py` contains. Three consequences
follow, and they are the reason this approach beats an adapted benchmark:

1. **The ground truth is exact, not pooled.** No sampling bias, no incomplete
   judgments.
2. **It tracks the live registry.** A benchmark snapshot goes stale the day it
   is cut; this regenerates in about four minutes.
3. **It is falsifiable end to end.** Trace replay re-issues the system's own
   recorded requests, so the claim "every number is a measurement" is tested
   directly rather than approximated by an accuracy score.

The cost is that this measures *this* system against *this* API. It is not a
leaderboard number and is not comparable to anyone else's. For the property
under test — that no number in a response was invented — that trade is worth
making.

_No dataset above was described from memory; each verdict comes from a page
opened during this work. Where a detail could not be verified on a page, it is
absent rather than guessed._
