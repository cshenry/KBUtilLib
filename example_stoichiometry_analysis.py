#!/usr/bin/env python
"""Example script demonstrating the analyze_reaction_stoichiometry function.

This script shows how to use the new AI curation function to analyze
and categorize reaction stoichiometry into primary, cofactor, and minor components.
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from kbutillib import AICurationUtils

def main():
    """Demonstrate reaction stoichiometry analysis."""

    # Initialize the AI curation utilities
    util = AICurationUtils()

    # Example: Load a model and analyze some reactions
    # (You would replace this with your actual model loading code)
    print("=" * 80)
    print("Reaction Stoichiometry Analysis Example")
    print("=" * 80)
    print()
    print("To use this function with your model:")
    print()
    print("  from kbutillib import AICurationUtils")
    print("  util = AICurationUtils()")
    print()
    print("  # Load your model")
    print("  from kbutillib import KBModelUtils")
    print("  model_util = KBModelUtils()")
    print("  pubmod = model_util.MSModelUtil.from_cobrapy('path/to/your/model.json')")
    print()
    print("  # Analyze a specific reaction")
    print("  rxn = pubmod.model.reactions.get_by_id('ANME_3')")
    print("  result = util.analyze_reaction_stoichiometry(rxn)")
    print()
    print("  # View the results")
    print("  print('Primary stoichiometry:', result['primary_stoichiometry'])")
    print("  print('Cofactor stoichiometry:', result['cofactor_stoichiometry'])")
    print("  print('Minor stoichiometry:', result['minor_stoichiometry'])")
    print("  print('Primary chemistry:', result['primary_chemistry'])")
    print("  print('Confidence:', result['confidence'])")
    print()
    print("Results are automatically cached, so repeated queries for the same")
    print("reaction (based on base_id) will return cached results instantly.")
    print()
    print("=" * 80)
    print()
    print("Example output structure:")
    print()
    print("{")
    print('  "primary_stoichiometry": {')
    print('    "Glucose": -1.0,')
    print('    "Glucose-6-phosphate": 1.0')
    print('  },')
    print('  "cofactor_stoichiometry": {')
    print('    "ATP": -1.0,')
    print('    "ADP": 1.0')
    print('  },')
    print('  "minor_stoichiometry": {')
    print('    "H+": 1.0')
    print('  },')
    print('  "primary_chemistry": "Phosphorylation of glucose at C6 position",')
    print('  "other_comments": "Clear hexokinase reaction with standard cofactors.",')
    print('  "confidence": "high"')
    print("}")
    print()

if __name__ == '__main__':
    main()
