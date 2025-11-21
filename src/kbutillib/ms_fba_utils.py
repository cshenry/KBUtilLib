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

    def run_fba(self,model,media=None,objective=None,run_pfba=True):
        """Run FBA on a model with a specified media and objective"""
        model = self._check_and_convert_model(model)
        self.set_media(model,media)
        self.set_objective_from_string(model,objective)    
        #Optimizing the model
        solution = model.model.optimize()
        if run_pfba:
            pfb_solution = pfba(model.model)
            pfb_solution.objective_value = solution.objective_value
            return pfb_solution
        return solution
    
    def run_fva(self,model,media=None,objective=None,fraction_of_optimum=0.9):
        model = self._check_and_convert_model(model)
        self.set_media(model,media)
        self.set_objective_from_string(model,objective)
        self.constrain_objective_to_fraction_of_optimum(model, fraction=fraction_of_optimum)
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