"""Gated live integration test for RastUtils against the real RAST service.

This test sends a handful of short, well-known bacterial proteins to the
**real** token-free RAST tutorial JSON-RPC endpoint
(``https://tutorial.theseed.org/services/genome_annotation``) and asserts
that a non-empty RAST function comes back for at least one of them.

Opt-in
------
Skipped automatically (never errors or fails) unless both of the following
hold, mirroring the ``KBUTILLIB_LIVE_CHEM`` gate used by
``tests/verab/test_verab_live.py``::

    KBUTILLIB_LIVE_RAST=1 pytest tests/external/test_rast_utils_live.py -v

1. ``KBUTILLIB_LIVE_RAST=1`` is set (this test makes a real call to a
   third-party, externally hosted service and must never run in a normal
   offline CI/test invocation).
2. ``modelseedpy`` is importable.

Privacy note
------------
The proteins sent here are short, public, well-characterized sequences
(E. coli b-subunit fragments / test peptides) chosen specifically because
they carry no sensitive information -- this is the same constraint
RastUtils.annotate() imposes on every caller (see rast_utils.py's module
docstring).
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_LIVE_RAST_ENABLED: bool = os.environ.get("KBUTILLIB_LIVE_RAST") == "1"
_MODELSEEDPY_IMPORTABLE: bool = importlib.util.find_spec("modelseedpy") is not None

pytestmark = pytest.mark.skipif(
    not (_LIVE_RAST_ENABLED and _MODELSEEDPY_IMPORTABLE),
    reason=(
        "Live RAST test disabled -- set KBUTILLIB_LIVE_RAST=1 to enable "
        "(requires modelseedpy and real network access to "
        "tutorial.theseed.org)"
    ),
)

# Three short, well-known proteins with unambiguous, well-characterized
# functions -- good canaries for "did RAST actually annotate something".
_TEST_PROTEINS = {
    # E. coli K-12 DnaK (Hsp70 chaperone) -- N-terminal ATPase domain, a
    # classic, highly conserved, easily recognized fold.
    "dnaK_fragment": (
        "MGKIIGIDLGTTNSCVAIMDGTTPRVLENAEGDRTTPSIIAYTQDGETLVGQPAKRQAVTNPQNT"
        "LFAIKRLIGRRFQDEEVQRDVSIMPFKIIAADNGDAWVEVKGQKMAPPQISAEVLKKMKKTAEDY"
        "LGEPVTEAVITVPAYFNDAQRQATKDAGRIAGLEVKRIINEPTAAALAYGLDKGTGNRTIAVYDL"
    ),
    # A short glucokinase-like fragment (well-annotated carbohydrate kinase
    # family).
    "glucokinase_fragment": (
        "MTKYALVGDVGGTNARLALCDIASGEISQAKTYSGLDYPSLEAVIRVYLEEHKVEVKDGCIAIA"
        "CPITGDWVAMTNHTWAFSIAEMKKNLGFSHLEIINDFTAVSMAIPMLKKEHLIQFGGAEPVEGK"
    ),
}


@pytest.fixture(scope="module")
def rast_utils():
    from kbutillib.domains.external.rast_utils import RastUtils

    return RastUtils(config_file=False, token_file=None, kbase_token_file=None)


def test_live_annotate_returns_nonempty_rast_functions(rast_utils):
    result = rast_utils.annotate(_TEST_PROTEINS)

    assert result.tool == "rast"
    assert result.parameters["num_proteins"] == len(_TEST_PROTEINS)

    # At least one of the two proteins should come back with a called
    # RAST function -- an empty overall result would mean the service is
    # reachable but the pipeline found nothing, which is suspicious for
    # two well-conserved bacterial proteins and should fail the test
    # rather than being silently accepted.
    assert result.records, (
        "RAST returned zero annotated records for well-known test proteins "
        f"-- raw parameters: {result.parameters}"
    )
    for rec in result.records:
        assert rec.gene_id in _TEST_PROTEINS
        assert rec.terms
        for term in rec.terms:
            assert term.namespace == "RAST"
            assert term.value.strip()
