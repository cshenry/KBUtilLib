"""
Comprehensive tests for EscherUtils enhanced arrow visualization

Tests cover:
- Helper methods for arrow directionality configuration
- Enhanced create_map_html functionality
- Output format handling (HTML, SVG, both)
- Backward compatibility
- Input validation
- Edge cases
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from kbutillib.escher_utils import EscherUtils, DEFAULT_ARROW_WIDTH_RANGE, DEFAULT_FLUX_COLOR_SCHEMES


# Fixtures

@pytest.fixture
def escher_utils():
    """Create EscherUtils instance for testing."""
    return EscherUtils()


@pytest.fixture
def mock_cobra_model():
    """Create a mock COBRApy model."""
    model = Mock()
    model.id = 'test_model'
    model.reactions = [Mock(id=f'R{i}') for i in range(10)]
    model.metabolites = [Mock(id=f'M{i}') for i in range(8)]
    model.genes = [Mock(id=f'G{i}') for i in range(5)]
    return model


@pytest.fixture
def sample_flux_data():
    """Sample flux solution data."""
    return {
        'PGI': 5.2,
        'PFK': 7.3,
        'FBA': 7.3,
        'GAPD': 16.0,
        'PGK': -16.0,  # Negative flux
        'PGM': -14.7,  # Negative flux
        'ENO': 14.7,
        'PYK': -1.8,   # Negative flux
        'LDH': 0.0001, # Near-zero flux
    }


@pytest.fixture
def positive_flux_data():
    """Flux data with only positive values."""
    return {
        'R1': 1.0,
        'R2': 5.0,
        'R3': 10.0,
        'R4': 15.0,
        'R5': 20.0,
    }


@pytest.fixture
def mock_builder():
    """Create a mock Escher Builder."""
    builder = Mock()
    builder.reaction_scale = []
    builder.reaction_styles = []
    builder.reaction_data = {}
    builder.reaction_data_threshold = 1e-6
    builder._repr_html_ = Mock(return_value='<html><svg></svg></html>')
    builder.save_html = Mock()
    return builder


# Test Helper Methods

class TestGetFluxDirection:
    """Tests for _get_flux_direction method."""

    def test_forward_flux(self, escher_utils):
        """Test detection of forward flux."""
        result = escher_utils._get_flux_direction('R1', 10.0, None)
        assert result == 'forward'

    def test_reverse_flux(self, escher_utils):
        """Test detection of reverse flux."""
        result = escher_utils._get_flux_direction('R1', -10.0, None)
        assert result == 'reverse'

    def test_bidirectional_zero(self, escher_utils):
        """Test detection of near-zero flux as bidirectional."""
        result = escher_utils._get_flux_direction('R1', 0.0, None)
        assert result == 'bidirectional'

    def test_bidirectional_small_positive(self, escher_utils):
        """Test small positive flux below threshold."""
        result = escher_utils._get_flux_direction('R1', 1e-7, None, threshold=1e-6)
        assert result == 'bidirectional'

    def test_bidirectional_small_negative(self, escher_utils):
        """Test small negative flux below threshold."""
        result = escher_utils._get_flux_direction('R1', -1e-7, None, threshold=1e-6)
        assert result == 'bidirectional'


class TestCalculateOptimalReactionScale:
    """Tests for _calculate_optimal_reaction_scale method."""

    def test_default_arrow_width_range(self, escher_utils, positive_flux_data):
        """Test using default arrow width range."""
        scale = escher_utils._calculate_optimal_reaction_scale(positive_flux_data)

        assert isinstance(scale, list)
        assert len(scale) == 5  # zero, Q1, median, Q3, max
        assert scale[0]['size'] == 2  # min width
        assert scale[-1]['size'] == 20  # max width

    def test_custom_arrow_width_range(self, escher_utils, positive_flux_data):
        """Test with custom arrow width range."""
        scale = escher_utils._calculate_optimal_reaction_scale(
            positive_flux_data,
            arrow_width_range=(5, 30)
        )

        assert scale[0]['size'] == 5  # custom min
        assert scale[-1]['size'] == 30  # custom max

    def test_empty_flux_data(self, escher_utils):
        """Test with no significant fluxes."""
        scale = escher_utils._calculate_optimal_reaction_scale({})

        assert isinstance(scale, list)
        assert len(scale) == 1
        assert scale[0]['type'] == 'value'

    def test_scale_has_quartile_types(self, escher_utils, positive_flux_data):
        """Test that scale uses statistical types."""
        scale = escher_utils._calculate_optimal_reaction_scale(positive_flux_data)

        types = [stop['type'] for stop in scale]
        assert 'Q1' in types
        assert 'median' in types
        assert 'Q3' in types
        assert 'max' in types


class TestCreateDirectionalColorScheme:
    """Tests for _create_directional_color_scheme method."""

    def test_magnitude_scheme(self, escher_utils):
        """Test magnitude color scheme."""
        scheme = escher_utils._create_directional_color_scheme('magnitude')

        assert isinstance(scheme, list)
        assert len(scheme) > 0
        assert scheme == DEFAULT_FLUX_COLOR_SCHEMES['magnitude']

    def test_directional_scheme(self, escher_utils):
        """Test directional color scheme."""
        scheme = escher_utils._create_directional_color_scheme('directional')

        assert isinstance(scheme, list)
        assert scheme == DEFAULT_FLUX_COLOR_SCHEMES['directional']

    def test_custom_scheme(self, escher_utils):
        """Test custom color scheme."""
        custom = [{'type': 'min', 'color': '#000000', 'size': 5}]
        scheme = escher_utils._create_directional_color_scheme('custom', custom)

        assert scheme == custom

    def test_invalid_scheme_returns_default(self, escher_utils):
        """Test that invalid scheme type returns magnitude default."""
        scheme = escher_utils._create_directional_color_scheme('invalid')

        assert scheme == DEFAULT_FLUX_COLOR_SCHEMES['magnitude']


class TestEnhanceReactionStylesForDirectionality:
    """Tests for _enhance_reaction_styles_for_directionality method."""

    def test_with_negative_fluxes_emphasize_direction(self, escher_utils, sample_flux_data):
        """Test styles with negative fluxes and direction emphasis."""
        styles = escher_utils._enhance_reaction_styles_for_directionality(
            sample_flux_data,
            emphasize_direction=True
        )

        assert 'color' in styles
        assert 'size' in styles
        assert 'text' in styles
        assert 'abs' not in styles  # Should NOT have abs for directional

    def test_without_negative_fluxes(self, escher_utils, positive_flux_data):
        """Test styles with only positive fluxes."""
        styles = escher_utils._enhance_reaction_styles_for_directionality(
            positive_flux_data,
            emphasize_direction=True
        )

        assert 'abs' in styles  # Should have abs when no negatives

    def test_no_direction_emphasis(self, escher_utils, sample_flux_data):
        """Test styles without direction emphasis."""
        styles = escher_utils._enhance_reaction_styles_for_directionality(
            sample_flux_data,
            emphasize_direction=False
        )

        assert 'abs' in styles  # Always has abs when not emphasizing direction


# Test SVG/HTML Helper Methods

class TestExtractSvgFromBuilder:
    """Tests for SVG extraction methods."""

    def test_extract_svg_from_repr_html(self, escher_utils, mock_builder):
        """Test SVG extraction from _repr_html_()."""
        mock_builder._repr_html_ = Mock(
            return_value='<html><body><svg>content</svg></body></html>'
        )

        svg = escher_utils._extract_svg_from_builder(mock_builder)

        assert svg is not None
        assert '<svg>' in svg
        assert '</svg>' in svg

    def test_validate_svg_content_valid(self, escher_utils):
        """Test validation of valid SVG."""
        valid_svg = '<svg><rect/></svg>'
        assert escher_utils._validate_svg_content(valid_svg) is True

    def test_validate_svg_content_invalid(self, escher_utils):
        """Test validation of invalid SVG."""
        assert escher_utils._validate_svg_content('') is False
        assert escher_utils._validate_svg_content('<div></div>') is False


# Test create_map_html Enhanced Functionality

class TestCreateMapHtmlEnhanced:
    """Tests for enhanced create_map_html functionality."""

    @patch('kbutillib.escher_utils.Builder')
    def test_standard_behavior_without_enhancements(
        self, mock_builder_class, escher_utils, mock_cobra_model
    ):
        """Test standard behavior when enhanced_arrows=False."""
        mock_builder = Mock()
        mock_builder_class.return_value = mock_builder

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, 'test.html')

            result = escher_utils.create_map_html(
                model=mock_cobra_model,
                output_file=output_file,
                enhanced_arrows=False
            )

            assert os.path.exists(result)
            assert result.endswith('.html')

    @patch('kbutillib.escher_utils.Builder')
    def test_enhanced_arrows_with_flux_data(
        self, mock_builder_class, escher_utils, mock_cobra_model, positive_flux_data
    ):
        """Test enhanced arrows with flux data."""
        mock_builder = Mock()
        mock_builder.reaction_scale = []
        mock_builder.reaction_styles = []
        mock_builder_class.return_value = mock_builder

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, 'enhanced.html')

            result = escher_utils.create_map_html(
                model=mock_cobra_model,
                flux_solution=positive_flux_data,
                output_file=output_file,
                enhanced_arrows=True
            )

            # Verify enhanced configuration was applied
            assert mock_builder.reaction_styles is not None
            assert os.path.exists(result)

    @patch('kbutillib.escher_utils.Builder')
    def test_output_format_svg(
        self, mock_builder_class, escher_utils, mock_cobra_model, positive_flux_data
    ):
        """Test SVG-only output format."""
        mock_builder = Mock()
        mock_builder._repr_html_ = Mock(
            return_value='<html><svg><rect/></svg></html>'
        )
        mock_builder_class.return_value = mock_builder

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, 'test.html')

            result = escher_utils.create_map_html(
                model=mock_cobra_model,
                flux_solution=positive_flux_data,
                output_file=output_file,
                output_format='svg'
            )

            assert result.endswith('.svg')

    @patch('kbutillib.escher_utils.Builder')
    def test_output_format_both(
        self, mock_builder_class, escher_utils, mock_cobra_model, positive_flux_data
    ):
        """Test generating both HTML and SVG."""
        mock_builder = Mock()
        mock_builder._repr_html_ = Mock(
            return_value='<html><svg><rect/></svg></html>'
        )
        mock_builder_class.return_value = mock_builder

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, 'test.html')

            result = escher_utils.create_map_html(
                model=mock_cobra_model,
                flux_solution=positive_flux_data,
                output_file=output_file,
                output_format='both'
            )

            assert isinstance(result, dict)
            assert 'html' in result
            assert 'svg' in result

    def test_input_validation_negative_threshold(self, escher_utils, mock_cobra_model):
        """Test validation of negative flux_threshold."""
        with pytest.raises(ValueError, match="flux_threshold must be non-negative"):
            escher_utils.create_map_html(
                model=mock_cobra_model,
                flux_threshold=-1.0
            )

    def test_input_validation_invalid_output_format(
        self, escher_utils, mock_cobra_model
    ):
        """Test validation of invalid output format."""
        with pytest.raises(ValueError, match="output_format must be"):
            escher_utils.create_map_html(
                model=mock_cobra_model,
                output_format='invalid'
            )


# Integration Tests

class TestBackwardCompatibility:
    """Tests to ensure backward compatibility."""

    @patch('kbutillib.escher_utils.Builder')
    def test_old_function_call_still_works(
        self, mock_builder_class, escher_utils, mock_cobra_model
    ):
        """Test that old-style function calls work without modification."""
        mock_builder = Mock()
        mock_builder_class.return_value = mock_builder

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = os.path.join(tmpdir, 'legacy.html')

            # Old-style call without any new parameters
            result = escher_utils.create_map_html(
                model=mock_cobra_model,
                output_file=output_file
            )

            assert isinstance(result, str)
            assert result.endswith('.html')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
