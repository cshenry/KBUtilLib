"""Utilities for managing and visualizing models on escher maps."""

import json
import os
import copy
from typing import Any, Dict, Optional, Union, Tuple
import pandas as pd
from pathlib import Path

from .kb_model_utils import KBModelUtils
from .ms_biochem_utils import MSBiochemUtils


class EscherUtils(KBModelUtils, MSBiochemUtils):
    """Tools for managing and visualizing models on escher maps.

    This class provides simplified utilities for creating Escher map visualizations
    with proper handling of reaction directionality based on flux solutions.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize Escher utilities.

        Args:
            **kwargs: Additional keyword arguments passed to parent classes
        """
        super().__init__(**kwargs)

    def _reverse_reaction_in_map(self, map_data: Dict, reaction_id: str) -> None:
        """Reverse the direction of a reaction in the map data (in-place).

        This modifies the stoichiometry of metabolites in the reaction by multiplying
        all coefficients by -1, effectively reversing the reaction direction.

        Args:
            map_data: Escher map data dictionary (modified in-place)
            reaction_id: ID of the reaction to reverse
        """
        if 'reactions' not in map_data:
            return

        # Find the reaction in the map
        for rxn_key, rxn_data in map_data['reactions'].items():
            if rxn_data.get('bigg_id') == reaction_id or rxn_data.get('id') == reaction_id:
                # Reverse metabolites in the reaction
                if 'metabolites' in rxn_data:
                    for met_data in rxn_data['metabolites']:
                        if 'coefficient' in met_data:
                            met_data['coefficient'] = -met_data['coefficient']

                # Also check segments (visual representation)
                if 'segments' in rxn_data:
                    # Segments represent the visual path of the reaction
                    # Reversing them ensures arrows point in the correct direction
                    for segment_key, segment_data in rxn_data['segments'].items():
                        if 'from_node_id' in segment_data and 'to_node_id' in segment_data:
                            # Swap from and to nodes
                            segment_data['from_node_id'], segment_data['to_node_id'] = \
                                segment_data['to_node_id'], segment_data['from_node_id']

                self.log_info(f"Reversed reaction {reaction_id} in map")
                break

    def _adapt_map_to_flux(
        self,
        map_data: Dict,
        flux_solution: Dict[str, float],
        threshold: float = 1e-6
    ) -> Tuple[Dict, Dict[str, float]]:
        """Adapt map to flux solution by reversing reactions with negative flux.

        Creates a deep copy of the map and modifies reactions that have negative flux
        by reversing their stoichiometry. Also adjusts the flux values to be positive.

        Args:
            map_data: Original Escher map data dictionary
            flux_solution: Dictionary mapping reaction IDs to flux values
            threshold: Minimum absolute flux value to consider (default: 1e-6)

        Returns:
            Tuple of (modified_map_data, adjusted_flux_solution)
        """
        # Create deep copy to avoid modifying original
        modified_map = copy.deepcopy(map_data)
        adjusted_flux = flux_solution.copy()

        reversed_count = 0
        for reaction_id, flux_value in flux_solution.items():
            # Check if flux is significantly negative
            if flux_value < -threshold:
                # Reverse the reaction in the map
                self._reverse_reaction_in_map(modified_map, reaction_id)
                # Make flux positive
                adjusted_flux[reaction_id] = abs(flux_value)
                reversed_count += 1

        if reversed_count > 0:
            self.log_info(f"Adapted map: reversed {reversed_count} reactions with negative flux")

        return modified_map, adjusted_flux

    def _sync_model_directions(
        self,
        model,
        flux_solution: Dict[str, float],
        threshold: float = 1e-6
    ):
        """Create a copy of the model with reactions reversed to match flux directions.

        For reactions with negative flux, this reverses the reaction in the model
        by multiplying all stoichiometric coefficients by -1 and swapping bounds.

        Args:
            model: COBRApy model object
            flux_solution: Dictionary mapping reaction IDs to flux values
            threshold: Minimum absolute flux value to consider (default: 1e-6)

        Returns:
            Modified copy of the model with reactions reversed
        """
        # Create a copy of the model
        model_copy = model.copy()

        reversed_count = 0
        for reaction_id, flux_value in flux_solution.items():
            if flux_value < -threshold:
                try:
                    rxn = model_copy.reactions.get_by_id(reaction_id)

                    # Store original bounds
                    old_lb = rxn.lower_bound
                    old_ub = rxn.upper_bound

                    # Reverse stoichiometry by multiplying all coefficients by -1
                    for metabolite in list(rxn.metabolites.keys()):
                        old_coeff = rxn.metabolites[metabolite]
                        rxn.add_metabolites({metabolite: -2 * old_coeff})

                    # Swap and negate bounds
                    rxn.lower_bound = -old_ub
                    rxn.upper_bound = -old_lb

                    reversed_count += 1
                    self.log_info(f"Reversed reaction {reaction_id} in model")

                except KeyError:
                    self.log_warning(f"Reaction {reaction_id} not found in model")

        if reversed_count > 0:
            self.log_info(f"Synced model: reversed {reversed_count} reactions")

        return model_copy

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
        flux_threshold: float = 1e-6,
        sync_model_directions: bool = False,
        **escher_kwargs
    ) -> Union[str, Tuple[str, Any]]:
        """Create an HTML file that renders an Escher map with model data.

        This simplified version focuses on correct display of flux directionality
        by adapting the map to the flux solution. Reactions with negative flux
        are reversed in the map (stoichiometry multiplied by -1) and flux values
        are made positive.

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
            flux_threshold: Minimum absolute flux value to display (default: 1e-6)
            sync_model_directions: If True, also return a model with reversed reactions (default: False)
            **escher_kwargs: Additional arguments passed to Escher Builder

        Returns:
            If sync_model_directions=False: Path to the created HTML file
            If sync_model_directions=True: Tuple of (html_path, modified_model)

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
            >>> # Create Escher visualization with corrected directionality
            >>> escher_utils = EscherUtils()
            >>> html_file = escher_utils.create_map_html(
            ...     model=model,
            ...     flux_solution=solution.fluxes,
            ...     map_name="E. coli core",
            ...     title="FBA Results",
            ...     output_file="my_map.html"
            ... )
            >>>
            >>> # With model synchronization
            >>> html_file, synced_model = escher_utils.create_map_html(
            ...     model=model,
            ...     flux_solution=solution.fluxes,
            ...     map_name="E. coli core",
            ...     sync_model_directions=True,
            ...     output_file="my_map.html"
            ... )
        """
        try:
            from escher import Builder
        except ImportError:
            raise ImportError(
                "escher package is required for map visualization. "
                "Install with: pip install escher"
            )

        # Handle model input (COBRApy model or MSModelUtil)
        if hasattr(model, 'model'):
            cobra_model = model.model
        else:
            cobra_model = model

        # Validate model
        if not hasattr(cobra_model, 'reactions'):
            raise ValueError(
                "Invalid model object. Must be a COBRApy model or MSModelUtil object."
            )

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

        # Adapt map to flux solution if provided
        model_to_use = cobra_model
        if map_data and flux_data_dict:
            self.log_info("Adapting map to flux solution...")
            map_data, flux_data_dict = self._adapt_map_to_flux(
                map_data, flux_data_dict, flux_threshold
            )

            # Optionally sync model directions
            if sync_model_directions:
                self.log_info("Synchronizing model reaction directions...")
                model_to_use = self._sync_model_directions(
                    cobra_model, flux_solution, flux_threshold
                )

        # Set default scales if not provided
        if reaction_scale is None:
            reaction_scale = [
                {"type": "value", "value": 0, "color": "#dcdcdc", "size": 4},
                {"type": "value", "value": 0.01, "color": "#c8e6ff", "size": 8},
                {"type": "value", "value": 1, "color": "#6699ff", "size": 12},
                {"type": "value", "value": 10, "color": "#0055cc", "size": 16},
                {"type": "value", "value": 100, "color": "#003380", "size": 20}
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
            'model': model_to_use,
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

        # Set flux threshold
        builder.reaction_data_threshold = flux_threshold

        # Add flux data (already adjusted for direction)
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

        # Generate HTML content
        html_content = self._generate_html(
            builder,
            title,
            map_name,
            cobra_model,
            flux_data_dict,
            metabolomic_data,
            transcriptomic_data,
            proteomic_data
        )

        # Write output file
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        self.log_info(f"Escher map HTML saved to: {output_path.absolute()}")

        # Return based on sync_model_directions flag
        if sync_model_directions:
            return str(output_path.absolute()), model_to_use
        else:
            return str(output_path.absolute())

    def _generate_html(
        self,
        builder,
        title: str,
        map_name: Optional[str],
        model,
        flux_data: Optional[Dict],
        metabolomic_data: Optional[Dict],
        transcriptomic_data: Optional[Dict],
        proteomic_data: Optional[Dict]
    ) -> str:
        """Generate complete HTML page with Escher map.

        Args:
            builder: Escher Builder object
            title: Page title
            map_name: Map name to display
            model: COBRApy model
            flux_data: Flux solution data
            metabolomic_data: Metabolite data
            transcriptomic_data: Gene expression data
            proteomic_data: Protein abundance data

        Returns:
            Complete HTML content as string
        """
        # Get Escher HTML embed code
        escher_html = self._get_escher_html_embed(builder)

        # Build HTML template
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
        <div class="data-info">Model: {model.id if hasattr(model, 'id') else 'Unnamed'}</div>
        <div class="data-info">Reactions: {len(model.reactions)}</div>
        <div class="data-info">Metabolites: {len(model.metabolites)}</div>
        <div class="data-info">Genes: {len(model.genes)}</div>
        {f'<div class="data-info">Flux data: {len(flux_data)} reactions</div>' if flux_data else ''}
        {f'<div class="data-info">Metabolomic data: {len(metabolomic_data)} metabolites</div>' if metabolomic_data else ''}
        {f'<div class="data-info">Transcriptomic data: {len(transcriptomic_data)} genes</div>' if transcriptomic_data else ''}
        {f'<div class="data-info">Proteomic data: {len(proteomic_data)} proteins</div>' if proteomic_data else ''}
    </div>

    <div class="map-container">
        <div id="escher-map"></div>
    </div>

    <div class="info-panel">
        <h3>Legend</h3>
        <p><strong>Reactions:</strong> Color and size indicate flux magnitude</p>
        {f'<p><strong>Metabolites:</strong> Color indicates concentration levels</p>' if metabolomic_data else ''}
        {f'<p><strong>Genes:</strong> Color indicates expression/protein abundance levels</p>' if (transcriptomic_data or proteomic_data) else ''}
        <p><strong>Note:</strong> Reactions with negative flux have been reversed in the map for correct visualization</p>
        <p>Generated using Escher pathway visualization</p>
    </div>

    <script>
        {escher_html}
    </script>
</body>
</html>"""

        return html_template

    def _get_escher_html_embed(self, builder) -> str:
        """Get the HTML/JavaScript code to embed the Escher map.

        Args:
            builder: Escher Builder object

        Returns:
            str: JavaScript/HTML code to render the map
        """
        try:
            # Try _repr_html_() - standard Jupyter/IPython method
            if hasattr(builder, '_repr_html_'):
                html_repr = builder._repr_html_()
                if html_repr and html_repr.strip():
                    self.log_info("Successfully got HTML from builder._repr_html_()")
                    return html_repr

            # Fallback: save to temp file and read
            if hasattr(builder, 'save_html'):
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as tmp_file:
                    tmp_path = tmp_file.name

                builder.save_html(tmp_path)
                with open(tmp_path, 'r', encoding='utf-8') as f:
                    full_html = f.read()

                # Extract body content
                import re
                body_match = re.search(r'<body[^>]*>(.*?)</body>', full_html, re.DOTALL)
                if body_match:
                    body_content = body_match.group(1)
                    # Get scripts from head
                    head_scripts = re.findall(r'<script[^>]*>.*?</script>', full_html, re.DOTALL)
                    result = body_content
                    if head_scripts:
                        result = '\n'.join(head_scripts) + '\n' + result

                    os.unlink(tmp_path)
                    self.log_info("Successfully got HTML from builder.save_html()")
                    return result
                else:
                    os.unlink(tmp_path)
                    return full_html

            # Last resort fallback
            self.log_warning("Could not extract Escher HTML, using fallback")
            return """
            <div id="escher-map"></div>
            <script>
            console.log("Escher map embedding requires escher.js library");
            </script>
            """

        except Exception as e:
            self.log_warning(f"Could not generate Escher embed code: {e}")
            return """
            <div id="escher-map"></div>
            <script>
            console.error("Error loading Escher map");
            </script>
            """

    def load_flux_solution(self, source: Union[str, Dict, pd.DataFrame, Any]) -> Dict:
        """Load flux solution from various sources.

        Args:
            source: Flux data source - can be:
                - File path (JSON, CSV, TSV)
                - Dictionary
                - pandas DataFrame or Series
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
                    return dict(zip(df.iloc[:, 0], df.iloc[:, 1]))
            else:
                raise ValueError(f"Unsupported file format: {path.suffix}")

        elif isinstance(source, dict):
            return source

        elif isinstance(source, pd.DataFrame):
            if len(source.columns) == 1:
                return source.iloc[:, 0].to_dict()
            else:
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
