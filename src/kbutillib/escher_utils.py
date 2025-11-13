"""Utilities for managing and visualizing models on escher maps."""

import pickle
from typing import Any, Dict, Optional, Union, Literal, Tuple
import pandas as pd
import re
import json
import os
from pathlib import Path
import statistics

from cobra.flux_analysis import flux_variability_analysis
from cobra.flux_analysis import pfba

from .kb_model_utils import KBModelUtils
from .ms_biochem_utils import MSBiochemUtils

# Default arrow width range for enhanced visualization
DEFAULT_ARROW_WIDTH_RANGE = (2, 20)

# Default color schemes for different visualization types
DEFAULT_FLUX_COLOR_SCHEMES = {
    'magnitude': [
        {'type': 'value', 'value': 0, 'color': '#f0f0f0', 'size': 2},
        {'type': 'Q1', 'color': '#6699ff', 'size': 8},
        {'type': 'median', 'color': '#3366cc', 'size': 12},
        {'type': 'Q3', 'color': '#ff8866', 'size': 16},
        {'type': 'max', 'color': '#cc3300', 'size': 20}
    ],
    'directional': [
        {'type': 'min', 'color': '#cc3300', 'size': 15},
        {'type': 'value', 'value': 0, 'color': '#eeeeee', 'size': 3},
        {'type': 'max', 'color': '#33cc00', 'size': 15}
    ]
}

class EscherUtils(KBModelUtils, MSBiochemUtils):
    """Tools for managing and visualizing models on escher maps
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize Escher utilities

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvironment
        """
        super().__init__(**kwargs)

    def _get_flux_direction(
        self,
        reaction_id: str,
        flux_value: float,
        model,
        threshold: float = 1e-6
    ) -> Literal['forward', 'reverse', 'bidirectional']:
        """Determine flux direction relative to reaction definition.

        Args:
            reaction_id: Reaction identifier
            flux_value: Flux value (positive = forward, negative = reverse)
            model: COBRApy model object
            threshold: Minimum absolute flux to consider directional

        Returns:
            'forward' if flux > threshold
            'reverse' if flux < -threshold
            'bidirectional' if |flux| <= threshold
        """
        if abs(flux_value) <= threshold:
            return 'bidirectional'
        return 'forward' if flux_value > 0 else 'reverse'

    def _calculate_optimal_reaction_scale(
        self,
        flux_data: Dict[str, float],
        arrow_width_range: Optional[Tuple[float, float]] = None
    ) -> list:
        """Generate smart reaction_scale based on flux distribution.

        Args:
            flux_data: Dict mapping reaction IDs to flux values
            arrow_width_range: (min_width, max_width) tuple for arrow sizing

        Returns:
            List of reaction_scale configuration objects
        """
        if arrow_width_range is None:
            arrow_width_range = DEFAULT_ARROW_WIDTH_RANGE

        min_width, max_width = arrow_width_range

        # Get absolute values for sizing (preserve sign for direction)
        abs_values = [abs(v) for v in flux_data.values() if abs(v) > 1e-10]

        if not abs_values:
            # No significant fluxes, return minimal scale
            return [
                {'type': 'value', 'value': 0, 'color': '#dcdcdc', 'size': min_width}
            ]

        # Calculate statistics
        min_flux = min(abs_values)
        max_flux = max(abs_values)

        # Use statistical scaling with quartiles
        return [
            {'type': 'value', 'value': 0, 'color': '#f0f0f0', 'size': min_width},
            {'type': 'Q1', 'color': '#6699ff', 'size': min_width + (max_width - min_width) * 0.3},
            {'type': 'median', 'color': '#3366cc', 'size': min_width + (max_width - min_width) * 0.5},
            {'type': 'Q3', 'color': '#ff8866', 'size': min_width + (max_width - min_width) * 0.75},
            {'type': 'max', 'color': '#cc3300', 'size': max_width}
        ]

    def _create_directional_color_scheme(
        self,
        scheme_type: Literal['magnitude', 'directional', 'custom'] = 'magnitude',
        custom_scheme: Optional[list] = None
    ) -> list:
        """Create color scheme configuration for flux visualization.

        Args:
            scheme_type: Type of color scheme to use
            custom_scheme: Custom reaction_scale configuration (if scheme_type='custom')

        Returns:
            List of reaction_scale configuration objects
        """
        if scheme_type == 'custom' and custom_scheme:
            return custom_scheme

        if scheme_type in DEFAULT_FLUX_COLOR_SCHEMES:
            return DEFAULT_FLUX_COLOR_SCHEMES[scheme_type].copy()

        # Default to magnitude scheme
        return DEFAULT_FLUX_COLOR_SCHEMES['magnitude'].copy()

    def _enhance_reaction_styles_for_directionality(
        self,
        flux_data: Optional[Dict[str, float]] = None,
        emphasize_direction: bool = True
    ) -> list:
        """Configure reaction_styles to emphasize arrows and directionality.

        Args:
            flux_data: Flux data to analyze for negative values
            emphasize_direction: Whether to emphasize flux direction

        Returns:
            List of reaction style options ['color', 'size', etc.]
        """
        styles = ['color', 'size', 'text']

        # Add 'abs' if we're not emphasizing direction OR if no negative fluxes
        if flux_data and emphasize_direction:
            has_negative = any(v < -1e-6 for v in flux_data.values())
            if not has_negative:
                styles.insert(2, 'abs')  # Insert before 'text'
        elif not emphasize_direction:
            styles.insert(2, 'abs')

        return styles

    def _extract_svg_from_builder(self, builder) -> Optional[str]:
        """Extract SVG content from Escher Builder object.

        Args:
            builder: Escher Builder object

        Returns:
            SVG content as string, or None if extraction fails
        """
        try:
            # Try to get HTML representation and extract SVG
            if hasattr(builder, '_repr_html_'):
                html_repr = builder._repr_html_()
                return self._parse_svg_from_html(html_repr)
        except Exception as e:
            self.log_warning(f"Could not extract SVG from builder: {e}")

        # Fallback: try save_html to temp file then parse
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as tmp:
                tmp_path = tmp.name

            builder.save_html(tmp_path)
            with open(tmp_path, 'r', encoding='utf-8') as f:
                html_content = f.read()

            os.unlink(tmp_path)
            return self._parse_svg_from_html(html_content)

        except Exception as e:
            self.log_warning(f"Fallback SVG extraction failed: {e}")
            return None

    def _parse_svg_from_html(self, html_content: str) -> Optional[str]:
        """Parse SVG content from HTML string.

        Args:
            html_content: HTML string containing SVG

        Returns:
            SVG content as string, or None if not found
        """
        # Try using regex to extract SVG (simple approach)
        svg_pattern = r'(<svg[^>]*>.*?</svg>)'
        matches = re.findall(svg_pattern, html_content, re.DOTALL)

        if matches:
            return matches[0]  # Return first SVG found

        # If regex fails, try more basic extraction
        start_tag = '<svg'
        end_tag = '</svg>'

        start_idx = html_content.find(start_tag)
        if start_idx == -1:
            return None

        end_idx = html_content.find(end_tag, start_idx)
        if end_idx == -1:
            return None

        return html_content[start_idx:end_idx + len(end_tag)]

    def _validate_svg_content(self, svg_string: str) -> bool:
        """Validate SVG structure.

        Args:
            svg_string: SVG content to validate

        Returns:
            True if valid SVG structure, False otherwise
        """
        if not svg_string:
            return False

        # Basic validation: check for svg tags
        has_open_tag = '<svg' in svg_string
        has_close_tag = '</svg>' in svg_string

        return has_open_tag and has_close_tag

    def _save_svg_file(self, svg_content: str, output_path: Union[str, Path]) -> str:
        """Write standalone SVG file.

        Args:
            svg_content: SVG content as string
            output_path: Output file path

        Returns:
            Absolute path to saved SVG file
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            # Add XML declaration if not present
            if not svg_content.strip().startswith('<?xml'):
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write(svg_content)

        self.log_info(f"SVG file saved to: {output_path.absolute()}")
        return str(output_path.absolute())

    def _generate_html_with_enhanced_escher(
        self,
        builder,
        title: str,
        metadata: Optional[Dict] = None
    ) -> str:
        """Create HTML with optimally configured Escher map.

        Args:
            builder: Configured Escher Builder object
            title: HTML page title
            metadata: Additional metadata to display

        Returns:
            Complete HTML content as string
        """
        # Use existing _get_escher_html_embed method
        escher_embed = self._get_escher_html_embed(builder)

        # Prepare metadata display
        metadata_html = ""
        if metadata:
            metadata_items = [
                f'<div class="data-info">{key}: {value}</div>'
                for key, value in metadata.items()
            ]
            metadata_html = "\n        ".join(metadata_items)

        html_template = f"""<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            text-align: center;
            margin-bottom: 20px;
            padding: 20px;
            background-color: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .map-container {{
            background-color: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            padding: 20px;
            overflow: auto;
        }}
        .data-info {{
            display: inline-block;
            margin: 5px 15px;
            padding: 5px 10px;
            background-color: #e8f4f8;
            border-radius: 4px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{title}</h1>
        {metadata_html}
    </div>

    <div class="map-container">
        <div id="escher-map"></div>
    </div>

    <script>
        {escher_embed}
    </script>
</body>
</html>"""

        return html_template

    def create_map_html(
        self,
        model,
        flux_solution: Optional[Union[Dict, pd.Series]] = None,
        map_json: Optional[Union[str, Dict]] = None,
        output_file: str = "escher_map.html",
        metabolomic_data: Optional[Dict] = None,
        transcriptomic_data: Optional[Dict] = None,
        proteomic_data: Optional[Dict] = None,
        title: str = "Metabolic Map Visualization",
        map_name: Optional[str] = None,
        reaction_scale: Optional[list] = None,
        metabolite_scale: Optional[list] = None,
        gene_scale: Optional[list] = None,
        height: int = 600,
        width: int = 900,
        # Enhanced arrow visualization parameters
        enhanced_arrows: bool = True,
        output_format: Literal['html', 'svg', 'both'] = 'html',
        arrow_width_scale: Optional[Tuple[float, float]] = None,
        flux_threshold: Optional[float] = None,
        arrow_color_scheme: Union[str, Dict, list] = 'magnitude',
        arrow_directionality: bool = True,
        **escher_kwargs
    ) -> Union[str, Dict[str, str]]:
        """Create an HTML file that renders an Escher map with model data.

        Args:
            model: COBRApy model object or MSModelUtil object
            flux_solution: Flux solution data (dict or pandas Series with reaction IDs as keys/index)
            map_json: Path to Escher map JSON file or map data as dict
            output_file: Output HTML file path
            metabolomic_data: Metabolite concentration data (dict with metabolite IDs as keys)
            transcriptomic_data: Gene expression data (dict with gene IDs as keys)
            proteomic_data: Protein abundance data (dict with gene/protein IDs as keys)
            title: HTML page title
            map_name: Name to display for the map
            reaction_scale: Custom reaction color scale for flux data
            metabolite_scale: Custom metabolite color scale for concentration data
            gene_scale: Custom gene color scale for expression/proteomic data
            height: Map height in pixels
            width: Map width in pixels
            enhanced_arrows: Enable enhanced arrow visualization with smart defaults (default: True)
            output_format: Output format - 'html', 'svg', or 'both' (default: 'html')
            arrow_width_scale: Tuple of (min_width, max_width) for arrow sizing (default: (2, 20))
            flux_threshold: Minimum flux value to display (default: 1e-6)
            arrow_color_scheme: Color scheme type - 'magnitude', 'directional', or custom list (default: 'magnitude')
            arrow_directionality: Emphasize flux direction in visualization (default: True)
            **escher_kwargs: Additional arguments passed to Escher Builder

        Returns:
            str: Path to the created HTML file (if output_format='html' or 'svg')
            Dict[str, str]: Dict with 'html' and 'svg' keys (if output_format='both')

        Raises:
            ImportError: If escher package is not available
            ValueError: If required data is missing or invalid
            
        Example:
            >>> from kbutillib import EscherUtils
            >>> import cobra
            >>> 
            >>> # Load model and run FBA
            >>> model = cobra.test.create_test_model("textbook")
            >>> solution = model.optimize()
            >>> 
            >>> # Create Escher visualization
            >>> escher_utils = EscherUtils()
            >>> html_file = escher_utils.create_map_html(
            ...     model=model,
            ...     flux_solution=solution.fluxes,
            ...     map_name="E. coli core",
            ...     title="FBA Results Visualization",
            ...     output_file="my_map.html"
            ... )
            >>> 
            >>> # With additional data
            >>> metabolite_data = {"glc__D_e": 10.0, "pyr_c": 5.2}
            >>> gene_data = {"b0008": 2.5, "b0114": 1.8}
            >>> 
            >>> html_file = escher_utils.create_map_html(
            ...     model=model,
            ...     flux_solution=solution.fluxes,
            ...     metabolomic_data=metabolite_data,
            ...     transcriptomic_data=gene_data,
            ...     output_file="enhanced_map.html"
            ... )
            >>>
            >>> # With enhanced arrow visualization (enabled by default)
            >>> html_file = escher_utils.create_map_html(
            ...     model=model,
            ...     flux_solution=solution.fluxes,
            ...     enhanced_arrows=True,  # Smart arrow scaling and coloring
            ...     arrow_color_scheme='magnitude',  # or 'directional'
            ...     output_format='both',  # Generate both HTML and SVG
            ...     output_file="optimized_map.html"
            ... )
            >>> # Returns: {'html': '/path/to/optimized_map.html', 'svg': '/path/to/optimized_map.svg'}
        """
        try:
            from escher import Builder
        except ImportError:
            raise ImportError("escher package is required for map visualization. Install with: pip install escher")
        
        # Handle model input (COBRApy model or MSModelUtil)
        if hasattr(model, 'model'):
            # MSModelUtil object
            cobra_model = model.model
        else:
            # Assume it's already a COBRApy model
            cobra_model = model
            
        # Validate model
        if not hasattr(cobra_model, 'reactions'):
            raise ValueError("Invalid model object. Must be a COBRApy model or MSModelUtil object.")

        # Task 4.4: Input validation for new parameters
        if flux_threshold is not None and flux_threshold < 0:
            raise ValueError("flux_threshold must be non-negative")

        if output_format not in ['html', 'svg', 'both']:
            raise ValueError("output_format must be 'html', 'svg', or 'both'")

        # Convert flux_solution to dict if needed
        flux_data_dict = None
        if flux_solution is not None:
            if isinstance(flux_solution, pd.Series):
                flux_data_dict = flux_solution.to_dict()
            elif isinstance(flux_solution, dict):
                flux_data_dict = flux_solution
            else:
                raise ValueError("flux_solution must be a dictionary or pandas Series")

        # Handle map data
        map_data = None
        if map_json:
            if isinstance(map_json, str):
                # Path to JSON file
                if os.path.exists(map_json):
                    with open(map_json, 'r') as f:
                        map_data = json.load(f)
                else:
                    # Try to treat as JSON string
                    try:
                        map_data = json.loads(map_json)
                    except json.JSONDecodeError:
                        raise ValueError(f"Map file not found: {map_json}")
            elif isinstance(map_json, dict):
                map_data = map_json
            else:
                raise ValueError("map_json must be a file path, JSON string, or dictionary")

        # Tasks 4.5-4.10: Enhanced arrows implementation
        reaction_styles_configured = None

        if enhanced_arrows and flux_data_dict:
            # Task 4.10: Log enhanced visualization
            self.log_info("Using enhanced arrow visualization with smart defaults")

            # Task 4.6: Calculate optimal reaction_scale if not provided
            if reaction_scale is None:
                reaction_scale = self._calculate_optimal_reaction_scale(
                    flux_data_dict,
                    arrow_width_scale
                )
                self.log_info(f"Generated smart reaction_scale with {len(reaction_scale)} stops")

            # Task 4.7: Apply directional color scheme
            if isinstance(arrow_color_scheme, str):
                # Check if we should use directional scheme
                if arrow_color_scheme == 'directional':
                    scheme_config = self._create_directional_color_scheme('directional')
                    # Override reaction_scale with directional scheme if not custom
                    if reaction_scale == self._calculate_optimal_reaction_scale(flux_data_dict, arrow_width_scale):
                        reaction_scale = scheme_config
                elif arrow_color_scheme == 'magnitude':
                    # Already set by _calculate_optimal_reaction_scale
                    pass
            elif isinstance(arrow_color_scheme, (list, dict)):
                # Custom scheme provided
                reaction_scale = arrow_color_scheme

            # Task 4.8: Configure reaction_styles for directionality
            reaction_styles_configured = self._enhance_reaction_styles_for_directionality(
                flux_data_dict,
                emphasize_direction=arrow_directionality
            )

        # Task 4.11: Backward compatibility - Set default scales if not provided
        # This ensures existing behavior when enhanced_arrows=False or no flux data
        if reaction_scale is None:
            reaction_scale = [
                {"type": "value", "value": 0, "color": "#dcdcdc", "size": 10},
                {"type": "value", "value": 0.000001, "color": "#9696ff", "size": 15},
                {"type": "value", "value": 1, "color": "#ff6666", "size": 25},
                {"type": "value", "value": 10, "color": "#ff0000", "size": 35},
                {"type": "value", "value": 100, "color": "#cc0000", "size": 50}
            ]
            
        if metabolite_scale is None:
            metabolite_scale = [
                {"type": "value", "value": 0, "color": "#f0f0f0", "size": 10},
                {"type": "value", "value": 0.001, "color": "#6699ff", "size": 15},
                {"type": "value", "value": 0.1, "color": "#0066cc", "size": 20},
                {"type": "value", "value": 1, "color": "#003d7a", "size": 25}
            ]
            
        if gene_scale is None:
            gene_scale = [
                {"type": "value", "value": 0, "color": "#e0e0e0", "size": 8},
                {"type": "value", "value": 0.5, "color": "#ffcc66", "size": 12},
                {"type": "value", "value": 1, "color": "#ff9900", "size": 16},
                {"type": "value", "value": 2, "color": "#cc6600", "size": 20},
                {"type": "value", "value": 5, "color": "#993300", "size": 25}
            ]
        
        # Create Escher Builder
        builder_kwargs = {
            'model': cobra_model,
            'height': height,
            'width': width,
            **escher_kwargs
        }

        if map_data:
            # Escher Builder expects map_json as a JSON string, not a dict
            builder_kwargs['map_json'] = json.dumps(map_data)
        elif map_name:
            builder_kwargs['map_name'] = map_name

        builder = Builder(**builder_kwargs)

        # Set scales
        builder.reaction_scale = reaction_scale
        builder.metabolite_scale = metabolite_scale
        builder.gene_scale = gene_scale

        # Task 4.8 & 4.9: Apply enhanced reaction_styles if configured
        if reaction_styles_configured:
            builder.reaction_styles = reaction_styles_configured

        # Apply flux threshold if specified
        if flux_threshold is not None:
            builder.reaction_data_threshold = flux_threshold
        elif enhanced_arrows:
            # Use default threshold for enhanced arrows
            builder.reaction_data_threshold = 1e-6

        # Add flux data
        if flux_data_dict is not None:
            builder.reaction_data = flux_data_dict
        
        # Add metabolomic data
        if metabolomic_data is not None:
            if isinstance(metabolomic_data, dict):
                builder.metabolite_data = metabolomic_data
            else:
                raise ValueError("metabolomic_data must be a dictionary")
        
        # Add gene expression/proteomic data
        gene_data = {}
        if transcriptomic_data is not None:
            if isinstance(transcriptomic_data, dict):
                gene_data.update(transcriptomic_data)
            else:
                raise ValueError("transcriptomic_data must be a dictionary")
                
        if proteomic_data is not None:
            if isinstance(proteomic_data, dict):
                gene_data.update(proteomic_data)
            else:
                raise ValueError("proteomic_data must be a dictionary")
        
        if gene_data:
            builder.gene_data = gene_data
        
        # Get Escher map HTML and scripts
        escher_html = self._get_escher_html_embed(builder)
        
        # Extract div and scripts from Escher HTML
        import re
        # Find the main div (usually has id or class related to escher)
        div_pattern = r'<div[^>]*id=["\']([^"\']*)["\'][^>]*>.*?</div>'
        div_match = re.search(r'<div[^>]*>.*?</div>', escher_html, re.DOTALL)
        escher_div = div_match.group(0) if div_match else '<div id="escher-map"></div>'
        
        # Extract all script tags
        script_pattern = r'<script[^>]*>.*?</script>'
        scripts = re.findall(script_pattern, escher_html, re.DOTALL)
        escher_scripts = '\n'.join(scripts) if scripts else ''
        
        # Create HTML content
        html_template = f"""<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            text-align: center;
            margin-bottom: 20px;
            padding: 20px;
            background-color: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .map-container {{
            background-color: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            padding: 20px;
            overflow: auto;
        }}
        .info-panel {{
            margin-top: 20px;
            padding: 15px;
            background-color: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .data-info {{
            display: inline-block;
            margin: 5px 15px;
            padding: 5px 10px;
            background-color: #e8f4f8;
            border-radius: 4px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{title}</h1>
        {f'<h2>Map: {map_name}</h2>' if map_name else ''}
        <div class="data-info">Model: {cobra_model.id if hasattr(cobra_model, 'id') else 'Unnamed'}</div>
        <div class="data-info">Reactions: {len(cobra_model.reactions)}</div>
        <div class="data-info">Metabolites: {len(cobra_model.metabolites)}</div>
        <div class="data-info">Genes: {len(cobra_model.genes)}</div>
        {f'<div class="data-info">Flux data: {len(flux_solution)} reactions</div>' if flux_solution else ''}
        {f'<div class="data-info">Metabolomic data: {len(metabolomic_data)} metabolites</div>' if metabolomic_data else ''}
        {f'<div class="data-info">Transcriptomic data: {len(transcriptomic_data)} genes</div>' if transcriptomic_data else ''}
        {f'<div class="data-info">Proteomic data: {len(proteomic_data)} proteins</div>' if proteomic_data else ''}
    </div>
    
    <div class="map-container">
        {escher_div}
    </div>
    
    <div class="info-panel">
        <h3>Legend</h3>
        <p><strong>Reactions:</strong> Color and size indicate flux magnitude</p>
        {f'<p><strong>Metabolites:</strong> Color indicates concentration levels</p>' if metabolomic_data else ''}
        {f'<p><strong>Genes:</strong> Color indicates expression/protein abundance levels</p>' if (transcriptomic_data or proteomic_data) else ''}
        <p>Generated using Escher pathway visualization</p>
    </div>
    
    {escher_scripts}
</body>
</html>"""
        
        # Task 5.0: Handle output format
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Task 5.2: Determine SVG file path if needed
        if output_format in ['svg', 'both']:
            # Replace .html extension with .svg, or add .svg
            if output_path.suffix.lower() == '.html':
                svg_path = output_path.with_suffix('.svg')
            else:
                svg_path = Path(str(output_path) + '.svg')

        # Task 5.1 & 5.3-5.6: Generate output based on format
        if output_format == 'html':
            # Standard HTML output
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_template)
            self.log_info(f"Escher map HTML saved to: {output_path.absolute()}")
            return str(output_path.absolute())

        elif output_format == 'svg':
            # SVG-only output
            svg_content = self._extract_svg_from_builder(builder)
            if svg_content and self._validate_svg_content(svg_content):
                svg_file = self._save_svg_file(svg_content, svg_path)
                return svg_file
            else:
                self.log_warning("SVG extraction failed, falling back to HTML output")
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(html_template)
                self.log_info(f"Escher map HTML saved to: {output_path.absolute()}")
                return str(output_path.absolute())

        elif output_format == 'both':
            # Generate both HTML and SVG
            results = {}

            # Save HTML
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_template)
            self.log_info(f"Escher map HTML saved to: {output_path.absolute()}")
            results['html'] = str(output_path.absolute())

            # Save SVG
            svg_content = self._extract_svg_from_builder(builder)
            if svg_content and self._validate_svg_content(svg_content):
                svg_file = self._save_svg_file(svg_content, svg_path)
                results['svg'] = svg_file
            else:
                self.log_warning("SVG extraction failed, only HTML generated")
                results['svg'] = None

            return results
    
    def _get_escher_html_embed(self, builder) -> str:
        """Get the HTML/JavaScript code to embed the Escher map.
        
        Args:
            builder: Escher Builder object
            
        Returns:
            str: Complete HTML including div and scripts to render the map
        """
        try:
            # Method 1: Try _repr_html_() - this is the standard Jupyter/IPython method
            if hasattr(builder, '_repr_html_'):
                try:
                    html_repr = builder._repr_html_()
                    if html_repr and html_repr.strip():
                        self.log_info("Successfully got HTML from builder._repr_html_()")
                        return html_repr
                    else:
                        self.log_warning("builder._repr_html_() returned empty string")
                except Exception as e:
                    self.log_warning(f"builder._repr_html_() raised exception: {e}")
            
            # Method 2: Try save_html to temp file and read it
            if hasattr(builder, 'save_html'):
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as tmp_file:
                    tmp_path = tmp_file.name
                
                try:
                    builder.save_html(tmp_path)
                    with open(tmp_path, 'r', encoding='utf-8') as f:
                        full_html = f.read()
                    
                    # Extract body content (div and scripts)
                    import re
                    # Find the body content
                    body_match = re.search(r'<body[^>]*>(.*?)</body>', full_html, re.DOTALL)
                    if body_match:
                        body_content = body_match.group(1)
                        # Also get any scripts in head
                        head_scripts = re.findall(r'<script[^>]*>.*?</script>', full_html, re.DOTALL)
                        result = body_content
                        if head_scripts:
                            result = '\n'.join(head_scripts) + '\n' + result
                        # Clean up temp file
                        import os
                        os.unlink(tmp_path)
                        self.log_info("Successfully got HTML from builder.save_html()")
                        return result
                    else:
                        # If no body found, return the full HTML
                        import os
                        os.unlink(tmp_path)
                        return full_html
                except Exception as e:
                    self.log_warning(f"builder.save_html() raised exception: {e}")
                    # Clean up temp file if it exists
                    import os
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
            
            # Method 3: Try to_html method
            if hasattr(builder, 'to_html'):
                try:
                    html = builder.to_html()
                    if html and html.strip():
                        return html
                except Exception as e:
                    self.log_warning(f"builder.to_html() raised exception: {e}")
            
            # Method 4: Try to_script method
            if hasattr(builder, 'to_script'):
                try:
                    script = builder.to_script()
                    return f'<div id="escher-map"></div>\n<script>{script}</script>'
                except Exception as e:
                    self.log_warning(f"builder.to_script() raised exception: {e}")
            
            # Last resort: create basic embedding code
            self.log_warning("All methods to get Escher HTML failed, using fallback")
            return f"""
            <div id="escher-map"></div>
            <script>
            // Basic Escher map embedding
            console.log("Escher map data loaded");
            // Note: Full Escher integration requires escher.js library
            document.getElementById('escher-map').innerHTML = 
                '<p>Escher map visualization would appear here. ' +
                'This requires the full Escher.js library for proper rendering.</p>';
            </script>
            """
        except Exception as e:
            self.log_warning(f"Could not generate Escher embed code: {e}")
            import traceback
            self.log_warning(f"Traceback: {traceback.format_exc()}")
            return f"""
            <div id="escher-map"></div>
            <script>
            console.log("Error loading Escher map: {e}");
            document.getElementById('escher-map').innerHTML = 
                '<p>Error loading Escher map visualization. Check console for details.</p>';
            </script>
            """

    def load_flux_solution(self, source: Union[str, Dict, pd.DataFrame, Any]) -> Dict:
        """Load flux solution from various sources.
        
        Args:
            source: Flux data source - can be:
                - File path (JSON, CSV, TSV)
                - Dictionary
                - pandas DataFrame
                - COBRApy solution object
                - MSModelUtil with flux solution
                
        Returns:
            Dict: Flux data with reaction IDs as keys
            
        Raises:
            ValueError: If source format is not supported
        """
        if isinstance(source, str):
            # File path
            path = Path(source)
            if not path.exists():
                raise ValueError(f"File not found: {source}")
                
            if path.suffix.lower() == '.json':
                with open(path, 'r') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
                else:
                    raise ValueError("JSON file must contain a dictionary")
                    
            elif path.suffix.lower() in ['.csv', '.tsv']:
                separator = ',' if path.suffix.lower() == '.csv' else '\t'
                df = pd.read_csv(path, sep=separator, index_col=0)
                if len(df.columns) == 1:
                    return df.iloc[:, 0].to_dict()
                else:
                    # Assume first column is reaction IDs, second is flux values
                    return dict(zip(df.iloc[:, 0], df.iloc[:, 1]))
            else:
                raise ValueError(f"Unsupported file format: {path.suffix}")
                
        elif isinstance(source, dict):
            return source
            
        elif isinstance(source, pd.DataFrame):
            if len(source.columns) == 1:
                return source.iloc[:, 0].to_dict()
            else:
                # Try to find a column that looks like flux values
                flux_cols = [col for col in source.columns if 
                           any(keyword in col.lower() for keyword in ['flux', 'rate', 'value'])]
                if flux_cols:
                    return source[flux_cols[0]].to_dict()
                else:
                    return source.iloc[:, 0].to_dict()
                    
        elif isinstance(source, pd.Series):
            return source.to_dict()
            
        elif hasattr(source, 'fluxes'):
            # COBRApy solution object
            if isinstance(source.fluxes, pd.Series):
                return source.fluxes.to_dict()
            else:
                return dict(source.fluxes)
                
        elif hasattr(source, 'get_flux_values'):
            # MSModelUtil or similar object
            return source.get_flux_values()
            
        else:
            raise ValueError(f"Unsupported flux solution source type: {type(source)}")

    def create_simple_map_from_model(
        self,
        model,
        central_metabolism: bool = True,
        output_file: str = "simple_map.html",
        **kwargs
    ) -> str:
        """Create a simple metabolic map visualization from a model without requiring pre-existing map data.
        
        Args:
            model: COBRApy model or MSModelUtil object
            central_metabolism: If True, focus on central metabolism pathways
            output_file: Output HTML file path
            **kwargs: Additional arguments passed to create_map_html
            
        Returns:
            str: Path to created HTML file
        """
        # This is a simplified version that creates a basic visualization
        # In a real implementation, you might generate a basic network layout
        
        # Handle model input
        if hasattr(model, 'model'):
            cobra_model = model.model
        else:
            cobra_model = model
            
        # Create a simple network representation
        network_data = self._create_simple_network_layout(cobra_model, central_metabolism)
        
        return self.create_map_html(
            model=model,
            map_json=network_data,
            output_file=output_file,
            title="Simple Metabolic Network",
            **kwargs
        )
    
    def _create_simple_network_layout(self, model, central_metabolism: bool = True) -> Dict:
        """Create a simple network layout for visualization.
        
        Args:
            model: COBRApy model
            central_metabolism: Focus on central metabolism
            
        Returns:
            Dict: Simple map data structure
        """
        # This is a placeholder for creating a simple map layout
        # A full implementation would create node positions and connections
        
        reactions = []
        metabolites = []
        
        # Filter reactions if focusing on central metabolism
        if central_metabolism:
            central_keywords = [
                'glycol', 'tca', 'citric', 'ppp', 'pentose', 'phosphate',
                'glucose', 'pyruvate', 'acetyl', 'oxaloacetate'
            ]
            model_reactions = [r for r in model.reactions 
                             if any(keyword in r.name.lower() for keyword in central_keywords)]
        else:
            model_reactions = list(model.reactions)[:50]  # Limit for simple visualization
        
        # Create simple data structure
        for i, rxn in enumerate(model_reactions):
            reactions.append({
                'bigg_id': rxn.id,
                'name': rxn.name or rxn.id,
                'x': 100 + (i % 10) * 80,
                'y': 100 + (i // 10) * 60
            })
        
        return {
            'reactions': {str(i): rxn for i, rxn in enumerate(reactions)},
            'nodes': {},
            'canvas': {'x': 0, 'y': 0, 'width': 1000, 'height': 600}
        }