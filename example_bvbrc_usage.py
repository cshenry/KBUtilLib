#!/usr/bin/env python
"""Example script demonstrating BV-BRC API utilities.

This script shows how to use the BVBRCUtils class to:
- Fetch genomes from the BV-BRC API
- Load genomes from local BV-BRC files
- Create synthetic genomes from multiple sources
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from kbutillib import BVBRCUtils


def main():
    """Demonstrate BV-BRC utilities."""

    print("=" * 80)
    print("KBUtilLib BV-BRC API Utilities Examples")
    print("=" * 80)
    print()

    print("SETUP:")
    print("-" * 80)
    print("""
from kbutillib import BVBRCUtils

# Initialize BV-BRC utilities
util = BVBRCUtils()
    """)
    print()

    print("=" * 80)
    print()
    print("1. FETCH GENOME FROM BV-BRC API:")
    print("-" * 80)
    print()
    print("Fetch a genome directly from the BV-BRC API:")
    print("""
# Fetch E. coli K-12 genome
genome = util.build_kbase_genome_from_api('511145.183')

print(f"Genome ID: {genome['id']}")
print(f"Scientific name: {genome['scientific_name']}")
print(f"Features: {len(genome['features'])}")
print(f"DNA size: {genome['dna_size']:,} bp")
print(f"GC content: {genome['gc_content']:.2%}")

# Save to JSON file
import json
with open('ecoli_genome.json', 'w') as f:
    json.dump(genome, f, indent=2)
    """)
    print()

    print("What gets fetched:")
    print("  - Genome metadata (taxonomy, GC content, etc.)")
    print("  - Contig sequences from genome_sequence API")
    print("  - All features from genome_feature API (paginated)")
    print("  - Feature sequences by MD5 hash (batched)")
    print()

    print("=" * 80)
    print()
    print("2. LOAD GENOME FROM LOCAL FILES:")
    print("-" * 80)
    print()
    print("Load genome from local BV-BRC files (3-file structure):")
    print("""
# Load from local files:
# - genome_metadata/{genome_id}.json
# - genomes/{genome_id}.fna
# - features/{genome_id}.json

genome = util.load_genome_from_local_files(
    genome_id='511145.183',
    features_dir='features',
    genomes_dir='genomes',
    metadata_dir='genome_metadata'
)

print(f"Loaded {len(genome['features'])} features")
print(f"Contigs: {genome['num_contigs']}")
    """)
    print()

    print("Optional parameters:")
    print("""
# Override taxonomy and scientific name
genome = util.load_genome_from_local_files(
    genome_id='511145.183',
    taxonomy='Bacteria; Proteobacteria; Gammaproteobacteria',
    scientific_name='Escherichia coli K-12'
)
    """)
    print()

    print("=" * 80)
    print()
    print("3. AGGREGATE TAXONOMIES:")
    print("-" * 80)
    print()
    print("Find consensus taxonomy from multiple genomes:")
    print("""
# Load multiple genomes
genomes = [
    util.load_genome_from_local_files('genome1'),
    util.load_genome_from_local_files('genome2'),
    util.load_genome_from_local_files('genome3'),
]

# Aggregate taxonomies
consensus_taxonomy, taxonomy_dict = util.aggregate_taxonomies(
    genomes=genomes,
    asv_id='ASV_001',
    output_dir='taxonomies'  # Optional: saves to taxonomies/ASV_001.json
)

print(f"Consensus: {consensus_taxonomy}")
print(f"Kingdom: {taxonomy_dict['Kingdom']}")
print(f"Phylum: {taxonomy_dict['Phylum']}")
    """)
    print()

    print("Taxonomy dictionary format:")
    print("""
{
  "Kingdom": ["Bacteria", "Bacteria", "Bacteria"],
  "Phylum": ["Proteobacteria", "Firmicutes", "Proteobacteria"],
  "Class": ["Gammaproteobacteria", "Bacilli", "Gammaproteobacteria"],
  ...
}
    """)
    print()

    print("=" * 80)
    print()
    print("4. CREATE SYNTHETIC GENOME:")
    print("-" * 80)
    print()
    print("Merge multiple genomes into a synthetic genome:")
    print("""
# Load source genomes
source_genomes = [
    util.load_genome_from_local_files('genome1'),
    util.load_genome_from_local_files('genome2'),
    util.load_genome_from_local_files('genome3'),
]

# Create synthetic genome
synthetic = util.create_synthetic_genome(
    asv_id='ASV_Ecoli_Group',
    genomes=source_genomes,
    save_taxonomy=True,
    taxonomy_output_dir='taxonomies'
)

print(f"Synthetic genome ID: {synthetic['id']}")
print(f"Features: {len(synthetic['features'])}")
print(f"Source genomes: {synthetic['source_id']}")
print(f"Taxonomy: {synthetic['taxonomy']}")

# Save to file
import json
with open('synthetic_genome.json', 'w') as f:
    json.dump(synthetic, f, indent=2)
    """)
    print()

    print("How it works:")
    print("  - Collects unique functions from all source genomes")
    print("  - Each unique function becomes one feature")
    print("  - Tracks probability of each function across genomes")
    print("  - Uses consensus taxonomy from source genomes")
    print("  - Calculates average GC content")
    print()

    print("Optional: provide explicit taxonomy:")
    print("""
synthetic = util.create_synthetic_genome(
    asv_id='ASV_Custom',
    genomes=source_genomes,
    taxonomy='Bacteria; Proteobacteria; Gammaproteobacteria; Enterobacterales',
    save_taxonomy=False  # Don't save taxonomy aggregation
)
    """)
    print()

    print("=" * 80)
    print()
    print("5. FETCH INDIVIDUAL COMPONENTS:")
    print("-" * 80)
    print()
    print("Fetch specific data from BV-BRC API:")
    print("""
# Fetch only metadata
metadata = util.fetch_genome_metadata('511145.183')
print(f"Genome name: {metadata['genome_name']}")
print(f"GC content: {metadata['gc_content']}")

# Fetch only sequences (contigs)
sequences = util.fetch_genome_sequences('511145.183')
print(f"Number of contigs: {len(sequences)}")

# Fetch only features (paginated automatically)
features = util.fetch_genome_features('511145.183')
print(f"Number of features: {len(features)}")

# Fetch feature sequences by MD5
md5_hashes = ['abc123...', 'def456...']
sequences = util.fetch_feature_sequences(md5_hashes)
print(f"Retrieved {len(sequences)} sequences")
    """)
    print()

    print("=" * 80)
    print()
    print("6. COMPLETE WORKFLOW EXAMPLE:")
    print("-" * 80)
    print()
    print("Full workflow from API to synthetic genome:")
    print("""
from kbutillib import BVBRCUtils
import json

# Initialize
util = BVBRCUtils()

# Step 1: Fetch genomes from BV-BRC API
print("Fetching genomes from BV-BRC API...")
genome_ids = ['511145.183', '511145.184', '511145.185']
genomes = []

for genome_id in genome_ids:
    genome = util.build_kbase_genome_from_api(genome_id)
    genomes.append(genome)

    # Save individual genome
    with open(f'{genome_id}_genome.json', 'w') as f:
        json.dump(genome, f, indent=2)
    print(f"  Saved {genome_id}")

# Step 2: Create synthetic genome
print("\\nCreating synthetic genome...")
synthetic = util.create_synthetic_genome(
    asv_id='Ecoli_K12_Synthetic',
    genomes=genomes,
    save_taxonomy=True,
    taxonomy_output_dir='taxonomies'
)

# Step 3: Save synthetic genome
with open('ecoli_synthetic.json', 'w') as f:
    json.dump(synthetic, f, indent=2)

print(f"\\nSynthetic genome created:")
print(f"  ID: {synthetic['id']}")
print(f"  Features: {len(synthetic['features'])}")
print(f"  CDS: {len(synthetic['cdss'])}")
print(f"  DNA size: {synthetic['dna_size']:,} bp")
print(f"  Taxonomy: {synthetic['taxonomy']}")
    """)
    print()

    print("=" * 80)
    print()
    print("KEY FEATURES:")
    print("-" * 80)
    print("API Access:")
    print("  - Automatic pagination for large feature sets")
    print("  - Batch fetching of feature sequences")
    print("  - SSL verification configurable")
    print()
    print("Local File Support:")
    print("  - Three-file structure (metadata, features, sequences)")
    print("  - FASTA parsing for contig sequences")
    print("  - Automatic GC content calculation")
    print()
    print("Synthetic Genomes:")
    print("  - Function-based merging (unique functions)")
    print("  - Probability tracking across source genomes")
    print("  - Consensus taxonomy calculation")
    print("  - Automatic feature ID generation")
    print()
    print("KBase Format:")
    print("  - Complete KBase Genome object structure")
    print("  - Proper feature relationships (genes, CDS)")
    print("  - MD5 hashing for genome and proteins")
    print("  - Taxonomy and domain determination")
    print()

    print("=" * 80)
    print()
    print("CUSTOMIZATION:")
    print("-" * 80)
    print("""
# Custom BV-BRC API endpoint
util = BVBRCUtils(
    base_url='https://custom-bvbrc-api.org/api',
    verify_ssl=True
)

# Access underlying session for advanced use
util.session.headers.update({'Custom-Header': 'value'})
    """)
    print()

    print("=" * 80)


if __name__ == '__main__':
    main()
