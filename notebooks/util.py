import sys
import os
import json
from os import path
from zipfile import ZipFile

# Add the parent directory to the sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
script_path = os.path.abspath(__file__)
script_dir = os.path.dirname(script_path)
base_dir = os.path.dirname(os.path.dirname(script_dir))
folder_name = os.path.basename(script_dir)

print(base_dir+"/KBUtilLib/src")
sys.path = [base_dir+"/KBUtilLib/src",base_dir+"/cobrakbase",base_dir+"/ModelSEEDpy/"] + sys.path

# Import utilities with error handling
from kbutillib import ModelStandardizationUtils, MSFBAUtils, AICurationUtils, NotebookUtils, EscherUtils, KBPLMUtils, BVBRCUtils

import hashlib
import pandas as pd
from pandas import DataFrame, read_csv, concat, set_option
from cobrakbase.core.kbasefba import FBAModel
import cobra
from cobra import Reaction, Metabolite
from cobra.flux_analysis import pfba
from cobra.io import save_json_model, load_json_model
from modelseedpy import AnnotationOntology, MSPackageManager, MSMedia, MSModelUtil, MSBuilder, MSATPCorrection, MSGapfill, MSGrowthPhenotype, MSGrowthPhenotypes, ModelSEEDBiochem, MSExpression
import re
import copy
import numpy as np

# Define the base classes based on what's available
# Note: KBPLMUtils inherits from KBGenomeUtils, so we use KBPLMUtils instead of KBGenomeUtils
class BVBRCUtil(NotebookUtils,BVBRCUtils):
    def __init__(self,**kwargs):
        super().__init__(
            notebook_folder=script_dir,
            name="BVBRCUtils",
            **kwargs
        )

class AICurationUtil(NotebookUtils,AICurationUtils):
    def __init__(self,backend="argo",proxy_port=None,**kwargs):
        super().__init__(
            notebook_folder=script_dir,
            name="AICurationUtils",
            backend=backend,
            proxy_port=proxy_port,
            **kwargs
        )