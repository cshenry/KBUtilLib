#!/usr/bin/env python3
"""Example usage of KBUniProtUtils for fetching protein information from UniProt.

This script demonstrates how to use the KBUniProtUtils class to:
1. Fetch protein sequences
2. Retrieve annotations
3. Get publications
4. Fetch Rhea IDs
5. Fetch PDB IDs
6. Get UniRef cluster IDs (most important!)
7. Fetch comprehensive information in one call
8. Process multiple entries in batch
"""

import json
import sys
from pathlib import Path

# Add the src directory to the path
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

from kbutillib.kb_uniprot_utils import KBUniProtUtils


def example_1_basic_sequence():
    """Example 1: Fetch protein sequence."""
    print("\n" + "="*80)
    print("EXAMPLE 1: Fetch Protein Sequence")
    print("="*80)

    utils = KBUniProtUtils()

    # Fetch sequence for P99999 (a test UniProt ID)
    # Using P31946 (14-3-3 protein beta/alpha - a real protein)
    uniprot_id = "P31946"

    print(f"\nFetching sequence for {uniprot_id}...")
    sequence = utils.get_protein_sequence(uniprot_id, format="raw")

    print(f"\nProtein sequence (first 100 characters):")
    print(sequence[:100] + "...")
    print(f"\nTotal sequence length: {len(sequence)} amino acids")


def example_2_annotations():
    """Example 2: Fetch protein annotations."""
    print("\n" + "="*80)
    print("EXAMPLE 2: Fetch Protein Annotations")
    print("="*80)

    utils = KBUniProtUtils()
    uniprot_id = "P31946"

    print(f"\nFetching annotations for {uniprot_id}...")
    annotations = utils.get_annotations(uniprot_id)

    print("\nAnnotations summary:")
    print(f"  - Protein name: {annotations.get('proteinDescription', {}).get('recommendedName', {}).get('fullName', {}).get('value', 'N/A')}")
    print(f"  - Organism: {annotations.get('organism', {}).get('scientificName', 'N/A')}")
    print(f"  - Gene names: {annotations.get('genes', [{}])[0].get('geneName', {}).get('value', 'N/A') if annotations.get('genes') else 'N/A'}")


def example_3_publications():
    """Example 3: Fetch publication references."""
    print("\n" + "="*80)
    print("EXAMPLE 3: Fetch Publication References")
    print("="*80)

    utils = KBUniProtUtils()
    uniprot_id = "P31946"

    print(f"\nFetching publications for {uniprot_id}...")
    publications = utils.get_publications(uniprot_id)

    print(f"\nFound {len(publications)} publications")
    print("\nFirst 3 publications:")
    for i, pub in enumerate(publications[:3], 1):
        print(f"\n  {i}. {pub.get('title', 'No title')[:80]}...")
        print(f"     PubMed ID: {pub.get('pubmed_id', 'N/A')}")
        print(f"     Year: {pub.get('year', 'N/A')}")


def example_4_rhea_ids():
    """Example 4: Fetch Rhea reaction IDs."""
    print("\n" + "="*80)
    print("EXAMPLE 4: Fetch Rhea Reaction IDs")
    print("="*80)

    utils = KBUniProtUtils()
    # Using P00395 (Cytochrome c oxidase) which should have Rhea IDs
    uniprot_id = "P00395"

    print(f"\nFetching Rhea IDs for {uniprot_id}...")
    try:
        rhea_ids = utils.get_rhea_ids(uniprot_id)

        if rhea_ids:
            print(f"\nFound {len(rhea_ids)} Rhea IDs:")
            for rhea_id in rhea_ids:
                print(f"  - {rhea_id}")
        else:
            print("\nNo Rhea IDs found for this protein.")
    except Exception as e:
        print(f"\nError fetching Rhea IDs: {e}")


def example_5_pdb_ids():
    """Example 5: Fetch PDB structure IDs."""
    print("\n" + "="*80)
    print("EXAMPLE 5: Fetch PDB Structure IDs")
    print("="*80)

    utils = KBUniProtUtils()
    uniprot_id = "P31946"

    print(f"\nFetching PDB IDs for {uniprot_id}...")
    pdb_ids = utils.get_pdb_ids(uniprot_id, full_info=False)

    if pdb_ids:
        print(f"\nFound {len(pdb_ids)} PDB structures:")
        for pdb_id in pdb_ids[:5]:  # Show first 5
            print(f"  - {pdb_id}")
        if len(pdb_ids) > 5:
            print(f"  ... and {len(pdb_ids) - 5} more")
    else:
        print("\nNo PDB structures found for this protein.")

    # Also get full info for first structure
    print("\nFetching full PDB information...")
    pdb_full_info = utils.get_pdb_ids(uniprot_id, full_info=True)
    if pdb_full_info:
        print("\nFirst PDB entry with full details:")
        print(json.dumps(pdb_full_info[0], indent=2))


def example_6_uniref_ids():
    """Example 6: Fetch UniRef cluster IDs (MOST IMPORTANT!)."""
    print("\n" + "="*80)
    print("EXAMPLE 6: Fetch UniRef Cluster IDs (MOST IMPORTANT!)")
    print("="*80)

    utils = KBUniProtUtils()
    uniprot_id = "P31946"

    print(f"\nFetching UniRef50 cluster ID for {uniprot_id}...")
    uniref_mapping = utils.get_uniref_ids(uniprot_id, uniref_type="UniRef50")

    print(f"\nUniRef50 cluster: {uniref_mapping.get(uniprot_id, 'Not found')}")

    # Also try UniRef90 and UniRef100
    print(f"\nFetching UniRef90 cluster ID...")
    uniref90_mapping = utils.get_uniref_ids(uniprot_id, uniref_type="UniRef90")
    print(f"UniRef90 cluster: {uniref90_mapping.get(uniprot_id, 'Not found')}")

    print(f"\nFetching UniRef100 cluster ID...")
    uniref100_mapping = utils.get_uniref_ids(uniprot_id, uniref_type="UniRef100")
    print(f"UniRef100 cluster: {uniref100_mapping.get(uniprot_id, 'Not found')}")


def example_7_comprehensive():
    """Example 7: Fetch all information in one call."""
    print("\n" + "="*80)
    print("EXAMPLE 7: Fetch All Information (Comprehensive)")
    print("="*80)

    utils = KBUniProtUtils()
    uniprot_id = "P31946"

    print(f"\nFetching comprehensive information for {uniprot_id}...")
    info = utils.get_uniprot_info(
        uniprot_id,
        include_sequence=True,
        include_annotations=True,
        include_publications=True,
        include_rhea_ids=True,
        include_pdb_ids=True,
        include_uniref_ids=True,
        uniref_type="UniRef50"
    )

    print("\nComprehensive information retrieved:")
    print(f"  - Sequence length: {len(info['sequence']) if info['sequence'] else 0} aa")
    print(f"  - Has annotations: {info['annotations'] is not None}")
    print(f"  - Number of publications: {len(info['publications']) if info['publications'] else 0}")
    print(f"  - Number of Rhea IDs: {len(info['rhea_ids']) if info['rhea_ids'] else 0}")
    print(f"  - Number of PDB IDs: {len(info['pdb_ids']) if info['pdb_ids'] else 0}")
    print(f"  - UniRef50 cluster: {info['uniref_ids']}")


def example_8_batch():
    """Example 8: Process multiple entries in batch."""
    print("\n" + "="*80)
    print("EXAMPLE 8: Batch Processing Multiple Entries")
    print("="*80)

    utils = KBUniProtUtils()

    # Multiple UniProt IDs to process
    uniprot_ids = ["P31946", "P62258", "P61981"]  # 14-3-3 family proteins

    print(f"\nProcessing {len(uniprot_ids)} UniProt entries in batch...")
    batch_results = utils.get_batch_uniprot_info(
        uniprot_ids,
        include_sequence=True,
        include_annotations=True,
        include_publications=False,  # Skip to save time
        include_rhea_ids=False,
        include_pdb_ids=True,
        include_uniref_ids=True,
        uniref_type="UniRef50"
    )

    print("\nBatch results summary:")
    for uniprot_id, info in batch_results.items():
        if "error" in info:
            print(f"\n  {uniprot_id}: ERROR - {info['error']}")
        else:
            print(f"\n  {uniprot_id}:")
            print(f"    - Sequence length: {len(info['sequence']) if info['sequence'] else 0} aa")
            print(f"    - PDB structures: {len(info['pdb_ids']) if info['pdb_ids'] else 0}")
            print(f"    - UniRef50 cluster: {info['uniref_ids']}")


def main():
    """Run all examples."""
    print("\n" + "="*80)
    print("KBUniProtUtils - UniProt API Wrapper Examples")
    print("="*80)

    try:
        example_1_basic_sequence()
        example_2_annotations()
        example_3_publications()
        example_4_rhea_ids()
        example_5_pdb_ids()
        example_6_uniref_ids()  # Most important!
        example_7_comprehensive()
        example_8_batch()

        print("\n" + "="*80)
        print("All examples completed successfully!")
        print("="*80 + "\n")

    except Exception as e:
        print(f"\n\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
