"""KBase genome utilities for working with genomic data and annotations."""

from typing import Any, Dict, List, Optional, Tuple
from os.path import exists
import json

from .base_utils import BaseUtils
from .kb_ws_utils import KBWSUtils

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