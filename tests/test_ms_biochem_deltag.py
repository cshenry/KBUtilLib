"""Tests for ModelSEED biochemistry free energy calculation utilities."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from src.kbutillib.ms_biochem_utils import MSBiochemUtils


@pytest.fixture
def mock_utils():
    """Create a mocked MSBiochemUtils instance that skips initialization."""
    with patch.object(MSBiochemUtils, '_ensure_database_available', return_value=None):
        with patch.object(MSBiochemUtils, '__init__', lambda x: None):
            utils = MSBiochemUtils()
            # Manually set required attributes
            utils._biochem_db = Mock()
            return utils


class TestCompoundDeltaG:
    """Test suite for get_compound_deltag method."""

    def test_get_compound_deltag_valid_value(self, mock_utils):
        """Test retrieving valid deltaG from compound."""
        # Create a mock compound with valid deltaG
        mock_compound = Mock()
        mock_compound.deltag = -237.18  # H2O formation energy
        mock_compound.name = "H2O"

        with patch.object(mock_utils, 'get_compound_by_id', return_value=mock_compound):
            result = mock_utils.get_compound_deltag('cpd00001')

        assert result == -237.18
        assert isinstance(result, float)

    def test_get_compound_deltag_unknown_value(self, mock_utils):
        """Test handling of unknown deltaG (10000000)."""
        mock_compound = Mock()
        mock_compound.deltag = 10000000  # Unknown value marker
        mock_compound.annotation = {}

        with patch.object(mock_utils, 'get_compound_by_id', return_value=mock_compound):
            result = mock_utils.get_compound_deltag('cpd99999')

        assert result is None

    def test_get_compound_deltag_from_annotation(self, mock_utils):
        """Test retrieving deltaG from compound annotations."""
        mock_compound = Mock()
        mock_compound.deltag = None
        mock_compound.annotation = {'deltag': -150.5}

        with patch.object(mock_utils, 'get_compound_by_id', return_value=mock_compound):
            result = mock_utils.get_compound_deltag('cpd00002')

        assert result == -150.5

    def test_get_compound_deltag_from_annotation_string(self, mock_utils):
        """Test retrieving deltaG from annotation as string."""
        mock_compound = Mock()
        mock_compound.deltag = None
        mock_compound.annotation = {'deltag': '-200.75'}

        with patch.object(mock_utils, 'get_compound_by_id', return_value=mock_compound):
            result = mock_utils.get_compound_deltag('cpd00003')

        assert result == -200.75

    def test_get_compound_deltag_not_found(self, mock_utils):
        """Test error when compound not in database."""
        with patch.object(mock_utils, 'get_compound_by_id', return_value=None):
            with pytest.raises(ValueError, match="not found in ModelSEED database"):
                mock_utils.get_compound_deltag('cpd_invalid')

    def test_get_compound_deltag_no_valid_value(self, mock_utils):
        """Test when compound exists but has no valid deltaG."""
        mock_compound = Mock()
        mock_compound.deltag = None
        mock_compound.annotation = {}

        with patch.object(mock_utils, 'get_compound_by_id', return_value=mock_compound):
            result = mock_utils.get_compound_deltag('cpd00004')

        assert result is None


class TestReactionDeltaGFromFormation:
    """Test suite for get_reaction_deltag_from_formation method."""

    def test_simple_reaction_all_compounds_valid(self, mock_utils):
        """Test calculation for simple reaction with all valid deltaG values."""
        # Mock reaction: A + B -> C + D
        # ΔG = (ΔGf_C + ΔGf_D) - (ΔGf_A + ΔGf_B)
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00001'
        mock_rxn.build_reaction_string = Mock(return_value='A + B -> C + D')

        # Create mock metabolites
        met_A = Mock()
        met_A.id = 'cpd00001_c0'
        met_A.name = 'A'

        met_B = Mock()
        met_B.id = 'cpd00002_c0'
        met_B.name = 'B'

        met_C = Mock()
        met_C.id = 'cpd00003_c0'
        met_C.name = 'C'

        met_D = Mock()
        met_D.id = 'cpd00004_c0'
        met_D.name = 'D'

        # Stoichiometry: -1 for reactants, +1 for products
        mock_rxn.metabolites = {
            met_A: -1.0,
            met_B: -1.0,
            met_C: 1.0,
            met_D: 1.0
        }

        # Mock deltaG values
        deltag_values = {
            'cpd00001': -100.0,  # A
            'cpd00002': -200.0,  # B
            'cpd00003': -150.0,  # C
            'cpd00004': -180.0   # D
        }

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            with patch.object(mock_utils, 'get_compound_deltag', side_effect=lambda x: deltag_values.get(x)):
                with patch.object(mock_utils, 'get_compound_by_id', return_value=None):
                    result = mock_utils.get_reaction_deltag_from_formation('rxn00001')

        # ΔG = (-150 - 180) - (-100 - 200) = -330 - (-300) = -30
        assert result['deltag'] == -30.0
        assert result['reaction_id'] == 'rxn00001'
        assert len(result['compound_contributions']) == 4
        assert len(result['missing_compounds']) == 0
        assert len(result['warnings']) == 0

    def test_reaction_with_missing_compound(self, mock_utils):
        """Test reaction with a compound lacking deltaG."""
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00002'
        mock_rxn.build_reaction_string = Mock(return_value='A -> B')

        met_A = Mock()
        met_A.id = 'cpd00001_c0'
        met_A.name = 'A'

        met_B = Mock()
        met_B.id = 'cpd00002_c0'
        met_B.name = 'B'

        mock_rxn.metabolites = {
            met_A: -1.0,
            met_B: 1.0
        }

        # A has deltaG, B does not
        def mock_deltag(cpd_id):
            if cpd_id == 'cpd00001':
                return -100.0
            return None

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            with patch.object(mock_utils, 'get_compound_deltag', side_effect=mock_deltag):
                with patch.object(mock_utils, 'get_compound_by_id', return_value=None):
                    # Test with require_all_compounds=False
                    result = mock_utils.get_reaction_deltag_from_formation('rxn00002', require_all_compounds=False)

        assert len(result['missing_compounds']) == 1
        assert result['missing_compounds'][0]['compound_id'] == 'cpd00002'
        assert len(result['warnings']) > 0

    def test_reaction_with_missing_compound_error(self, mock_utils):
        """Test error when require_all_compounds=True and compound missing deltaG."""
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00003'
        mock_rxn.build_reaction_string = Mock(return_value='A -> B')

        met_A = Mock()
        met_A.id = 'cpd00001_c0'
        met_A.name = 'A'

        met_B = Mock()
        met_B.id = 'cpd00002_c0'
        met_B.name = 'B'

        mock_rxn.metabolites = {
            met_A: -1.0,
            met_B: 1.0
        }

        def mock_deltag(cpd_id):
            if cpd_id == 'cpd00001':
                return -100.0
            return None

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            with patch.object(mock_utils, 'get_compound_deltag', side_effect=mock_deltag):
                with patch.object(mock_utils, 'get_compound_by_id', return_value=None):
                    with pytest.raises(ValueError, match="contains compounds without valid deltaG values"):
                        mock_utils.get_reaction_deltag_from_formation('rxn00003', require_all_compounds=True)

    def test_reaction_not_found(self, mock_utils):
        """Test error when reaction not in database."""
        with patch.object(mock_utils, 'get_reaction_by_id', return_value=None):
            with pytest.raises(ValueError, match="not found in ModelSEED database"):
                mock_utils.get_reaction_deltag_from_formation('rxn_invalid')

    def test_reaction_with_stoichiometry_coefficients(self, mock_utils):
        """Test reaction with stoichiometric coefficients != 1."""
        # Mock reaction: 2A + 3B -> C
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00004'
        mock_rxn.build_reaction_string = Mock(return_value='2 A + 3 B -> C')

        met_A = Mock()
        met_A.id = 'cpd00001_c0'
        met_A.name = 'A'

        met_B = Mock()
        met_B.id = 'cpd00002_c0'
        met_B.name = 'B'

        met_C = Mock()
        met_C.id = 'cpd00003_c0'
        met_C.name = 'C'

        mock_rxn.metabolites = {
            met_A: -2.0,
            met_B: -3.0,
            met_C: 1.0
        }

        deltag_values = {
            'cpd00001': -50.0,   # A
            'cpd00002': -100.0,  # B
            'cpd00003': -500.0   # C
        }

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            with patch.object(mock_utils, 'get_compound_deltag', side_effect=lambda x: deltag_values.get(x)):
                with patch.object(mock_utils, 'get_compound_by_id', return_value=None):
                    result = mock_utils.get_reaction_deltag_from_formation('rxn00004')

        # ΔG = (-500) - (-2*50 - 3*100) = -500 - (-100 - 300) = -500 + 400 = -100
        assert result['deltag'] == -100.0
        assert result['compound_contributions']['cpd00001']['contribution'] == -2.0 * -50.0
        assert result['compound_contributions']['cpd00002']['contribution'] == -3.0 * -100.0

    def test_error_propagation(self, mock_utils):
        """Test error propagation in deltaG calculation."""
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00005'
        mock_rxn.build_reaction_string = Mock(return_value='A -> B')

        met_A = Mock()
        met_A.id = 'cpd00001_c0'
        met_A.name = 'A'

        met_B = Mock()
        met_B.id = 'cpd00002_c0'
        met_B.name = 'B'

        mock_rxn.metabolites = {
            met_A: -1.0,
            met_B: 1.0
        }

        # Create mock compounds with errors
        mock_cpd_A = Mock()
        mock_cpd_A.deltagerr = 2.0

        mock_cpd_B = Mock()
        mock_cpd_B.deltagerr = 3.0

        deltag_values = {
            'cpd00001': -100.0,
            'cpd00002': -150.0
        }

        def get_cpd_mock(cpd_id):
            if cpd_id == 'cpd00001':
                return mock_cpd_A
            elif cpd_id == 'cpd00002':
                return mock_cpd_B
            return None

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            with patch.object(mock_utils, 'get_compound_deltag', side_effect=lambda x: deltag_values.get(x)):
                with patch.object(mock_utils, 'get_compound_by_id', side_effect=get_cpd_mock):
                    result = mock_utils.get_reaction_deltag_from_formation('rxn00005')

        # Error propagation: σ² = Σ(νi² × σi²) = (1² × 2²) + (1² × 3²) = 4 + 9 = 13
        # σ = √13 ≈ 3.606
        assert result['deltag_error'] is not None
        assert abs(result['deltag_error'] - 3.606) < 0.01


class TestCalculateReactionDeltaG:
    """Test suite for calculate_reaction_deltag method."""

    def test_calculate_with_formation_energies(self, mock_utils):
        """Test calculation using compound formation energies."""
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00001'
        mock_rxn.build_reaction_string = Mock(return_value='A -> B')

        met_A = Mock()
        met_A.id = 'cpd00001_c0'

        met_B = Mock()
        met_B.id = 'cpd00002_c0'

        mock_rxn.metabolites = {met_A: -1.0, met_B: 1.0}

        deltag_values = {'cpd00001': -100.0, 'cpd00002': -150.0}

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            with patch.object(mock_utils, 'get_compound_deltag', side_effect=lambda x: deltag_values.get(x)):
                with patch.object(mock_utils, 'get_compound_by_id', return_value=None):
                    result = mock_utils.calculate_reaction_deltag('rxn00001', use_compound_formation=True)

        assert result['deltag'] == -50.0  # -150 - (-100)

    def test_calculate_with_reaction_stored_deltag(self, mock_utils):
        """Test calculation using reaction's stored deltaG."""
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00001'
        mock_rxn.deltag = -75.5
        mock_rxn.deltagerr = 5.0
        mock_rxn.build_reaction_string = Mock(return_value='A -> B')

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            result = mock_utils.calculate_reaction_deltag('rxn00001', use_compound_formation=False)

        assert result['deltag'] == -75.5
        assert result['deltag_error'] == 5.0
        assert result['source'] == 'reaction_attribute'

    def test_calculate_with_reaction_object(self, mock_utils):
        """Test calculation by passing reaction object instead of ID."""
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00001'
        mock_rxn.deltag = -80.0
        mock_rxn.deltagerr = None  # Explicitly set to None
        mock_rxn.build_reaction_string = Mock(return_value='A -> B')

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            result = mock_utils.calculate_reaction_deltag(mock_rxn, use_compound_formation=False)

        assert result['deltag'] == -80.0
        assert result['reaction_id'] == 'rxn00001'

    def test_calculate_reaction_not_found(self, mock_utils):
        """Test error when reaction not found."""
        with patch.object(mock_utils, 'get_reaction_by_id', return_value=None):
            with pytest.raises(ValueError, match="not found in ModelSEED database"):
                mock_utils.calculate_reaction_deltag('rxn_invalid')

    def test_calculate_with_unknown_stored_deltag(self, mock_utils):
        """Test handling of unknown stored deltaG value."""
        mock_rxn = Mock()
        mock_rxn.id = 'rxn00001'
        mock_rxn.deltag = 10000000  # Unknown marker
        mock_rxn.deltagerr = None  # Explicitly set to None
        mock_rxn.build_reaction_string = Mock(return_value='A -> B')

        with patch.object(mock_utils, 'get_reaction_by_id', return_value=mock_rxn):
            result = mock_utils.calculate_reaction_deltag('rxn00001', use_compound_formation=False)

        assert result['deltag'] is None
        assert any('unknown' in w.lower() for w in result['warnings'])


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
