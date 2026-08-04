"""Compiled local trust root for the graph-research controller.

This is deliberately outside optimizer-writable ``research/policy.json``.  It
prevents a caller from supplying a self-consistent hostile policy, but it is
still only a local code anchor; signed external run-controller authority is a
documented promotion blocker.
"""

EXPECTED_POLICY_HASH = "sha256:8e8c4d303115f1085507935ad691818037437f5b079c4f1e8bb0f81801caa4bf"
EXPECTED_IMMUTABLE_FILES = frozenset(
    {
        "app/proof_kernel.py",
        "app/experiment.py",
        "app/robustness.py",
        "app/walk_forward.py",
        "app/promotion.py",
        "app/research_evaluator.py",
        "app/research_graph.py",
        "app/research_loop.py",
        "experiment/manifest.json",
    }
)
