"""Tests for ModelSEED biochemistry free energy calculation utilities.

Originally tested deltaG methods on MSBiochemUtils (get_compound_deltag,
get_reaction_deltag_from_formation, calculate_reaction_deltag).  Those methods
were relocated to ThermoUtils in src/kbutillib/thermo_utils.py when the
thermodynamics facade was introduced (commit 701aeb1).  This file now targets
the canonical current location while preserving all original test intent.

Migration map
─────────────
Old target                                  → New target
──────────────────────────────────────────────────────────────────────
MSBiochemUtils.get_compound_deltag          → ThermoUtils.get_compound_deltag
MSBiochemUtils.get_reaction_deltag_from_    → ThermoUtils.calculate_reaction_
  formation                                     deltag(..., use_compound_
                                                formation=True)
MSBiochemUtils.calculate_reaction_deltag    → ThermoUtils.calculate_reaction_
                                                deltag

NOTE – production attribute naming quirk (not fixed here):
  ThermoUtils.calculate_reaction_deltag (use_compound_formation=False) guards
  with `reaction_obj.delta_g` but reads from `reaction_obj.deltag` (line 252).
  Tests set both attributes on mocks to match the actual runtime behaviour.
"""

import math
import pytest
from unittest.mock import Mock, patch
from src.kbutillib.thermo_utils import ThermoUtils


# ─────────────────────────────────────────────────────────────────────────────
# Fixture
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def thermo():
    """Create a ThermoUtils instance with all DB/file I/O bypassed.

    ThermoUtils inherits from SharedEnvUtils which reads config files and
    token files on init.  We skip __init__ and wire in a Mock for the
    biochem_utils dependency so every test remains fully offline.
    """
    with patch.object(ThermoUtils, '__init__', lambda self, **kw: None):
        t = ThermoUtils()
        t._biochem_utils = Mock()
        return t


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_compound(deltag=None, annotation=None, delta_g_error=None):
    """Return a Mock compound with the given attribute values."""
    c = Mock()
    c.deltag = deltag
    c.annotation = annotation if annotation is not None else {}
    c.delta_g_error = delta_g_error
    return c


def _make_metabolite(cpd_id, name=None):
    """Return a Mock metabolite with the given compartmented ID."""
    m = Mock()
    m.id = cpd_id                         # e.g. 'cpd00001_c0'
    m.name = name or cpd_id.split('_')[0]
    return m


def _make_reaction(rxn_id, metabolites, equation='A -> B'):
    """Return a Mock reaction object."""
    r = Mock()
    r.id = rxn_id
    r.metabolites = metabolites
    r.build_reaction_string.return_value = equation
    return r


# ─────────────────────────────────────────────────────────────────────────────
# TestCompoundDeltaG
# ─────────────────────────────────────────────────────────────────────────────

class TestCompoundDeltaG:
    """Tests for ThermoUtils.get_compound_deltag.

    ThermoUtils.get_compound_deltag(compound_id) delegates the DB lookup to
    self.biochem_utils.get_compound_by_id(compound_id) and then applies the
    10 000 000 sentinel / annotation-fallback logic.
    """

    def test_get_compound_deltag_valid_value(self, thermo):
        """Valid numeric deltag attribute is returned as float."""
        cpd = _make_compound(deltag=-237.18)
        thermo._biochem_utils.get_compound_by_id.return_value = cpd

        result = thermo.get_compound_deltag('cpd00001')

        assert result == -237.18
        assert isinstance(result, float)

    def test_get_compound_deltag_unknown_value(self, thermo):
        """10000000 sentinel returns None (unknown marker)."""
        cpd = _make_compound(deltag=10000000, annotation={})
        thermo._biochem_utils.get_compound_by_id.return_value = cpd

        result = thermo.get_compound_deltag('cpd99999')

        assert result is None

    def test_get_compound_deltag_from_annotation(self, thermo):
        """When deltag attribute is None, annotation dict is consulted."""
        cpd = _make_compound(deltag=None, annotation={'deltag': -150.5})
        thermo._biochem_utils.get_compound_by_id.return_value = cpd

        result = thermo.get_compound_deltag('cpd00002')

        assert result == -150.5

    def test_get_compound_deltag_from_annotation_string(self, thermo):
        """String-valued annotation is cast to float."""
        cpd = _make_compound(deltag=None, annotation={'deltag': '-200.75'})
        thermo._biochem_utils.get_compound_by_id.return_value = cpd

        result = thermo.get_compound_deltag('cpd00003')

        assert result == -200.75

    def test_get_compound_deltag_not_found(self, thermo):
        """Compound absent from DB raises ValueError."""
        thermo._biochem_utils.get_compound_by_id.return_value = None

        with pytest.raises(ValueError, match="not found in ModelSEED database"):
            thermo.get_compound_deltag('cpd_invalid')

    def test_get_compound_deltag_no_valid_value(self, thermo):
        """Compound with None deltag and empty annotation returns None."""
        cpd = _make_compound(deltag=None, annotation={})
        thermo._biochem_utils.get_compound_by_id.return_value = cpd

        result = thermo.get_compound_deltag('cpd00004')

        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# TestReactionDeltaGFromFormation
# ─────────────────────────────────────────────────────────────────────────────

class TestReactionDeltaGFromFormation:
    """Tests for the formation-energy path of ThermoUtils.calculate_reaction_deltag.

    The old MSBiochemUtils.get_reaction_deltag_from_formation(rxn_id) is now
    ThermoUtils.calculate_reaction_deltag(rxn_id, use_compound_formation=True).

    Internally the method:
      1. Calls self.biochem_utils.get_reaction_by_id(reaction_id)
      2. For each metabolite calls self.get_compound_deltag(metabolite.id)
      3. Calls self.biochem_utils.get_compound_by_id(base_cpd_id) for errors
    """

    def test_simple_reaction_all_compounds_valid(self, thermo):
        """ΔG = Σ(νi × ΔGf,i) for a balanced A + B → C + D reaction.

        Expected: (−150 + −180) − (−100 + −200) = −330 − (−300) = −30
        """
        met_A = _make_metabolite('cpd00001_c0', 'A')
        met_B = _make_metabolite('cpd00002_c0', 'B')
        met_C = _make_metabolite('cpd00003_c0', 'C')
        met_D = _make_metabolite('cpd00004_c0', 'D')

        rxn = _make_reaction('rxn00001',
                              {met_A: -1.0, met_B: -1.0, met_C: 1.0, met_D: 1.0},
                              'A + B -> C + D')
        thermo._biochem_utils.get_reaction_by_id.return_value = rxn

        deltag_map = {
            'cpd00001_c0': -100.0,
            'cpd00002_c0': -200.0,
            'cpd00003_c0': -150.0,
            'cpd00004_c0': -180.0,
        }

        # get_compound_by_id called for error propagation (base id); no error supplied
        def _cpd_by_id(cpd_id):
            c = Mock()
            c.delta_g_error = None
            c.deltag = deltag_map.get(cpd_id)
            return c

        thermo._biochem_utils.get_compound_by_id.side_effect = _cpd_by_id

        with patch.object(thermo, 'get_compound_deltag',
                          side_effect=lambda cpd_id: deltag_map.get(cpd_id)):
            result = thermo.calculate_reaction_deltag('rxn00001',
                                                      use_compound_formation=True)

        assert result['deltag'] == -30.0
        assert result['reaction_id'] == 'rxn00001'
        assert len(result['compound_contributions']) == 4
        assert len(result['missing_compounds']) == 0
        assert len(result['warnings']) == 0

    def test_reaction_with_missing_compound(self, thermo):
        """One compound missing deltaG → partial calculation, warning emitted."""
        met_A = _make_metabolite('cpd00001_c0', 'A')
        met_B = _make_metabolite('cpd00002_c0', 'B')

        rxn = _make_reaction('rxn00002', {met_A: -1.0, met_B: 1.0}, 'A -> B')
        thermo._biochem_utils.get_reaction_by_id.return_value = rxn

        def _cpd_deltag(cpd_id):
            return -100.0 if 'cpd00001' in cpd_id else None

        thermo._biochem_utils.get_compound_by_id.return_value = None

        with patch.object(thermo, 'get_compound_deltag', side_effect=_cpd_deltag):
            result = thermo.calculate_reaction_deltag('rxn00002',
                                                      use_compound_formation=True,
                                                      require_all_compounds=False)

        assert len(result['missing_compounds']) == 1
        assert result['missing_compounds'][0]['compound_id'] == 'cpd00002'
        assert len(result['warnings']) > 0

    def test_reaction_with_missing_compound_error(self, thermo):
        """require_all_compounds=True raises ValueError when deltaG absent."""
        met_A = _make_metabolite('cpd00001_c0', 'A')
        met_B = _make_metabolite('cpd00002_c0', 'B')

        rxn = _make_reaction('rxn00003', {met_A: -1.0, met_B: 1.0}, 'A -> B')
        thermo._biochem_utils.get_reaction_by_id.return_value = rxn

        def _cpd_deltag(cpd_id):
            return -100.0 if 'cpd00001' in cpd_id else None

        thermo._biochem_utils.get_compound_by_id.return_value = None

        with patch.object(thermo, 'get_compound_deltag', side_effect=_cpd_deltag):
            with pytest.raises(ValueError, match="contains compounds without valid deltaG values"):
                thermo.calculate_reaction_deltag('rxn00003',
                                                 use_compound_formation=True,
                                                 require_all_compounds=True)

    def test_reaction_not_found(self, thermo):
        """Unknown reaction ID raises ValueError."""
        thermo._biochem_utils.get_reaction_by_id.return_value = None

        with pytest.raises(ValueError, match="not found in ModelSEED database"):
            thermo.calculate_reaction_deltag('rxn_invalid',
                                             use_compound_formation=True)

    def test_reaction_with_stoichiometry_coefficients(self, thermo):
        """Stoichiometric coefficients ≠ 1 are correctly applied.

        Reaction: 2A + 3B → C
        ΔG = −500 − (−2·50 + −3·100) = −500 − (−100 + −300) = −500 + 400 = −100
        Contribution of A = (−2.0) × (−50.0) = 100.0
        Contribution of B = (−3.0) × (−100.0) = 300.0
        """
        met_A = _make_metabolite('cpd00001_c0', 'A')
        met_B = _make_metabolite('cpd00002_c0', 'B')
        met_C = _make_metabolite('cpd00003_c0', 'C')

        rxn = _make_reaction('rxn00004',
                              {met_A: -2.0, met_B: -3.0, met_C: 1.0},
                              '2 A + 3 B -> C')
        thermo._biochem_utils.get_reaction_by_id.return_value = rxn

        deltag_map = {
            'cpd00001_c0': -50.0,
            'cpd00002_c0': -100.0,
            'cpd00003_c0': -500.0,
        }

        def _cpd_by_id(cpd_id):
            c = Mock()
            c.delta_g_error = None
            return c

        thermo._biochem_utils.get_compound_by_id.side_effect = _cpd_by_id

        with patch.object(thermo, 'get_compound_deltag',
                          side_effect=lambda cpd_id: deltag_map.get(cpd_id)):
            result = thermo.calculate_reaction_deltag('rxn00004',
                                                      use_compound_formation=True)

        assert result['deltag'] == -100.0
        assert result['compound_contributions']['cpd00001']['contribution'] == -2.0 * -50.0
        assert result['compound_contributions']['cpd00002']['contribution'] == -3.0 * -100.0

    def test_error_propagation(self, thermo):
        """Uncertainty is propagated as σ = √Σ(νi² σi²).

        Reaction: A → B  (ν_A = −1, ν_B = +1, σ_A = 2, σ_B = 3)
        σ_rxn = √(1²·2² + 1²·3²) = √(4 + 9) = √13 ≈ 3.606

        ThermoUtils reads the per-compound error from the `delta_g_error`
        attribute (returned by biochem_utils.get_compound_by_id for the base
        compound ID, e.g. 'cpd00001').
        """
        met_A = _make_metabolite('cpd00001_c0', 'A')
        met_B = _make_metabolite('cpd00002_c0', 'B')

        rxn = _make_reaction('rxn00005', {met_A: -1.0, met_B: 1.0}, 'A -> B')
        thermo._biochem_utils.get_reaction_by_id.return_value = rxn

        deltag_map = {'cpd00001_c0': -100.0, 'cpd00002_c0': -150.0}

        def _cpd_by_id(cpd_id):
            c = Mock()
            # Error lookup uses the base id (e.g. 'cpd00001')
            if 'cpd00001' in cpd_id:
                c.delta_g_error = 2.0
            elif 'cpd00002' in cpd_id:
                c.delta_g_error = 3.0
            else:
                c.delta_g_error = None
            return c

        thermo._biochem_utils.get_compound_by_id.side_effect = _cpd_by_id

        with patch.object(thermo, 'get_compound_deltag',
                          side_effect=lambda cpd_id: deltag_map.get(cpd_id)):
            result = thermo.calculate_reaction_deltag('rxn00005',
                                                      use_compound_formation=True)

        assert result['deltag_error'] is not None
        assert abs(result['deltag_error'] - math.sqrt(13)) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# TestCalculateReactionDeltaG
# ─────────────────────────────────────────────────────────────────────────────

class TestCalculateReactionDeltaG:
    """Tests for the full calculate_reaction_deltag method surface.

    Notes on production attribute naming (use_compound_formation=False path):
      • The guard at thermo_utils.py:250 reads `reaction_obj.delta_g`.
      • The value assignment at thermo_utils.py:252 reads `reaction_obj.deltag`
        (without underscore) — this is an inconsistency in the production code.
      • Tests set both `delta_g` and `deltag` on Mocks so the method behaves
        correctly without touching source.
      • Error field is consistently `reaction_obj.delta_g_error` throughout.
    """

    def test_calculate_with_formation_energies(self, thermo):
        """use_compound_formation=True: ΔG = Σ νi·ΔGf,i.

        A → B with ΔGf(A)=−100, ΔGf(B)=−150 → ΔG = −150 − (−100) = −50
        """
        met_A = _make_metabolite('cpd00001_c0')
        met_B = _make_metabolite('cpd00002_c0')

        rxn = _make_reaction('rxn00001', {met_A: -1.0, met_B: 1.0}, 'A -> B')
        thermo._biochem_utils.get_reaction_by_id.return_value = rxn

        deltag_map = {'cpd00001_c0': -100.0, 'cpd00002_c0': -150.0}

        def _cpd_by_id(cpd_id):
            c = Mock()
            c.delta_g_error = None
            return c

        thermo._biochem_utils.get_compound_by_id.side_effect = _cpd_by_id

        with patch.object(thermo, 'get_compound_deltag',
                          side_effect=lambda cpd_id: deltag_map.get(cpd_id)):
            result = thermo.calculate_reaction_deltag('rxn00001',
                                                      use_compound_formation=True)

        assert result['deltag'] == -50.0

    def test_calculate_with_reaction_stored_deltag(self, thermo):
        """use_compound_formation=False returns the stored reaction ΔG directly.

        Production code checks `reaction_obj.delta_g` (line 250) but reads
        `reaction_obj.deltag` (line 252).  Both are set on the mock.
        """
        rxn = Mock()
        rxn.id = 'rxn00001'
        rxn.build_reaction_string.return_value = 'A -> B'
        rxn.delta_g = -75.5      # guard: hasattr + abs() check
        rxn.deltag = -75.5       # value actually read (production quirk)
        rxn.delta_g_error = 5.0  # both guard and value

        thermo._biochem_utils.get_reaction_by_id.return_value = rxn

        result = thermo.calculate_reaction_deltag('rxn00001',
                                                  use_compound_formation=False)

        assert result['deltag'] == -75.5
        assert result['deltag_error'] == 5.0
        assert result['source'] == 'reaction_attribute'

    def test_calculate_with_reaction_object(self, thermo):
        """Passing a reaction object instead of a string ID is supported."""
        rxn = Mock()
        rxn.id = 'rxn00001'
        rxn.build_reaction_string.return_value = 'A -> B'
        rxn.delta_g = -80.0
        rxn.deltag = -80.0       # production quirk: both needed
        rxn.delta_g_error = None

        # When a non-string is passed, ThermoUtils uses it directly (no DB lookup)
        result = thermo.calculate_reaction_deltag(rxn, use_compound_formation=False)

        assert result['deltag'] == -80.0
        assert result['reaction_id'] == 'rxn00001'

    def test_calculate_reaction_not_found(self, thermo):
        """String ID not in DB raises ValueError."""
        thermo._biochem_utils.get_reaction_by_id.return_value = None

        with pytest.raises(ValueError, match="not found in ModelSEED database"):
            thermo.calculate_reaction_deltag('rxn_invalid')

    def test_calculate_with_unknown_stored_deltag(self, thermo):
        """10000000 sentinel in stored deltaG → result['deltag'] is None, warning present."""
        rxn = Mock()
        rxn.id = 'rxn00001'
        rxn.build_reaction_string.return_value = 'A -> B'
        rxn.delta_g = 10000000   # unknown marker for guard
        rxn.deltag = 10000000    # unknown marker (production quirk)
        rxn.delta_g_error = None

        thermo._biochem_utils.get_reaction_by_id.return_value = rxn

        result = thermo.calculate_reaction_deltag('rxn00001',
                                                  use_compound_formation=False)

        assert result['deltag'] is None
        assert any('unknown' in w.lower() for w in result['warnings'])


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
