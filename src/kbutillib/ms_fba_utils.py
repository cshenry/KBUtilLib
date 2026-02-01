"""KBase model utilities for constraint-based metabolic modeling."""

import pickle
from typing import Any, Dict
import pandas as pd
import re
import json

from cobra.flux_analysis import flux_variability_analysis
from cobra.flux_analysis import pfba 

from .kb_model_utils import KBModelUtils

class MSFBAUtils(KBModelUtils):
    """Tools for running a wide range of FBA analysis on metabolic models in KBase
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize MS model utilities

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvironment
        """
        super().__init__(**kwargs)

    def set_media(self, model, media):
        """Sets the media for the model"""
        if media is None:
            return
        model = self._check_and_convert_model(model)
        if isinstance(media,str):
            media = self.get_media(media,None)
        if isinstance(media,dict):
            media = self.MSMediaUtil(media)
        model.pkgmgr.getpkg("KBaseMediaPkg").build_package(media)
        return media
    
    def set_objective_from_string(self, model, objective: str):
        """Sets the objective for the model from a string"""
        if objective is None:
            return
        model = self._check_and_convert_model(model)
        model.pkgmgr.getpkg("ObjectivePkg").build_package(objective)
    
    def constrain_objective(self, model, objective=None, lower_bound=None, upper_bound=None):
        """Constrains the current objective to set upper/lower bounds"""
        self.set_objective_from_string(model,objective)
        model = self._check_and_convert_model(model)
        model.pkgmgr.getpkg("ObjConstPkg").build_package(lower_bound, upper_bound)

    def constrain_objective_to_fraction_of_optimum(self, model, fraction=0.9, media=None, objective=None):
        """Constrains the current objective to a fraction of the optimum"""
        model = self._check_and_convert_model(model)
        self.set_media(model,media)
        self.set_objective_from_string(model,objective)
        objective_value = model.model.slim_optimize()
        lower_bound = objective_value*fraction
        upper_bound = None
        if model.model.objective_direction == "min":
            lower_bound = None
            upper_bound = objective_value/fraction
        self.constrain_objective(model,lower_bound=lower_bound,upper_bound=upper_bound)
        return objective_value

    def configure_fba_formulation(self,model,media=None,objective=None,fraction_of_optimum=None):
        model = self._check_and_convert_model(model)
        if media is not None:
            self.set_media(model,media)
        if objective is not None:
            self.set_objective_from_string(model,objective)
        if fraction_of_optimum is not None:
            self.constrain_objective_to_fraction_of_optimum(model, fraction=fraction_of_optimum)
        return model

    def run_fba(self,model,media=None,objective=None,run_pfba=True):
        """Run FBA on a model with a specified media and objective"""
        model =self.configure_fba_formulation(model,media=media,objective=objective)
        #Optimizing the model
        solution = model.model.optimize()
        if run_pfba:
            pfb_solution = pfba(model.model)
            pfb_solution.objective_value = solution.objective_value
            return pfb_solution
        return solution
    
    def run_fva(self,model,media=None,objective=None,fraction_of_optimum=0.9):
        model = self.configure_fba_formulation(model,media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
        original_objective = model.model.objective
        results = {}
        for rxn in model.model.reactions:
            self.set_objective_from_string(model, objective="MAX{" + rxn.id + "}")
            results[rxn.id] = {}
            results[rxn.id]["MAX"] = model.model.slim_optimize()
            self.set_objective_from_string(model, objective="MIN{" + rxn.id + "}")
            results[rxn.id]["MIN"] = model.model.slim_optimize()
        model.model.objective = original_objective
        return results

    def analyzed_reaction_objective_coupling(self,model,solution,media=None,objective=None,fraction_of_optimum=None,biomass_objective_coupling=False,biomass_id=None):
        model = self.configure_fba_formulation(model,media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
        original_objective = model.model.objective
        print(original_objective.expression)
        # Categorize reactions by flux
        output = {}
        zero_flux_rxns = []
        active_rxns = []
        
        for rxn_id, flux in solution.fluxes.items():
            if rxn_id not in [r.id for r in model.model.reactions]:
                continue
            if abs(flux) <= 1e-9:
                zero_flux_rxns.append(rxn_id)
            else:
                active_rxns.append((rxn_id, flux))
        
        self.log_info(f"Zero-flux reactions: {len(zero_flux_rxns)}")
        self.log_info(f"Active reactions: {len(active_rxns)}")
        
        #Building this outside of with model statement because model is failing to clear the additional constraints consistently
        if biomass_objective_coupling:
            self.log_info(f"Building flexible biomass package")
            model.pkgmgr.getpkg("FlexibleBiomassPkg").build_package({"bio_rxn_id":biomass_id,"set_min_flex_biomass_objective":False})
            for rxn in model.model.reactions:
                if rxn.id.startswith("FLEX_"):
                    rxn.lower_bound = 0
                    rxn.upper_bound = 0

        # Get baseline growth with constrained model
        print(f"Baseline objective: {model.model.objective.expression}")
        output["baseline_objective_value"] = model.model.optimize().objective_value
        # Test each active reaction knockout
        output["essential_count"] = 0
        output["reduced_count"] = 0
        output["reaction_objective_coupling"] = {}
        with model.model:
            #model.model.objective = original_objective
            # Set zero-flux reactions to have zero bounds
            for rxn_id in zero_flux_rxns:
                rxn = model.model.reactions.get_by_id(rxn_id)
                rxn.lower_bound = 0
                rxn.upper_bound = 0
            
            #Consider setting max flux to current flux of every reaction as an optional procedure
            
            for rxn_id, original_flux in active_rxns:
                output["reaction_objective_coupling"][rxn_id] = {"original_flux":original_flux}
                rxn = model.model.reactions.get_by_id(rxn_id)
                
                # Save original bounds
                orig_lb = rxn.lower_bound
                orig_ub = rxn.upper_bound
                
                # Knock out the reaction
                rxn.lower_bound = 0
                rxn.upper_bound = 0
                
                # Optimize
                ko_solution = model.model.optimize()
                if ko_solution.status == 'optimal':
                    output["reaction_objective_coupling"][rxn_id]["ko_objective_value"] = ko_solution.objective_value
                    output["reaction_objective_coupling"][rxn_id]["objective_ratio"] = ko_solution.objective_value / output["baseline_objective_value"]  if output["baseline_objective_value"] > 0 else 0
                else:
                    output["reaction_objective_coupling"][rxn_id]["objective_ratio"] = 0
                    output["reaction_objective_coupling"][rxn_id]["ko_objective_value"] = None
                
                # Categorize impact
                if output["reaction_objective_coupling"][rxn_id]["objective_ratio"] < 0.01:
                    output["reaction_objective_coupling"][rxn_id]["impact"] = "essential"
                    output["essential_count"] += 1
                elif output["reaction_objective_coupling"][rxn_id]["objective_ratio"] < 0.95:
                    output["reaction_objective_coupling"][rxn_id]["impact"] = "reduced"
                    output["reduced_count"] += 1
                else:
                    output["reaction_objective_coupling"][rxn_id]["impact"] = "dispensable"
                
                if biomass_objective_coupling and output["reaction_objective_coupling"][rxn_id]["impact"] in ["essential","reduced"] and rxn_id != biomass_id:
                    output["reaction_objective_coupling"][rxn_id]["biomass_coupling"] = self.determine_biomass_objective_coupling(model,biomass_id,output["baseline_objective_value"],media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
                
                # Restore original bounds
                rxn.upper_bound = orig_ub
                rxn.lower_bound = orig_lb

        #Now let's repeat the analysis while allowing flux through the zero flux reactions
        # Get baseline growth with constrained model
        print(f"Unconstrained baseline objective: {model.model.objective.expression}")
        output["unconstrained_baseline_objective_value"] = model.model.optimize().objective_value
        # Test each active reaction knockout
        output["unconstrained_essential_count"] = 0
        output["unconstrained_reduced_count"] = 0
        with model.model:
            for rxn_id, original_flux in active_rxns:
                rxn = model.model.reactions.get_by_id(rxn_id)
                
                # Save original bounds
                orig_lb = rxn.lower_bound
                orig_ub = rxn.upper_bound
                
                # Knock out the reaction
                rxn.lower_bound = 0
                rxn.upper_bound = 0
                
                # Optimize
                ko_solution = model.model.optimize()
                if ko_solution.status == 'optimal':
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_ko_objective_value"] = ko_solution.objective_value
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_objective_ratio"] = ko_solution.objective_value / output["unconstrained_baseline_objective_value"]  if output["unconstrained_baseline_objective_value"] > 0 else 0
                else:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_objective_ratio"] = 0
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_ko_objective_value"] = None
                
                # Categorize impact
                if output["reaction_objective_coupling"][rxn_id]["unconstrained_objective_ratio"] < 0.01:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_impact"] = "essential"
                    output["unconstrained_essential_count"] += 1
                elif output["reaction_objective_coupling"][rxn_id]["unconstrained_objective_ratio"] < 0.95:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_impact"] = "reduced"
                    output["unconstrained_reduced_count"] += 1
                else:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_impact"] = "dispensable"
                
                if biomass_objective_coupling and output["reaction_objective_coupling"][rxn_id]["unconstrained_impact"] in ["essential","reduced"] and rxn_id != biomass_id:
                    output["reaction_objective_coupling"][rxn_id]["unconstrained_biomass_coupling"] = self.determine_biomass_objective_coupling(model,biomass_id,output["unconstrained_baseline_objective_value"],media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
                
                # Restore original bounds
                rxn.upper_bound = orig_ub
                rxn.lower_bound = orig_lb
        return output

    def determine_biomass_objective_coupling(self,model,biomass_id,biomass_flux,media=None,objective=None,fraction_of_optimum=None):
        model = self.configure_fba_formulation(model,media=media,objective=objective,fraction_of_optimum=fraction_of_optimum)
        
        #Checking if flexible biomass package is already built
        original_objective = model.model.objective
        flex_found = False
        for rxn in model.model.reactions:
            if rxn.id.startswith("FLEX_"):
                flex_found = True
                rxn.lower_bound = -1000
                rxn.upper_bound = 1000
        if not flex_found:
            model.pkgmgr.getpkg("FlexibleBiomassPkg").build_package({"bio_rxn_id":biomass_id,"set_min_flex_biomass_objective":True})
        else:
            model.pkgmgr.getpkg("FlexibleBiomassPkg").set_min_flex_biomass_objective()
        #Forcing biomass to stay at a fixed value
        biorxn = model.model.reactions.get_by_id(biomass_id)
        original_lower_bound = biorxn.lower_bound
        original_upper_bound = biorxn.upper_bound
        biorxn.lower_bound = biomass_flux
        biorxn.upper_bound = biomass_flux

        #Optimizing the model
        solution = model.model.optimize()
        #Getting the impacted biomass components
        impacted_biomass_components = {}
        for rxn in model.model.reactions:
            if rxn.id.startswith("FLEX_") and solution.fluxes[rxn.id] < 0:
                impacted_component = rxn.id[6+len(biomass_id):]
                impacted_biomass_components[impacted_component] = solution.fluxes[rxn.id]

        #Disabling FLEX variables
        for rxn in model.model.reactions:
            if rxn.id.startswith("FLEX_"):
                rxn.lower_bound = 0
                rxn.upper_bound = 0

        model.model.objective = original_objective
        biorxn.lower_bound = original_lower_bound
        biorxn.upper_bound = original_upper_bound
        return impacted_biomass_components

    def unblock_objective_with_exchanges(self, model, media=None, objective=None, min_threshold=0.1, solution_count=10,exclude_metabolites=[]):
        """Find minimal sets of exchanges needed to unblock an objective.

        This function helps debug why a model cannot achieve flux through an objective.
        It adds temporary exchanges for all non-extracellular metabolites that don't
        already have exchanges, constrains the objective to exceed a threshold, then
        minimizes the total exchange flux to find what metabolites need to be supplied
        or removed.

        Args:
            model: COBRApy model or MSModelUtil instance
            media: Media to apply (optional)
            objective: Objective string (e.g., "MAX{ANME_2}")
            min_threshold: Minimum flux required through objective
            solution_count: Maximum number of alternative solutions to find

        Returns:
            List of solution dictionaries, each containing:
            - 'active_exchanges': dict of {rxn_id: flux} for active temporary exchanges
            - 'objective_value': the objective value achieved
            - 'total_exchange_flux': sum of absolute flux through temporary exchanges
        """
        # Configure model with media and objective (returns MSModelUtil instance)
        mdlutl = self.configure_fba_formulation(model, media=media, objective=objective)
        cobra_model = mdlutl.model

        # Use MSModelUtil's exchange_hash to find metabolites that already have exchanges
        existing_exchange_hash = mdlutl.exchange_hash()
        existing_exchange_mets = set(met for met in existing_exchange_hash.keys())

        # Extracellular compartments - metabolites here don't need temporary exchanges
        extracellular_compartments = ['e', 'e0', 'env']

        # Find metabolites that need temporary exchanges:
        # - Not in extracellular compartment
        # - Don't already have an exchange
        mets_needing_exchanges = []
        for met in cobra_model.metabolites:
            if met.compartment not in extracellular_compartments and met not in existing_exchange_mets and met.id not in exclude_metabolites:
                mets_needing_exchanges.append(met)

        # Use MSModelUtil's add_exchanges_for_metabolites to add temporary exchanges
        if mets_needing_exchanges:
            mdlutl.add_exchanges_for_metabolites(
                mets_needing_exchanges,
                uptake=1000,
                excretion=1000,
                prefix="EX_temp_"
            )

        # Get list of added exchange reaction objects
        added_exchanges = []
        for rxn in cobra_model.reactions:
            if rxn.id.startswith("EX_temp_"):
                added_exchanges.append(rxn)

        self.log_info(f"Added {len(added_exchanges)} temporary exchanges for non-extracellular metabolites")

        # Constrain the objective to exceed the minimum threshold
        self.constrain_objective(mdlutl, objective=objective, lower_bound=min_threshold)

        # Set objective to minimize total absolute exchange flux
        objective_terms = []
        for ex_rxn in added_exchanges:
            objective_terms.append(ex_rxn.forward_variable)
            objective_terms.append(ex_rxn.reverse_variable)
        min_objective = cobra_model.problem.Objective(sum(objective_terms), direction='min')
        original_objective = cobra_model.objective
        cobra_model.objective = min_objective

        # Find multiple solutions by iteratively blocking active exchanges
        solutions = []
        blocked_exchanges = set()

        for i in range(solution_count):
            # Try to optimize
            model.printlp(print=True,path="models/lpfiles/",filename="unblock_objective_with_exchanges_"+str(i))
            solution = cobra_model.optimize()

            if solution.status != 'optimal':
                self.log_info(f"Solution {i+1}: No feasible solution found")
                break

            # Find active temporary exchanges
            active_exchanges = {}
            for ex_rxn in added_exchanges:
                if ex_rxn.id not in blocked_exchanges:
                    flux = solution.fluxes.get(ex_rxn.id, 0)
                    if abs(flux) > 1e-6:
                        # Get the metabolite name for better readability
                        met_id = ex_rxn.id.replace("EXC_temp_", "")
                        active_exchanges[ex_rxn.id] = {
                            'flux': flux,
                            'metabolite': met_id,
                            'direction': 'uptake' if flux < 0 else 'excretion'
                        }

            if not active_exchanges:
                self.log_info(f"Solution {i+1}: No active temporary exchanges (objective achievable without them)")
                solutions.append({
                    'solution_number': i + 1,
                    'active_exchanges': {},
                    'objective_value': solution.objective_value,
                    'message': 'Objective achievable without temporary exchanges'
                })
                break

            # Record solution
            sol_record = {
                'solution_number': i + 1,
                'active_exchanges': active_exchanges,
                'objective_value': solution.objective_value,
                'total_exchange_flux': sum(abs(v['flux']) for v in active_exchanges.values()),
                'fluxes': dict(solution.fluxes)
            }
            solutions.append(sol_record)

            self.log_info(f"Solution {i+1}: {len(active_exchanges)} active exchanges, total flux: {sol_record['total_exchange_flux']:.4f}")
            for rxn_id, data in active_exchanges.items():
                self.log_info(f"  {data['metabolite']}: {data['flux']:.4f} ({data['direction']})")

            # Block the active exchanges for next iteration
            for rxn_id in active_exchanges.keys():
                ex_rxn = cobra_model.reactions.get_by_id(rxn_id)
                ex_rxn.lower_bound = 0
                ex_rxn.upper_bound = 0
                blocked_exchanges.add(rxn_id)

        # Clean up: remove temporary exchanges
        cobra_model.remove_reactions(added_exchanges, remove_orphans=False)
        cobra_model.objective = original_objective

        return solutions

    def fit_flux_to_mutant_growth_rate_data(
        self,
        model,
        genome,
        data_source,
        media_dict,
        conditions=None,
        excluded_conditions=None,
        default_coef=0.01,
        activation_threshold=0.90,
        deactivation_threshold=0.95,
        biomass_reaction_id="bio1",
        growth_fraction=0.5,
        use_activation_constraints=False,
        run_reaction_coupling_analysis=True,
        verbose=True
    ):
        """Fit metabolic model fluxes to mutant growth rate data across multiple conditions.

        This function takes mutant growth rate phenotype data (normalized ratios), creates
        MSExpression constraints, and analyzes reaction essentiality for each condition.

        Args:
            model: COBRApy model, MSModelUtil instance, or path to model JSON file
            genome: MSGenome object or dict containing genome data (for gene lookups)
            data_source: One of:
                - str: Path to spreadsheet (.xls, .xlsx), JSON, or TSV file
                - dict: Dictionary of {condition: {gene_id: value}} data
                - pd.DataFrame: DataFrame with genes as rows, conditions as columns
            media_dict: Dictionary mapping condition names to media objects/dicts
            conditions: List of condition names to analyze. If None, uses all conditions
                from media_dict keys
            excluded_conditions: List of condition names to skip (default: None)
            default_coef: Default coefficient for expression fitting (default: 0.01)
            activation_threshold: Threshold below which genes are considered "on" (default: 0.90)
            deactivation_threshold: Threshold above which genes are considered "off" (default: 0.95)
            biomass_reaction_id: ID of the biomass/growth reaction (default: "bio1")
            growth_fraction: Fraction of optimal growth to constrain to (default: 0.5)
            use_activation_constraints: Whether to use hard activation constraints (default: False)
            run_reaction_coupling_analysis: Whether to run reaction KO analysis (default: True)
            verbose: Print progress messages (default: True)

        Returns:
            dict: Results dictionary with structure:
                {
                    "condition_name": {
                        "fluxes": {rxn_id: flux_value, ...},
                        "growth_rate": float,
                        "fraction": float,
                        "status": str,
                        "on_on": [rxn_ids...],  # Expression "on" AND flux active
                        "on_off": [rxn_ids...], # Expression "on" BUT flux inactive
                        "off_on": [rxn_ids...], # Expression "off" BUT flux active
                        "off_off": [rxn_ids...], # Expression "off" AND flux inactive
                        "none_on": [rxn_ids...], # No data, flux active
                        "none_off": [rxn_ids...], # No data, flux inactive
                        "on_genes": [gene_ids...], # Genes marked as "on"
                        "off_genes": [gene_ids...], # Genes marked as "off"
                        "on_rxn_genes": {rxn_id: [gene_ids]}, # Genes inducing "on" reactions
                        "baseline_growth": float,
                        "essential_count": int,
                        "reduced_count": int,
                        "reaction_objective_coupling": {...},
                        "on_on_reduced": [rxn_ids...],
                        "off_on_reduced": [rxn_ids...],
                        "none_on_reduced": [rxn_ids...],
                        "unconstrained_baseline_growth": float,
                        "unconstrained_essential_count": int,
                        "unconstrained_reduced_count": int
                    },
                    ...
                }
        """
        import cobra.io
        from modelseedpy import MSExpression, MSMedia
        from modelseedpy.core.msmodelutl import MSModelUtil

        # Load/convert model
        if isinstance(model, str):
            model = MSModelUtil.from_cobrapy(model)
        elif not isinstance(model, MSModelUtil):
            model = MSModelUtil.get(model)

        # Load/convert genome
        if isinstance(genome, dict):
            genome = self.get_msgenome_from_dict(genome.get("data", genome))

        # Load expression data from various sources
        if isinstance(data_source, str):
            # File path - determine type from extension
            if data_source.endswith(('.xls', '.xlsx')):
                expression = MSExpression.from_spreadsheet(
                    filename=data_source,
                    type="NormalizedRatios"
                )
            elif data_source.endswith('.json'):
                with open(data_source, 'r') as f:
                    data_dict = json.load(f)
                expression = MSExpression.load_from_dict(
                    genome_or_model=genome,
                    data_dict=data_dict,
                    value_type="NormalizedRatios"
                )
            elif data_source.endswith('.tsv'):
                df = pd.read_csv(data_source, sep='\t', index_col=0)
                expression = MSExpression.from_dataframe(
                    genome_or_model=genome,
                    df=df,
                    type="NormalizedRatios"
                )
            else:
                raise ValueError(f"Unsupported file type: {data_source}")
        elif isinstance(data_source, dict):
            expression = MSExpression.load_from_dict(
                genome_or_model=genome,
                data_dict=data_source,
                value_type="NormalizedRatios"
            )
        elif isinstance(data_source, pd.DataFrame):
            expression = MSExpression.from_dataframe(
                genome_or_model=genome,
                df=data_source,
                type="NormalizedRatios"
            )
        else:
            raise ValueError(f"Unsupported data_source type: {type(data_source)}")

        # Determine conditions to process
        if conditions is None:
            conditions = list(media_dict.keys())

        if excluded_conditions is None:
            excluded_conditions = []

        # Store results
        results = {}
        optimal_growth_rates = {}

        if verbose:
            self.log_info(f"Fitting flux to mutant growth rate data for {len(conditions)} conditions")
            self.log_info(f"Parameters: default_coef={default_coef}, activation_threshold={activation_threshold}, deactivation_threshold={deactivation_threshold}")

        for condition in conditions:
            if condition in excluded_conditions:
                if verbose:
                    self.log_info(f"Skipping excluded condition: {condition}")
                continue

            if condition not in media_dict:
                self.log_warning(f"No media found for condition: {condition}, skipping")
                continue

            if verbose:
                self.log_info(f"\nProcessing condition: {condition}")

            try:
                # Create a copy of the model for this condition
                model_copy = MSModelUtil.from_cobrapy(cobra.io.json.to_json(model.model))

                # Get media for this condition
                media = media_dict[condition]
                if isinstance(media, dict):
                    media = MSMedia.from_dict(media)

                # Set growth constraint to fraction of optimum
                optimal_growth = self.constrain_objective_to_fraction_of_optimum(
                    model_copy,
                    media=media,
                    objective=f"MAX{{{biomass_reaction_id}}}",
                    fraction=growth_fraction
                )
                optimal_growth_rates[condition] = optimal_growth

                if verbose:
                    self.log_info(f"  Optimal growth: {optimal_growth:.4f}, constrained to {growth_fraction*100}%")

                # Fit flux to mutant growth rate data
                model_copy.util = self
                fit_result = expression.fit_flux_to_mutant_growth_rate_data(
                    model=model_copy,
                    condition=condition,
                    default_coef=default_coef,
                    activation_threshold=activation_threshold,
                    deactivation_threshold=deactivation_threshold,
                    use_activation_constraints=use_activation_constraints
                )

                solution = fit_result.get('solution')
                if solution is None or solution.status != 'optimal':
                    status = solution.status if solution else 'no_solution'
                    self.log_warning(f"  Optimization failed for {condition}: {status}")
                    results[condition] = {'status': status}
                    continue

                if verbose:
                    self.log_info(f"  FBA successful, growth rate: {solution.objective_value:.4f}")

                # Run reaction objective coupling analysis if requested
                coupling_output = {}
                if run_reaction_coupling_analysis:
                    # Remove growth constraint for coupling analysis
                    self.constrain_objective_to_fraction_of_optimum(
                        model_copy, media=media,
                        objective=f"MAX{{{biomass_reaction_id}}}",
                        fraction=0
                    )

                    coupling_output = self.analyzed_reaction_objective_coupling(
                        model_copy,
                        solution,
                        biomass_objective_coupling=True,
                        biomass_id=biomass_reaction_id
                    )

                    # Initialize reduced lists
                    coupling_output["on_on_reduced"] = []
                    coupling_output["off_on_reduced"] = []
                    coupling_output["none_on_reduced"] = []

                    # Categorize reactions by expression status
                    for rxn in model_copy.model.reactions:
                        rxn_id = rxn.id
                        if rxn_id not in coupling_output.get("reaction_objective_coupling", {}):
                            continue
                        rxn_coupling = coupling_output["reaction_objective_coupling"][rxn_id]
                        if "objective_ratio" not in rxn_coupling:
                            continue

                        # Determine expression status
                        if rxn_id in fit_result.get("on_on", []) or rxn_id in fit_result.get("on_off", []):
                            rxn_coupling["expression_data_status"] = "on"
                            if rxn_coupling["objective_ratio"] < 0.95:
                                coupling_output["on_on_reduced"].append(rxn_id)
                        elif rxn_id in fit_result.get("off_on", []) or rxn_id in fit_result.get("off_off", []):
                            rxn_coupling["expression_data_status"] = "off"
                            if rxn_coupling["objective_ratio"] < 0.95:
                                coupling_output["off_on_reduced"].append(rxn_id)
                        else:
                            rxn_coupling["expression_data_status"] = "none"
                            if rxn_coupling["objective_ratio"] < 0.95:
                                coupling_output["none_on_reduced"].append(rxn_id)

                # Store results for this condition
                results[condition] = {
                    "fluxes": solution.fluxes.to_dict(),
                    "growth_rate": solution.fluxes.get(biomass_reaction_id, solution.objective_value),
                    "fraction": solution.fluxes.get(biomass_reaction_id, solution.objective_value) / optimal_growth if optimal_growth > 0 else 0,
                    "status": solution.status,
                    "on_on": fit_result.get("on_on", []),
                    "on_off": fit_result.get("on_off", []),
                    "off_on": fit_result.get("off_on", []),
                    "off_off": fit_result.get("off_off", []),
                    "none_on": fit_result.get("none_on", []),
                    "none_off": fit_result.get("none_off", []),
                    "on_genes": fit_result.get("on_genes", []),
                    "off_genes": fit_result.get("off_genes", []),
                    "on_rxn_genes": fit_result.get("on_rxn_genes", {}),
                    "baseline_growth": coupling_output.get("baseline_objective_value", 0),
                    "essential_count": coupling_output.get("essential_count", 0),
                    "reduced_count": coupling_output.get("reduced_count", 0),
                    "reaction_objective_coupling": coupling_output.get("reaction_objective_coupling", {}),
                    "on_on_reduced": coupling_output.get("on_on_reduced", []),
                    "off_on_reduced": coupling_output.get("off_on_reduced", []),
                    "none_on_reduced": coupling_output.get("none_on_reduced", []),
                    "unconstrained_baseline_growth": coupling_output.get("unconstrained_baseline_objective_value", 0),
                    "unconstrained_essential_count": coupling_output.get("unconstrained_essential_count", 0),
                    "unconstrained_reduced_count": coupling_output.get("unconstrained_reduced_count", 0)
                }

                if verbose:
                    self.log_info(f"  Essential reactions: {results[condition]['essential_count']}")
                    self.log_info(f"  On/On reactions: {len(results[condition]['on_on'])}")
                    self.log_info(f"  On genes: {len(results[condition]['on_genes'])}")

            except Exception as e:
                self.log_error(f"Error processing condition {condition}: {str(e)}")
                results[condition] = {'status': 'error', 'error': str(e)}

        if verbose:
            self.log_info(f"\nCompleted analysis for {len([r for r in results.values() if r.get('status') == 'optimal'])} conditions")

        return results