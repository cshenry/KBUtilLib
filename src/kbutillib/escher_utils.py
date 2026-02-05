"""Utilities for managing and visualizing models on escher maps."""

import pickle
from typing import Any, Dict, Optional, Union, Literal, Tuple
import pandas as pd
import re
import json
import os
import cobra
from pathlib import Path
import statistics

from cobra.flux_analysis import flux_variability_analysis
from cobra.flux_analysis import pfba

from .kb_model_utils import KBModelUtils
from .ms_biochem_utils import MSBiochemUtils

# Default arrow width range for enhanced visualization
DEFAULT_ARROW_WIDTH_RANGE = (2, 20)

# 10 distinct colors for reaction class badges
REACTION_CLASS_COLORS = [
    '#e6194b',  # Red
    '#3cb44b',  # Green
    '#4363d8',  # Blue
    '#f58231',  # Orange
    '#911eb4',  # Purple
    '#42d4f4',  # Cyan
    '#f032e6',  # Magenta
    '#bfef45',  # Lime
    '#fabed4',  # Pink
    '#469990',  # Teal
]

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
        self.local_map_directory = os.path.join(os.path.dirname(__file__),'..','..', 'data', 'escher_maps')
        self.kbase_map_ws_id = 93991
        # Private attributes for lazy loading
        self._local_map_index = None
        self._kbase_map_index = None

    @property
    def local_map_index(self) -> Dict[str, Dict]:
        """Lazy-loaded index of local escher maps.

        Returns:
            Dict mapping map name (filename without .json) to stats dict with 'filename' added
        """
        if self._local_map_index is None:
            self._local_map_index = {}
            if os.path.isdir(self.local_map_directory):
                for filename in os.listdir(self.local_map_directory):
                    if filename.endswith('.json'):
                        map_name = filename[:-5]  # Remove .json extension
                        filepath = os.path.join(self.local_map_directory, filename)
                        try:
                            with open(filepath, 'r') as f:
                                map_data = json.load(f)
                            stats = self._compute_stats_from_map_data(map_data)
                            stats['filename'] = filepath
                            self._local_map_index[map_name] = stats
                        except (json.JSONDecodeError, IOError) as e:
                            self.log_warning(f"Could not load local map {filename}: {e}")
        return self._local_map_index

    @property
    def kbase_map_index(self) -> Dict[str, Dict]:
        """Lazy-loaded index of KBase workspace escher maps.

        Returns:
            Dict mapping map name (workspace object name) to stats dict with 'kbase_ref' added
        """
        if self._kbase_map_index is None:
            self._kbase_map_index = {}
            try:
                # List objects in the EscherMaps workspace
                # list_ws_objects returns dict: {obj_name: obj_info_tuple}
                objects = self.list_ws_objects(self.kbase_map_ws_id)
                for obj_name, obj_info in objects.items():
                    obj_ref = f"{obj_info[6]}/{obj_info[0]}/{obj_info[4]}"  # ws_id/obj_id/version
                    try:
                        obj_result = self.get_object(obj_ref)
                        map_data = obj_result.get("data") if isinstance(obj_result, dict) else obj_result
                        stats = self._compute_stats_from_map_data(map_data)
                        stats['kbase_ref'] = obj_ref
                        self._kbase_map_index[obj_name] = stats
                    except Exception as e:
                        self.log_warning(f"Could not load KBase map {obj_name}: {e}")
            except Exception as e:
                self.log_warning(f"Could not access KBase workspace {self.kbase_map_ws_id}: {e}")
        return self._kbase_map_index

    def _compute_stats_from_map_data(self, map_data, model=None) -> Dict:
        """Computes stats from map json data.

        Computes total metabolites and reactions in the map. If a model is provided,
        also computes how many genes, reactions, and metabolites from the model are in the map.

        Args:
            map_data: Escher map JSON data (list with header and canvas data)
            model: Optional COBRApy model or MSModelUtil to compare against

        Returns:
            Dict containing:
                - reaction_count: Total reactions in map
                - metabolite_count: Total metabolites in map
                - reaction_ids: List of reaction IDs in map
                - metabolite_ids: List of metabolite IDs in map
                If model provided:
                - model_reactions_in_map: Count of model reactions found in map
                - model_metabolites_in_map: Count of model metabolites found in map
                - model_genes_in_map: Count of model genes found in map
                - model_reaction_coverage: Fraction of model reactions in map
                - model_metabolite_coverage: Fraction of model metabolites in map
        """
        stats = {
            'reaction_count': 0,
            'metabolite_count': 0,
            'reaction_ids': [],
            'metabolite_ids': []
        }

        # Handle both KBase and Escher map formats
        if self._is_kbase_map_format(map_data):
            # KBase format: {"metadata": {...}, "layout": {...}}
            canvas_data = map_data.get('layout', {})
        elif self._is_escher_map_format(map_data):
            # Escher format: [header, canvas_data]
            canvas_data = map_data[1]
        else:
            self.log_warning("Map data does not have expected Escher or KBase structure")
            return stats

        # Extract reactions
        reactions = canvas_data.get('reactions', {})
        for rxn_data in reactions.values():
            bigg_id = rxn_data.get('bigg_id', '')
            if bigg_id and bigg_id not in stats['reaction_ids']:
                stats['reaction_ids'].append(bigg_id)

        stats['reaction_count'] = len(stats['reaction_ids'])

        # Extract metabolites (nodes of type 'metabolite')
        nodes = canvas_data.get('nodes', {})
        for node_data in nodes.values():
            if node_data.get('node_type') == 'metabolite':
                bigg_id = node_data.get('bigg_id', '')
                if bigg_id and bigg_id not in stats['metabolite_ids']:
                    stats['metabolite_ids'].append(bigg_id)

        stats['metabolite_count'] = len(stats['metabolite_ids'])

        # If model provided, compute coverage statistics
        if model is not None:
            # Handle model input (COBRApy model or MSModelUtil)
            if hasattr(model, 'model'):
                cobra_model = model.model
            else:
                cobra_model = model

            # Get model IDs
            model_rxn_ids = {rxn.id for rxn in cobra_model.reactions}
            model_met_ids = {met.id for met in cobra_model.metabolites}

            # Calculate overlaps
            map_rxn_set = set(stats['reaction_ids'])
            map_met_set = set(stats['metabolite_ids'])

            rxns_in_map = model_rxn_ids & map_rxn_set
            mets_in_map = model_met_ids & map_met_set

            # For genes, we need to check which genes are associated with reactions in the map
            genes_in_map = set()
            for rxn_id in rxns_in_map:
                if rxn_id in cobra_model.reactions:
                    rxn = cobra_model.reactions.get_by_id(rxn_id)
                    genes_in_map.update(gene.id for gene in rxn.genes)

            stats['model_reactions_in_map'] = len(rxns_in_map)
            stats['model_metabolites_in_map'] = len(mets_in_map)
            stats['model_genes_in_map'] = len(genes_in_map)

            # Coverage fractions
            stats['model_reaction_coverage'] = len(rxns_in_map) / len(model_rxn_ids) if model_rxn_ids else 0.0
            stats['model_metabolite_coverage'] = len(mets_in_map) / len(model_met_ids) if model_met_ids else 0.0

        return stats

    def list_available_maps(self, model=None, as_df: bool = False) -> Union[list, pd.DataFrame]:
        """List all available Escher maps from both local and KBase sources with stats.

        Retrieves all maps from the local map directory and KBase workspace,
        computes statistics for each map, and returns the results.

        Args:
            model: Optional COBRApy model or MSModelUtil. If provided, computes
                   model coverage statistics for each map. If None, uses cached stats.
            as_df: If True, returns results as a pandas DataFrame. Default False.

        Returns:
            Union[list, pd.DataFrame]: List of dictionaries (or DataFrame if as_df=True)
                containing map information and stats:
                - name: Map name
                - source: 'local' or 'kbase'
                - filename: File path (for local maps)
                - kbase_ref: KBase reference (for KBase maps)
                - reaction_count: Number of reactions in map
                - metabolite_count: Number of metabolites in map
                If model provided:
                - model_reactions_in_map: Count of model reactions in map
                - model_metabolites_in_map: Count of model metabolites in map
                - model_genes_in_map: Count of model genes in map
                - model_reaction_coverage: Fraction of model reactions in map
                - model_metabolite_coverage: Fraction of model metabolites in map

        Example:
            >>> from kbutillib import EscherUtils
            >>> import cobra
            >>>
            >>> eu = EscherUtils()
            >>> # List all maps without model coverage
            >>> maps = eu.list_available_maps()
            >>> print(f"Found {len(maps)} maps")
            >>>
            >>> # List maps with model coverage as DataFrame
            >>> model = cobra.test.create_test_model("textbook")
            >>> df = eu.list_available_maps(model=model, as_df=True)
            >>> print(df.sort_values('model_reaction_coverage', ascending=False))
        """
        results = []

        # Process local maps
        if model is None:
            # Use cached stats from lazy-loaded index
            for map_name, stats in self.local_map_index.items():
                entry = {
                    'name': map_name,
                    'source': 'local',
                    'filename': stats.get('filename', ''),
                    'kbase_ref': None,
                    'reaction_count': stats.get('reaction_count', 0),
                    'metabolite_count': stats.get('metabolite_count', 0),
                }
                # Don't include reaction_ids and metabolite_ids in output (too verbose)
                results.append(entry)
        else:
            # Compute stats with model for local maps
            if os.path.isdir(self.local_map_directory):
                for filename in os.listdir(self.local_map_directory):
                    if filename.endswith('.json'):
                        map_name = filename[:-5]
                        filepath = os.path.join(self.local_map_directory, filename)
                        try:
                            with open(filepath, 'r') as f:
                                map_data = json.load(f)
                            stats = self._compute_stats_from_map_data(map_data, model=model)
                            entry = {
                                'name': map_name,
                                'source': 'local',
                                'filename': filepath,
                                'kbase_ref': None,
                                'reaction_count': stats.get('reaction_count', 0),
                                'metabolite_count': stats.get('metabolite_count', 0),
                                'model_reactions_in_map': stats.get('model_reactions_in_map', 0),
                                'model_metabolites_in_map': stats.get('model_metabolites_in_map', 0),
                                'model_genes_in_map': stats.get('model_genes_in_map', 0),
                                'model_reaction_coverage': stats.get('model_reaction_coverage', 0.0),
                                'model_metabolite_coverage': stats.get('model_metabolite_coverage', 0.0),
                            }
                            results.append(entry)
                        except (json.JSONDecodeError, IOError) as e:
                            self.log_warning(f"Could not load local map {filename}: {e}")

        # Process KBase maps
        if model is None:
            # Use cached stats from lazy-loaded index
            for map_name, stats in self.kbase_map_index.items():
                entry = {
                    'name': map_name,
                    'source': 'kbase',
                    'filename': None,
                    'kbase_ref': stats.get('kbase_ref', ''),
                    'reaction_count': stats.get('reaction_count', 0),
                    'metabolite_count': stats.get('metabolite_count', 0),
                }
                results.append(entry)
        else:
            # Compute stats with model for KBase maps
            try:
                objects = self.list_ws_objects(self.kbase_map_ws_id)
                for obj_name, obj_info in objects.items():
                    obj_ref = f"{obj_info[6]}/{obj_info[0]}/{obj_info[4]}"
                    try:
                        obj_result = self.get_object(obj_ref)
                        map_data = obj_result.get("data") if isinstance(obj_result, dict) else obj_result
                        stats = self._compute_stats_from_map_data(map_data, model=model)
                        entry = {
                            'name': obj_name,
                            'source': 'kbase',
                            'filename': None,
                            'kbase_ref': obj_ref,
                            'reaction_count': stats.get('reaction_count', 0),
                            'metabolite_count': stats.get('metabolite_count', 0),
                            'model_reactions_in_map': stats.get('model_reactions_in_map', 0),
                            'model_metabolites_in_map': stats.get('model_metabolites_in_map', 0),
                            'model_genes_in_map': stats.get('model_genes_in_map', 0),
                            'model_reaction_coverage': stats.get('model_reaction_coverage', 0.0),
                            'model_metabolite_coverage': stats.get('model_metabolite_coverage', 0.0),
                        }
                        results.append(entry)
                    except Exception as e:
                        self.log_warning(f"Could not load KBase map {obj_name}: {e}")
            except Exception as e:
                self.log_warning(f"Could not access KBase workspace {self.kbase_map_ws_id}: {e}")

        if as_df:
            return pd.DataFrame(results)
        return results

    def map_stats(self, map_identifier: str, model=None) -> Dict:
        """Load a map and compute its statistics.

        Loads the specified map using _load_map and computes statistics
        including reaction/metabolite counts and optionally model coverage.

        Args:
            map_identifier: Name of map, filename, or KBase reference
            model: Optional COBRApy model or MSModelUtil. If provided, computes
                   model coverage statistics.

        Returns:
            Dict containing:
                - name: Map identifier used
                - reaction_count: Total reactions in map
                - metabolite_count: Total metabolites in map
                - reaction_ids: List of reaction IDs in map
                - metabolite_ids: List of metabolite IDs in map
                If model provided:
                - model_reactions_in_map: Count of model reactions found in map
                - model_metabolites_in_map: Count of model metabolites found in map
                - model_genes_in_map: Count of model genes found in map
                - model_reaction_coverage: Fraction of model reactions in map
                - model_metabolite_coverage: Fraction of model metabolites in map

        Raises:
            ValueError: If the map cannot be found or loaded

        Example:
            >>> from kbutillib import EscherUtils
            >>> import cobra
            >>>
            >>> eu = EscherUtils()
            >>> # Get stats for a map without model
            >>> stats = eu.map_stats("core")
            >>> print(f"Map has {stats['reaction_count']} reactions")
            >>>
            >>> # Get stats with model coverage
            >>> model = cobra.test.create_test_model("textbook")
            >>> stats = eu.map_stats("core", model=model)
            >>> print(f"Model coverage: {stats['model_reaction_coverage']:.1%}")
        """
        # Load the map
        map_data = self._load_map(map_identifier)

        # Compute stats
        stats = self._compute_stats_from_map_data(map_data, model=model)

        # Add the map identifier to the stats
        stats['name'] = map_identifier

        return stats

    def _load_map(self, map_identifier: str) -> Dict:
        """Loads a map from an input name, filename, or KBase reference.

        Attempts to load the map in the following order:
        1. Check if it's a file path that exists
        2. Check if it's in the local_map_index
        3. Check if it's in the kbase_map_index
        4. Check if it's a KBase reference and attempt to fetch from workspace

        Args:
            map_identifier: Name of map, filename, or KBase reference

        Returns:
            dict: Map data as loaded from JSON

        Raises:
            ValueError: If the map cannot be found or loaded from any source
        """
        attempts = []

        # 1. Check if the map is a filename by checking if the file exists
        if os.path.isfile(map_identifier):
            try:
                with open(map_identifier, 'r') as f:
                    map_data = json.load(f)
                # Validate it's a valid Escher map (should be a list with at least 2 elements)
                if isinstance(map_data, list) and len(map_data) >= 2:
                    self.log_info(f"Loaded map from file: {map_identifier}")
                    return map_data
                else:
                    attempts.append(f"File '{map_identifier}' exists but is not a valid Escher map format")
            except json.JSONDecodeError as e:
                attempts.append(f"File '{map_identifier}' exists but is not valid JSON: {e}")
            except IOError as e:
                attempts.append(f"File '{map_identifier}' exists but could not be read: {e}")
        else:
            attempts.append(f"File '{map_identifier}' does not exist")

        # 2. Check if the map is in local_map_index
        if map_identifier in self.local_map_index:
            filepath = self.local_map_index[map_identifier].get('filename')
            if filepath and os.path.isfile(filepath):
                try:
                    with open(filepath, 'r') as f:
                        map_data = json.load(f)
                    self.log_info(f"Loaded map '{map_identifier}' from local index: {filepath}")
                    return map_data
                except (json.JSONDecodeError, IOError) as e:
                    attempts.append(f"Map '{map_identifier}' found in local index but failed to load: {e}")
            else:
                attempts.append(f"Map '{map_identifier}' found in local index but file missing")
        else:
            attempts.append(f"Map '{map_identifier}' not found in local map index")

        # 3. Check if the map is in kbase_map_index
        if map_identifier in self.kbase_map_index:
            kbase_ref = self.kbase_map_index[map_identifier].get('kbase_ref')
            if kbase_ref:
                try:
                    obj_result = self.get_object(kbase_ref)
                    map_data = obj_result.get("data") if isinstance(obj_result, dict) else obj_result
                    # Translate from KBase format to Escher format if needed
                    if self._is_kbase_map_format(map_data):
                        map_data = self._translate_map_from_kbase(map_data)
                    self.log_info(f"Loaded map '{map_identifier}' from KBase index: {kbase_ref}")
                    return map_data
                except Exception as e:
                    attempts.append(f"Map '{map_identifier}' found in KBase index but failed to load: {e}")
            else:
                attempts.append(f"Map '{map_identifier}' found in KBase index but no reference available")
        else:
            attempts.append(f"Map '{map_identifier}' not found in KBase map index")

        # 4. Check if it's a KBase reference directly
        if self.is_ref(map_identifier):
            try:
                obj_result = self.get_object(map_identifier)
                map_data = obj_result.get("data") if isinstance(obj_result, dict) else obj_result
                # Translate from KBase format to Escher format if needed
                if self._is_kbase_map_format(map_data):
                    map_data = self._translate_map_from_kbase(map_data)
                    self.log_info(f"Loaded and translated map from KBase reference: {map_identifier}")
                    return map_data
                elif self._is_escher_map_format(map_data):
                    self.log_info(f"Loaded map from KBase reference: {map_identifier}")
                    return map_data
                else:
                    attempts.append(f"KBase reference '{map_identifier}' loaded but is not a valid Escher or KBase map format")
            except Exception as e:
                attempts.append(f"KBase reference '{map_identifier}' failed to load: {e}")
        else:
            attempts.append(f"'{map_identifier}' is not a valid KBase reference")

        # If everything fails, report failure with all attempts
        error_msg = f"Could not load map '{map_identifier}'. Attempted:\n"
        error_msg += "\n".join(f"  - {attempt}" for attempt in attempts)
        raise ValueError(error_msg)

    def _translate_map_from_kbase(self, kbase_map: Dict) -> list:
        """Convert a KBase map format to standard Escher map format.

        KBase maps use a dict structure: {"metadata": {...}, "layout": {...}}
        Escher maps use a list structure: [header_dict, canvas_dict]

        Additionally handles:
        - Converting reversibility from int (0/1) to bool
        - Preserving all other fields unchanged

        Args:
            kbase_map: Map data in KBase format (dict with 'metadata' and 'layout' keys)

        Returns:
            list: Map data in standard Escher format [header, canvas]
        """
        import copy

        # If it's already in Escher format, return as-is
        if isinstance(kbase_map, list) and len(kbase_map) >= 2:
            return kbase_map

        # Validate KBase format
        if not isinstance(kbase_map, dict):
            raise ValueError(f"Expected dict for KBase map, got {type(kbase_map).__name__}")

        if 'layout' not in kbase_map:
            raise ValueError("KBase map missing 'layout' key")

        # Extract metadata (header) - use 'metadata' if present, or construct from available fields
        metadata = kbase_map.get('metadata', {})
        header = {
            'map_name': metadata.get('map_name', ''),
            'map_id': metadata.get('map_id', ''),
            'map_description': metadata.get('map_description', ''),
            'homepage': metadata.get('homepage', 'https://escher.github.io'),
            'schema': metadata.get('schema', 'https://escher.github.io/escher/jsonschema/1-0-0#')
        }

        # Extract layout (canvas)
        layout = copy.deepcopy(kbase_map['layout'])

        # Convert reversibility from int to bool in reactions
        reactions = layout.get('reactions', {})
        for rxn_data in reactions.values():
            if 'reversibility' in rxn_data:
                # KBase uses 0/1, Escher uses true/false
                rxn_data['reversibility'] = bool(rxn_data['reversibility'])

        return [header, layout]

    def _translate_map_to_kbase(self, escher_map: list, authors: list = None) -> Dict:
        """Convert a standard Escher map format to KBase map format.

        Escher maps use a list structure: [header_dict, canvas_dict]
        KBase maps use a dict structure: {"metadata": {...}, "layout": {...}}

        Additionally handles:
        - Converting reversibility from bool to int (0/1)
        - Adding 'authors' field to metadata

        Args:
            escher_map: Map data in standard Escher format [header, canvas]
            authors: Optional list of author names to include in metadata

        Returns:
            dict: Map data in KBase format with 'metadata' and 'layout' keys
        """
        import copy

        # If it's already in KBase format, return as-is
        if isinstance(escher_map, dict) and 'layout' in escher_map:
            return escher_map

        # Validate Escher format
        if not isinstance(escher_map, list) or len(escher_map) < 2:
            raise ValueError(f"Expected list with at least 2 elements for Escher map, got {type(escher_map).__name__}")

        header = escher_map[0]
        canvas = copy.deepcopy(escher_map[1])

        # Build metadata from header
        metadata = {
            'map_name': header.get('map_name', ''),
            'map_id': header.get('map_id', ''),
            'map_description': header.get('map_description', ''),
            'homepage': header.get('homepage', 'https://escher.github.io'),
            'schema': header.get('schema', 'https://escher.github.io/escher/jsonschema/1-0-0#'),
            'authors': authors if authors else []
        }

        # Convert reversibility from bool to int in reactions
        reactions = canvas.get('reactions', {})
        for rxn_data in reactions.values():
            if 'reversibility' in rxn_data:
                # Escher uses true/false, KBase uses 0/1
                rxn_data['reversibility'] = 1 if rxn_data['reversibility'] else 0

        return {
            'metadata': metadata,
            'layout': canvas
        }

    def _is_kbase_map_format(self, map_data) -> bool:
        """Check if map data is in KBase format.

        Args:
            map_data: Map data to check

        Returns:
            bool: True if map is in KBase format (dict with 'layout' key)
        """
        return isinstance(map_data, dict) and 'layout' in map_data

    def _is_escher_map_format(self, map_data) -> bool:
        """Check if map data is in standard Escher format.

        Args:
            map_data: Map data to check

        Returns:
            bool: True if map is in Escher format (list with at least 2 elements)
        """
        return isinstance(map_data, list) and len(map_data) >= 2

    def _translate_map_with_flux(self, map_data: Dict, flux: Dict[str, float]) -> Dict:
        """Reverses map reaction definitions for negative fluxes and adjusts reaction IDs.

        For reactions with negative flux values, this function:
        1. Reverses the reaction direction in the map (swaps segment orientations)
        2. Changes the reaction ID to <ID>-rev

        Args:
            map_data: Escher map data dictionary containing reaction definitions
            flux: Dictionary mapping reaction IDs to flux values

        Returns:
            Dict: Modified map data with reversed reactions for negative fluxes
        """
        import copy

        # Get reactions from the map (structure: map_data[1]['reactions'])
        if not isinstance(map_data, list) or len(map_data) < 2:
            self.log_warning("Map data does not have expected structure")
            return map_data

        reactions = map_data[1].get('reactions', {})

        # Track which reactions need to be reversed
        reactions_to_reverse = []
        for rxn_id, flux_value in flux.items():
            if flux_value < 0:
                reactions_to_reverse.append(rxn_id)

        # Process each reaction in the map
        new_reactions = {}
        for map_rxn_id, rxn_data in reactions.items():
            bigg_id = rxn_data.get('bigg_id', '')

            if bigg_id in reactions_to_reverse:
                # Create reversed version
                reversed_data = copy.deepcopy(rxn_data)

                # Update the bigg_id to add -rev suffix
                reversed_data['bigg_id'] = f"{bigg_id}-rev"
                # Update label_x/label_y positions if needed (keep same for now)
                if 'label' in reversed_data:
                    reversed_data['label'] = f"{bigg_id}(R)"

                if 'metabolites' in reversed_data:
                    for metabolite_data in reversed_data["metabolites"]:
                        metabolite_data["coefficient"] = -1*metabolite_data["coefficient"]

                new_reactions[map_rxn_id] = reversed_data
            else:
                new_reactions[map_rxn_id] = rxn_data

        map_data[1]['reactions'] = new_reactions
        return map_data

    def _get_short_reaction_name(self, reaction) -> str:
        """Get a short display name for a reaction.

        For ModelSEED reactions (rxnXXXXX pattern), finds the shortest alias name
        from the biochemistry database. For other reactions, uses the reaction ID.

        Args:
            reaction: COBRA reaction object

        Returns:
            str: Short display name for the reaction
        """
        # Try to extract ModelSEED ID from reaction
        msid = self.reaction_to_msid(reaction)

        if msid:
            # Fetch the biochemistry reaction to get aliases
            biochem_rxn = self.get_reaction_by_id(msid)
            if biochem_rxn:
                # Collect all name aliases
                all_names = []
                if biochem_rxn.name:
                    all_names.append(biochem_rxn.name)
                if hasattr(biochem_rxn, 'names') and biochem_rxn.names:
                    all_names.extend(list(biochem_rxn.names))

                # Find the shortest name
                if all_names:
                    shortest = min(all_names, key=len)
                    return shortest

        # Fallback: use the reaction ID (without any -rev suffix for cleaner display)
        rxn_id = reaction.id
        if rxn_id.endswith('-rev'):
            rxn_id = rxn_id[:-4]
        return rxn_id

    def _get_short_name_for_reaction_id(self, reaction_id: str) -> str:
        """Get a short display name for a reaction ID string.

        For ModelSEED reactions (rxnXXXXX pattern), finds the shortest alias name
        from the biochemistry database. For other reactions, uses the reaction ID.

        Args:
            reaction_id: Reaction ID string (e.g., 'rxn00001_c0', 'PFK')

        Returns:
            str: Short display name for the reaction
        """
        # Try to extract ModelSEED ID from reaction ID string
        msid = self.reaction_id_to_msid(reaction_id)

        if msid:
            # Fetch the biochemistry reaction to get aliases
            biochem_rxn = self.get_reaction_by_id(msid)
            if biochem_rxn:
                # Collect all name aliases
                all_names = []
                if biochem_rxn.name:
                    all_names.append(biochem_rxn.name)
                if hasattr(biochem_rxn, 'names') and biochem_rxn.names:
                    all_names.extend(list(biochem_rxn.names))

                # Find the shortest name
                if all_names:
                    shortest = min(all_names, key=len)
                    return shortest

        # Fallback: use the reaction ID (without any -rev suffix for cleaner display)
        if reaction_id.endswith('-rev'):
            reaction_id = reaction_id[:-4]
        return reaction_id

    def update_map_reaction_names(
        self,
        map_identifier: str,
        output_path: Optional[str] = None
    ) -> Dict:
        """Update reaction names in an Escher map to use short ModelSEED names.

        For each reaction in the map:
        - If it's a ModelSEED reaction (rxnXXXXX pattern), uses the shortest
          alias name from the biochemistry database
        - Otherwise, uses the reaction's bigg_id as the name

        Args:
            map_identifier: Name of map, filename, or KBase reference
            output_path: Optional path to save the modified map. If None,
                        the map is modified but not saved.

        Returns:
            Dict: The modified map data (in Escher format)

        Example:
            >>> from kbutillib import EscherUtils
            >>> eu = EscherUtils()
            >>> # Update a map and save to file
            >>> eu.update_map_reaction_names("my_model_map.json", "my_model_map_short_names.json")
            >>> # Or update without saving
            >>> map_data = eu.update_map_reaction_names("my_model_map.json")
        """
        # Load the map
        if isinstance(map_identifier, str):
            map_data = self._load_map(map_identifier)
        else:
            map_data = map_identifier

        # Get reactions from the map
        if not isinstance(map_data, list) or len(map_data) < 2:
            self.log_warning("Map data does not have expected structure")
            return map_data

        reactions = map_data[1].get('reactions', {})
        updated_count = 0

        # Update each reaction's name
        for map_rxn_id, rxn_data in reactions.items():
            bigg_id = rxn_data.get('bigg_id', '')
            if bigg_id:
                short_name = self._get_short_name_for_reaction_id(bigg_id)
                rxn_data['name'] = short_name
                updated_count += 1

        self.log_info(f"Updated names for {updated_count} reactions in map")

        # Save to file if output path provided
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(map_data, f, indent=2)
            self.log_info(f"Saved updated map to: {output_path}")

        return map_data

    def _translate_model_with_flux(self, model, flux: Dict[str, float], use_short_rxn_names=True):
        """Clones a model and reverses all reactions associated with negative fluxes.

        For reactions with negative flux values, this function:
        1. Reverses the reaction (swaps reactants and products)
        2. Changes the reaction ID to <ID>-rev
        3. Adjusts bounds appropriately
        4. Sets short display names for all reactions (shortest alias for ModelSEED reactions, ID for others)

        Args:
            model: COBRApy model object or MSModelUtil object
            flux: Dictionary mapping reaction IDs to flux values

        Returns:
            COBRApy model: Cloned model with reversed reactions for negative fluxes
                          and shortened reaction names for Escher display
        """
        # Handle model input (COBRApy model or MSModelUtil)
        if hasattr(model, 'model'):
            cobra_model = model.model
        else:
            cobra_model = model

        # Clone the model
        cloned_model = cobra.io.json.from_json(cobra.io.json.to_json(cobra_model))

        # Track which reactions need to be reversed
        reactions_to_reverse = []
        for rxn_id, flux_value in flux.items():
            if flux_value < 0 and rxn_id in cloned_model.reactions:
                reactions_to_reverse.append(rxn_id)

        # Reverse each reaction with negative flux
        for rxn_id in reactions_to_reverse:
            rxn = cloned_model.reactions.get_by_id(rxn_id)

            # Store original metabolites (coefficient dict)
            original_metabolites = {met: coef for met, coef in rxn.metabolites.items()}

            # Clear and add reversed metabolites (negate all coefficients)
            rxn.subtract_metabolites(original_metabolites)
            reversed_metabolites = {met: -coef for met, coef in original_metabolites.items()}
            rxn.add_metabolites(reversed_metabolites)

            # Swap and negate bounds
            old_lower = rxn.lower_bound
            old_upper = rxn.upper_bound
            rxn.lower_bound = -old_upper
            rxn.upper_bound = -old_lower

            # Change the reaction ID to add -rev suffix
            rxn.id = f"{rxn_id}-rev"

        # IMPORTANT: Repair the model to update internal indices after ID changes
        # Without this, the model's reactions dict still uses old IDs as keys
        cloned_model.repair()

        if use_short_rxn_names:
            # Set short display names for all reactions
            for rxn in cloned_model.reactions:
                rxn.name = self._get_short_reaction_name(rxn)

        return cloned_model


    def _translate_flux(self, flux: Dict[str, float]) -> Dict[str, float]:
        """Changes all negative fluxes to positive values while adjusting IDs.

        For reactions with negative flux values, this function:
        1. Negates the flux value to make it positive
        2. Changes the reaction ID to <ID>-rev

        Args:
            flux: Dictionary mapping reaction IDs to flux values

        Returns:
            Dict[str, float]: Modified flux dictionary with all positive values
                and adjusted IDs for originally negative fluxes
        """
        translated_flux = {}

        for rxn_id, flux_value in flux.items():
            if flux_value < 0:
                # Negate to make positive and add -rev suffix to ID
                translated_flux[f"{rxn_id}-rev"] = -flux_value
            else:
                # Keep as-is
                translated_flux[rxn_id] = flux_value

        return translated_flux

    def _translate_reaction_classes(self, reaction_classes: Dict[str, str],
                                     flux: Optional[Dict[str, float]]) -> Dict[str, str]:
        """Adjust reaction_classes keys to match translated reaction IDs.

        For reactions with negative flux, their bigg_id in the map becomes <ID>-rev.
        This method updates the reaction_classes dict keys accordingly.

        Args:
            reaction_classes: Dict mapping reaction IDs to class names
            flux: Dict mapping reaction IDs to flux values (or None)

        Returns:
            Dict with adjusted keys matching translated bigg_ids
        """
        if not flux:
            return dict(reaction_classes)
        translated = {}
        for rxn_id, class_name in reaction_classes.items():
            if rxn_id in flux and flux[rxn_id] < 0:
                translated[f"{rxn_id}-rev"] = class_name
            else:
                translated[rxn_id] = class_name
        return translated

    def _inject_reaction_class_overlays(self, html_path: str,
                                         reaction_classes: Dict[str, str]) -> None:
        """Inject reaction class badge overlays into an Escher HTML file.

        Post-processes the HTML file produced by Escher's save_html() to add
        colored rectangle badges near each reaction label, indicating which
        class the reaction belongs to. Also adds a legend in the bottom-right.

        Badges are appended inside Escher's own SVG reaction-label-group elements
        so they pan/zoom with the map. They use pointer-events:none so Escher's
        click/drag handlers are not disrupted.

        Args:
            html_path: Path to the HTML file to modify
            reaction_classes: Dict mapping reaction bigg_ids to class names
                              (already translated for negative flux if needed)
        """
        if not reaction_classes:
            return

        # Build color mapping from class names to palette
        unique_classes = sorted(set(reaction_classes.values()))
        if len(unique_classes) > len(REACTION_CLASS_COLORS):
            self.log_warning(
                f"More than {len(REACTION_CLASS_COLORS)} reaction classes provided. "
                f"Colors will wrap around."
            )
        class_colors = {}
        for i, class_name in enumerate(unique_classes):
            class_colors[class_name] = REACTION_CLASS_COLORS[i % len(REACTION_CLASS_COLORS)]

        # Read the HTML file
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        # Serialize data for JavaScript injection
        rc_json = json.dumps(reaction_classes)
        cc_json = json.dumps(class_colors)

        # Build legend items HTML
        legend_items = ''
        for class_name in unique_classes:
            color = class_colors[class_name]
            legend_items += (
                f'<div class="rxn-legend-item">'
                f'<div class="rxn-legend-swatch" style="background:{color}"></div>'
                f'<span>{class_name}</span></div>'
            )

        injection = f'''
<style>
.rxn-class-badge {{
    pointer-events: none;
    opacity: 0.85;
}}
#reaction-class-legend {{
    position: fixed;
    bottom: 10px;
    right: 10px;
    background: rgba(255,255,255,0.95);
    border: 1px solid #ccc;
    border-radius: 5px;
    padding: 10px 14px;
    font-family: sans-serif;
    font-size: 12px;
    z-index: 10000;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    max-height: 80vh;
    overflow-y: auto;
    pointer-events: auto;
}}
#reaction-class-legend h4 {{
    margin: 0 0 8px 0;
    font-size: 13px;
    border-bottom: 1px solid #ddd;
    padding-bottom: 4px;
}}
.rxn-legend-item {{
    display: flex;
    align-items: center;
    margin-bottom: 4px;
}}
.rxn-legend-swatch {{
    width: 14px;
    height: 14px;
    border-radius: 2px;
    margin-right: 8px;
    flex-shrink: 0;
}}
</style>
<div id="reaction-class-legend">
    <h4>Reaction Classes</h4>
    {legend_items}
</div>
<script>
(function() {{
    var reactionClasses = {rc_json};
    var classColors = {cc_json};

    var BADGE_WIDTH = 14;
    var BADGE_HEIGHT = 10;
    var BADGE_OFFSET_X = -20;
    var BADGE_OFFSET_Y = -14;

    function addBadges() {{
        var reactions = document.querySelectorAll('.reaction');
        if (!reactions.length) return false;
        var added = 0;
        reactions.forEach(function(reactionGroup) {{
            var data = reactionGroup.__data__;
            if (!data || !data.bigg_id) return;
            var biggId = data.bigg_id;
            if (!(biggId in reactionClasses)) return;
            var className = reactionClasses[biggId];
            var color = classColors[className];
            if (!color) return;

            var labelGroup = reactionGroup.querySelector('.reaction-label-group');
            if (!labelGroup) return;

            // Avoid duplicates if called more than once
            if (labelGroup.querySelector('.rxn-class-badge')) return;

            var ns = 'http://www.w3.org/2000/svg';
            var rect = document.createElementNS(ns, 'rect');
            rect.setAttribute('class', 'rxn-class-badge');
            rect.setAttribute('x', BADGE_OFFSET_X);
            rect.setAttribute('y', BADGE_OFFSET_Y);
            rect.setAttribute('width', BADGE_WIDTH);
            rect.setAttribute('height', BADGE_HEIGHT);
            rect.setAttribute('rx', '2');
            rect.setAttribute('fill', color);
            labelGroup.appendChild(rect);
            added++;
        }});
        return added > 0;
    }}

    function waitAndAddBadges() {{
        var container = document.getElementById('map-container');
        if (!container) {{
            setTimeout(waitAndAddBadges, 200);
            return;
        }}
        if (addBadges()) return;

        var observer = new MutationObserver(function(mutations, obs) {{
            var reactions = document.querySelectorAll('.reaction');
            if (reactions.length > 0) {{
                obs.disconnect();
                setTimeout(addBadges, 500);
            }}
        }});
        observer.observe(container, {{ childList: true, subtree: true }});

        // Safety fallback
        setTimeout(function() {{
            observer.disconnect();
            addBadges();
        }}, 10000);
    }}

    if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', waitAndAddBadges);
    }} else {{
        waitAndAddBadges();
    }}
}})();
</script>
'''

        # Inject before </body>
        html_content = html_content.replace('</body>', injection + '\n</body>')

        # Write back
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        self.log_info(
            f"Injected reaction class overlays for {len(reaction_classes)} reactions "
            f"across {len(unique_classes)} classes"
        )

    def create_map_html2(self,model,map,output_path,flux=None,reaction_classes=None,height=600,width=900,use_short_rxn_names=True):
        """Create an HTML file that renders an Escher map with model data.

        Args:
            model: COBRApy model object or MSModelUtil object
            map: Map identifier (name, filename, or KBase reference)
            output_path: Path to write the HTML file
            flux: Flux solution data (dict or pandas Series with reaction IDs as keys/index).
                  Used for reaction data display and directionality (negative flux reactions
                  are reversed in the map).
            reaction_classes: Optional dict mapping reaction IDs to class name strings
                  (e.g. {"rxn00001_c0": "glycolysis", "rxn00002_c0": "TCA cycle"}).
                  Up to 10 distinct classes are supported with unique colors. When provided,
                  colored badges are overlaid on classified reactions and a legend is added.
            height: Height of the map container in pixels
            width: Width of the map container in pixels
            use_short_rxn_names: If True, use shortest ModelSEED alias names for reactions
        """
        try:
            from escher import Builder
        except ImportError:
            raise ImportError("escher package is required for map visualization. Install with: pip install escher")

        #Translating model to modelutl
        if not isinstance(model, self.MSModelUtil):
            model = self.MSModelUtil(model)
        
        #Loading map from file
        map_data = self._load_map(map)
        if use_short_rxn_names:
            map_data = self.update_map_reaction_names(map_data)

        #If there's flux, translating map, model, and flux
        cobramodel = model.model
        if flux is not None:
            map_data = self._translate_map_with_flux(map_data,flux)
            cobramodel = self._translate_model_with_flux(model,flux,use_short_rxn_names=use_short_rxn_names)
            flux = self._translate_flux(flux)

        # Create Escher Builder
        builder_kwargs = {
            'model': cobramodel,
            'height': height,
            'width': width,
            'map_json': json.dumps(map_data)
        }

        builder = Builder(**builder_kwargs)

        if flux is not None:
            builder.reaction_data = flux

        # Use reaction names (short names set by _translate_model_with_flux) instead of IDs
        builder.identifiers_on_map = 'name'

        builder.save_html(output_path)

        # Post-process HTML to add reaction class badge overlays
        if reaction_classes:
            translated_classes = self._translate_reaction_classes(reaction_classes, flux)
            self._inject_reaction_class_overlays(output_path, translated_classes)
