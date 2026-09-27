"""Shared fixtures for the Models and Analyses app tests.

All tests use the SHIPPED fake store (:class:`FakeKorosArcStore` from
``kbutillib.koros_arc_store_testing``) — never a hand-written double (binding
T4). The fake enforces the same validation rules as the real store, so a test
that passes against it cannot pass against behaviour the real store rejects, and
the tests never touch a real runs tree.

The helpers here only BUILD valid records and seed them through the fake's own
``record_analysis`` / ``seed_arc`` / ``seed_project`` API — they never
reimplement the store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from kbutillib.koros_arc_store import (
    AnalysisRecord,
    ArcProvenance,
    ArcRecord,
    ProjectRecord,
    derive_analysis_id,
    derive_record_id,
)
from kbutillib.koros_arc_store_testing import FakeKorosArcStore

# A fixed, well-formed timestamp (S32: YYYY-MM-DDTHH:MM:SS.ffffffZ).
TS = "2026-09-20T10:00:00.000000Z"


def make_arc(project: str, slug: str, **prov_kwargs: Any) -> ArcRecord:
    """Build a valid :class:`ArcRecord` with parsed provenance."""
    provenance = ArcProvenance(run_id="run-" + slug, created_at=TS, **prov_kwargs)
    return ArcRecord(
        project=project,
        slug=slug,
        path=Path(f"/runs/{project}/arcs/{slug}"),
        provenance=provenance,
        valid=True,
        invalid_reason=None,
    )


def make_record(
    kind: str,
    subject: str,
    *,
    model_uri: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    trust_tier: str = "hypothesis",
    status: str = "ok",
    created_at: str = TS,
    run_uid: str = "u1",
    significant_params: Optional[Dict[str, Any]] = None,
) -> AnalysisRecord:
    """Build a valid :class:`AnalysisRecord` using the SHARED identity helpers.

    ``model_uri`` becomes the ``artifacts.model_id`` value and MUST be a valid
    artifact URI the store accepts (an absolute path, ``file://…`` or
    ``obj://<id>``). The ``model:<ns>:<id>`` grouping key the app uses is derived
    from it. A non-``verified`` tier gets a minimal valid provenance dict (the
    store rejects a non-verified record with empty provenance).
    """
    params = significant_params or {}
    analysis_id = derive_analysis_id(kind, subject, params)
    record_id = derive_record_id(analysis_id, run_uid)
    artifacts = {"model_id": model_uri} if model_uri else {}
    if trust_tier == "verified":
        provenance: Dict[str, Any] = {}
    else:
        provenance = {
            "bridge_kind": "model_prediction",
            "metric": {"name": "confidence", "value": 1},
        }
    return AnalysisRecord(
        record_id=record_id,
        analysis_id=analysis_id,
        run_uid=run_uid,
        kind=kind,
        created_at=created_at,
        producer="test-producer",
        producer_version="1.0",
        subject=subject,
        status=status,
        artifacts=artifacts,
        payload=payload,
        trust_tier=trust_tier,
        provenance=provenance,
        contract_version=1,
    )


@pytest.fixture
def store() -> FakeKorosArcStore:
    """An empty shipped fake store (enabled) — the ONLY store used in these tests."""
    return FakeKorosArcStore()


@pytest.fixture
def seeded_store() -> FakeKorosArcStore:
    """A fake store seeded with a representative multi-arc portfolio.

    Layout:
      projA:
        main   — a model build + a measured fitness analysis + a foreign
                 annotation record (owned + foreign mix)
        leg1   — leg_of=main, only foreign annotation records (zero models)
        empty  — no records at all (flagged empty)
      projB:
        solo   — one FBA run, one gapfill on a second model
      unattributed — one FBA run with no project/arc
    """
    s = FakeKorosArcStore()
    s.seed_project(ProjectRecord(name="projA", path=Path("/runs/projA"), arc_count=3))
    s.seed_project(ProjectRecord(name="projB", path=Path("/runs/projB"), arc_count=1))

    s.seed_arc(make_arc("projA", "main", run_name="Main arc", role="anchor"))
    s.seed_arc(make_arc("projA", "leg1", run_name="Leg 1", leg_of="main"))
    s.seed_arc(make_arc("projA", "empty", run_name="Empty arc"))
    s.seed_arc(make_arc("projB", "solo", run_name="Solo arc"))

    # projA/main: model build, measured fitness (perfect concordance), foreign.
    s.record_analysis(
        "projA",
        "main",
        make_record(
            "kbdl.model_build",
            "genomeX",
            model_uri="obj://mX",
            payload={
                "model_name": "E. coli X",
                "genome": "genomeX",
                "template": "GramNegative",
                "reaction_count": 1200,
                "gene_count": 900,
            },
        ),
    )
    s.record_analysis(
        "projA",
        "main",
        make_record(
            "kbdl.fitness_analysis",
            "genomeX",
            model_uri="obj://mX",
            payload={
                "gene_agreement": [
                    {"reference_class": "essential-forward", "measured_fitness": -2.0},
                    {"reference_class": "active", "measured_fitness": 0.1},
                    {"reference_class": "essential", "measured_fitness": -3.0},
                    {"reference_class": "blocked", "measured_fitness": 0.2},
                    # a gene with no measured fitness — must be EXCLUDED, not TN
                    {"reference_class": "active", "measured_fitness": None},
                ]
            },
        ),
    )
    s.record_analysis(
        "projA",
        "main",
        make_record("kbdl.annotation", "genomeX", trust_tier="verified"),
    )

    # projA/leg1: annotation-only (foreign) — zero models, non-zero other.
    s.record_analysis(
        "projA",
        "leg1",
        make_record("kbdl.annotation", "genomeY", trust_tier="verified"),
    )
    s.record_analysis(
        "projA",
        "leg1",
        make_record("kbdl.skani", "genomeY", trust_tier="verified"),
    )

    # projB/solo: an FBA run and a gapfill on a file-backed model.
    s.record_analysis(
        "projB",
        "solo",
        make_record(
            "kbutillib.fba",
            "/data/mZ.json",
            model_uri="file:///data/mZ.json",
            payload={"model_name": "Model Z"},
        ),
    )
    s.record_analysis(
        "projB",
        "solo",
        make_record(
            "kbutillib.gapfill",
            "/data/mZ.json",
            model_uri="file:///data/mZ.json",
            payload={"gapfill_media": "Carbon-D-Glucose"},
        ),
    )

    # Unattributed: one FBA run with no project/arc.
    s.record_analysis(
        None,
        None,
        make_record(
            "kbutillib.fba",
            "/data/mU.json",
            model_uri="file:///data/mU.json",
            payload={"model_name": "Model U"},
        ),
    )
    return s
