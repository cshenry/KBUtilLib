"""Utilities for managing and visualizing models on escher maps."""

import pickle
from typing import Any, Dict, Optional, Union
import pandas as pd
import re
import json
import os
from pathlib import Path

from cobra.flux_analysis import flux_variability_analysis
from cobra.flux_analysis import pfba 

from .kb_model_utils import KBModelUtils
from .ms_biochem_utils import MSBiochemUtils

class EscherUtils(KBModelUtils, MSBiochemUtils):
    """Tools for managing and visualizing models on escher maps
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize Escher utilities

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvironment
        """
        super().__init__(**kwargs)

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
        **escher_kwargs
    ) -> str:
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
            **escher_kwargs: Additional arguments passed to Escher Builder
            
        Returns:
            str: Path to the created HTML file
            
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
        
        # Set default scales if not provided
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
            builder_kwargs['map_json'] = map_data
        elif map_name:
            builder_kwargs['map_name'] = map_name
            
        builder = Builder(**builder_kwargs)
        
        # Set scales
        builder.reaction_scale = reaction_scale
        builder.metabolite_scale = metabolite_scale
        builder.gene_scale = gene_scale
        
        # Add flux data
        if flux_solution is not None:
            if isinstance(flux_solution, pd.Series):
                builder.reaction_data = flux_solution.to_dict()
            elif isinstance(flux_solution, dict):
                builder.reaction_data = flux_solution
            else:
                raise ValueError("flux_solution must be a dictionary or pandas Series")
        
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
        <div id="escher-map"></div>
    </div>
    
    <div class="info-panel">
        <h3>Legend</h3>
        <p><strong>Reactions:</strong> Color and size indicate flux magnitude</p>
        {f'<p><strong>Metabolites:</strong> Color indicates concentration levels</p>' if metabolomic_data else ''}
        {f'<p><strong>Genes:</strong> Color indicates expression/protein abundance levels</p>' if (transcriptomic_data or proteomic_data) else ''}
        <p>Generated using Escher pathway visualization</p>
    </div>
    
    <script>
        // Insert Escher map here
        {self._get_escher_html_embed(builder)}
    </script>
</body>
</html>"""
        
        # Write HTML file
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_template)
        
        self.log_info(f"Escher map HTML saved to: {output_path.absolute()}")
        return str(output_path.absolute())
    
    def _get_escher_html_embed(self, builder) -> str:
        """Get the HTML/JavaScript code to embed the Escher map.
        
        Args:
            builder: Escher Builder object
            
        Returns:
            str: JavaScript code to render the map
        """
        try:
            # Try to get the HTML representation
            if hasattr(builder, '_repr_html_'):
                html_repr = builder._repr_html_()
                # Extract just the JavaScript part
                if '<script>' in html_repr and '</script>' in html_repr:
                    start = html_repr.find('<script>') + len('<script>')
                    end = html_repr.find('</script>')
                    return html_repr[start:end]
                else:
                    return html_repr
            else:
                # Fallback: create basic embedding code
                return f"""
                // Basic Escher map embedding
                console.log("Escher map data loaded");
                // Note: Full Escher integration requires escher.js library
                document.getElementById('escher-map').innerHTML = 
                    '<p>Escher map visualization would appear here. ' +
                    'This requires the full Escher.js library for proper rendering.</p>';
                """
        except Exception as e:
            self.log_warning(f"Could not generate Escher embed code: {e}")
            return f"""
            console.log("Error loading Escher map: {e}");
            document.getElementById('escher-map').innerHTML = 
                '<p>Error loading Escher map visualization. Check console for details.</p>';
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