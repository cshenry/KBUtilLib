#!/usr/bin/env python3
"""KBUtilLib Usage Examples

This file demonstrates how to use the modular utility framework in various ways.
Run this script to see the different patterns in action.
"""

import sys
from pathlib import Path

# Add the src directory to the path so we can import kbutillib
sys.path.insert(0, str(Path(__file__).parent / "src"))

from kbutillib import (
    KBGenomeUtils,
    KBModelUtils,
    KBSDKUtils,
    MSBiochemUtils,
    SharedEnvUtils,
)
from kbutillib.examples import (
    FullUtilityStack,
    KBaseSDKWorkflow,
    KBaseWorkbench,
    NotebookAnalysis,
)


def example_basic_usage():
    """Example 1: Basic usage of individual utility modules."""
    print("\n=== Example 1: Basic Individual Module Usage ===")

    # Initialize shared environment
    env = SharedEnvUtils(config_file="config.yaml")
    print(f"Loaded configuration with {len(env._config_hash)} settings")

    # Use individual utilities
    genome_utils = KBGenomeUtils()
    biochem_utils = MSBiochemUtils()

    # Example with biochemistry utilities
    try:
        glucose_results = biochem_utils.search_compounds("glucose", max_results=3)
        print(f"Found {len(glucose_results)} glucose compounds")
    except Exception as e:
        print(f"Biochemistry search example failed: {e}")
        print("This is normal if modelseedpy is not installed")

    # Example with genome utilities
    test_sequence = "ATGAAAGCCTAG"
    translated = genome_utils.translate_sequence(test_sequence)
    print(f"Translated sequence: {test_sequence} -> {translated}")


def example_composite_classes():
    """Example 2: Using pre-built composite classes."""
    print("\n=== Example 2: Composite Class Usage ===")

    # KBase workbench with multiple utilities
    workbench = KBaseWorkbench(config_file="config.yaml")

    # Notebook analysis environment
    notebook_env = NotebookAnalysis()
    print(f"Running in notebook: {notebook_env.is_notebook_environment()}")

    # Full utility stack
    full_stack = FullUtilityStack()
    print("Full utility stack initialized with all modules")


def example_custom_composition():
    """Example 3: Creating custom utility combinations."""
    print("\n=== Example 3: Custom Utility Composition ===")

    # Create a custom combination using multiple inheritance
    class MyAnalysisTools(MSBiochemUtils, SharedEnvUtils):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.log_info("Custom analysis tools initialized")

        def combined_analysis(self, compound_query):
            """Custom method combining multiple utilities."""
            # Use biochemistry utilities
            if compound_query:
                try:
                    results = self.search_compounds(compound_query, max_results=5)
                    self.log_info(
                        f"Found {len(results)} compounds for '{compound_query}'"
                    )
                    return results
                except Exception as e:
                    self.log_error(f"Compound search failed: {e}")
                    return []
            return []

    # Use the custom class
    custom_tools = MyAnalysisTools(config_file="config.yaml")

    # Example compound query
    results = custom_tools.combined_analysis("glucose")
    print(f"Custom analysis result: {results}")


def example_environment_management():
    """Example 4: Environment and configuration management."""
    print("\n=== Example 4: Environment Management ===")

    # Create shared environment
    env = SharedEnvUtils()

    # Set some tokens
    env.set_token("my_api_key", "secret_value_here")
    env.set_token("database_password", "another_secret")

    # Try to get auth tokens (will return None without real tokens)
    kbase_token = env.get_token("kbase")
    print(f"KBase token available: {kbase_token is not None}")

    # Export environment state (without exposing secrets)
    env_state = env.export_environment()
    print(f"Environment state: {list(env_state.keys())}")


def example_genome_analysis():
    """Example 5: Genome analysis workflows."""
    print("\n=== Example 5: Genome Analysis ===")

    genome_utils = KBGenomeUtils()

    # Example genome data structure (simplified)
    example_genome = {
        "id": "example_genome",
        "scientific_name": "Escherichia coli",
        "domain": "Bacteria",
        "dna_size": 4641652,
        "num_contigs": 1,
        "gc_content": 0.507,
        "features": [
            {
                "id": "gene_1",
                "type": "CDS",
                "function": "DNA polymerase",
                "location": [["contig_1", 100, "+", 300]],
            },
            {
                "id": "gene_2",
                "type": "CDS",
                "function": "RNA polymerase",
                "location": [["contig_1", 500, "-", 400]],
            },
        ],
        "contigs": [
            {
                "id": "contig_1",
                "sequence": "ATGAAAGCCTAG" * 100,  # Simplified sequence
            }
        ],
    }

    # Parse genome
    genome_info = genome_utils.parse_genome_object(example_genome)
    print(
        f"Genome info: {genome_info['scientific_name']} with {genome_info['features']} features"
    )

    # Extract CDS features
    cds_features = genome_utils.extract_features_by_type(example_genome, "CDS")
    print(f"Found {len(cds_features)} CDS features")

    # Analyze sequences
    test_dna = "ATGAAAGCCTAG"
    protein = genome_utils.translate_sequence(test_dna)
    gc_content = genome_utils.calculate_gc_content(test_dna)

    print(f"DNA: {test_dna}")
    print(f"Protein: {protein}")
    print(f"GC content: {gc_content:.3f}")


def example_biochemistry_analysis():
    """Example 6: Biochemistry database analysis workflows."""
    print("\n=== Example 6: Biochemistry Database Analysis ===")

    try:
        biochem_utils = MSBiochemUtils()

        # Search for compounds
        glucose_results = biochem_utils.search_compounds("glucose", max_results=3)
        print(f"Found {len(glucose_results)} glucose compounds")

        for result in glucose_results[:3]:  # Show top 3 results
            print(
                f"  - {result['compound_id']}: {result['name']} ({result['formula']})"
            )

    except Exception as e:
        print(f"Biochemistry analysis example failed: {e}")
        print(
            "This is normal if modelseedpy is not installed or database not available"
        )


def example_model_analysis():
    """Example 7: Metabolic model analysis."""
    print("\n=== Example 7: Metabolic Model Analysis ===")

    model_utils = KBModelUtils()

    # Example model data (simplified structure)
    example_model = {
        "id": "iML1515",
        "name": "E. coli core model",
        "modelreactions": [
            {
                "id": "bio1",
                "name": "Biomass reaction",
                "direction": "=>",
                "modelReactionReagents": [
                    {"modelcompound_ref": "cpd_glucose_c0", "coefficient": -1},
                    {"modelcompound_ref": "cpd_biomass_c0", "coefficient": 1},
                ],
            },
            {
                "id": "EX_glc_e",
                "name": "Glucose exchange",
                "direction": "<=",
                "modelReactionReagents": [
                    {"modelcompound_ref": "cpd_glucose_e0", "coefficient": -1}
                ],
            },
        ],
        "modelcompounds": [
            {"id": "cpd_glucose_c0", "name": "Glucose cytoplasm"},
            {"id": "cpd_glucose_e0", "name": "Glucose extracellular"},
            {"id": "cpd_biomass_c0", "name": "Biomass"},
        ],
        "modelcompartments": [
            {"id": "c0", "label": "Cytoplasm"},
            {"id": "e0", "label": "Extracellular"},
        ],
    }

    # Parse model
    model_info = model_utils.parse_model_object(example_model)
    print(f"Model: {model_info['name']} ({model_info['num_reactions']} reactions)")

    # Get exchange reactions
    exchange_rxns = model_utils.get_model_reactions(
        example_model, filter_type="exchange"
    )
    print(f"Exchange reactions: {len(exchange_rxns)}")

    # Calculate statistics
    stats = model_utils.calculate_model_statistics(example_model)
    print(
        f"Model statistics: {stats['total_reactions']} reactions, {stats['total_compounds']} compounds"
    )

    # Prepare FBA constraints
    media = {"glucose": 10.0}  # 10 mmol/gDW/hr glucose uptake
    constraints = model_utils.prepare_fba_constraints(
        example_model, media_conditions=media
    )
    print(
        f"FBA constraints prepared for {len(constraints['reaction_bounds'])} reactions"
    )


def example_biochem_database():
    """Example 8: ModelSEED Biochemistry Database search."""
    print("\n=== Example 8: ModelSEED Biochemistry Database ===")

    try:
        # Initialize biochemistry utilities
        biochem_utils = MSBiochemUtils()

        # Search for compounds by name
        glucose_results = biochem_utils.search_compounds("glucose", max_results=5)
        print(f"Found {len(glucose_results)} compounds matching 'glucose'")

        for result in glucose_results[:3]:  # Show top 3 results
            print(
                f"  - {result['compound_id']}: {result['name']} ({result['formula']})"
            )

        # Search by formula
        formula_results = biochem_utils.search_by_formula("C6H12O6", exact_match=True)
        print(f"Found {len(formula_results)} compounds with formula C6H12O6")

        # Search reactions by equation
        atp_reactions = biochem_utils.search_reactions(
            "ATP", search_fields=["name", "equation"], max_results=3
        )
        print(f"Found {len(atp_reactions)} reactions involving ATP")

        for result in atp_reactions[:2]:  # Show top 2 results
            print(f"  - {result['reaction_id']}: {result['name']}")

        # Get compound details by ID
        if glucose_results:
            compound_id = glucose_results[0]["compound_id"]
            compound_info = biochem_utils.get_compound_structure_info(compound_id)
            print(f"Compound details for {compound_id}:")
            print(f"  Name: {compound_info.get('name', 'N/A')}")
            print(f"  Formula: {compound_info.get('formula', 'N/A')}")
            print(f"  Mass: {compound_info.get('mass', 'N/A')}")

        # Find similar compounds
        if glucose_results:
            compound_id = glucose_results[0]["compound_id"]
            similar = biochem_utils.find_similar_compounds(compound_id, max_results=3)
            print(f"Found {len(similar)} compounds similar to {compound_id}")

        # Get database statistics
        stats = biochem_utils.get_database_statistics()
        print(
            f"Database contains {stats['total_compounds']} compounds and {stats['total_reactions']} reactions"
        )

        # Export search results
        if glucose_results:
            biochem_utils.export_search_results(
                glucose_results, "glucose_search.json", format="json"
            )
            print("Search results exported to glucose_search.json")

    except Exception as e:
        print(f"Biochem database example failed: {e}")
        print(
            "This is normal if modelseedpy is not installed or database not available"
        )


def example_sdk_utilities():
    """Example 8: KBase SDK utilities workflows."""
    print("\n=== Example 8: KBase SDK Utilities ===")

    sdk_utils = KBSDKUtils(module_name="TestModule", config_file="config.yaml")

    # Example method call initialization
    sdk_utils.initialize_call(
        "test_method",
        {"input_param": "test_value", "workspace": "my_workspace"},
        print_params=True,
    )

    print(f"SDK module: {sdk_utils.module_name}")
    print(f"Working directory: {sdk_utils.working_dir}")
    print(f"Timestamp: {sdk_utils.timestamp}")

    # Example file operations
    test_data = {"analysis": "results", "count": 42}
    sdk_utils.save_json("test_results", test_data)

    loaded_data = sdk_utils.load_json("test_results")
    print(f"Saved and loaded data: {loaded_data}")

    # Example workspace reference creation
    ref = sdk_utils.create_ref("my_object", "my_workspace")
    print(f"Created reference: {ref}")

    # Example environment detection
    env = sdk_utils.kb_environment()
    print(f"KBase environment: {env}")

    # Example argument validation
    try:
        params = {"required_param": "value"}
        validated = sdk_utils.validate_args(
            params, ["required_param"], {"optional_param": "default"}
        )
        print(f"Validated parameters: {validated}")
    except ValueError as e:
        print(f"Validation error: {e}")


def example_sdk_composite_workflow():
    """Example 9: SDK composite workflow."""
    print("\n=== Example 9: SDK Composite Workflow ===")

    # SDK workflow with multiple utilities
    sdk_workflow = KBaseSDKWorkflow(config_file="config.yaml")

    # Example workflow method
    result = sdk_workflow.my_specialized_workflow()
    print(f"Workflow result: {result}")

    # Full utility stack with SDK
    full_stack = FullUtilityStack(config_file="config.yaml")
    print(f"Full stack has SDK utils: {hasattr(full_stack, 'initialize_call')}")
    print(f"Full stack has genome utils: {hasattr(full_stack, 'translate_sequence')}")
    print(f"Full stack has MS utils: {hasattr(full_stack, 'find_peaks')}")


def main():
    """Run all examples."""
    print("KBUtilLib Usage Examples")
    print("=" * 50)

    try:
        example_basic_usage()
        example_composite_classes()
        example_custom_composition()
        example_environment_management()
        example_genome_analysis()
        example_biochemistry_analysis()
        example_biochem_database()
        example_model_analysis()
        example_sdk_utilities()
        example_sdk_composite_workflow()

        print("\n" + "=" * 50)
        print("All examples completed successfully!")
        print("\nNext steps:")
        print("1. Configure your environment variables (KB_AUTH_TOKEN, etc.)")
        print("2. Customize the config.yaml file for your needs")
        print("3. Create your own composite utility classes")
        print("4. Start migrating your existing utility code into the framework")
        print("5. Use KBSDKUtils for KBase SDK development workflows")

    except Exception as e:
        print(f"\nError running examples: {e}")
        print("This is normal if dependencies are not installed or configured")


if __name__ == "__main__":
    main()
