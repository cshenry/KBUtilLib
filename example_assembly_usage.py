#!/usr/bin/env python
"""Example script demonstrating Assembly upload and download functionality.

This script shows how to use the new Assembly utilities in kb_reads_utils.py
to upload and download KBase Assembly and AssemblySet objects.
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from kbutillib import KBReadsUtils


def main():
    """Demonstrate Assembly upload and download functionality."""

    print("=" * 80)
    print("KBUtilLib Assembly Upload & Download Examples")
    print("=" * 80)
    print()

    print("SETUP:")
    print("-" * 80)
    print("""
from kbutillib import KBReadsUtils

# Initialize with your KBase token
util = KBReadsUtils(
    token="your_kbase_token",
    workspace="your_workspace"
)
    """)
    print()

    print("=" * 80)
    print()
    print("1. DOWNLOAD ASSEMBLIES:")
    print("-" * 80)
    print()
    print("Download single Assembly or AssemblySet objects:")
    print("""
# Download one or more assemblies
assemblies = util.download_assembly(
    assembly_refs=[
        "12345/6/1",  # Single Assembly reference
        "12345/7/1"   # Another Assembly reference
    ],
    output_dir="./downloaded_assemblies"
)

# This creates:
# ./downloaded_assemblies/
# ├── assemblies_metadata.json  # JSON with all assembly metadata
# ├── assembly1.fasta          # FASTA file for first assembly
# └── assembly2.fasta          # FASTA file for second assembly

# Access downloaded assemblies
for assembly_name, assembly in assemblies.assemblies.items():
    print(f"Assembly: {assembly_name}")
    print(f"  FASTA: {assembly.fasta_file}")
    print(f"  Contigs: {assembly.num_contigs}")
    print(f"  Size: {assembly.dna_size} bp")
    print(f"  GC%: {assembly.gc_content:.2f}")
    """)
    print()

    print("Download an AssemblySet (downloads all assemblies in the set):")
    print("""
# Download an AssemblySet - automatically expands to all assemblies
assemblies = util.download_assembly(
    assembly_refs=["12345/8/1"],  # AssemblySet reference
    output_dir="./assemblyset_download"
)

# All assemblies from the set are downloaded
print(f"Downloaded {len(assemblies.assemblies)} assemblies from set")
    """)
    print()

    print("=" * 80)
    print()
    print("2. UPLOAD ASSEMBLIES:")
    print("-" * 80)
    print()
    print("Upload individual FASTA files:")
    print("""
# Upload specific FASTA files
result = util.upload_assembly(
    input_paths=[
        "./genomes/genome1.fasta",
        "./genomes/genome2.fasta"
    ],
    workspace_name="your_workspace"
)

print(f"Uploaded assemblies: {result['assemblies']}")
# Output: ['12345/10/1', '12345/11/1']
    """)
    print()

    print("Upload all FASTA files from a directory:")
    print("""
# Upload all FASTA files in a directory
# Automatically finds all .fasta, .fa, and .fna files
result = util.upload_assembly(
    input_paths=["./genomes/"],
    workspace_name="your_workspace"
)

print(f"Uploaded {len(result['assemblies'])} assemblies")
    """)
    print()

    print("Upload with custom assembly IDs:")
    print("""
# Map filenames to desired workspace IDs
result = util.upload_assembly(
    input_paths=["./genomes/"],
    workspace_name="your_workspace",
    assembly_id_map={
        "genome1.fasta": "EcoliK12",
        "genome2.fasta": "PseudomonasPA14"
    }
)

# Other files use filename (without extension) as ID
    """)
    print()

    print("Upload and create an AssemblySet:")
    print("""
# Upload assemblies and group them in an AssemblySet
result = util.upload_assembly(
    input_paths=[
        "./genomes/ecoli1.fasta",
        "./genomes/ecoli2.fasta",
        "./genomes/ecoli3.fasta"
    ],
    workspace_name="your_workspace",
    assemblyset_id="EcoliStrains",
    taxon_ref="1234/5/6"  # Optional taxon reference
)

print(f"Uploaded assemblies: {result['assemblies']}")
print(f"Created AssemblySet: {result['assemblyset_ref']}")
# Output:
# Uploaded assemblies: ['12345/10/1', '12345/11/1', '12345/12/1']
# Created AssemblySet: 12345/13/1
    """)
    print()

    print("=" * 80)
    print()
    print("3. COMPLETE WORKFLOW EXAMPLE:")
    print("-" * 80)
    print()
    print("Upload local genomes, create a set, download and verify:")
    print("""
from kbutillib import KBReadsUtils

# Initialize
util = KBReadsUtils(token="your_token", workspace="MyWorkspace")

# Step 1: Upload all genomes from a directory
print("Uploading assemblies...")
upload_result = util.upload_assembly(
    input_paths=["./my_genomes/"],
    assemblyset_id="MyGenomeCollection",
    assembly_type="Isolate",
    taxon_ref="1234/5/6"
)

print(f"Uploaded {len(upload_result['assemblies'])} assemblies")
print(f"Created set: {upload_result['assemblyset_ref']}")

# Step 2: Download the AssemblySet to verify
print("\\nDownloading for verification...")
assemblies = util.download_assembly(
    assembly_refs=[upload_result['assemblyset_ref']],
    output_dir="./verification"
)

# Step 3: Verify assemblies
print("\\nVerifying downloaded assemblies:")
for name, assembly in assemblies.assemblies.items():
    print(f"  {name}: {assembly.num_contigs} contigs, "
          f"{assembly.dna_size:,} bp, "
          f"GC={assembly.gc_content:.2f}%")
    """)
    print()

    print("=" * 80)
    print()
    print("4. ASSEMBLY AND ASSEMBLYSET CLASSES:")
    print("-" * 80)
    print()
    print("Work with Assembly objects:")
    print("""
from kbutillib.kb_reads_utils import Assembly, AssemblySet

# Create Assembly object
assembly = Assembly(
    name="MyGenome",
    fasta_file="./genome.fasta",
    metadata={
        "num_contigs": 150,
        "dna_size": 5000000,
        "gc_content": 52.3,
        "type": "Isolate"
    }
)

# Save to JSON
assembly.to_json("assembly_metadata.json")

# Load from JSON
loaded = Assembly.from_json("assembly_metadata.json")
    """)
    print()

    print("Work with AssemblySet collections:")
    print("""
from kbutillib.kb_reads_utils import AssemblySet, Assembly

# Create AssemblySet
assemblyset = AssemblySet(
    name="EcoliStrains",
    description="Collection of E. coli isolates"
)

# Add assemblies
assemblyset.add_assembly(assembly1)
assemblyset.add_assembly(assembly2)

# List assemblies
for name in assemblyset.list_assemblies():
    assembly = assemblyset.get_assembly(name)
    print(f"{name}: {assembly.dna_size} bp")

# Save to JSON
assemblyset.to_json("assemblyset.json")

# Load from JSON
loaded_set = AssemblySet.from_json("assemblyset.json")
    """)
    print()

    print("=" * 80)
    print()
    print("KEY FEATURES:")
    print("-" * 80)
    print("Download:")
    print("  - Download single Assembly or AssemblySet objects")
    print("  - Automatic expansion of AssemblySet to individual assemblies")
    print("  - FASTA files + JSON metadata for all assemblies")
    print("  - Preserves all assembly metadata (contigs, size, GC%, etc.)")
    print()
    print("Upload:")
    print("  - Upload individual files or entire directories")
    print("  - Automatic scanning for FASTA files (.fasta, .fa, .fna)")
    print("  - Custom assembly ID mapping")
    print("  - Optional AssemblySet creation for grouping")
    print("  - Automatic calculation of assembly statistics")
    print("  - Shock storage integration with handle management")
    print()
    print("Classes:")
    print("  - Assembly: Represents single genome assembly")
    print("  - AssemblySet: Manages collections of assemblies")
    print("  - Both support JSON serialization/deserialization")
    print()

    print("=" * 80)
    print()
    print("SUPPORTED FASTA EXTENSIONS:")
    print("-" * 80)
    print("  - .fasta")
    print("  - .fa")
    print("  - .fna")
    print()

    print("=" * 80)


if __name__ == '__main__':
    main()
