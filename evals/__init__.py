"""Evaluation harness for the ClinicalTrials.gov query-to-visualization agent.

Nothing in this package imports `app.ctgov`, `app.executor`, or `app.planner`
for the purpose of *measuring* anything. The probe client here
(`evals.api`) is a deliberately separate, minimal implementation so that a
bug in the system under test cannot also corrupt the ground truth it is
scored against.

`evals.run_eval` does import the app — but only to *run* the pipeline. Every
number it compares against is produced by `evals.api`.
"""
