"""Example usage of KBPLMUtils for protein homology search.

This example demonstrates how to use the Protein Language Model utilities
to find homologs for proteins in a KBase genome or feature container object.
"""

from kbutillib import KBPLMUtils

# Initialize the PLM utilities
plm_utils = KBPLMUtils(log_level="INFO")

# Example 1: Load a genome from KBase and find best hits
# Note: This requires authentication to KBase workspace
# plm_utils.load_kbase_gene_container("MyGenome.1", ws="MyWorkspace")

# Example 2: Load from a local JSON file
# If you have a genome JSON file downloaded locally
# plm_utils.load_kbase_gene_container("/path/to/genome.json", localname="my_genome")

# Example 3: Find best hits for features (after loading)
# This will:
# 1. Extract protein sequences from features
# 2. Query the PLM API for top 100 hits
# 3. Retrieve UniProt sequences
# 4. Create a BLAST database
# 5. Run BLAST to find best matches
# 6. Return results with best UniProt IDs

# Full results with all details
# results = plm_utils.find_best_hits_for_features(
#     feature_container_name="my_genome",
#     max_plm_hits=100,          # Get top 100 hits from PLM API
#     similarity_threshold=0.0,   # No threshold (get all hits)
#     blast_evalue=0.001          # BLAST E-value threshold
# )

# Print detailed results
# for feature_id, hit_data in results.items():
#     print(f"Feature: {feature_id}")
#     print(f"  Best UniProt ID: {hit_data['best_uniprot_id']}")
#     print(f"  PLM Score: {hit_data['plm_score']}")
#     print(f"  BLAST E-value: {hit_data['blast_evalue']}")
#     print(f"  BLAST Bit Score: {hit_data['blast_bit_score']}")
#     print(f"  BLAST Identity: {hit_data['blast_identity']}")
#     print(f"  Total PLM hits: {len(hit_data['all_plm_hits'])}")
#     print()

# Example 4: Get just the best UniProt IDs (simplified)
# uniprot_mapping = plm_utils.get_best_uniprot_ids(
#     feature_container_name="my_genome",
#     max_plm_hits=50  # Use fewer hits for faster processing
# )

# Print simple mapping
# for feature_id, uniprot_id in uniprot_mapping.items():
#     print(f"{feature_id} -> {uniprot_id}")

# Example 5: Query PLM API directly with custom sequences
query_sequences = [
    {
        "id": "protein1",
        "sequence": "MKLAVLGAAVLGAAVIGPGQFHQFFGDVEGTPVDIFHKYFQGASAQHEGGAFIFNMNVNGSKQKLQAANDVVTS"
    },
    {
        "id": "protein2",
        "sequence": "MKLVLLGFAGLLLGSALAHGQGFNMQTVDTAHFGFQDTSQRIQAYWTEGEMLQSQFDLGMGSDRKAIEKYGLQF"
    }
]

# Query PLM API
plm_results = plm_utils.query_plm_api(
    query_sequences=query_sequences,
    max_hits=10,
    similarity_threshold=0.5
)

print("PLM API Results:")
for hits_data in plm_results.get("hits", []):
    query_id = hits_data.get("query_id", "")
    print(f"\nQuery: {query_id}")
    print(f"Total hits: {hits_data.get('total_hits', 0)}")

    for i, hit in enumerate(hits_data.get("hits", [])[:5], 1):
        print(f"  Hit {i}: {hit.get('id', 'N/A')} (score: {hit.get('score', 0):.3f})")

# Example 6: Get UniProt sequences
uniprot_ids = ["P12345", "Q9Y6K9", "O15552"]  # Example UniProt IDs
sequences = plm_utils.get_uniprot_sequences(uniprot_ids)

print("\nRetrieved UniProt Sequences:")
for uniprot_id, sequence in sequences.items():
    print(f"{uniprot_id}: {sequence[:50]}... (length: {len(sequence)})")

# Note: BLAST functionality requires NCBI BLAST+ to be installed
# On Ubuntu/Debian: sudo apt-get install ncbi-blast+
# On MacOS: brew install blast

print("\n" + "="*60)
print("Example completed!")
print("="*60)
