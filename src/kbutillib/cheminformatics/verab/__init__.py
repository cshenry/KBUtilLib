"""verAB methoxy-aromatic Pickaxe sub-package.

This sub-package implements rule discovery and genome screening for the verAB
aryl methyl ether O-demethylation pathway (EC 1.14.13.82).  It is RDKit-lazy:
importing this package never requires RDKit or minedatabase to be installed.

Public surface
--------------
**smarts** — module-level string constants:

* :data:`~kbutillib.cheminformatics.verab.smarts.VERAB_ODEMETHYLATION_SMARTS`
  — reaction SMARTS for Ar-OCH3 → Ar-OH + HCHO.
* :data:`~kbutillib.cheminformatics.verab.smarts.METHOXY_AROMATIC_SMARTS`
  — substructure query for aromatic methoxy groups.
* :data:`~kbutillib.cheminformatics.verab.smarts.METHOXY_AROMATIC_SMARTS_STRICT`
  — stricter variant (excludes ester-like environments).
* :data:`~kbutillib.cheminformatics.verab.smarts.SEED_COMPOUNDS`
  — list of 5 canonical seed compound dicts.

**models** — JSON-serialisable dataclasses with ``to_dict()``:

* :class:`~kbutillib.cheminformatics.verab.models.VerabRuleMatch`
* :class:`~kbutillib.cheminformatics.verab.models.VerabDiscoveryResult`
* :class:`~kbutillib.cheminformatics.verab.models.ScreeningRecord`
* :class:`~kbutillib.cheminformatics.verab.models.ScreeningReport`

**rule_discovery** — rule matching and discovery functions (S3):

* :func:`~kbutillib.cheminformatics.verab.rule_discovery.match_transformation`
* :func:`~kbutillib.cheminformatics.verab.rule_discovery.discover_verab_rules`

Later slices (S4-S8) will add:
    * screening.py       — screen_products, predict_genome_degradation
    * king_artifacts.py  — emit_king_workflow
"""

from __future__ import annotations

from .models import (
    ScreeningRecord,
    ScreeningReport,
    VerabDiscoveryResult,
    VerabRuleMatch,
)
from .smarts import (
    METHOXY_AROMATIC_SMARTS,
    METHOXY_AROMATIC_SMARTS_STRICT,
    SEED_COMPOUNDS,
    VERAB_ODEMETHYLATION_SMARTS,
)
from .substructure import MethoxyAromaticFilter

__all__ = [
    # smarts constants
    "VERAB_ODEMETHYLATION_SMARTS",
    "METHOXY_AROMATIC_SMARTS",
    "METHOXY_AROMATIC_SMARTS_STRICT",
    "SEED_COMPOUNDS",
    # dataclasses
    "VerabRuleMatch",
    "VerabDiscoveryResult",
    "ScreeningRecord",
    "ScreeningReport",
    # substructure filter (S2)
    "MethoxyAromaticFilter",
]
