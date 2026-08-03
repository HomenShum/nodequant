# Evaluation report

Generated 2026-08-03T05:09:42+00:00 · planner `structured` · model `gpt-5.4-mini`
Golden set generated 2026-08-03T02:57:28+00:00 (2.0h before this run).

## Aggregate

| Metric | Value |
|---|---|
| Cases | 31 (0 errored) |
| **Hard pass** (all hard metrics) | **31/31 — 100.0%** |
| Count accuracy (cases) | 100.0% |
| Numbers checked / wrong | 331 / 0 |
| **Trace replay pass rate** | **100.0%** (339/339 steps) |
| Trace coverage (replayable / all steps) | 100.0% (0 non-replayable) |
| Routing — dimension | 100.0% |
| Routing — viz type | 100.0% |
| No fabricated numbers | 100.0% |
| Schema valid | 100.0% |
| Exclusivity correct | 100.0% |
| Median case latency | 27.79s |

## Per case

| Case | Dim (want→got) | Viz | Counts | Replay | Fab | Excl | Hard | s |
|---|---|---|---|---|---|---|---|--:|
| `phase_melanoma` | phase→phase ok | bar_chart ok | ok | 7/7 | ok | ok | PASS | 19.01 |
| `phase_pembrolizumab` | phase→phase ok | bar_chart ok | ok | 7/7 | ok | ok | PASS | 20.78 |
| `phase_glioblastoma` | phase→phase ok | bar_chart ok | ok | 7/7 | ok | ok | PASS | 19.14 |
| `phase_nsclc_recruiting` | phase→phase ok | bar_chart ok | ok | 7/7 | ok | ok | PASS | 20.96 |
| `phase_pfizer` | phase→phase ok | bar_chart ok | ok | 7/7 | ok | ok | PASS | 19.52 |
| `phase_covid_windowed` | phase→phase ok | bar_chart ok | ok | 7/7 | ok | ok | PASS | 19.76 |
| `status_melanoma` | status→status ok | bar_chart ok | ok | 9/9 | ok | ok | PASS | 27.79 |
| `status_pembrolizumab` | status→status ok | bar_chart ok | ok | 9/9 | ok | ok | PASS | 21.01 |
| `status_breast_cancer` | status→status ok | bar_chart ok | ok | 9/9 | ok | ok | PASS | 31.12 |
| `status_alzheimers` | status→status ok | bar_chart ok | ok | 9/9 | ok | ok | PASS | 29.11 |
| `year_glioblastoma` | year→year ok | time_series ok | ok | 8/8 | ok | ok | PASS | 21.13 |
| `year_pembrolizumab` | year→year ok | time_series ok | ok | 9/9 | ok | ok | PASS | 24.94 |
| `year_covid` | year→year ok | time_series ok | ok | 6/6 | ok | ok | PASS | 12.17 |
| `year_melanoma` | year→year ok | time_series ok | ok | 9/9 | ok | ok | PASS | 32.76 |
| `country_pancreatic_recruiting` | country→country ok | bar_chart ok | ok | 13/13 | ok | ok | PASS | 47.81 |
| `country_melanoma` | country→country ok | bar_chart ok | ok | 13/13 | ok | ok | PASS | 52.37 |
| `country_diabetes` | country→country ok | bar_chart ok | ok | 13/13 | ok | ok | PASS | 50.25 |
| `itype_melanoma` | intervention_type→intervention_type ok | bar_chart ok | ok | 10/10 | ok | ok | PASS | 28.09 |
| `itype_alzheimers` | intervention_type→intervention_type ok | bar_chart ok | ok | 10/10 | ok | ok | PASS | 26.93 |
| `total_pembrolizumab` | none→none ok | single_value ok | ok | 1/1 | ok | ok | PASS | 1.37 |
| `total_melanoma_recruiting` | none→none ok | single_value ok | ok | 1/1 | ok | ok | PASS | 1.7 |
| `total_novartis` | none→none ok | single_value ok | ok | 1/1 | ok | ok | PASS | 1.57 |
| `total_glioblastoma_since_2020` | none→none ok | single_value ok | ok | 1/1 | ok | ok | PASS | 1.7 |
| `compare_pembro_nivo` | phase→phase ok | grouped_bar_chart ok | ok | 13/13 | ok | ok | PASS | 38.92 |
| `compare_melanoma_nsclc_status` | status→status ok | grouped_bar_chart ok | ok | 17/17 | ok | ok | PASS | 48.94 |
| `compare_pfizer_novartis_phase` | phase→phase ok | grouped_bar_chart ok | ok | 13/13 | ok | ok | PASS | 37.47 |
| `sponsor_melanoma` | sponsor→sponsor ok | bar_chart ok | ok | 11/11 | ok | ok | PASS | 41.84 |
| `sponsor_lung_cancer` | sponsor→sponsor ok | bar_chart ok | ok | 11/11 | ok | ok | PASS | 40.44 |
| `network_sponsor_drug_melanoma` | sponsor_drug_network→sponsor_drug_network ok | network_graph ok | ok | 37/37 | ok | ok | PASS | 99.75 |
| `network_drug_drug_nsclc` | drug_drug_network→drug_drug_network ok | network_graph ok | ok | 27/27 | ok | ok | PASS | 74.66 |
| `network_sponsor_drug_pembrolizumab` | sponsor_drug_network→sponsor_drug_network ok | network_graph ok | ok | 37/37 | ok | ok | PASS | 105.36 |

