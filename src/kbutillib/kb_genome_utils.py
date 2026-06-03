"""KBase genome utilities for working with genomic data and annotations."""

import hashlib
import logging
import time
import warnings
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from os.path import exists
import json

from .base_utils import BaseUtils
from .kb_ws_utils import KBWSUtils

logger = logging.getLogger(__name__)

genetic_code_standard = {
    "TTT": "F",
    "TTC": "F",
    "TTA": "L",
    "TTG": "L",
    "TCT": "S",
    "TCC": "S",
    "TCA": "S",
    "TCG": "S",
    "TAT": "Y",
    "TAC": "Y",
    "TAA": "*",
    "TAG": "*",
    "TGT": "C",
    "TGC": "C",
    "TGA": "*",
    "TGG": "W",
    "CTT": "L",
    "CTC": "L",
    "CTA": "L",
    "CTG": "L",
    "CCT": "P",
    "CCC": "P",
    "CCA": "P",
    "CCG": "P",
    "CAT": "H",
    "CAC": "H",
    "CAA": "Q",
    "CAG": "Q",
    "CGT": "R",
    "CGC": "R",
    "CGA": "R",
    "CGG": "R",
    "ATT": "I",
    "ATC": "I",
    "ATA": "I",
    "ATG": "M",
    "ACT": "T",
    "ACC": "T",
    "ACA": "T",
    "ACG": "T",
    "AAT": "N",
    "AAC": "N",
    "AAA": "K",
    "AAG": "K",
    "AGT": "S",
    "AGC": "S",
    "AGA": "R",
    "AGG": "R",
    "GTT": "V",
    "GTC": "V",
    "GTA": "V",
    "GTG": "V",
    "GCT": "A",
    "GCC": "A",
    "GCA": "A",
    "GCG": "A",
    "GAT": "D",
    "GAC": "D",
    "GAA": "E",
    "GAG": "E",
    "GGT": "G",
    "GGC": "G",
    "GGA": "G",
    "GGG": "G",
}

class KBGenomeUtils(KBWSUtils):
    """Utilities for working with KBase objects that contain features and annotations.

    Provides methods for feature manipulation, feature extraction, sequence
    analysis, and other genome-specific operations in the KBase environment.
    Works on Genomes, ProtSeqSets, DNASeqSets, Metagenomes, FeatureSets.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize KBase genome utilities.

        Args:
            **kwargs: Additional keyword arguments passed to BaseUtils
        """
        super().__init__(**kwargs)
        self.genetic_code = genetic_code_standard
        self.object_hash = {}

    #######################Generic API for interfaceing with feature containing KBase objects###########################
    def _check_for_object(self,name):
        """Checks if object exists in API cache"""
        if name not in self.object_hash:
            self.log_critical(f"Object {name} not loaded in cache. Load object first!")
            return False
        return True

    def load_kbase_gene_container(self,id_or_ref_or_filename,ws=None,localname=None):
        """Loads an object from KBase JSON into a local cache with key localname"""
        if localname is None:
            localname = id_or_ref_or_filename
        if exists(id_or_ref_or_filename):
            with open(id_or_ref_or_filename) as f:
                self.object_hash[localname] = {"type":"file","data":json.load(f)}
        else:
            self.object_hash[localname] = self.get_object(id_or_ref_or_filename, ws)
            self.object_hash[localname]["type"] = "ws"
        return self.object_hash[localname]

    def object_to_features(self, name):
        """Extracts features from a loaded KBase object."""
        if not self._check_for_object(name): return []
        if "_featurelist" not in self.object_hash[name]:
            self.object_hash[name]["_featurelist"] = []
            if "features" in self.object_hash[name].get("data", {}):
                self.object_hash[name]["_featurelist"].extend(self.object_hash[name]["data"]["features"])
            if "cdss" in self.object_hash[name].get("data", {}):
                self.object_hash[name]["_featurelist"].extend(self.object_hash[name]["data"]["cdss"])
            if "mrnas" in self.object_hash[name].get("data", {}):
                self.object_hash[name]["_featurelist"].extend(self.object_hash[name]["data"]["mrnas"])
            if "non_coding_features" in self.object_hash[name].get("data", {}):
                self.object_hash[name]["_featurelist"].extend(self.object_hash[name]["data"]["non_coding_features"])
        return self.object_hash[name]["_featurelist"]

    def get_ftr(self, name,ftrid):
        """Returns a feature object from the loaded KBase object."""
        if not self._check_for_object(name):return None
        if "_feature_hash" not in self.object_hash[name]:
            self.object_hash[name]["_feature_hash"] = {}
            ftrs = self.object_to_features(name)
            for ftr in ftrs:
                self.object_hash[name]["_feature_hash"][ftr["id"]] = ftr
        return self.object_hash[name]["_feature_hash"].get(ftrid,None)

    def ftr_to_aliases(self,name,ftrid):
        """Returns a hash of feature ID with aliases as values"""
        if not self._check_for_object(name):return {}
        ftr = self.get_ftr(name,ftrid)
        if ftr is None:
            self.log_warning(f"Feature {ftrid} not found in object {name}.")
            return {}
        if "_aliases" not in ftr:
            ftr["_aliases"] = {}
            if "aliases" in ftr:
                for alias in ftr.get("aliases", []):
                    ftr["_aliases"][alias[1]] = alias[0]
            if "db_xrefs" in ftr:
                for alias in ftr.get("db_xrefs", []):
                    ftr["_aliases"][alias[1]] = alias[0]
            if "md5" in ftr:
                ftr["_aliases"][ftr["md5"]] = "md5"
            if "protein_md5" in ftr:
                ftr["_aliases"][ftr["protein_md5"]] = "protein_md5"
        return ftr["_aliases"]

    def alias_to_ftrs(self,name, alias):
        """Returns list of features that match the input alias"""
        if not self._check_for_object(name): return []
        if "_alias_to_ftr_hash" not in self.object_hash[name]:
            self.object_hash[name]["_alias_to_ftr_hash"] = {}
            ftrs = self.object_to_features(name)
            for ftr in ftrs:
                aliases = self.ftr_to_aliases(name,ftr["id"])
                for alias in aliases:
                    self.object_hash[name]["_alias_to_ftr_hash"].setdefault(alias, []).append(ftr["id"])
        return self.object_hash[name]["_alias_to_ftr_hash"].get(alias, [])

    #######################Utility functions to make programming apps against KBase datatypes easier###########################
    def object_to_proteins(self, ref):
        output = self.get_object(ref, self.ws_id)
        self.object_info_hash[ref] = output["info"]
        sequence_list = []
        # TODO: add support for other object types
        if "features" in output["data"]:
            for ftr in output["data"]["features"]:
                if "protein_translation" in ftr:
                    if len(ftr["protein_translation"]) > 0:
                        sequence_list.append([ftr["id"], ftr["protein_translation"]])
        return sequence_list

    def add_annotations_to_object(self, reference, suffix, annotations):
        """Loads specified gene annotation into KBase genome object

        Parameters
        ----------
        string - genome_ref
            KBase workspace reference to genome where annotations should be saved
        string - suffix
            Suffix to be used when saving modified genome back to KBase
        mapping<string gene_id,mapping<string ontology,mapping<string term,{"type":string,"score":float}>>> - annotations
            Annotations to be saved to genome

        Returns:
        -------
        dict

        Raises:
        ------
        """
        ontology_inputs = {}
        for geneid in annotations:
            for ontology in annotations[geneid]:
                if ontology not in ontology_inputs:
                    ontology_inputs[ontology] = {}
                if geneid not in ontology_inputs[ontology]:
                    ontology_inputs[ontology][geneid] = []
                for term in annotations[geneid][ontology]:
                    anno_data = {"term": term}
                    if "scores" in annotations[geneid][ontology][term]:
                        anno_data["evidence"] = {
                            "scores": annotations[geneid][ontology][term]["scores"]
                        }
                    if "name" in annotations[geneid][ontology][term]:
                        anno_data["name"] = (
                            annotations[geneid][ontology][term]["name"] + suffix
                        )
                    ontology_inputs[ontology][geneid].append(anno_data)

        anno_api_input = {
            "input_ref": reference,
            "output_name": self.object_info_hash[reference][1] + suffix,
            "output_workspace": self.ws_id,
            "overwrite_matching": 1,
            "save": 1,
            "provenance": self.provenance(),
            "events": [],
        }
        for ontology in ontology_inputs.keys():
            anno_api_input["events"].append(
                {
                    "ontology_id": ontology,
                    "method": self.name + "." + self.method,
                    "method_version": self.version,
                    "timestamp": self.timestamp,
                    "ontology_terms": ontology_inputs[ontology],
                }
            )
        anno_api_output = self.anno_client().add_annotation_ontology_events(
            anno_api_input
        )
        self.obj_created.append(
            {
                "ref": anno_api_output["output_ref"],
                "description": "Saving annotation for "
                + self.object_info_hash[reference][1],
            }
        )
        return anno_api_output

    ###########################################General Genomics-oriented Utility Functions############################
    def reverse_complement(self, dna_sequence: str) -> str:
        """Get the reverse complement of a DNA sequence.

        Args:
            dna_sequence: DNA sequence string

        Returns:
            Reverse complement sequence
        """
        complement_map = {"A": "T", "T": "A", "G": "C", "C": "G", "N": "N"}

        # Handle lowercase and other characters
        complement_map.update({k.lower(): v.lower() for k, v in complement_map.items()})

        complement = "".join(complement_map.get(base, base) for base in dna_sequence)
        return complement[::-1]  # Reverse the string

    def translate_sequence(
        self, dna_sequence: str, genetic_code: Optional[int] = None
    ) -> str:
        """Translate a DNA sequence to amino acids.

        Args:
            dna_sequence: DNA sequence to translate
            genetic_code: Genetic code table number (defaults to standard code)

        Returns:
            Translated amino acid sequence
        """
        # Use standard genetic code for now (could be extended for other codes)
        if genetic_code and genetic_code != 11:
            self.log_warning(
                f"Genetic code {genetic_code} not implemented, using standard code"
            )

        # Ensure sequence length is multiple of 3
        seq_len = len(dna_sequence)
        if seq_len % 3 != 0:
            self.log_warning(
                f"Sequence length {seq_len} not divisible by 3, truncating"
            )
            dna_sequence = dna_sequence[: seq_len - (seq_len % 3)]

        # Translate codons
        amino_acids = []
        for i in range(0, len(dna_sequence), 3):
            codon = dna_sequence[i : i + 3].upper()
            amino_acid = self.genetic_code.get(codon, "X")  # 'X' for unknown
            amino_acids.append(amino_acid)

        return "".join(amino_acids)

    def calculate_gc_content(self, sequence: str) -> float:
        """Calculate GC content of a DNA sequence.

        Args:
            sequence: DNA sequence string

        Returns:
            GC content as a fraction (0.0 to 1.0)
        """
        if not sequence:
            return 0.0

        sequence = sequence.upper()
        gc_count = sequence.count("G") + sequence.count("C")
        total_bases = len([base for base in sequence if base in "ATGC"])

        return gc_count / total_bases if total_bases > 0 else 0.0

    def _create_cds_features(self, features: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Create CDS features for protein-coding genes."""
        cdss = []

        for feature in features:
            if feature.get('protein_translation'):
                cds = feature.copy()
                cds['id'] = f"{feature['id']}_CDS_1"
                cds['type'] = 'CDS'
                cds['parent_gene'] = feature['id']
                feature['cdss'] = [cds['id']]
                cdss.append(cds)

        return cdss

    def load_genome_from_local_files(
        self,
        genome_id: str,
        features_dir: str = "features",
        genomes_dir: str = "genomes",
        metadata_dir: str = "genome_metadata",
        taxonomy: Optional[str] = None,
        scientific_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Load genome from local BV-BRC files.

        Reads from:
        - genome_metadata/{genome_id}.json - Taxonomy, GC content, genome stats
        - genomes/{genome_id}.fna - Genome sequences in FASTA format
        - features/{genome_id}.json - Feature metadata from BV-BRC API

        Args:
            genome_id: BV-BRC genome ID
            features_dir: Directory containing feature JSON files
            genomes_dir: Directory containing genome FASTA files
            metadata_dir: Directory containing genome metadata files
            taxonomy: Optional taxonomy string (overrides metadata)
            scientific_name: Optional scientific name (overrides metadata)

        Returns:
            KBase Genome object dictionary
        """
        self.log_info(f"Loading genome {genome_id} from local files")

        # Construct file paths
        metadata_file = Path(metadata_dir) / f"{genome_id}.json"
        features_file = Path(features_dir) / f"{genome_id}.json"
        genome_file = Path(genomes_dir) / f"{genome_id}.fna"

        # Load metadata
        metadata = {}
        if metadata_file.exists():
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)

        # Load features
        features_data = []
        if features_file.exists():
            with open(features_file, 'r') as f:
                features_data = json.load(f)

        # Load sequences
        sequences = {}
        if genome_file.exists():
            sequences = self._parse_fasta(str(genome_file))

        # Calculate contig information
        contig_ids = sorted(sequences.keys())
        contig_lengths = [len(sequences[cid]) for cid in contig_ids]
        total_dna_size = sum(contig_lengths)

        # Get GC content
        gc_content = 0.5
        if metadata.get('gc_content'):
            gc_content = float(metadata['gc_content']) / 100.0
        elif sequences:
            all_seq = ''.join(sequences.values()).upper()
            g_count = all_seq.count('G')
            c_count = all_seq.count('C')
            total = len(all_seq)
            if total > 0:
                gc_content = (g_count + c_count) / total

        # Calculate genome MD5
        genome_md5 = ''
        if sequences:
            sorted_seqs = [sequences[cid] for cid in contig_ids]
            genome_md5 = hashlib.md5(''.join(sorted_seqs).encode()).hexdigest()

        # Build taxonomy
        if not taxonomy and metadata:
            if 'taxon_lineage_names' in metadata and len(metadata['taxon_lineage_names']) > 1:
                taxonomy = '; '.join(metadata['taxon_lineage_names'][1:])

        # Get scientific name
        if not scientific_name and metadata:
            scientific_name = metadata.get('genome_name', genome_id)

        # Determine domain
        domain = 'Bacteria'
        if metadata:
            superkingdom = metadata.get('superkingdom', '').lower()
            if 'archaea' in superkingdom:
                domain = 'Archaea'
            elif 'eukaryot' in superkingdom:
                domain = 'Eukaryota'

        # Process features
        kbase_features = []
        non_coding_features = []
        feature_counts = defaultdict(int)

        for idx, feature in enumerate(features_data):
            kbase_feature = self._convert_local_feature(feature, idx, genome_id)

            if kbase_feature:
                feature_type = kbase_feature['type']
                feature_counts[feature_type] += 1

                if feature_type in ['CDS', 'gene', 'protein_encoding_gene']:
                    kbase_features.append(kbase_feature)
                else:
                    non_coding_features.append(kbase_feature)

        # Create CDS features
        cdss = self._create_cds_features(kbase_features)

        # Build genome object
        genome = {
            'id': genome_id,
            'scientific_name': scientific_name or genome_id,
            'domain': domain,
            'taxonomy': taxonomy or '',
            'genetic_code': 11,
            'dna_size': total_dna_size,
            'num_contigs': len(contig_ids),
            'contig_ids': contig_ids,
            'contig_lengths': contig_lengths,
            'gc_content': gc_content,
            'md5': genome_md5,
            'molecule_type': 'DNA',
            'source': 'PATRIC',
            'source_id': genome_id,
            'assembly_ref': '',
            'external_source_origination_date': metadata.get('completion_date',
                                                             datetime.now().isoformat()),
            'notes': f'Imported from local BV-BRC files on {datetime.now().isoformat()}',
            'features': kbase_features,
            'non_coding_features': non_coding_features,
            'cdss': cdss,
            'mrnas': [],
            'feature_counts': dict(feature_counts),
            'publications': [],
            'genome_tiers': ['User'],
            'warnings': [],
            'taxon_ref': '',
        }

        self.log_info(f"Loaded genome: {len(kbase_features)} features, {total_dna_size:,} bp")

        return genome

    def _convert_local_feature(
        self,
        feature: Dict[str, Any],
        index: int,
        genome_id: str
    ) -> Optional[Dict[str, Any]]:
        """Convert local BV-BRC feature to KBase format."""
        patric_id = feature.get('patric_id', '')
        product = feature.get('product', '')
        feature_type = feature.get('feature_type', 'gene')

        # Build functions
        functions = []
        if product:
            functions.append(product)

        # Build aliases
        aliases = [['PATRIC_id', patric_id]]
        for family_type, ont_key in [('figfam_id', 'FIGFAM'),
                                      ('pgfam_id', 'PGFAM'),
                                      ('plfam_id', 'PLFAM')]:
            family_id = feature.get(family_type, '')
            if family_id:
                aliases.append([ont_key, family_id])

        feature_id = f"{genome_id}_{index}"

        return {
            'id': feature_id,
            'type': feature_type,
            'location': [],
            'functions': functions,
            'aliases': aliases,
            'dna_sequence': '',
            'dna_sequence_length': 0,
            'md5': '',
        }

    def _parse_fasta(self, fasta_file: str) -> Dict[str, str]:
        """Parse FASTA file and return dict of sequence_id -> sequence."""
        sequences = {}
        current_id = None
        current_seq = []

        with open(fasta_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('>'):
                    if current_id:
                        sequences[current_id] = ''.join(current_seq)
                    current_id = line[1:].split()[0]
                    current_seq = []
                else:
                    current_seq.append(line)

            if current_id:
                sequences[current_id] = ''.join(current_seq)

        return sequences

    def aggregate_taxonomies(
        self,
        genomes: List[Dict[str, Any]],
        asv_id: str,
        output_dir: Optional[str] = None
    ) -> Tuple[str, Dict[str, List[str]]]:
        """Aggregate taxonomies from multiple genomes.

        Args:
            genomes: List of genome dictionaries
            asv_id: Identifier for the ASV/synthetic genome
            output_dir: Optional directory to save taxonomy JSON

        Returns:
            Tuple of (consensus_taxonomy_string, taxonomy_dict)
        """
        tax_levels = ["Kingdom", "Phylum", "Class", "Order", "Family", "Genus", "Species"]

        # Collect all taxonomies
        all_taxonomies = []
        for genome in genomes:
            taxonomy = genome.get('taxonomy', '')
            if taxonomy:
                all_taxonomies.append(taxonomy)

        if not all_taxonomies:
            self.log_warning("No taxonomies found in source genomes")
            return "Unknown", {}

        # Parse taxonomies into levels
        taxonomy_by_level = {level: [] for level in tax_levels}

        for taxonomy_str in all_taxonomies:
            parts = [p.strip() for p in taxonomy_str.replace(';', '|').split('|')]
            for i, part in enumerate(parts):
                if i < len(tax_levels) and part:
                    taxonomy_by_level[tax_levels[i]].append(part)

        # Find most common taxonomy at each level
        consensus_taxonomy = []
        for level in tax_levels:
            if taxonomy_by_level[level]:
                counts = Counter(taxonomy_by_level[level])
                most_common = counts.most_common(1)[0][0]
                consensus_taxonomy.append(most_common)
            else:
                break

        consensus_str = "; ".join(consensus_taxonomy)

        output_dict = {
            level: taxonomy_by_level[level]
            for level in tax_levels
            if taxonomy_by_level[level]
        }

        # Save to JSON if output_dir provided
        if output_dir:
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            output_file = output_path / f"{asv_id}.json"
            with open(output_file, 'w') as f:
                json.dump(output_dict, f, indent=2)
            self.log_info(f"Taxonomy saved to {output_file}")

        return consensus_str, output_dict

    def create_synthetic_genome(
        self,
        asv_id: str,
        genomes: List[Dict[str, Any]],
        taxonomy: Optional[str] = None,
        save_taxonomy: bool = True,
        taxonomy_output_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create synthetic genome from multiple source genomes.

        Args:
            asv_id: Identifier for the synthetic genome
            genomes: List of genome dictionaries to merge
            taxonomy: Optional taxonomy string (if None, uses consensus from genomes)
            save_taxonomy: Whether to save taxonomy aggregation
            taxonomy_output_dir: Directory to save taxonomy JSON

        Returns:
            KBase Genome object dictionary
        """
        self.log_info(f"Creating synthetic genome: {asv_id} from {len(genomes)} source genomes")

        if not genomes:
            raise ValueError("No source genomes provided")

        # Aggregate taxonomies
        consensus_taxonomy = taxonomy
        if save_taxonomy or not taxonomy:
            consensus_taxonomy, _ = self.aggregate_taxonomies(
                genomes, asv_id, taxonomy_output_dir
            )
            if not taxonomy:
                taxonomy = consensus_taxonomy

        # Calculate average GC content
        gc_contents = [float(g.get('gc_content', 0.5)) for g in genomes if 'gc_content' in g]
        avg_gc = sum(gc_contents) / len(gc_contents) if gc_contents else 0.5

        # Determine domain
        domain = 'Bacteria'
        if taxonomy:
            first_level = taxonomy.split(';')[0].strip().lower()
            if 'archaea' in first_level:
                domain = 'Archaea'
            elif 'eukaryot' in first_level:
                domain = 'Eukaryota'

        # Initialize synthetic genome
        synthetic_genome = {
            'id': asv_id,
            'scientific_name': taxonomy.split(';')[-1].strip() if taxonomy else asv_id,
            'taxonomy': taxonomy or '',
            'domain': domain,
            'genetic_code': 11,
            'dna_size': 0,
            'num_contigs': 0,
            'contig_ids': [],
            'contig_lengths': [],
            'gc_content': avg_gc,
            'md5': '',
            'molecule_type': 'DNA',
            'source': 'Synthetic',
            'source_id': '|'.join([g.get('id', '') for g in genomes]),
            'assembly_ref': '',
            'features': [],
            'non_coding_features': [],
            'cdss': [],
            'mrnas': [],
            'feature_counts': {},
            'publications': [],
            'genome_tiers': ['User'],
            'warnings': ['Synthetic genome created by merging multiple source genomes'],
            'taxon_ref': '',
        }

        # Track unique functions
        functions = {}
        features = {}
        md5_list = []

        # Process source genomes
        for genome_idx, source_genome in enumerate(genomes):
            genome_functions = {}

            for source_feature in source_genome.get('features', []):
                if 'functions' not in source_feature or not source_feature['functions']:
                    continue

                for function in source_feature['functions']:
                    if function not in functions:
                        # Create new feature
                        feature_id = f"{asv_id}_{len(synthetic_genome['contig_ids']) + 1}"

                        synthetic_genome['contig_ids'].append(f"{feature_id}.contig")
                        dna_length = len(source_feature.get('dna_sequence', ''))
                        synthetic_genome['contig_lengths'].append(dna_length)
                        synthetic_genome['num_contigs'] += 1
                        synthetic_genome['dna_size'] += dna_length

                        protein_seq = source_feature.get('protein_translation', '')
                        protein_md5 = hashlib.md5(protein_seq.encode()).hexdigest() if protein_seq else ''
                        if protein_md5:
                            md5_list.append(protein_md5)

                        functions[function] = {'feature_id': feature_id, 'probability': 1}

                        features[feature_id] = {
                            'id': feature_id,
                            'type': source_feature.get('type', 'gene'),
                            'aliases': source_feature.get('aliases', [])[:],
                            'cdss': [f"{feature_id}.CDS"],
                            'functions': [function],
                            'dna_sequence': source_feature.get('dna_sequence', ''),
                            'dna_sequence_length': dna_length,
                            'location': [[f"{feature_id}.contig", 1, "+", dna_length]],
                            'md5': hashlib.md5(source_feature.get('dna_sequence', '').encode()).hexdigest(),
                            'protein_md5': protein_md5,
                            'protein_translation': protein_seq,
                            'protein_translation_length': len(protein_seq),
                            'warnings': []
                        }

                        # Create CDS
                        cds_feature = features[feature_id].copy()
                        del cds_feature['cdss']
                        cds_feature['id'] = f"{feature_id}.CDS"
                        cds_feature['type'] = 'CDS'
                        cds_feature['parent_gene'] = feature_id
                        synthetic_genome['cdss'].append(cds_feature)
                        synthetic_genome['features'].append(features[feature_id])

                    elif function not in genome_functions:
                        functions[function]['probability'] += 1

                    genome_functions[function] = True

        # Normalize probabilities
        num_genomes = len(genomes)
        for function in functions:
            functions[function]['probability'] /= num_genomes

        # Update feature counts
        synthetic_genome['feature_counts'] = {
            'CDS': len(synthetic_genome['cdss']),
            'gene': len(synthetic_genome['features']),
            'protein_encoding_gene': len(synthetic_genome['features']),
        }

        # Calculate genome MD5
        md5_list.sort()
        genome_md5 = hashlib.md5(";".join(md5_list).encode()).hexdigest()
        synthetic_genome['md5'] = genome_md5

        self.log_info(f"Synthetic genome created: {len(synthetic_genome['features'])} features, "
                     f"{synthetic_genome['dna_size']:,} bp")

        return synthetic_genome

    # ── New save / validate / build methods ─────────────────────────────────

    def save_genome_object(self, genome_dict: Dict[str, Any], workspace, name: str) -> str:
        """Save a KBaseGenomes.Genome typed object directly to the Workspace.

        Uses the inherited ``save_ws_object`` transport (no EE2 job needed).

        Args:
            genome_dict: Complete KBase Genome object dict.
            workspace: Workspace ID (int) or name (str).
            name: Object name to save under.

        Returns:
            ``'ws_id/obj_id/version'`` reference string.
        """
        result = self.save_ws_object(name, workspace, genome_dict, "KBaseGenomes.Genome")
        # save_ws_object returns the list returned by save_objects; first element is
        # the object info: [obj_id, name, type, save_date, version, saved_by, ws_id, ...]
        info = result[0]
        return f"{info[6]}/{info[0]}/{info[4]}"

    def save_assembly_from_fasta(self, fasta_path, workspace, name: str, *, wait: bool = True, timeout: int = 600) -> str:
        """Save a FASTA file as a KBase Assembly via AssemblyUtil EE2 job.

        This method on the bare ``KBGenomeUtils`` legacy class is not
        supported.  The EE2 job path requires the composition facade which
        holds a ``KBJobUtils`` instance.

        Raises:
            RuntimeError: Always — directs the caller to use the facade.
        """
        raise RuntimeError(
            "save_assembly_from_fasta requires the composition-facade path; "
            "use kbu = KBUtilLib(); kbu.genome.save_assembly_from_fasta(...)"
        )

    def save_genome_with_assembly(
        self,
        fasta_path,
        genome_dict: Dict[str, Any],
        workspace,
        base_name: str,
        *,
        assembly_suffix: str = "_assembly",
    ) -> Tuple[str, str]:
        """Save FASTA as Assembly, splice assembly_ref, then save the Genome.

        Args:
            fasta_path: Path to the FASTA file.
            genome_dict: Genome object dict (not mutated — shallow copy is made).
            workspace: Workspace ID (int) or name (str).
            base_name: Base name; assembly saved as ``base_name + assembly_suffix``.
            assembly_suffix: Suffix appended to ``base_name`` for the assembly object.

        Returns:
            ``(assembly_ref, genome_ref)`` tuple of ``'ws_id/obj_id/version'`` strings.
        """
        assembly_ref = self.save_assembly_from_fasta(
            fasta_path, workspace, base_name + assembly_suffix
        )
        genome_dict = dict(genome_dict)  # shallow copy; don't mutate caller's input
        genome_dict["assembly_ref"] = assembly_ref
        genome_ref = self.save_genome_object(genome_dict, workspace, base_name)
        return assembly_ref, genome_ref

    def validate_genome(
        self, genome_dict: Dict[str, Any], *, require_assembly_ref: bool = True
    ) -> List[str]:
        """Validate a Genome dict against KBaseGenomes.Genome required fields.

        Schema-only validation: checks for required keys, types, and
        cross-field consistency.  Does NOT recompute MD5 or translate sequences.

        Args:
            genome_dict: The genome dict to validate.
            require_assembly_ref: When True (default), flags missing or empty
                ``assembly_ref``.  Set to False when validating a Genome dict
                built before the assembly has been saved (e.g., immediately
                after ``build_genome_from_fasta_gff``).

        Returns:
            List of human-readable error strings; empty list means valid.
        """
        errors: List[str] = []

        required_scalar_fields = [
            "id", "scientific_name", "domain", "molecule_type",
            "source", "source_id", "taxonomy",
        ]
        for field in required_scalar_fields:
            if not genome_dict.get(field):
                errors.append(f"Missing or empty required field: '{field}'")

        # Numeric fields
        if not isinstance(genome_dict.get("genetic_code"), int):
            errors.append("'genetic_code' must be an int")
        if not isinstance(genome_dict.get("dna_size"), int) or genome_dict.get("dna_size", 0) <= 0:
            errors.append("'dna_size' must be a positive int")
        if not isinstance(genome_dict.get("num_contigs"), int) or genome_dict.get("num_contigs", 0) <= 0:
            errors.append("'num_contigs' must be a positive int")

        gc = genome_dict.get("gc_content")
        if not isinstance(gc, (int, float)) or not (0.0 <= gc <= 1.0):
            errors.append("'gc_content' must be a float in [0, 1]")

        if not isinstance(genome_dict.get("md5"), str) or not genome_dict.get("md5"):
            errors.append("'md5' must be a non-empty str")

        # assembly_ref — conditionally required
        if require_assembly_ref:
            if not genome_dict.get("assembly_ref"):
                errors.append("'assembly_ref' must be a non-empty str (use require_assembly_ref=False for pre-assembly dicts)")

        # contig_ids / contig_lengths consistency
        contig_ids = genome_dict.get("contig_ids")
        contig_lengths = genome_dict.get("contig_lengths")
        if not isinstance(contig_ids, list):
            errors.append("'contig_ids' must be a list")
            contig_ids = []
        if not isinstance(contig_lengths, list):
            errors.append("'contig_lengths' must be a list")
            contig_lengths = []
        if isinstance(contig_ids, list) and isinstance(contig_lengths, list):
            if len(contig_ids) != len(contig_lengths):
                errors.append(
                    f"'contig_ids' length ({len(contig_ids)}) != "
                    f"'contig_lengths' length ({len(contig_lengths)})"
                )

        contig_id_set = set(contig_ids)

        # Feature list fields
        for list_field in ("features", "cdss", "mrnas", "non_coding_features"):
            if not isinstance(genome_dict.get(list_field), list):
                errors.append(f"'{list_field}' must be a list")

        if not isinstance(genome_dict.get("feature_counts"), dict):
            errors.append("'feature_counts' must be a dict")

        # Per-feature checks across all feature lists
        seen_ids: set = set()
        all_features = []
        for list_field in ("features", "cdss", "mrnas", "non_coding_features"):
            val = genome_dict.get(list_field, [])
            if isinstance(val, list):
                all_features.extend(val)

        for ftr in all_features:
            fid = ftr.get("id")
            if not isinstance(fid, str) or not fid:
                errors.append(f"Feature missing 'id': {ftr}")
                continue
            if fid in seen_ids:
                errors.append(f"Duplicate feature id: '{fid}'")
            seen_ids.add(fid)

            if not isinstance(ftr.get("type"), str):
                errors.append(f"Feature '{fid}' missing 'type'")

            location = ftr.get("location")
            if not isinstance(location, list):
                errors.append(f"Feature '{fid}' 'location' must be a list")
            else:
                for loc in location:
                    if not isinstance(loc, (list, tuple)) or len(loc) != 4:
                        errors.append(
                            f"Feature '{fid}' has malformed location tuple: {loc}"
                        )
                    elif contig_id_set and loc[0] not in contig_id_set:
                        errors.append(
                            f"Feature '{fid}' references unknown contig '{loc[0]}'"
                        )

        return errors

    def build_genome_from_fasta_gff(
        self,
        fasta_path,
        gff_path=None,
        *,
        scientific_name: str,
        taxonomy: str,
        genetic_code: int = 11,
        source: str = "User",
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a KBase Genome dict from a FASTA (and optional GFF3) file.

        Args:
            fasta_path: Path to the genome FASTA file.
            gff_path: Path to a GFF3 annotation file, or None for gene-free genome.
            scientific_name: Organism scientific name.
            taxonomy: Full taxonomy string (semicolon-separated lineage).
            genetic_code: NCBI genetic code table number (default 11, bacterial).
            source: Source label stored in the Genome object (default "User").
            source_id: Source identifier; defaults to stem of fasta_path if None.

        Returns:
            KBase Genome object dict (passes ``validate_genome(..., require_assembly_ref=False)``).
        """
        fasta_path = str(fasta_path)
        sequences = self._parse_fasta(fasta_path)

        contig_ids = sorted(sequences.keys())
        contig_lengths = [len(sequences[cid]) for cid in contig_ids]
        total_dna_size = sum(contig_lengths)

        # GC content
        all_seq = "".join(sequences[cid] for cid in contig_ids).upper()
        gc_count = all_seq.count("G") + all_seq.count("C")
        atgc_count = sum(1 for b in all_seq if b in "ATGC")
        gc_content = gc_count / atgc_count if atgc_count > 0 else 0.0

        # Genome-level MD5
        genome_md5 = hashlib.md5(
            "".join(sequences[cid] for cid in contig_ids).encode()
        ).hexdigest()

        # Derive domain from first taxonomy segment
        first_segment = taxonomy.split(";")[0].strip().lower()
        if "archaea" in first_segment:
            domain = "Archaea"
        elif "eukaryot" in first_segment:
            domain = "Eukaryota"
        else:
            domain = "Bacteria"

        if source_id is None:
            source_id = Path(fasta_path).stem

        features: List[Dict[str, Any]] = []
        cdss: List[Dict[str, Any]] = []
        mrnas: List[Dict[str, Any]] = []
        non_coding_features: List[Dict[str, Any]] = []

        if gff_path is not None:
            gff_path = str(gff_path)
            features, cdss, mrnas, non_coding_features = self._parse_gff(
                gff_path, sequences, contig_ids, genetic_code
            )

        feature_counts: Dict[str, int] = {}
        for ftr in features + cdss + mrnas + non_coding_features:
            ftype = ftr.get("type", "unknown")
            feature_counts[ftype] = feature_counts.get(ftype, 0) + 1

        genome: Dict[str, Any] = {
            "id": source_id,
            "scientific_name": scientific_name,
            "domain": domain,
            "taxonomy": taxonomy,
            "genetic_code": genetic_code,
            "dna_size": total_dna_size,
            "num_contigs": len(contig_ids),
            "contig_ids": contig_ids,
            "contig_lengths": contig_lengths,
            "gc_content": gc_content,
            "md5": genome_md5,
            "molecule_type": "DNA",
            "source": source,
            "source_id": source_id,
            "assembly_ref": "",
            "features": features,
            "cdss": cdss,
            "mrnas": mrnas,
            "non_coding_features": non_coding_features,
            "feature_counts": feature_counts,
        }

        return genome

    def _parse_gff(
        self,
        gff_path: str,
        sequences: Dict[str, str],
        contig_ids: List[str],
        genetic_code: int,
    ) -> Tuple[List, List, List, List]:
        """Parse a GFF3 file into KBase feature lists.

        Returns:
            (features, cdss, mrnas, non_coding_features) tuple.
        """
        contig_id_set = set(contig_ids)
        features: List[Dict[str, Any]] = []
        cdss: List[Dict[str, Any]] = []
        mrnas: List[Dict[str, Any]] = []
        non_coding_features: List[Dict[str, Any]] = []

        # Track gene objects by GFF ID for CDS->gene linking
        gene_by_id: Dict[str, Dict[str, Any]] = {}

        # Counter per type for synthesized IDs
        type_counters: Dict[str, int] = defaultdict(int)
        used_ids: Dict[str, int] = {}  # id -> count for deduplication

        def _unique_id(base_id: str) -> str:
            if base_id not in used_ids:
                used_ids[base_id] = 0
                return base_id
            used_ids[base_id] += 1
            return f"{base_id}_{used_ids[base_id] + 1}"

        def _parse_attrs(attr_str: str) -> Dict[str, str]:
            attrs: Dict[str, str] = {}
            for part in attr_str.strip().split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip()] = v.strip()
            return attrs

        def _get_dna(seqid: str, start_1based: int, length: int, strand: str) -> str:
            seq = sequences.get(seqid, "")
            s = start_1based - 1
            e = s + length
            sub = seq[s:e]
            if strand == "-":
                sub = self.reverse_complement(sub)
            return sub.upper()

        handled_types = {"CDS", "gene", "tRNA", "rRNA", "ncRNA", "mRNA"}

        with open(gff_path, "r") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 8:
                    continue
                seqid, source_col, gff_type, start_str, end_str, score, strand, phase = parts[:8]
                attr_str = parts[8] if len(parts) > 8 else ""

                if gff_type not in handled_types:
                    continue

                if seqid not in contig_id_set:
                    logger.warning(
                        "_parse_gff: feature on unknown contig '%s' (skipping)", seqid
                    )
                    continue

                start = int(start_str)  # 1-based inclusive (GFF convention)
                end = int(end_str)      # 1-based inclusive
                length = abs(end - start) + 1
                strand = strand if strand in ("+", "-") else "+"

                attrs = _parse_attrs(attr_str)
                gff_id = attrs.get("ID") or attrs.get("locus_tag") or attrs.get("Name")
                if not gff_id:
                    type_counters[gff_type] += 1
                    gff_id = f"{seqid}_{gff_type}_{type_counters[gff_type]}"

                ftr_id = _unique_id(gff_id)

                product = attrs.get("product", "")
                functions = [product] if product else []

                aliases = []
                dbxref = attrs.get("Dbxref", "")
                if dbxref:
                    for entry in dbxref.split(","):
                        entry = entry.strip()
                        if ":" in entry:
                            db, val = entry.split(":", 1)
                            aliases.append([db, val])
                locus_tag = attrs.get("locus_tag", "")
                if locus_tag and locus_tag != ftr_id:
                    aliases.append(["locus_tag", locus_tag])

                location = [[seqid, start, strand, length]]
                dna_seq = _get_dna(seqid, start, length, strand)
                dna_len = len(dna_seq)
                ftr_md5 = hashlib.md5(dna_seq.encode()).hexdigest()

                base_ftr: Dict[str, Any] = {
                    "id": ftr_id,
                    "type": gff_type,
                    "location": location,
                    "functions": functions,
                    "aliases": aliases,
                    "dna_sequence": dna_seq,
                    "dna_sequence_length": dna_len,
                    "md5": ftr_md5,
                }

                if gff_type == "gene":
                    base_ftr["cdss"] = []
                    features.append(base_ftr)
                    gene_by_id[gff_id] = base_ftr

                elif gff_type == "CDS":
                    protein = self.translate_sequence(dna_seq, genetic_code)
                    # Trim trailing stop codon
                    if protein.endswith("*"):
                        protein = protein[:-1]
                    protein_md5 = hashlib.md5(protein.encode()).hexdigest()
                    base_ftr["type"] = "CDS"
                    base_ftr["protein_translation"] = protein
                    base_ftr["protein_translation_length"] = len(protein)
                    base_ftr["protein_md5"] = protein_md5
                    # Link to parent gene
                    parent_gff_id = attrs.get("Parent", "")
                    if parent_gff_id and parent_gff_id in gene_by_id:
                        base_ftr["parent_gene"] = gene_by_id[parent_gff_id]["id"]
                        gene_by_id[parent_gff_id]["cdss"].append(ftr_id)
                    cdss.append(base_ftr)

                elif gff_type == "mRNA":
                    parent_gff_id = attrs.get("Parent", "")
                    if parent_gff_id and parent_gff_id in gene_by_id:
                        base_ftr["parent_gene"] = gene_by_id[parent_gff_id]["id"]
                    mrnas.append(base_ftr)

                else:
                    # tRNA, rRNA, ncRNA -> non_coding_features
                    non_coding_features.append(base_ftr)

        return features, cdss, mrnas, non_coding_features


# ── Composition-based implementation ─────────────────────────────────────

class KBGenomeUtilsImpl:
    """Composition-based genome utilities.

    Holds ``env``, ``ws``, and ``jobs`` instead of inheriting from ``KBWSUtils``.
    Delegates all method calls to an internal legacy instance via ``__getattr__``.

    The ``save_assembly_from_fasta`` method is explicitly overridden here (not
    on the legacy class) because it requires the ``KBJobUtils`` instance.
    """

    def __init__(self, env, ws, jobs, **kwargs):
        self._env = env
        self._ws = ws
        self._jobs = jobs
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        try:
            _kwargs["token"] = env.get_token("kbase")
        except Exception:
            pass
        _kwargs.update(kwargs)
        self._delegate = KBGenomeUtils(**_kwargs)

    @property
    def env(self):
        return self._env

    @property
    def ws(self):
        return self._ws

    @property
    def jobs(self):
        return self._jobs

    def save_assembly_from_fasta(
        self,
        fasta_path,
        workspace,
        name: str,
        *,
        wait: bool = True,
        timeout: int = 600,
    ) -> str:
        """Save a FASTA file as a KBase Assembly via AssemblyUtil EE2 job.

        Submits ``AssemblyUtil.save_assembly_from_fasta`` to EE2, optionally
        polls to terminal state, and returns the assembly ref.

        Args:
            fasta_path: Local path to the FASTA file.
            workspace: Workspace name (str) or ID (int).
            name: Name for the Assembly object in the workspace.
            wait: When True (default), poll until the job completes and return
                the assembly ref.  When False, return the EE2 job ID immediately.
            timeout: Maximum seconds to poll (default 600).

        Returns:
            ``'ws_id/obj_id/version'`` assembly ref string (when ``wait=True``),
            or the EE2 job ID string (when ``wait=False``).

        Raises:
            RuntimeError: If the job fails or times out.
        """
        from .kb_job_utils.state import JobState

        record = self._jobs.run_job(
            method="AssemblyUtil.save_assembly_from_fasta",
            params=[{
                "file": {"path": str(fasta_path)},
                "workspace_name": workspace,
                "assembly_name": name,
            }],
        )
        if not wait:
            return record.job_id

        # Poll to terminal state
        deadline = time.time() + timeout
        while True:
            record = self._jobs.check_job(record.job_id)
            if record.state.is_terminal:
                break
            if time.time() > deadline:
                raise RuntimeError(
                    f"save_assembly_from_fasta timed out after {timeout}s "
                    f"(job {record.job_id})"
                )
            time.sleep(5)

        if record.state != JobState.COMPLETED:
            raise RuntimeError(
                f"save_assembly_from_fasta job {record.job_id} ended with "
                f"state '{record.state}': {record.error_message}"
            )

        # AssemblyUtil.save_assembly_from_fasta returns {"assembly_ref": "<ws_id/obj_id/ver>"}
        # Verified against AssemblyUtil KIDL spec (AssemblyUtil.spec, 2026-06-02).
        raw = record.ee2_raw
        result = raw.get("result", [{}])
        if isinstance(result, list) and result:
            result = result[0]
        assembly_ref = result.get("assembly_ref")
        if not assembly_ref:
            raise RuntimeError(
                f"save_assembly_from_fasta job {record.job_id} result "
                f"did not contain 'assembly_ref'. Raw result: {result}"
            )
        return assembly_ref

    def save_genome_with_assembly(
        self,
        fasta_path,
        genome_dict: Dict[str, Any],
        workspace,
        base_name: str,
        *,
        assembly_suffix: str = "_assembly",
    ) -> Tuple[str, str]:
        """Save FASTA as Assembly (via EE2), splice assembly_ref, then save the Genome.

        Args:
            fasta_path: Path to the FASTA file.
            genome_dict: Genome object dict (not mutated — shallow copy is made).
            workspace: Workspace ID (int) or name (str).
            base_name: Base name; assembly saved as ``base_name + assembly_suffix``.
            assembly_suffix: Suffix appended to ``base_name`` for the assembly object.

        Returns:
            ``(assembly_ref, genome_ref)`` tuple of ``'ws_id/obj_id/version'`` strings.
        """
        assembly_ref = self.save_assembly_from_fasta(
            fasta_path, workspace, base_name + assembly_suffix
        )
        genome_dict = dict(genome_dict)  # shallow copy; don't mutate caller's input
        genome_dict["assembly_ref"] = assembly_ref
        genome_ref = self._delegate.save_genome_object(genome_dict, workspace, base_name)
        return assembly_ref, genome_ref

    def __getattr__(self, name):
        return getattr(self._delegate, name)
