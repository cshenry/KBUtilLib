"""BV-BRC API utilities for genome data fetching and conversion to KBase format.

This module provides utilities for:
- Fetching genome data from the BV-BRC (formerly PATRIC) API
- Converting BV-BRC genome data to KBase Genome object format
- Loading genomes from local BV-BRC files
- Creating synthetic genomes from multiple source genomes
"""

import os
import json
import hashlib
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict, Counter
from pathlib import Path

from .kb_genome_utils import KBGenomeUtils
from .kb_annotation_utils import KBAnnotationUtils

class BVBRCUtils(KBGenomeUtils,KBAnnotationUtils):
    """Utilities for working with BV-BRC (formerly PATRIC) genome data.

    Provides methods for fetching genome data from the BV-BRC API,
    converting to KBase format, and creating synthetic genomes.
    """

    def __init__(
        self,
        base_url: str = "https://www.patricbrc.org/api",
        verify_ssl: bool = False,
        **kwargs: Any
    ) -> None:
        """Initialize BV-BRC utilities.

        Args:
            base_url: Base URL for BV-BRC API (default: https://www.patricbrc.org/api)
            verify_ssl: Whether to verify SSL certificates (default: False)
            **kwargs: Additional arguments passed to BaseUtils
        """
        super().__init__(**kwargs)

        self.base_url = base_url
        self.session = requests.Session()
        self.session.verify = verify_ssl

        # Suppress SSL warnings if not verifying
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def fetch_genome_metadata(self, genome_id: str) -> Dict[str, Any]:
        """Fetch genome metadata from BV-BRC API.

        Args:
            genome_id: BV-BRC genome ID

        Returns:
            Genome metadata dictionary

        Raises:
            ValueError: If no genome found with the given ID
            requests.HTTPError: If API request fails
        """
        url = f"{self.base_url}/genome/?eq(genome_id,{genome_id})&http_accept=application/json"

        self.log_info(f"Fetching genome metadata for {genome_id}")
        response = self.session.get(url)
        response.raise_for_status()

        data = response.json()
        if not data:
            raise ValueError(f"No genome found with ID {genome_id}")

        return data[0]

    def fetch_genome_sequences(self, genome_id: str) -> List[Dict[str, Any]]:
        """Fetch genome sequences (contigs) from BV-BRC API.

        Args:
            genome_id: BV-BRC genome ID

        Returns:
            List of contig dictionaries
        """
        url = f"{self.base_url}/genome_sequence/?eq(genome_id,{genome_id})&http_accept=application/json"

        self.log_info(f"Fetching genome sequences for {genome_id}")
        response = self.session.get(url)
        response.raise_for_status()

        return response.json()

    def fetch_genome_features(self, genome_id: str) -> List[Dict[str, Any]]:
        """Fetch all genome features from BV-BRC API (paginated).

        Args:
            genome_id: BV-BRC genome ID

        Returns:
            List of feature dictionaries
        """
        features = []
        start = 0
        limit = 10000

        self.log_info(f"Fetching genome features for {genome_id}")
        while True:
            url = f"{self.base_url}/genome_feature/?eq(genome_id,{genome_id})&http_accept=application/json&limit({limit},{start})"

            response = self.session.get(url)
            response.raise_for_status()

            batch = response.json()
            if not batch:
                break

            features.extend(batch)
            self.log_debug(f"Retrieved {len(features)} features so far...")
            start += limit

            if len(batch) < limit:
                break

        self.log_info(f"Total features retrieved: {len(features)}")
        return features

    def fetch_feature_sequences(self, md5_hashes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch feature sequences by MD5 hash (batched).

        Args:
            md5_hashes: List of MD5 hashes

        Returns:
            Dictionary mapping MD5 hash to sequence data
        """
        sequences = {}
        batch_size = 100

        self.log_info(f"Fetching feature sequences for {len(md5_hashes)} unique sequences")

        for i in range(0, len(md5_hashes), batch_size):
            batch = md5_hashes[i:i+batch_size]
            md5_list = ",".join(batch)
            # Note: BV-BRC API has a default limit of 25, so we must specify limit >= batch_size
            url = f"{self.base_url}/feature_sequence/?in(md5,({md5_list}))&limit({batch_size})&http_accept=application/json"

            try:
                response = self.session.get(url)
                response.raise_for_status()

                for seq_data in response.json():
                    md5 = seq_data.get('md5')
                    seq_type = seq_data.get('sequence_type', '')
                    sequence = seq_data.get('sequence', '')

                    if md5:
                        if md5 not in sequences:
                            sequences[md5] = {}
                        sequences[md5][seq_type] = sequence

                self.log_debug(f"Retrieved {len(sequences)} sequences so far...")
            except Exception as e:
                self.log_warning(f"Failed to fetch batch: {e}")
                continue

        return sequences

    def build_kbase_genome_from_api(
        self,
        genome_id: str,
        workspace_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build complete KBase genome object from BV-BRC API.

        Args:
            genome_id: BV-BRC genome ID
            add_ontology_events: If True, add ontology annotations as events (requires KBAnnotationUtils)
            workspace_name: Workspace to save genome with ontology events (required if add_ontology_events=True)

        Returns:
            KBase Genome object dictionary (with ontology events if requested)
        """
        self.log_info(f"Building KBase genome object for {genome_id}")

        # Fetch all data from BV-BRC
        genome_meta = self.fetch_genome_metadata(genome_id)
        contigs = self.fetch_genome_sequences(genome_id)
        features_data = self.fetch_genome_features(genome_id)

        # Parse taxonomy
        taxonomy = genome_meta.get('taxon_lineage_names', [])
        taxonomy_str = "; ".join(taxonomy) if taxonomy else genome_meta.get('genome_name', '')

        # Determine domain
        domain = "Bacteria"
        if taxonomy:
            first_level = taxonomy[0].lower()
            if 'archaea' in first_level:
                domain = "Archaea"
            elif 'eukaryota' in first_level or 'eukarya' in first_level:
                domain = "Eukaryota"

        # Process contigs
        sorted_contigs = sorted(contigs, key=lambda x: x.get('accession', ''))
        contig_ids = []
        contig_lengths = []
        contig_sequences = []
        total_dna_size = 0

        for contig in sorted_contigs:
            contig_id = contig.get('accession', contig.get('sequence_id', ''))
            sequence = contig.get('sequence', '')
            length = len(sequence)

            contig_ids.append(contig_id)
            contig_lengths.append(length)
            contig_sequences.append(sequence)
            total_dna_size += length

        # Calculate genome MD5
        genome_md5 = hashlib.md5("".join(contig_sequences).encode()).hexdigest()

        # Create contig ID mapping
        contig_map = {c.get('sequence_id', ''): c.get('accession', c.get('sequence_id', ''))
                      for c in contigs}

        # Collect MD5 hashes for feature sequences
        md5_hashes = set()
        for feature in features_data:
            if feature.get('na_sequence_md5'):
                md5_hashes.add(feature['na_sequence_md5'])
            if feature.get('aa_sequence_md5'):
                md5_hashes.add(feature['aa_sequence_md5'])

        # Fetch feature sequences
        sequences = self.fetch_feature_sequences(list(md5_hashes))

        # Initialize ontology collection (similar to Perl implementation)
        ontologies = {
            'SSO': {},      # SEED Subsystem Ontology
            'RefSeq': {},   # RefSeq annotations
            'FIGFAM': {},   # FIG families
            'PGFAM': {},    # PATRIC genus families
            'PLFAM': {},    # PATRIC local families
            'GO': {}        # Gene Ontology
        }

        # Process features
        kbase_features = []
        non_coding_features = []
        feature_counts = defaultdict(int)

        self.log_info("Processing features...")
        for idx, feature in enumerate(features_data):
            kbase_feature = self._convert_bvbrc_feature(
                feature, idx, genome_id, contig_map, sequences, ontologies
            )

            if kbase_feature:
                feature_type = kbase_feature['type']
                feature_counts[feature_type] += 1

                if feature_type in ['CDS', 'gene', 'protein_encoding_gene']:
                    kbase_features.append(kbase_feature)
                    if feature_type in ['CDS', 'protein_encoding_gene']:
                        feature_counts['protein_encoding_gene'] += 1
                else:
                    non_coding_features.append(kbase_feature)
                    if feature_type not in ['CDS', 'gene']:
                        feature_counts['non-protein_encoding_gene'] += 1

        # Create CDS features
        cdss = self._create_cds_features(kbase_features)

        # Build genome object
        genome = {
            'id': genome_id,
            'scientific_name': genome_meta.get('genome_name', ''),
            'domain': domain,
            'taxonomy': taxonomy_str,
            'genetic_code': int(genome_meta.get('genetic_code', 11)),
            'dna_size': total_dna_size,
            'num_contigs': len(contig_ids),
            'contig_ids': contig_ids,
            'contig_lengths': contig_lengths,
            'gc_content': float(genome_meta.get('gc_content', 0.5)),
            'md5': genome_md5,
            'molecule_type': 'DNA',
            'source': 'PATRIC',
            'source_id': genome_id,
            'assembly_ref': '',
            'external_source_origination_date': genome_meta.get('completion_date',
                                                                datetime.now().isoformat()),
            'notes': f'Imported from BV-BRC on {datetime.now().isoformat()}',
            'features': kbase_features,
            'non_coding_features': non_coding_features,
            'cdss': cdss,
            'mrnas': [],
            'feature_counts': dict(feature_counts),
            'publications': [],
            'genome_tiers': ['ExternalDB', 'User'],
            'warnings': [],
            'taxon_ref': '',
        }

        self.log_info(f"Genome object created: {len(kbase_features)} features, "
                     f"{len(cdss)} CDS, {total_dna_size:,} bp")

        # Add ontology events if requested
        self.log_info("Creating ontology events from collected annotations...")
        # Create ontology events from collected ontologies
        ontology_events = []
        for ontology_type, gene_terms in ontologies.items():
            # Skip empty ontologies
            if not gene_terms:
                continue

            # Build ontology event structure
            ontology_terms = {}
            for gene_id, terms in gene_terms.items():
                ontology_terms[gene_id] = [{"term": term} for term in terms.keys()]

            event = {
                "description": f"{ontology_type} annotations imported from BV-BRC",
                "ontology_id": ontology_type,
                "method": "BVBRCUtils-build_kbase_genome_from_api",
                "method_version": "1.0",
                "timestamp": datetime.now().isoformat(),
                "ontology_terms": ontology_terms
            }
            ontology_events.append(event)
            self.log_debug(f"Created {ontology_type} event with {len(ontology_terms)} genes")
        self.save("test_genome",genome)
        # Add events to genome
        output = self.add_ontology_events(
            object=genome,
            type="KBaseGenomes.Genome",
            events=ontology_events,
            overwrite_matching=True
        )

        return output["object"]

    def _convert_bvbrc_feature(
        self,
        feature: Dict[str, Any],
        index: int,
        genome_id: str,
        contig_map: Dict[str, str],
        sequences: Dict[str, Dict[str, Any]],
        ontologies: Optional[Dict[str, Dict[str, Dict[str, int]]]] = None
    ) -> Optional[Dict[str, Any]]:
        """Convert BV-BRC feature to KBase format.

        Args:
            feature: BV-BRC feature data
            index: Feature index for ID generation
            genome_id: Genome ID
            contig_map: Mapping from sequence IDs to contig IDs
            sequences: Sequence data by MD5 hash
            ontologies: Optional dictionary to populate with ontology terms
        """
        feature_type = feature.get('feature_type', 'gene')
        patric_id = feature.get('patric_id', '')

        # Get sequences
        # BV-BRC API uses 'NA' for nucleic acid and 'AA' for amino acid sequences
        na_md5 = feature.get('na_sequence_md5', '')
        aa_md5 = feature.get('aa_sequence_md5', '')

        na_sequence = sequences.get(na_md5, {}).get('NA', '') if na_md5 else ''
        aa_sequence = sequences.get(aa_md5, {}).get('AA', '') if aa_md5 else ''

        # Get contig ID
        sequence_id = feature.get('sequence_id', '')
        contig_id = contig_map.get(sequence_id, sequence_id)

        # Build location
        start = feature.get('start', 0)
        strand = feature.get('strand', '+')
        length = feature.get('na_length', len(na_sequence))
        location = [[contig_id, start, strand, length]]

        # Build functions
        functions = []
        product = feature.get('product', '')
        if product:
            functions.append(product)

        # Build aliases
        aliases = [['PATRIC_id', patric_id]]
        refseq_locus_tag = feature.get('refseq_locus_tag', '')
        gene_name = feature.get('gene', '')
        if refseq_locus_tag:
            aliases.append(['RefSeq_locus_tag', refseq_locus_tag])
        if gene_name:
            aliases.append(['gene_name', gene_name])

        # Build feature object
        feature_id = f"{genome_id}_{index}"
        kbase_feature = {
            'id': feature_id,
            'type': feature_type,
            'location': location,
            'functions': functions,
            'aliases': aliases,
            'dna_sequence': na_sequence,
            'dna_sequence_length': len(na_sequence),
            'md5': hashlib.md5(na_sequence.encode()).hexdigest() if na_sequence else '',
        }

        # Add protein data if available
        if aa_sequence:
            kbase_feature['protein_translation'] = aa_sequence
            kbase_feature['protein_translation_length'] = len(aa_sequence)
            kbase_feature['protein_md5'] = hashlib.md5(aa_sequence.encode()).hexdigest()

        # Populate ontology terms if ontologies dict provided
        if ontologies is not None:
            # Add product to SSO (SEED Subsystem Ontology) and RefSeq
            if product:
                if feature_id not in ontologies['SSO']:
                    ontologies['SSO'][feature_id] = {}
                if feature_id not in ontologies['RefSeq']:
                    ontologies['RefSeq'][feature_id] = {}
                ontologies['SSO'][feature_id][product] = 1
                ontologies['RefSeq'][feature_id][product] = 1

            # Add FIGFAM if available
            figfam_id = feature.get('figfam_id', '')
            if figfam_id:
                if feature_id not in ontologies['FIGFAM']:
                    ontologies['FIGFAM'][feature_id] = {}
                ontologies['FIGFAM'][feature_id][figfam_id] = 1

            # Add PGFAM if available
            pgfam_id = feature.get('pgfam_id', '')
            if pgfam_id:
                if feature_id not in ontologies['PGFAM']:
                    ontologies['PGFAM'][feature_id] = {}
                ontologies['PGFAM'][feature_id][pgfam_id] = 1

            # Add PLFAM if available
            plfam_id = feature.get('plfam_id', '')
            if plfam_id:
                if feature_id not in ontologies['PLFAM']:
                    ontologies['PLFAM'][feature_id] = {}
                ontologies['PLFAM'][feature_id][plfam_id] = 1

            # Add GO terms if available
            go_terms = feature.get('go', '')
            if go_terms:
                if feature_id not in ontologies['GO']:
                    ontologies['GO'][feature_id] = {}
                # GO terms might be comma-separated
                for term in go_terms:
                    term = term.strip()
                    term = term.split('|')[0]
                    if term:
                        ontologies['GO'][feature_id][term] = 1

        return kbase_feature