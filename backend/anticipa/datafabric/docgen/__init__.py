"""Deterministic synthetic clinical-document generator (templates only, no model calls).

    uv run --directory backend python -m anticipa.datafabric.docgen generate --dataset-version V --residents N|all --seed S

Public entry points are imported lazily by callers (`anticipa.datafabric.docgen.generate.generate`,
`.groundtruth.verify_ground_truth`); importing this package stays cheap so the job worker can load `handlers`.
"""
