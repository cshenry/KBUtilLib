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

    def create_map_html2(self,model,map,output_path,flux=None,height=600,width=900,use_short_rxn_names=True):
        """Create an HTML file that renders an Escher map with model data

        Args:
            model: COBRApy model object or MSModelUtil object
            flux: Flux solution data (dict or pandas Series with reaction IDs as keys/index)

        Returns:
            str: Path to the created HTML file
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

            # Set builder properties for full interactivity before saving
            if hasattr(builder, 'menu'):
                builder.menu = 'all'
            if hasattr(builder, 'enable_keys'):
                builder.enable_keys = True
            if hasattr(builder, 'enable_editing'):
                builder.enable_editing = True
            if hasattr(builder, 'scroll_behavior'):
                builder.scroll_behavior = 'zoom'

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
        # Escher control options
        menu: Literal['all', 'zoom', 'none'] = 'all',
        enable_keys: bool = True,
        enable_editing: bool = True,
        scroll_behavior: Literal['pan', 'zoom'] = 'zoom',
        enable_search: bool = True,
        enable_tooltips: bool = True,
        # Display options
        identifiers_on_map: Literal['bigg_id', 'name'] = 'name',
        hide_secondary_metabolites: bool = False,
        show_gene_reaction_rules: bool = False,
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
            menu: Menu type - 'all' for full menu, 'zoom' for zoom buttons only, 'none' to hide (default: 'all')
            enable_keys: Enable keyboard shortcuts for map interaction (default: True)
            enable_editing: Enable map editing functions (default: True)
            scroll_behavior: Scroll wheel behavior - 'pan' to move map, 'zoom' to adjust magnification (default: 'zoom')
            enable_search: Enable search functionality for finding reactions/metabolites (default: True)
            enable_tooltips: Enable tooltips on hover (default: True)
            identifiers_on_map: What to display as labels - 'name' for compound/reaction names, 'bigg_id' for IDs (default: 'name')
            hide_secondary_metabolites: Hide secondary metabolites like water, ATP, etc. (default: False)
            show_gene_reaction_rules: Show gene reaction rules on the map (default: False)
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

        # Set Builder options for interactivity
        if enable_search:
            builder.enable_search = True
        if enable_tooltips:
            builder.enable_tooltips = ['label', 'object']

        # Set display options
        builder.identifiers_on_map = identifiers_on_map
        builder.hide_secondary_metabolites = hide_secondary_metabolites
        builder.show_gene_reaction_rules = show_gene_reaction_rules

        # Get Escher map HTML and scripts with control options
        escher_html = self._get_escher_html_embed(
            builder,
            menu=menu,
            enable_keys=enable_keys,
            enable_editing=enable_editing,
            scroll_behavior=scroll_behavior,
            enable_search=enable_search,
            enable_tooltips=enable_tooltips
        )
        
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
    
    def _get_escher_html_embed(
        self,
        builder,
        menu: str = 'all',
        enable_keys: bool = True,
        enable_editing: bool = True,
        scroll_behavior: str = 'zoom',
        enable_search: bool = True,
        enable_tooltips: bool = True
    ) -> str:
        """Get the HTML/JavaScript code to embed the Escher map.

        Args:
            builder: Escher Builder object
            menu: Menu type - 'all' for full menu, 'zoom' for zoom buttons only (default: 'all')
            enable_keys: Enable keyboard shortcuts (default: True)
            enable_editing: Enable editing functions (default: True)
            scroll_behavior: Scroll behavior - 'pan' or 'zoom' (default: 'zoom')
            enable_search: Enable search functionality (default: True)
            enable_tooltips: Enable tooltips (default: True)

        Returns:
            str: Complete HTML including div and scripts to render the map
        """
        try:
            # Set Builder properties for interactivity controls
            # These must be set before generating HTML
            if hasattr(builder, 'menu'):
                builder.menu = menu
            if hasattr(builder, 'enable_keys'):
                builder.enable_keys = enable_keys
            if hasattr(builder, 'enable_editing'):
                builder.enable_editing = enable_editing
            if hasattr(builder, 'scroll_behavior'):
                builder.scroll_behavior = scroll_behavior
            if hasattr(builder, 'enable_search'):
                builder.enable_search = enable_search
            if hasattr(builder, 'enable_tooltips'):
                builder.enable_tooltips = ['label', 'object'] if enable_tooltips else []

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
                    # Properties were already set on builder above, just save
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