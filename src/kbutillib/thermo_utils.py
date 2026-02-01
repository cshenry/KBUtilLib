"""Thermodynamic utilities for metabolic reactions and compounds."""

import re
from typing import Any, Optional, Dict, List, Tuple

from .shared_env_utils import SharedEnvUtils


class ThermoUtils(SharedEnvUtils):
    """Utilities for thermodynamic calculations on metabolic reactions.

    Provides methods for computing ion transfer across compartments,
    calculating reaction deltaG values, and retrieving compound
    formation energies from the ModelSEED database.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize ThermoUtils.

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvUtils
        """
        super().__init__(**kwargs)
        self._biochem_utils = None

    @property
    def biochem_utils(self):
        """Lazy-load MSBiochemUtils for compound/reaction lookups."""
        if self._biochem_utils is None:
            from .ms_biochem_utils import MSBiochemUtils
            self._biochem_utils = MSBiochemUtils(**self._init_kwargs)
        return self._biochem_utils

    @property
    def _init_kwargs(self) -> Dict[str, Any]:
        """Get kwargs used to initialize this instance for passing to sub-utils."""
        return {}

    def _parse_id(self, obj) -> Tuple[str, Optional[str], Optional[str]]:
        """Parse an ID or object to extract base_id, compartment, and index.

        Args:
            obj: Either a string ID or an object with an 'id' attribute

        Returns:
            Tuple of (base_id, compartment, index)
        """
        if hasattr(obj, 'id'):
            obj_id = obj.id
        else:
            obj_id = str(obj)

        # Try pattern: base_id_compartment0 or base_id_compartment
        match = re.match(r'^(.+)_([a-z])(\d*)$', obj_id)
        if match:
            return (match.group(1), match.group(2), match.group(3) if match.group(3) else None)

        # Try pattern: base_id[compartment]
        match = re.match(r'^(.+)\[([a-z])\]$', obj_id)
        if match:
            return (match.group(1), match.group(2), None)

        # No compartment found
        return (obj_id, None, None)

    def get_compound_deltag(self, compound_id: str) -> Optional[float]:
        """Get the standard Gibbs free energy of formation for a compound.

        Retrieves deltaG value from:
        1. ModelSEED compound object deltag attribute
        2. Compound annotations if available

        Args:
            compound_id: ModelSEED compound ID (e.g., 'cpd00001')

        Returns:
            Standard Gibbs free energy of formation in kJ/mol, or None if not available

        Raises:
            ValueError: If compound not found in ModelSEED database and no deltag in annotations
        """
        # Get the compound object from ModelSEED database
        compound = self.biochem_utils.get_compound_by_id(compound_id)

        if compound is None:
            raise ValueError(
                f"Compound '{compound_id}' not found in ModelSEED database"
            )

        # First, try to get deltaG from the compound object's deltag attribute
        if hasattr(compound, 'deltag') and compound.deltag is not None:
            # Check if it's a valid value (ModelSEED uses 10000000 for unknown)
            if abs(compound.deltag) < 10000000:
                return float(compound.deltag)

        # Try to get from annotations
        if hasattr(compound, 'annotation') and compound.annotation:
            if 'deltag' in compound.annotation:
                deltag_val = compound.annotation['deltag']
                if isinstance(deltag_val, (int, float)) and abs(deltag_val) < 10000000:
                    return float(deltag_val)
                elif isinstance(deltag_val, str):
                    try:
                        val = float(deltag_val)
                        if abs(val) < 10000000:
                            return val
                    except ValueError:
                        pass

        # If we get here, no valid deltaG was found
        return None

    def calculate_reaction_deltag(
        self,
        reaction,
        use_compound_formation: bool = True,
        require_all_compounds: bool = True
    ) -> Dict[str, Any]:
        """Calculate standard free energy change for a reaction.

        This method can use either:
        1. Compound formation energies (recommended, more accurate)
        2. Reaction's stored deltaG value if available

        Args:
            reaction: Either a ModelSEED reaction ID (str) or a reaction object
            use_compound_formation: If True, calculate from compound formation energies.
                                   If False, use reaction's stored deltaG if available.
            require_all_compounds: Only used if use_compound_formation=True.
                                  If True, raises error if any compound lacks deltaG.

        Returns:
            Dictionary with calculation results:
                - 'deltag': Calculated deltaG value
                - 'deltag_error': Error estimate if available
                - 'reaction_id': The reaction ID
                - 'equation': Reaction equation string
                - 'compound_contributions': Dict of compound contributions
                - 'missing_compounds': List of compounds without deltaG
                - 'warnings': List of warning messages

        Raises:
            ValueError: If reaction not found or if calculation requirements not met
        """
        # Get reaction ID
        if isinstance(reaction, str):
            reaction_id = reaction
            reaction_obj = self.biochem_utils.get_reaction_by_id(reaction_id)
        else:
            reaction_obj = reaction
            reaction_id = reaction_obj.id if hasattr(reaction_obj, 'id') else str(reaction)

        if reaction_obj is None:
            raise ValueError(f"Reaction '{reaction_id}' not found in ModelSEED database")

        # Initialize result structure
        result = {
            'deltag': None,
            'deltag_error': None,
            'reaction_id': reaction_id,
            'equation': reaction_obj.build_reaction_string(use_metabolite_names=True),
            'compound_contributions': {},
            'missing_compounds': [],
            'warnings': []
        }

        if use_compound_formation:
            # Calculate deltaG from stoichiometry
            deltag_sum = 0.0
            deltag_error_sum = 0.0  # Sum of squared errors for error propagation
            missing_compounds = []
            compound_contributions = {}

            # Iterate through metabolites (compounds) in the reaction
            for metabolite, stoichiometry in reaction_obj.metabolites.items():
                # Get base compound ID (remove compartment suffix)
                base_cpd_id = metabolite.id.split('_')[0]

                try:
                    # Get deltaG for this compound
                    deltag_f = self.get_compound_deltag(metabolite.id)

                    if deltag_f is None:
                        missing_compounds.append({
                            'compound_id': base_cpd_id,
                            'stoichiometry': stoichiometry,
                            'name': metabolite.name if hasattr(metabolite, 'name') else base_cpd_id
                        })
                        continue

                    # Calculate contribution: stoichiometry * deltaG_f
                    contribution = stoichiometry * deltag_f
                    compound_contributions[base_cpd_id] = {
                        'deltag_f': deltag_f,
                        'stoichiometry': stoichiometry,
                        'contribution': contribution
                    }

                    deltag_sum += contribution

                    # Try to get error for error propagation
                    compound = self.biochem_utils.get_compound_by_id(base_cpd_id)
                    if compound and hasattr(compound, 'delta_g_error') and compound.delta_g_error is not None:
                        if abs(compound.delta_g_error) < 10000000:
                            # Error propagation: sigma^2 = sum(nu_i^2 * sigma_i^2)
                            deltag_error_sum += (stoichiometry ** 2) * (compound.delta_g_error ** 2)

                except ValueError as e:
                    missing_compounds.append({
                        'compound_id': base_cpd_id,
                        'stoichiometry': stoichiometry,
                        'error': str(e)
                    })

            # Check if we have missing compounds
            if missing_compounds:
                result['missing_compounds'] = missing_compounds

                if require_all_compounds:
                    compound_list = [f"{c['compound_id']} (stoich: {c['stoichiometry']})"
                                for c in missing_compounds]
                    raise ValueError(
                        f"Reaction '{reaction_id}' contains compounds without valid deltaG values: "
                        f"{', '.join(compound_list)}. These compounds are either not in ModelSEED "
                        f"database or do not have deltaG values in their annotations."
                    )
                else:
                    result['warnings'].append(
                        f"Partial calculation: {len(missing_compounds)} compound(s) missing deltaG values"
                    )

            # Set the calculated values
            result['deltag'] = deltag_sum
            result['compound_contributions'] = compound_contributions

            # Calculate propagated error
            if deltag_error_sum > 0:
                result['deltag_error'] = deltag_error_sum ** 0.5
        else:
            # Use reaction's stored deltaG
            result = {
                'deltag': None,
                'deltag_error': None,
                'reaction_id': reaction_id,
                'equation': reaction_obj.build_reaction_string() if hasattr(reaction_obj, 'build_reaction_string') else str(reaction_obj),
                'source': 'reaction_attribute',
                'warnings': []
            }

            if hasattr(reaction_obj, 'delta_g') and reaction_obj.delta_g is not None:
                if abs(reaction_obj.delta_g) < 10000000:
                    result['deltag'] = float(reaction_obj.deltag)
                else:
                    result['warnings'].append("Reaction deltaG value is unknown (10000000)")
            else:
                result['warnings'].append("Reaction has no stored deltaG value")

            if hasattr(reaction_obj, 'delta_g_error') and reaction_obj.delta_g_error is not None:
                if abs(reaction_obj.delta_g_error) < 10000000:
                    result['deltag_error'] = float(reaction_obj.delta_g_error)

        return result

    def compute_ion_transfer(
        self,
        reaction,
        compartment_pairs: List[Tuple[str, str]],
        add_to_notes: bool = False
    ) -> Dict[str, Any]:
        """Compute ion transfer for a reaction across compartment pairs.

        For each compartment pair, identifies metabolites that are transferred from
        one compartment to the other and computes the ion transfer value as:
        min(|coef1|, |coef2|) * charge * sign(coef2)

        where coef1 and coef2 are the stoichiometric coefficients in compartments 1 and 2.

        Args:
            reaction: A COBRApy Reaction object
            compartment_pairs: List of (compartment1, compartment2) tuples specifying
                              which compartment transfers to check (e.g., [('c', 'e'), ('c', 'p')])
            add_to_notes: If True, add the ion_transfer result to reaction.notes

        Returns:
            Dictionary with:
                - 'total_ion_transfer': Sum of ion transfers across all pairs
                - 'by_pair': Dict mapping each pair to its ion transfer details
                - 'transferred_metabolites': List of metabolites involved in transfers
        """
        result = {
            'total_ion_transfer': 0.0,
            'by_pair': {},
            'transferred_metabolites': []
        }

        # Build a mapping of base_id -> {compartment: (metabolite, coefficient)}
        # Base ID is the metabolite ID without compartment suffix
        metabolite_by_base = {}
        for metabolite, coef in reaction.metabolites.items():
            base_id, compartment, index = self._parse_id(metabolite)
            if compartment is not None:
                metabolite_by_base.setdefault(base_id, {})
                metabolite_by_base[base_id][compartment] = (metabolite, coef)

        # Process each compartment pair
        for comp1, comp2 in compartment_pairs:
            pair_key = f"{comp1}->{comp2}"
            pair_result = {
                'ion_transfer': 0.0,
                'metabolites': []
            }

            # Find metabolites present in both compartments
            for base_id, compartments in metabolite_by_base.items():
                if comp1 in compartments and comp2 in compartments:
                    met1, coef1 = compartments[comp1]
                    met2, coef2 = compartments[comp2]

                    # Get charge from metabolite
                    charge = 0
                    if hasattr(met1, 'charge') and met1.charge is not None:
                        charge = met1.charge
                    elif hasattr(met2, 'charge') and met2.charge is not None:
                        charge = met2.charge

                    # Compute ion transfer:
                    # min(|coef1|, |coef2|) * charge * sign(coef2)
                    min_coef = min(abs(coef1), abs(coef2))
                    sign_coef2 = 1 if coef2 > 0 else (-1 if coef2 < 0 else 0)
                    ion_transfer = -1 * min_coef * charge * sign_coef2

                    if ion_transfer != 0 or charge != 0:
                        met_info = {
                            'base_id': base_id,
                            'name': met1.name if hasattr(met1, 'name') else base_id,
                            'charge': charge,
                            'coef_comp1': coef1,
                            'coef_comp2': coef2,
                            'ion_transfer': ion_transfer
                        }
                        pair_result['metabolites'].append(met_info)
                        pair_result['ion_transfer'] += ion_transfer

                        if met_info not in result['transferred_metabolites']:
                            result['transferred_metabolites'].append(met_info)

            result['by_pair'][pair_key] = pair_result
            result['total_ion_transfer'] += pair_result['ion_transfer']

        # Add to reaction notes if requested
        if add_to_notes:
            if not hasattr(reaction, 'notes') or reaction.notes is None:
                reaction.notes = {}
            reaction.notes['ion_transfer'] = result['total_ion_transfer']

        return result
