#!/usr/bin/env python
"""Example script demonstrating the query_plm_api_batch function.

This script shows how to use the new batch query function to simplify
calls to the PLM service. The function automatically handles batching,
progress tracking, and error handling.
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from kbutillib import KBPLMUtils


def main():
    """Demonstrate PLM batch query functionality."""

    print("=" * 80)
    print("PLM Batch Query Example")
    print("=" * 80)
    print()

    # Example usage
    print("BEFORE (Manual Batching):")
    print("-" * 80)
    print("""
    # Old approach - manual batching code
    batch_size = 50
    total_batches = (len(query_sequences) + batch_size - 1) // batch_size

    gene_uniprot_data = {}

    for batch_idx in range(total_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, len(query_sequences))
        batch = query_sequences[start_idx:end_idx]

        print(f"  Batch {batch_idx + 1}/{total_batches}: ", end="", flush=True)

        try:
            plm_results = util.query_plm_api(
                batch,
                max_hits=5,
                similarity_threshold=0.0,
                return_embeddings=False
            )

            # Process results
            for hit_data in plm_results.get("hits", []):
                query_id = hit_data.get("query_id", "")
                hits = hit_data.get("hits", [])

                if hits:
                    gene_uniprot_data[query_id] = [
                        hit.get("id", "") for hit in hits
                    ]

            print(f"OK ({len(plm_results.get('hits', []))} results)")

        except Exception as e:
            print(f"ERROR: {str(e)}")

    # Calculate summary
    genes_with_hits = len(gene_uniprot_data)
    total_hits = sum(len(hits) for hits in gene_uniprot_data.values())
    """)

    print()
    print("AFTER (Using query_plm_api_batch):")
    print("-" * 80)
    print("""
    # New approach - simplified with batch function
    from kbutillib import KBPLMUtils

    util = KBPLMUtils()

    # Single call handles all batching automatically
    results = util.query_plm_api_batch(
        query_sequences,
        max_hits=5,
        similarity_threshold=0.0,
        return_embeddings=False,
        batch_size=50  # Optional: defaults to 50
    )

    # Process aggregated results
    gene_uniprot_data = {}
    for hit_data in results["hits"]:
        query_id = hit_data.get("query_id", "")
        hits = hit_data.get("hits", [])

        if hits:
            gene_uniprot_data[query_id] = [
                hit.get("id", "") for hit in hits
            ]

    # Summary statistics included in results
    print(f"Total queries: {results['total_queries']}")
    print(f"Successful queries: {results['successful_queries']}")
    print(f"Failed batches: {results['failed_batches']}")

    genes_with_hits = len(gene_uniprot_data)
    total_hits = sum(len(hits) for hits in gene_uniprot_data.values())
    print(f"Genes with hits: {genes_with_hits}")
    print(f"Total hits: {total_hits}")
    """)

    print()
    print("=" * 80)
    print()
    print("KEY BENEFITS:")
    print("- Automatic batch processing with configurable batch size")
    print("- Built-in progress tracking via logging")
    print("- Per-batch error handling with error collection")
    print("- Automatic result aggregation across all batches")
    print("- Summary statistics included in return value")
    print("- Cleaner, more maintainable code")
    print()
    print("=" * 80)
    print()

    print("RETURN VALUE STRUCTURE:")
    print()
    print("{")
    print('  "hits": [')
    print('    {')
    print('      "query_id": "gene_001",')
    print('      "hits": [')
    print('        {"id": "P12345", "score": 0.95},')
    print('        {"id": "Q67890", "score": 0.88}')
    print('      ]')
    print('    },')
    print('    ...')
    print('  ],')
    print('  "total_queries": 1000,')
    print('  "successful_queries": 1000,')
    print('  "failed_batches": 0,')
    print('  "errors": []')
    print("}")
    print()


if __name__ == '__main__':
    main()
