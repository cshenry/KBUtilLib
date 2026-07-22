"""Pickaxe network-expansion backend.

Wraps the Tyo/Henry-lab ``minedatabase`` package (Pickaxe) as an optional
:class:`kbutillib.cheminformatics.base.ExpansionBackend`. Pickaxe applies
SMARTS reaction operators to seed compounds to enumerate a predicted reaction
network (de-novo metabolite / promiscuity prediction).

Optional dependency
-------------------
``minedatabase`` (and its transitive RDKit / libSBML / lxml / pymongo deps) is
NOT a hard requirement of KBUtilLib. Importing this module never imports
``minedatabase``; the import is attempted lazily in :meth:`_probe`. When it is
missing the backend reports ``available == False`` with installation guidance
and never crashes.

Verified API contract (minedatabase 2.1.0, read from source + live-run)
----------------------------------------------------------------------
* ``Pickaxe(rule_list=<tsv>, coreactant_list=<tsv>, explicit_h=False,
  quiet=True, errors=False, ...)``
* ``.load_compound_set(compound_file=<csv>, id_field="id")`` -- CSV needs an
  ``id`` column and a structure column (``smiles`` is auto-detected by
  ``minedatabase.utils.file_to_dict_list`` / ``_mol_from_dict``).
* ``.transform_all(processes=1, generations=N)``
* ``.compounds[cid]`` dict keys: ``ID, _id, SMILES, InChI_key, Type,
  Generation, atom_count, Expand, Formula``. ``Type`` is one of
  ``"Starting Compound"``, ``"Coreactant"``, ``"Predicted"``.
* ``.reactions[rid]`` dict keys: ``_id, Reactants=[(stoich, cid)],
  Products=[(stoich, cid)], Operators`` (a ``set`` of rule names),
  ``SMILES_rxn``.

Bundled rule sets (shipped inside the installed ``minedatabase`` package,
``minedatabase/data/``)
* ``"bnice"`` / ``"enzymatic"`` -> ``original_rules/EnzymaticReactionRules.tsv``
  + ``EnzymaticCoreactants.tsv`` (atom-mapped BNICE operators).
* ``"metacyc_generalized"`` (default) -> ``metacyc_rules/
  metacyc_generalized_rules.tsv`` + ``metacyc_coreactants.tsv``.
* ``"metacyc_intermediate"`` -> ``metacyc_rules/
  metacyc_intermediate_rules.tsv`` + ``metacyc_coreactants.tsv``.

A custom rule set is supported via ``rule_list``/``coreactant_list`` kwargs
(absolute paths) to :meth:`expand`.
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .base import (
    BackendUnavailableError,
    ExpansionBackend,
    ExpansionResult,
    PredictedCompound,
    PredictedReaction,
)

#: name -> (rule file relative to data/, coreactant file relative to data/)
_BUNDLED_RULESETS = {
    "metacyc_generalized": (
        "metacyc_rules/metacyc_generalized_rules.tsv",
        "metacyc_rules/metacyc_coreactants.tsv",
    ),
    "metacyc_intermediate": (
        "metacyc_rules/metacyc_intermediate_rules.tsv",
        "metacyc_rules/metacyc_coreactants.tsv",
    ),
    "bnice": (
        "original_rules/EnzymaticReactionRules.tsv",
        "original_rules/EnzymaticCoreactants.tsv",
    ),
    "enzymatic": (
        "original_rules/EnzymaticReactionRules.tsv",
        "original_rules/EnzymaticCoreactants.tsv",
    ),
    "chemical_damage": (
        "original_rules/ChemicalDamageReactionRules.tsv",
        "original_rules/ChemicalDamageCoreactants.tsv",
    ),
}

#: Default rule set if the caller does not specify one.
_DEFAULT_RULESET = "metacyc_generalized"

#: Subpath within a coarse-grain-rxns clone to the mechinferred operator TSV.
_MECHINFORMED_RULES_SUBPATH = (
    "data/processed/rules/mechinferred_dt_019_rules_w_coreactants.tsv"
)

#: Honest graceful-degrade message emitted when the mechinformed TSV is unresolved.
_MECHINFORMED_DEGRADE_MSG = (
    "[pickaxe] mechanism-informed operators (Pate 2026, stefanpate/coarse-grain-rxns) "
    "not found. Falling back to bundled 'metacyc_intermediate' (the most "
    "mechanism-aware built-in set). To use the mechanism-informed operators, clone "
    "github.com/stefanpate/coarse-grain-rxns and set "
    "cheminformatics.verab.operator_rule_tsv (or env KBUTILLIB_VERAB_OPERATOR_TSV) "
    "to data/processed/rules/mechinferred_dt_019_rules_w_coreactants.tsv. "
    "NOTE: that repo has no LICENSE, so KBUtilLib does not redistribute the file."
)

# ---------------------------------------------------------------------------
# Code-owned coreactant role -> SMILES map for the mechinformed operator set.
# This table is NOT vendored from coarse-grain-rxns; it is a KBUtilLib-authored
# best-effort mapping of the role labels embedded in the Pate 2026 rule rows to
# canonical SMILES.  A curated coreactant TSV can be supplied via
# cheminformatics.verab.operator_coreactant_tsv (or env
# KBUTILLIB_VERAB_COREACTANT_TSV) for higher fidelity.
# ---------------------------------------------------------------------------
_MECHINFORMED_COREACTANTS: Dict[str, str] = {
    # Common metabolites
    "WATER": "O",
    "OXYGEN": "O=O",
    "NAD": "O=C(c1ccncc1)[C@@H]1OC([n+]2cccc(C(N)=O)c2)[C@H](O)[C@@H]1O",  # NAD+
    "NADH": "O=C(c1ccncc1)[C@@H]1OC([nH+]2cccc(C(N)=O)c2)[C@H](O)[C@@H]1O",  # NADH (simplified)
    "NADP": "NC(=O)c1ccc[n+](C2OC(COP(=O)(O)OP(=O)(O)OCC3OC(n4cnc5c(N)ncnc54)C(OP(=O)(O)O)C3O)C(O)C2O)c1",
    "NADPH": "NC(=O)C1=CN(C2OC(COP(=O)(O)OP(=O)(O)OCC3OC(n4cnc5c(N)ncnc54)C(OP(=O)(O)O)C3O)C(O)C2O)C=CC1",
    # SAM / SAH — methyl donor / acceptor (verAB-relevant!)
    "METHYL_DONOR_CoF": "C[S+](CC[C@@H](N)C(=O)[O-])C[C@@H]1O[C@@H]([n+]2cnc3c(N)ncnc32)[C@H](O)[C@@H]1O",  # SAM
    "METHYL_ACCEPTOR_CoF": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](CCS(CC[C@@H](N)C(=O)[O-])C)[C@@H](O)[C@H]1O",  # SAH (simplified)
    # Phosphate donors / acceptors
    "PHOSPHATE_DONOR_CoF": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(=O)(O)OP(=O)(O)OP(=O)(O)O)[C@@H](O)[C@H]1O",  # ATP
    "PHOSPHATE_ACCEPTOR_CoF": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(=O)(O)OP(=O)(O)O)[C@@H](O)[C@H]1O",  # ADP
    # Inorganic phosphate species
    "PPI": "O=P(O)(O)OP(=O)(O)O",   # pyrophosphate
    "Pi": "OP(=O)(O)O",              # inorganic phosphate
    # CoA species
    "CoA": "CC(C)(COP(=O)(O)OP(=O)(O)OCC1OC(n2cnc3c(N)ncnc32)C(O)C1OP(=O)(O)O)C(O)C(=O)NCCC(=O)NCCS",
    "ACYL_CoA": "CC(=O)SCCNC(=O)CCNC(=O)C(O)C(C)(C)COP(=O)(O)OP(=O)(O)OCC1OC(n2cnc3c(N)ncnc32)C(O)C1OP(=O)(O)O",
    # Glutathione
    "GLUTATHIONE": "N[C@@H](CCC(=O)N[C@@H](CS)C(=O)NCC(=O)O)C(=O)O",
    "GSSG": "O=C(NCC(=O)O)C(CS)NC(=O)CC[C@@H](N)C(=O)O",
    # Ferredoxin (placeholder SMILES for iron-sulfur protein cofactor)
    "FERREDOXIN_OX": "[Fe]",
    "FERREDOXIN_RED": "[Fe]",
    # Flavin cofactors
    "FAD": "Cc1cc2nc3c(=O)[nH]c(=O)nc3n(C[C@H](O)[C@H](O)[C@H](O)COP(=O)(O)OP(=O)(O)OCC3OC(n4cnc5c(N)ncnc54)C(O)C3O)c2cc1C",
    "FADH2": "Cc1cc2[nH]c3c(=O)[nH]c(=O)nc3n(C[C@H](O)[C@H](O)[C@H](O)COP(=O)(O)OP(=O)(O)OCC3OC(n4cnc5c(N)ncnc54)C(O)C3O)c2cc1C",
    "FMN": "Cc1cc2nc3c(=O)[nH]c(=O)nc3n(C[C@H](O)[C@H](O)[C@H](O)COP(=O)(O)O)c2cc1C",
}

# ---------------------------------------------------------------------------
# Normalized-rule-TSV cache: source_path_str -> (mtime, normalized_temp_path)
# Prevents re-normalizing the same unchanged file.
# ---------------------------------------------------------------------------
_NORMALIZED_RULE_CACHE: Dict[str, Tuple[float, Path]] = {}


def _normalize_rule_tsv(source_path: Path) -> Path:
    """Return a path to a Pickaxe-compatible 5-column rule TSV.

    If *source_path* already has a ``Comments`` column this returns it
    unchanged.  If it only has the 4 columns ``Name, Reactants, SMARTS,
    Products`` (the Pate 2026 coarse-grain-rxns format), a normalized copy
    with an empty ``Comments`` column is written into a temp directory.

    The normalized copy is cached keyed by (path, mtime) so repeated calls
    are cheap.  The source file is never mutated.

    Args:
        source_path: Path to the original rule TSV.

    Returns:
        Path to a Pickaxe-compatible TSV (may be identical to *source_path*
        when it already has ≥ 5 columns or the first row does not contain
        ``Comments``).
    """
    global _NORMALIZED_RULE_CACHE

    try:
        mtime = source_path.stat().st_mtime
    except OSError:
        return source_path  # can't stat → return as-is

    cache_key = str(source_path)
    cached = _NORMALIZED_RULE_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        cached_path = cached[1]
        if cached_path.is_file():
            return cached_path

    # Inspect header
    try:
        with open(source_path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh, delimiter="\t")
            header = next(reader, None)
    except Exception:
        return source_path  # unreadable → pass through

    if header is None:
        return source_path

    header_lower = [h.strip().lower() for h in header]
    if "comments" in header_lower:
        # Already has Comments column — no normalization needed.
        _NORMALIZED_RULE_CACHE[cache_key] = (mtime, source_path)
        return source_path

    # Need to add an empty Comments column.
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix="_comments.tsv",
        prefix="kbutillib_mechinformed_",
        delete=False,
        newline="",
        encoding="utf-8",
    )
    try:
        with open(source_path, "r", newline="", encoding="utf-8") as src_fh:
            reader = csv.reader(src_fh, delimiter="\t")
            writer = csv.writer(tmp, delimiter="\t", lineterminator="\n")
            first = True
            for row in reader:
                if first:
                    writer.writerow(list(row) + ["Comments"])
                    first = False
                else:
                    writer.writerow(list(row) + [""])
        tmp.close()
        normalized = Path(tmp.name)
        _NORMALIZED_RULE_CACHE[cache_key] = (mtime, normalized)
        return normalized
    except Exception:
        tmp.close()
        return source_path  # normalization failed → return original


def _synthesize_coreactant_tsv() -> Path:
    """Write a temporary Pickaxe-compatible coreactant TSV from :data:`_MECHINFORMED_COREACTANTS`.

    This is KBUtilLib's own code-owned role->SMILES table (not vendored from
    coarse-grain-rxns) and covers the role labels embedded in the Pate 2026
    operator rows.  It is written to a temp file so Pickaxe can load it via
    its ``coreactant_list=<path>`` parameter.

    The file is re-created on every call (it is cheap for < 50 rows).

    Returns:
        Path to the synthesized TSV.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix="_coreactants.tsv",
        prefix="kbutillib_mechinformed_coact_",
        delete=False,
        newline="",
        encoding="utf-8",
    )
    try:
        writer = csv.writer(tmp, delimiter="\t", lineterminator="\n")
        writer.writerow(["Name", "SMILES", "Type"])
        for role, smiles in _MECHINFORMED_COREACTANTS.items():
            writer.writerow([role, smiles, "Coreactant"])
        tmp.close()
        return Path(tmp.name)
    except Exception:
        tmp.close()
        return Path(tmp.name)


class PickaxeBackend:
    """Optional Pickaxe (``minedatabase``) reaction-network expansion backend.

    Args:
        config_resolver: Optional callable ``(key, default=None) -> value`` used
            to look up configuration (e.g. ``cheminformatics.pickaxe.rule_set``
            or a custom rule/coreactant path). Typically
            ``SharedEnvUtils.get_config_value``.
        logger: Optional object exposing ``log_info`` / ``log_warning``.
    """

    name = "pickaxe"

    def __init__(
        self,
        config_resolver: Optional[Any] = None,
        logger: Optional[Any] = None,
    ) -> None:
        self._cfg = config_resolver
        self._logger = logger
        self._available: Optional[bool] = None
        self._reason: Optional[str] = None
        self._data_dir: Optional[Path] = None

    # ── logging helpers ─────────────────────────────────────────────────

    def _log_info(self, msg: str) -> None:
        if self._logger is not None and hasattr(self._logger, "log_info"):
            self._logger.log_info(msg)

    def _log_warning(self, msg: str) -> None:
        if self._logger is not None and hasattr(self._logger, "log_warning"):
            self._logger.log_warning(msg)

    def _config(self, key: str, default: Any = None) -> Any:
        if self._cfg is None:
            return default
        try:
            return self._cfg(key, default=default)
        except TypeError:
            try:
                return self._cfg(key, default)
            except Exception:
                return default
        except Exception:
            return default

    # ── availability ────────────────────────────────────────────────────

    def _ensure_dependency_on_path(self) -> None:
        """Put a declared MINE-Database source checkout on sys.path.

        Uses KBUtilLib's DependencyManager (dependencies.yaml) — the same
        mechanism that wires ModelSEEDpy / ModelSEEDDatabase / cobrakbase — so a
        ``minedatabase`` source checkout becomes importable without a pip install
        or a site-packages .pth shim (the Tyo repo pins python <3.10, which
        blocks ``pip install -e`` on 3.11). Best-effort and never raises.
        """
        try:
            import sys

            from ..dependency_manager import get_dependency_path

            dep = get_dependency_path("MINE-Database")
            if dep:
                dep_str = str(dep)
                if dep_str not in sys.path:
                    sys.path.insert(0, dep_str)
        except Exception:  # pragma: no cover - best-effort
            pass

    def _probe(self) -> bool:
        """Attempt to import minedatabase and locate its bundled data dir."""
        if self._available is not None:
            return self._available
        # Ensure a declared MINE-Database source checkout (dependencies.yaml) is
        # on sys.path before importing — this is the canonical KBUtilLib way to
        # consume a research repo, and avoids needing a pip install or a .pth.
        self._ensure_dependency_on_path()
        try:
            import minedatabase  # noqa: F401
            from minedatabase.pickaxe import Pickaxe  # noqa: F401
        except Exception as exc:  # ImportError or transitive dep failure
            self._available = False
            self._reason = (
                f"minedatabase (Pickaxe) not importable: {type(exc).__name__}: "
                f"{exc}. Install with `pip install minedatabase` (pulls RDKit, "
                f"python-libsbml, lxml, pymongo)."
            )
            self._log_warning(f"[pickaxe] {self._reason}")
            return False

        mod_file = getattr(minedatabase, "__file__", None)
        if not mod_file:
            self._available = False
            self._reason = "minedatabase imported but has no __file__; cannot locate data/."
            self._log_warning(f"[pickaxe] {self._reason}")
            return False

        data_dir = self._resolve_data_dir(Path(mod_file).resolve().parent)
        if data_dir is None:
            self._available = False
            self._reason = (
                "minedatabase is importable but its bundled rule data/ directory "
                "could not be located (the PyPI wheel ships code only, not the "
                "rule TSVs). Point KBUtilLib at a MINE-Database checkout via the "
                "config key 'cheminformatics.pickaxe.data_dir' or the env var "
                "KBUTILLIB_PICKAXE_DATA_DIR (path to .../minedatabase/data)."
            )
            self._log_warning(f"[pickaxe] {self._reason}")
            return False

        self._data_dir = data_dir
        self._available = True
        self._reason = None
        self._log_info(f"[pickaxe] available; rule data dir: {data_dir}")
        return True

    def _resolve_data_dir(self, pkg_dir: Path) -> Optional[Path]:
        """Locate the directory containing Pickaxe rule TSVs.

        The PyPI wheel ships code only, so we search, in priority order:
        config ``cheminformatics.pickaxe.data_dir`` -> env
        ``KBUTILLIB_PICKAXE_DATA_DIR`` -> the installed package's own ``data/``
        -> a MINE-Database source checkout sitting next to the install or under
        common scratch/clone locations. A candidate is accepted only if it
        actually contains a known rule set file.
        """
        import os

        def _valid(d: Path) -> bool:
            return (d / "metacyc_rules" / "metacyc_generalized_rules.tsv").is_file() or (
                d / "original_rules" / "EnzymaticReactionRules.tsv"
            ).is_file()

        candidates: list[Path] = []
        cfg_dir = self._config("cheminformatics.pickaxe.data_dir", None)
        if cfg_dir:
            candidates.append(Path(str(cfg_dir)).expanduser())
        env_dir = os.environ.get("KBUTILLIB_PICKAXE_DATA_DIR")
        if env_dir:
            candidates.append(Path(env_dir).expanduser())
        # Installed package's own data dir (works for editable/source installs).
        candidates.append(pkg_dir / "data")
        # A MINE-Database source checkout next to the installed package.
        candidates.append(pkg_dir.parent / "MINE-Database" / "minedatabase" / "data")
        # A MINE-Database checkout declared in KBUtilLib's dependencies.yaml.
        try:
            from ..dependency_manager import get_dependency_path

            dep = get_dependency_path("MINE-Database")
            if dep:
                candidates.append(Path(dep) / "minedatabase" / "data")
        except Exception:
            pass

        for cand in candidates:
            try:
                if cand.is_dir() and _valid(cand):
                    return cand.resolve()
            except OSError:
                continue
        return None

    @property
    def available(self) -> bool:
        """Whether minedatabase is importable and its rule data is present."""
        return self._probe()

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Explanation when :attr:`available` is ``False``."""
        self._probe()
        return self._reason

    @property
    def capabilities(self) -> "frozenset[str]":
        """Capability tags. Pickaxe supports full network expansion."""
        return frozenset({"expand"})

    # ── rule resolution ─────────────────────────────────────────────────

    def _resolve_mechinformed(self) -> Optional[Path]:
        """Locate the Pate 2026 mechinformed rule TSV without auto-cloning.

        Resolution precedence (first valid file wins):
        1. Config key ``cheminformatics.verab.operator_rule_tsv`` (explicit path).
        2. Env var ``KBUTILLIB_VERAB_OPERATOR_TSV``.
        3. DependencyManager: ``get_data_path("coarse-grain-rxns",
           <_MECHINFORMED_RULES_SUBPATH>)`` — accepted only if the file exists.

        Returns:
            A :class:`~pathlib.Path` to the TSV if found, else ``None``.
        """
        candidates: List[Path] = []

        # 1. Config
        cfg_tsv = self._config("cheminformatics.verab.operator_rule_tsv", None)
        if cfg_tsv:
            candidates.append(Path(str(cfg_tsv)).expanduser())

        # 2. Env var
        env_tsv = os.environ.get("KBUTILLIB_VERAB_OPERATOR_TSV")
        if env_tsv:
            candidates.append(Path(env_tsv).expanduser())

        # 3. DependencyManager (no auto-clone)
        try:
            from ..dependency_manager import get_data_path

            dep_tsv = get_data_path("coarse-grain-rxns", _MECHINFORMED_RULES_SUBPATH)
            if dep_tsv:
                candidates.append(dep_tsv)
        except Exception:
            pass

        for cand in candidates:
            try:
                if cand.is_file():
                    return cand
            except OSError:
                continue
        return None

    def _resolve_mechinformed_coreactants(self) -> Path:
        """Locate or synthesize a coreactant TSV for the mechinformed rule set.

        Resolution precedence:
        1. Config key ``cheminformatics.verab.operator_coreactant_tsv``.
        2. Env var ``KBUTILLIB_VERAB_COREACTANT_TSV``.
        3. Synthesize a temp TSV from :data:`_MECHINFORMED_COREACTANTS` (our
           own code-owned role→SMILES map; not vendored from the upstream repo).

        Returns:
            A :class:`~pathlib.Path` to a valid coreactant TSV.
        """
        # 1. Config
        cfg_coact = self._config(
            "cheminformatics.verab.operator_coreactant_tsv", None
        )
        if cfg_coact:
            p = Path(str(cfg_coact)).expanduser()
            if p.is_file():
                return p

        # 2. Env var
        env_coact = os.environ.get("KBUTILLIB_VERAB_COREACTANT_TSV")
        if env_coact:
            p = Path(env_coact).expanduser()
            if p.is_file():
                return p

        # 3. Synthesize from code-owned table
        self._log_info(
            "[pickaxe] No coreactant TSV configured for mechinformed rule set; "
            "synthesizing from built-in role→SMILES table. For higher fidelity "
            "supply a curated TSV via cheminformatics.verab.operator_coreactant_tsv "
            "or env KBUTILLIB_VERAB_COREACTANT_TSV."
        )
        return _synthesize_coreactant_tsv()

    def _resolve_ruleset(
        self,
        rule_set: Optional[str],
        rule_list: Optional[str],
        coreactant_list: Optional[str],
    ) -> Tuple[str, str, str]:
        """Return ``(label, rule_path, coreactant_path)``.

        Explicit ``rule_list`` + ``coreactant_list`` win. Otherwise resolve a
        named rule set.  The special name ``"mechinformed"`` triggers runtime
        resolution of the Pate 2026 TSV; if unresolved it degrades gracefully
        to ``"metacyc_intermediate"`` with an honest warning.  All other names
        are looked up in :data:`_BUNDLED_RULESETS`.
        """
        if rule_list and coreactant_list:
            rp, cp = Path(rule_list), Path(coreactant_list)
            if not rp.is_file():
                raise BackendUnavailableError(f"rule_list not found: {rp}")
            if not cp.is_file():
                raise BackendUnavailableError(f"coreactant_list not found: {cp}")
            return ("custom", str(rp), str(cp))

        name = (
            rule_set
            or self._config("cheminformatics.pickaxe.rule_set", _DEFAULT_RULESET)
            or _DEFAULT_RULESET
        )
        name = str(name).lower()

        # ── mechinformed: runtime-resolve, never bundled ──────────────────
        if name == "mechinformed":
            rp = self._resolve_mechinformed()
            if rp is None:
                # Honest graceful degrade
                self._log_warning(_MECHINFORMED_DEGRADE_MSG)
                name = "metacyc_intermediate"
                # Fall through to bundled resolution below
            else:
                cp = self._resolve_mechinformed_coreactants()
                rp_norm = _normalize_rule_tsv(rp)
                return ("mechinformed", str(rp_norm), str(cp))

        if name not in _BUNDLED_RULESETS:
            raise BackendUnavailableError(
                f"Unknown bundled rule set '{name}'. "
                f"Available: {sorted(_BUNDLED_RULESETS)} + 'mechinformed', "
                f"or pass explicit rule_list + coreactant_list."
            )
        assert self._data_dir is not None
        rel_rule, rel_core = _BUNDLED_RULESETS[name]
        rp = self._data_dir / rel_rule
        cp = self._data_dir / rel_core
        if not rp.is_file() or not cp.is_file():
            raise BackendUnavailableError(
                f"Bundled rule set '{name}' files missing under {self._data_dir}."
            )
        return (name, str(rp), str(cp))

    # ── expansion ───────────────────────────────────────────────────────

    def expand(
        self,
        seed_smiles: Mapping[str, str],
        generations: int = 1,
        rule_set: Optional[str] = None,
        rule_list: Optional[str] = None,
        coreactant_list: Optional[str] = None,
        processes: int = 1,
        explicit_h: bool = False,
        **kwargs: Any,
    ) -> ExpansionResult:
        """Expand seed compounds into a predicted reaction network with Pickaxe.

        Args:
            seed_smiles: Mapping of compound id -> SMILES for the seed set.
            generations: Number of expansion rounds.
            rule_set: Named bundled rule set (see module docstring). Defaults to
                config ``cheminformatics.pickaxe.rule_set`` or
                ``"metacyc_generalized"``.
            rule_list: Absolute path to a custom rule TSV (with
                ``coreactant_list``); overrides ``rule_set``.
            coreactant_list: Absolute path to a custom coreactant TSV.
            processes: Parallel worker processes for ``transform_all``.
            explicit_h: Whether the rule set uses explicit hydrogens.
            **kwargs: Ignored (reserved for forward-compatible options).

        Returns:
            An :class:`ExpansionResult` mapping Pickaxe's compound/reaction
            dicts into KBUtilLib's domain types.

        Raises:
            BackendUnavailableError: If minedatabase is unavailable or the rule
                set cannot be resolved.
        """
        if not self.available:
            raise BackendUnavailableError(self._reason or "pickaxe unavailable")
        if not seed_smiles:
            raise BackendUnavailableError("no seed compounds supplied")

        from minedatabase.pickaxe import Pickaxe

        label, rule_path, coreact_path = self._resolve_ruleset(
            rule_set, rule_list, coreactant_list
        )

        result = ExpansionResult(backend=self.name, generations=generations)
        result.raw["rule_set"] = label

        tmpdir = tempfile.mkdtemp(prefix="kbutillib_pickaxe_")
        seed_csv = Path(tmpdir) / "seed_compounds.csv"
        with open(seed_csv, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "smiles"])
            for cid, smi in seed_smiles.items():
                writer.writerow([cid, smi])

        self._log_info(
            f"[pickaxe] expanding {len(seed_smiles)} seed(s) x {generations} "
            f"generation(s) with rule set '{label}'"
        )

        pk = Pickaxe(
            rule_list=rule_path,
            coreactant_list=coreact_path,
            explicit_h=explicit_h,
            quiet=True,
            errors=False,
        )
        try:
            pk.load_compound_set(compound_file=str(seed_csv), id_field="id")
            pk.transform_all(processes=processes, generations=generations)
        except Exception as exc:
            result.warnings.append(
                f"pickaxe expansion failed: {type(exc).__name__}: {exc}"
            )
            return result

        self._ingest(pk, result)
        if not result.is_expanded:
            result.warnings.append(
                f"rule set '{label}' produced no reactions for the supplied "
                f"seeds (no operator matched)"
            )
        return result

    def _ingest(self, pk: Any, result: ExpansionResult) -> None:
        """Map a finished Pickaxe object's compounds/reactions into ``result``."""
        for cid, cd in pk.compounds.items():
            ctype = cd.get("Type", "")
            # Skip pure coreactants from the reported network; they are
            # ubiquitous cofactors, not part of the predicted novelty. They
            # still appear inside reaction reactant/product lists by id.
            is_seed = ctype == "Starting Compound"
            result.compounds[cid] = PredictedCompound(
                compound_id=cid,
                smiles=cd.get("SMILES"),
                inchikey=cd.get("InChI_key"),
                formula=cd.get("Formula"),
                generation=cd.get("Generation"),
                is_seed=is_seed,
                raw={"type": ctype, "name": cd.get("ID")},
            )

        for rid, rd in pk.reactions.items():
            raw_operators = rd.get("Operators")
            op_list: List[str] = []
            op_label: Optional[str] = None
            if raw_operators:
                # Operators is a set; sort for stability. Preserve the FULL list.
                op_list = sorted(str(o) for o in raw_operators)
                # Scalar display field: join all operator ids (backward compat).
                op_label = ";".join(op_list)
            reactant_ids = [str(c) for _, c in rd.get("Reactants", [])]
            product_ids = [str(c) for _, c in rd.get("Products", [])]
            result.reactions.append(
                PredictedReaction(
                    reaction_id=str(rd.get("_id", rid)),
                    backend=self.name,
                    operator=op_label,
                    operators=op_list,
                    reactant_ids=reactant_ids,
                    product_ids=product_ids,
                    reaction_smiles=rd.get("SMILES_rxn"),
                    generation=rd.get("Generation"),
                    raw={
                        "reactants": list(rd.get("Reactants", [])),
                        "products": list(rd.get("Products", [])),
                    },
                )
            )


# Static structural conformance check (documents intent; no runtime cost).
_: type[ExpansionBackend] = PickaxeBackend
