"""dcgrade — the open verifier an agent's IFC4 submission is scored by.

    from dc_model.dcgrade.score import score_task_a
    report = score_task_a("submission.ifc", "ground_truth.ifc")
    report.score, report.recovery_confusion, report.compliance

Or from a shell::

    python -m dc_model.dcgrade.score \\
        --submission out.ifc --ground-truth ground_truth.ifc --task A

Two modules, split so that the rules can be tested without ifcopenshell:

``rules``
    the sourced rule battery over plain geometry (``rules.Scene``).
``score``
    IFC reading, the gates, the Hungarian recovery matcher, and the two task
    scores.

Nothing is re-exported here, matching ``dcgen``'s empty package init: a package
``__init__`` that imports its own CLI submodule makes ``python -m
dc_model.dcgrade.score`` emit a runpy "found in sys.modules" warning on
every grading run.

The grader is *open* — agents may read it. That is deliberate: every threshold
traces to a published standard, so reading the grader teaches the standard
rather than the grader's quirks.
"""
