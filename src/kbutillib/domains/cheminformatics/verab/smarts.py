"""verAB O-demethylation SMARTS constants and canonical seed compounds.

All values in this module are pure Python strings — no RDKit import at module
load time (or ever in this file). Heavy chemistry is deferred to the modules
that need it (rule_discovery.py, substructure.py), following the optional-
dependency contract of the broader cheminformatics sub-package.

References
----------
* Pate 2026 JCIM doi:10.1021/acs.jcim.6c01595 — mechanism-informed operators.
* EC 1.14.13.82 — vanillate monooxygenase (canonical verAB O-demethylase).
* pickaxe-findings §3-4 (repo research document) — verified SMILES/InChIKeys.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Reaction SMARTS / target-transformation pattern
# ---------------------------------------------------------------------------

#: Reaction SMARTS describing the verAB aryl methyl ether O-demethylation:
#:   Ar-OCH3  (+O2/H2O)  ->  Ar-OH  +  HCHO
#:
#: Atom-mapped form: the aromatic carbon [c:1] bonded to [O:2]-[CH3:3] on the
#: reactant side becomes [c:1]-[OH:2] on the product side; [CH3:3] is released
#: as formaldehyde ([CH2:3]=O).  Coreactants (O2, H2O) are implicit here; the
#: actual Pickaxe rule includes them via the coreactant list.
VERAB_ODEMETHYLATION_SMARTS: str = (
    "[c:1][O:2][CH3:3]>>[c:1][OH:2].[CH2:3]=O"
)

# ---------------------------------------------------------------------------
# Substructure query patterns (no reaction mapping)
# ---------------------------------------------------------------------------

#: Substructure SMARTS: any aromatic carbon bonded via a single O to a methyl.
#: Matches guaiacol, vanillate, veratrate, methoxybenzenes, etc.
#: Use with RDKit ``Chem.MolFromSmarts`` + ``mol.HasSubstructMatch``.
METHOXY_AROMATIC_SMARTS: str = "[c]-[OX2]-[CH3]"

#: Stricter variant: additionally excludes ester-like oxygen environments by
#: requiring the methyl oxygen neighbour to carry *no* double-bond connections
#: (i.e. the oxygen is a simple ether, not a carboxylate or carbonate).
METHOXY_AROMATIC_SMARTS_STRICT: str = "[c]-[OX2;!$(O=*);!$(O~[#7,#8,#16])]-[CH3]"

# ---------------------------------------------------------------------------
# Canonical verAB seed compounds
# ---------------------------------------------------------------------------

#: Five canonical seed compounds for the verAB O-demethylation Pickaxe run.
#: Keys per row: ``id``, ``name``, ``smiles``, ``inchikey``, ``kegg``.
#:
#: SMILES are the primary operational identifiers (used as Pickaxe input).
#: InChIKeys and KEGG IDs are provided for DB cross-referencing; verify against
#: the local biochem DB if strict provenance is required.
#:
#: Source: pickaxe-findings §4 (repository research document, Jul 2026).
SEED_COMPOUNDS: list[dict] = [
    {
        "id": "cpd_vanillate",
        "name": "vanillate",
        "smiles": "COc1cc(C(=O)O)ccc1O",
        "inchikey": "HQQDVBWLGYYUPT-UHFFFAOYSA-N",
        "kegg": "C00943",
    },
    {
        "id": "cpd_isovanillate",
        "name": "isovanillate",
        "smiles": "COc1cc(C(=O)O)cc(O)c1",
        "inchikey": "RXXOQGJQYQXQDS-UHFFFAOYSA-N",
        "kegg": None,
    },
    {
        "id": "cpd_guaiacol",
        "name": "guaiacol",
        "smiles": "COc1ccccc1O",
        "inchikey": "ISWSIDIOOBJBQZ-UHFFFAOYSA-N",
        "kegg": "C02268",
    },
    {
        "id": "cpd_4methoxybenzoate",
        "name": "4-methoxybenzoate",
        "smiles": "COc1ccc(C(=O)O)cc1",
        "inchikey": "LJYMWKQXKJXWAC-UHFFFAOYSA-N",
        "kegg": "C01468",
    },
    {
        "id": "cpd_veratrate",
        "name": "veratrate",
        "smiles": "COc1cc(OC)ccc1C(=O)O",
        "inchikey": "VJQWQGZQZQKJCS-UHFFFAOYSA-N",
        "kegg": None,
    },
]
