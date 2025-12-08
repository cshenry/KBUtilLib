#!/usr/bin/env python
"""Example script demonstrating AI curation with both Argo and Claude Code backends.

This script shows how to use AICurationUtils with either backend for AI-powered
reaction analysis and curation.
"""

import sys
from pathlib import Path

# Add src directory to path
project_root = Path(__file__).parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from kbutillib import AICurationUtils


def example_argo_backend():
    """Example using Argo backend (default)."""
    print("=" * 80)
    print("EXAMPLE 1: Using Argo Backend")
    print("=" * 80)
    print()

    # Initialize with Argo backend (default)
    util = AICurationUtils()

    print(f"Backend: {util.ai_backend}")
    print()

    # Example: Simple chat query
    print("Sending test query to Argo...")
    try:
        response = util.chat(
            prompt="What is the EC number for alcohol dehydrogenase?",
            system="You are an expert in enzyme classification."
        )
        print(f"Response: {response}")
    except Exception as e:
        print(f"Error: {e}")

    print()


def example_claude_code_backend():
    """Example using Claude Code backend."""
    print("=" * 80)
    print("EXAMPLE 2: Using Claude Code Backend")
    print("=" * 80)
    print()

    # Initialize with Claude Code backend
    # Note: This requires claude-code to be installed and available
    try:
        util = AICurationUtils(backend="claude-code")

        print(f"Backend: {util.ai_backend}")
        print(f"Executable: {util.claude_code_executable}")
        print()

        # Example: Simple chat query
        print("Sending test query to Claude Code...")
        response = util.chat(
            prompt="What is ATP?",
            system="You are an expert in biochemistry. Respond in JSON format with a 'definition' field."
        )
        print(f"Response: {response}")

    except FileNotFoundError:
        print("Claude Code not found. Please install it to use this backend.")
    except Exception as e:
        print(f"Error: {e}")

    print()


def example_config_based_backend():
    """Example using configuration file to specify backend."""
    print("=" * 80)
    print("EXAMPLE 3: Backend from Configuration")
    print("=" * 80)
    print()

    # Backend is determined by config.yaml
    # Set ai_curation.backend to "argo" or "claude-code" in config
    util = AICurationUtils()

    print(f"Backend (from config): {util.ai_backend}")
    print()
    print("To change backend, edit config.yaml:")
    print("  ai_curation:")
    print("    backend: 'claude-code'  # or 'argo'")
    print("    claude_code_executable: 'claude-code'")
    print()


def example_reaction_analysis():
    """Example showing reaction analysis (works with either backend)."""
    print("=" * 80)
    print("EXAMPLE 4: Reaction Analysis with AI")
    print("=" * 80)
    print()

    # This example shows how the backend is transparent to the analysis functions
    # You can use either Argo or Claude Code without changing your code

    print("Note: This example requires a valid reaction object from a metabolic model.")
    print("The analyze_reaction_directionality() method will work with either backend.")
    print()
    print("Example usage:")
    print("""
    from kbutillib import AICurationUtils
    import cobra

    # Load a model
    model = cobra.io.read_sbml_model("path/to/model.xml")

    # Initialize with desired backend
    util = AICurationUtils(backend="claude-code")  # or "argo"

    # Analyze reaction directionality
    for rxn in model.reactions[:5]:
        result = util.analyze_reaction_directionality(rxn)
        print(f"{rxn.id}: {result['directionality']} (confidence: {result['confidence']})")
    """)


def main():
    """Run all examples."""
    print()
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "AI Curation Utils - Backend Examples" + " " * 22 + "║")
    print("╚" + "═" * 78 + "╝")
    print()

    example_config_based_backend()
    example_argo_backend()
    example_claude_code_backend()
    example_reaction_analysis()

    print("=" * 80)
    print("KEY FEATURES:")
    print("=" * 80)
    print("- Two AI backends: Argo (default) and Claude Code")
    print("- Configuration via config.yaml (ai_curation.backend)")
    print("- Override backend at initialization: AICurationUtils(backend='claude-code')")
    print("- All analysis methods work transparently with either backend")
    print("- Claude Code runs locally, Argo uses remote API")
    print("- Caching works the same way regardless of backend")
    print()


if __name__ == "__main__":
    main()
