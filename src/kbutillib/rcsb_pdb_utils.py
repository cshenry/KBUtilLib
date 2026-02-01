"""RCSB PDB utility class for querying protein structures from the RCSB database."""

import json
import logging
import asyncio
import aiohttp
import requests
from requests.exceptions import ConnectionError, HTTPError, RequestException
from typing import Any, Dict, List, Optional

from .base_utils import BaseUtils

try:
    from python_graphql_client import GraphqlClient
except ImportError:
    GraphqlClient = None

logger = logging.getLogger(__name__)

# GraphQL query template for fetching PDB metadata
META_DATA_QUERY = """{
    entries(entry_ids:["%s"]) {
        rcsb_id
        exptl {
            method
        }
        rcsb_primary_citation {
            title
            rcsb_authors
            journal_abbrev
            year
            pdbx_database_id_PubMed
        }
        polymer_entities {
            rcsb_id
            entity_poly {
                pdbx_seq_one_letter_code
                pdbx_strand_id
            }
            rcsb_entity_source_organism {
                ncbi_taxonomy_id
                ncbi_scientific_name
            }
            rcsb_polymer_entity_container_identifiers {
                reference_sequence_identifiers {
                    database_accession
                    database_name
                }
            }
            rcsb_polymer_entity {
                rcsb_ec_lineage {
                    id
                }
            }
            uniprots {
              rcsb_uniprot_protein {
                name {
                  value
                }
                ec {
                  number
                  provenance_code
                }
              }
            }
        }
        nonpolymer_entities {
          nonpolymer_comp {
            rcsb_chem_comp_synonyms {
              name
              provenance_source
            }
            rcsb_chem_comp_descriptor {
              InChI
              InChIKey
              SMILES
            }
          }
        }
    }
}
"""


class RCSBPDBUtils(BaseUtils):
    """Utility class for querying RCSB PDB database.

    Provides methods for:
    - Sequence-based similarity search against PDB structures
    - Fetching metadata for PDB entries via GraphQL
    - Batch processing of protein sequences to find structural matches
    """

    def __init__(
        self,
        inchikey_cpd_map: Optional[Dict[str, str]] = None,
        max_hits: int = 2,
        log_level: str = "INFO",
        **kwargs: Any
    ) -> None:
        """Initialize RCSBPDBUtils.

        Args:
            inchikey_cpd_map: Optional mapping of InChIKey prefixes to compound IDs.
                Used to map PDB ligands to ModelSEED compounds.
            max_hits: Maximum number of PDB hits to retain per protein (default: 2).
            log_level: Logging level (default: "INFO").
            **kwargs: Additional arguments passed to BaseUtils.
        """
        super().__init__(name="RCSBPDBUtils", log_level=log_level, **kwargs)

        if GraphqlClient is None:
            raise ImportError(
                "python_graphql_client is required for RCSBPDBUtils. "
                "Install it with: pip install python-graphql-client"
            )

        self.graphql_client = GraphqlClient(endpoint='https://data.rcsb.org/graphql')
        self.inchikey_cpd_map = inchikey_cpd_map or {}
        self.max_hits = max_hits

    def query_rcsb_with_sequence(
        self,
        sequence: str,
        threshold_type: str = "evalue",
        threshold_value: float = 0.00001,
        max_hits: int = 500
    ) -> Dict[str, Dict[str, Any]]:
        """Query RCSB for PDB structures matching a protein sequence.

        Args:
            sequence: Amino acid sequence to search.
            threshold_type: Type of similarity threshold, either "evalue" or "identity".
            threshold_value: Threshold value (e.g., 0.00001 for evalue, 0.9 for identity).
            max_hits: Maximum number of results to return (default: 500).

        Returns:
            Dictionary mapping PDB entity IDs to match data containing:
            - sequence_identity: Fraction of identical residues
            - evalue: E-value of the alignment
            - bitscore: Bit score of the alignment
            - alignment_length: Length of the alignment
            - mismatches: Number of mismatched positions
            - gaps_opened: Number of gap openings
            - query_beg/end: Query sequence alignment positions
            - subject_beg/end: Subject sequence alignment positions
            - query_length/subject_length: Full sequence lengths
            - query_aligned_seq/subject_aligned_seq: Aligned sequence strings
        """
        query_input = {
            "query": {
                "type": "terminal",
                "service": "sequence",
                "parameters": {
                    "sequence_type": "protein",
                    "value": sequence
                }
            },
            "return_type": "polymer_entity",
            "request_options": {
                "results_verbosity": "verbose",
                "paginate": {
                    "start": 0,
                    "rows": max_hits
                },
                "results_content_type": ["experimental"],
                "sort": [{"sort_by": "score", "direction": "desc"}],
                "scoring_strategy": "combined"
            }
        }

        # Set similarity threshold based on type
        if threshold_type == "evalue":
            query_input["query"]["parameters"]["evalue_cutoff"] = threshold_value
        else:
            query_input["query"]["parameters"]["identity_cutoff"] = threshold_value

        # Query RCSB
        results = {}
        try:
            response = requests.post(
                'https://search.rcsb.org/rcsbsearch/v2/query',
                json=query_input
            )
            response.raise_for_status()
            if len(response.text) > 0:
                results = json.loads(response.text)
        except (HTTPError, ConnectionError, RequestException) as e:
            self.log_warning(f"RCSB query error: {e}")
            return {}
        except Exception as e:
            self.log_error(f"Unexpected error querying RCSB: {e}")
            return {}

        # Parse and format results
        formatted_results = {}
        if "result_set" in results:
            for item in results["result_set"]:
                if "identifier" not in item:
                    continue
                if "services" not in item or len(item["services"]) < 1:
                    continue
                service = item["services"][0]
                if "nodes" not in service or len(service["nodes"]) < 1:
                    continue
                node = service["nodes"][0]
                if "match_context" not in node or len(node["match_context"]) < 1:
                    continue
                match_data = node["match_context"][0]
                formatted_results[item["identifier"]] = match_data

        return formatted_results

    def query_rcsb_metadata_by_id(
        self,
        id_list: List[str],
        bundle_size: int = 100
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch metadata for a list of PDB IDs via GraphQL.

        Args:
            id_list: List of PDB IDs (4-character codes) to query.
            bundle_size: Number of IDs to query per request (default: 100).

        Returns:
            Dictionary mapping PDB IDs to metadata containing:
            - reference: [pubmed_id, title, journal, author, year] or None
            - methods: List of experimental methods (e.g., ["X-RAY DIFFRACTION"])
            - compounds: List of compound data dicts with InChI, SMILES, etc.
            - proteins: Dict mapping entity IDs to protein data with taxonomy,
              UniProt IDs, EC numbers, etc.
        """
        count = 0
        current_bundle = []
        metadata_hash = {}

        for pdb_id in id_list:
            count += 1
            current_bundle.append(pdb_id)

            if len(current_bundle) >= bundle_size or count >= len(id_list):
                query_string = META_DATA_QUERY % '", "'.join(current_bundle)
                raw_data = {}

                try:
                    evt_loop = asyncio.get_event_loop()
                    raw_data = evt_loop.run_until_complete(
                        self.graphql_client.execute_async(query=query_string)
                    )
                    if 'errors' in raw_data:
                        raise ConnectionError(raw_data['errors'][0]['message'])
                except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                    self.log_warning("Connection error to RCSB GraphQL endpoint")
                    return {'data': None}
                except (HTTPError, ConnectionError, RequestException) as e:
                    self.log_warning(f"RCSB GraphQL connection error: {e}")
                    return {'data': None}
                except (RuntimeError, TypeError, KeyError, ValueError) as e:
                    err_msg = f"RCSB query error: {getattr(e, 'message', str(e))}"
                    raise ValueError(err_msg)

                if raw_data.get('data') and raw_data['data'].get('entries'):
                    for entry in raw_data["data"]["entries"]:
                        pdb_id = entry['rcsb_id']
                        metadata_hash[pdb_id] = self._parse_entry_metadata(entry)

                current_bundle = []
                self.log_info(f"Done querying metadata for {count} of {len(id_list)}")

        return metadata_hash

    def _parse_entry_metadata(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a single PDB entry into structured metadata.

        Args:
            entry: Raw entry data from GraphQL response.

        Returns:
            Structured metadata dictionary.
        """
        metadata = {
            "reference": None,
            "methods": [],
            "compounds": [],
            "proteins": {}
        }

        # Parse citation
        if entry.get('rcsb_primary_citation'):
            citation = entry["rcsb_primary_citation"]
            pubmed_id = citation.get('pdbx_database_id_PubMed')
            title = citation.get('title', '')
            journal = citation.get('journal_abbrev', 'NA')
            year = citation.get('year', 'NA')
            authors = citation.get('rcsb_authors', [])
            author = authors[0] if authors else ""
            metadata["reference"] = [pubmed_id, title, journal, author, str(year)]

        # Parse experimental methods
        for exp in entry.get('exptl', []):
            if exp.get('method'):
                metadata["methods"].append(exp['method'])

        # Parse polymer entities (proteins)
        for pe in entry.get('polymer_entities', []):
            protein_data = self._parse_polymer_entity(pe)
            metadata["proteins"][pe['rcsb_id']] = protein_data

        # Parse non-polymer entities (compounds/ligands)
        for npe in entry.get('nonpolymer_entities', []):
            if npe.get('nonpolymer_comp'):
                compound_data = self._parse_nonpolymer_entity(npe)
                if compound_data:
                    metadata["compounds"].append(compound_data)

        return metadata

    def _parse_polymer_entity(self, pe: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a polymer entity (protein) from PDB entry.

        Args:
            pe: Polymer entity data from GraphQL response.

        Returns:
            Structured protein data dictionary.
        """
        protein_data = {
            "strands": pe['entity_poly']['pdbx_strand_id'].split(','),
            'taxonomy': [],
            'uniprotID': [],
            'ec_numbers': [],
            'uniprot_name': [],
            'uniprot_ec': []
        }

        # Parse taxonomy
        if pe.get('rcsb_entity_source_organism'):
            for org in pe['rcsb_entity_source_organism']:
                protein_data['taxonomy'].append((
                    org.get('ncbi_taxonomy_id', ''),
                    org.get('ncbi_scientific_name', '')
                ))

        # Parse reference sequence IDs
        container_ids = pe.get('rcsb_polymer_entity_container_identifiers', {})
        if container_ids.get('reference_sequence_identifiers'):
            protein_data['ref_sequence_ids'] = container_ids['reference_sequence_identifiers']
            for rsid in protein_data['ref_sequence_ids']:
                protein_data['uniprotID'].append(rsid.get('database_accession', ''))

        # Parse EC numbers from RCSB
        polymer_entity = pe.get('rcsb_polymer_entity', {})
        if polymer_entity.get('rcsb_ec_lineage'):
            for ec_entry in polymer_entity['rcsb_ec_lineage']:
                if ec_entry['id'] and ec_entry['id'] not in protein_data['ec_numbers']:
                    protein_data['ec_numbers'].append(ec_entry['id'])

        # Parse UniProt data
        if pe.get('uniprots'):
            for unp in pe['uniprots']:
                if unp.get('rcsb_uniprot_protein'):
                    uniprot_prot = unp['rcsb_uniprot_protein']
                    if uniprot_prot.get('name'):
                        protein_data['uniprot_name'].append(
                            uniprot_prot['name'].get('value', '')
                        )
                    if uniprot_prot.get('ec'):
                        protein_data['uniprot_ec'].extend(uniprot_prot['ec'])

        return protein_data

    def _parse_nonpolymer_entity(self, npe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse a non-polymer entity (ligand/compound) from PDB entry.

        Args:
            npe: Non-polymer entity data from GraphQL response.

        Returns:
            Structured compound data dictionary, or None if no useful data.
        """
        comp = npe.get('nonpolymer_comp', {})
        compound_data = {'pdb_ref_name': ''}

        # Get PDB reference name
        for syn in comp.get('rcsb_chem_comp_synonyms', []):
            if syn.get('provenance_source') == 'PDB Reference Data':
                compound_data['pdb_ref_name'] = syn.get('name', '')
                break

        # Get chemical descriptors
        descriptor = comp.get('rcsb_chem_comp_descriptor', {})
        for desc_type in ('InChI', 'InChIKey', 'SMILES'):
            if descriptor.get(desc_type):
                compound_data[desc_type] = descriptor[desc_type]

        # Try to map to ModelSEED compound
        inchi_key = compound_data.get('InChIKey', '')
        if inchi_key and self.inchikey_cpd_map:
            # Match on prefix (without last 2 characters)
            ik_prefix = inchi_key[:-2] if len(inchi_key) > 2 else inchi_key
            for key in self.inchikey_cpd_map:
                if ik_prefix in key:
                    compound_data["mscpd"] = self.inchikey_cpd_map[key]
                    break

        return compound_data if len(compound_data) > 1 else None

    def query_rcsb_with_proteins(
        self,
        sequence_list: List[List[str]],
        cutoff_type: str = "evalue",
        threshold: float = 0.00001,
        bundle_size: int = 100,
        max_hits: Optional[int] = None
    ) -> Dict[str, List[Any]]:
        """Query RCSB with a list of protein sequences and aggregate results.

        This is the main method for batch querying. It:
        1. Queries RCSB for each protein sequence
        2. Fetches metadata for all unique PDB structures found
        3. Filters and ranks hits by EC number and e-value
        4. Returns a structured result table

        Args:
            sequence_list: List of [gene_id, sequence] pairs.
            cutoff_type: Type of similarity threshold ("evalue" or "identity").
            threshold: Threshold value for similarity filtering.
            bundle_size: Number of PDB IDs to query per metadata request.
            max_hits: Maximum hits to retain per protein (uses self.max_hits if None).

        Returns:
            Dictionary with columns as keys and lists of values:
            - id: Gene IDs
            - rcsbid: PDB entity IDs
            - method: Experimental methods
            - strand: Chain identifiers
            - similarity: [evalue, identity] pairs
            - taxonomy: [(taxid, name)] lists
            - name: UniProt protein names
            - components: Compound data lists
            - ec: [[ec_number, source]] lists
            - uniprotID: UniProt accession lists
            - references: [pubmed_id, title, journal, author, year] lists
        """
        if max_hits is None:
            max_hits = self.max_hits

        output_table = {
            "id": [], "rcsbid": [], "method": [], "strand": [],
            "similarity": [], "taxonomy": [], "name": [], "components": [],
            "ec": [], "uniprotID": [], "references": []
        }

        # Query RCSB for each sequence
        all_hits = {}
        distinct_ids = {}
        count = 0

        for item in sequence_list:
            count += 1
            gene_id, sequence = item[0], item[1]
            hits = self.query_rcsb_with_sequence(sequence, cutoff_type, threshold)

            for pdb_entity_id, hit_data in hits.items():
                if gene_id not in all_hits:
                    all_hits[gene_id] = {}
                all_hits[gene_id][pdb_entity_id] = hit_data
                pdb_id = pdb_entity_id.split("_")[0]
                distinct_ids[pdb_id] = 1

            if count % 100 == 0:
                self.log_info(f"Done querying sequences for {count} of {len(sequence_list)}")

        self.log_info(
            f"{len(distinct_ids)} distinct PDB IDs hit across {len(sequence_list)} input features"
        )

        # Fetch metadata for all PDB structures
        metadata_hash = self.query_rcsb_metadata_by_id(list(distinct_ids.keys()), bundle_size)

        # Build results table
        for item in sequence_list:
            gene_id = item[0]
            if gene_id not in all_hits:
                continue

            # Filter and rank hits by EC number
            retained_hits = self._filter_hits_by_ec(
                all_hits[gene_id], metadata_hash, max_hits
            )

            # Add retained hits to output table
            for hit in retained_hits:
                pdb_id = hit.split("_")[0]
                struct_row = metadata_hash[pdb_id]
                prot_row = struct_row["proteins"][hit]

                output_table["id"].append(gene_id)
                output_table["rcsbid"].append(hit)
                output_table["name"].append(
                    prot_row["uniprot_name"][0] if prot_row["uniprot_name"] else ""
                )
                output_table["method"].append(
                    struct_row["methods"][0] if struct_row["methods"] else ""
                )
                output_table["strand"].append(prot_row["strands"])
                output_table["similarity"].append([
                    all_hits[gene_id][hit]["evalue"],
                    all_hits[gene_id][hit]["sequence_identity"]
                ])
                output_table["taxonomy"].append(prot_row["taxonomy"])
                output_table["components"].append(struct_row["compounds"])
                output_table["ec"].append(prot_row.get("combined_ec", []))
                output_table["uniprotID"].append(prot_row["uniprotID"])
                output_table["references"].append(struct_row["reference"])

        return output_table

    def _filter_hits_by_ec(
        self,
        hits: Dict[str, Dict[str, Any]],
        metadata_hash: Dict[str, Dict[str, Any]],
        max_hits: int
    ) -> List[str]:
        """Filter and rank PDB hits by EC number and e-value.

        Prioritizes hits that have EC numbers and selects the best hit
        (lowest e-value) for each unique EC number.

        Args:
            hits: Dictionary of PDB entity ID to match data.
            metadata_hash: Metadata for all PDB structures.
            max_hits: Maximum number of hits to return.

        Returns:
            List of retained PDB entity IDs.
        """
        retained_hits = []
        ec_numbers = {}
        ec_scores = {}

        for hit, hit_data in hits.items():
            pdb_id = hit.split("_")[0]
            if pdb_id not in metadata_hash:
                continue

            prot_row = metadata_hash[pdb_id]["proteins"].get(hit)
            if not prot_row:
                continue

            # Combine EC numbers from RCSB and UniProt
            prot_row["combined_ec"] = []
            if prot_row.get("ec_numbers") or prot_row.get("uniprot_ec"):
                ec_hash = {}

                # Process RCSB EC numbers
                if prot_row.get("ec_numbers"):
                    longest_ec = ""
                    longest_ec_size = 0
                    for ec in prot_row["ec_numbers"]:
                        parts = ec.split(".")
                        if len(parts) == 4:
                            ec_hash[ec] = "RCSB"
                            longest_ec = None
                        if longest_ec and len(parts) > longest_ec_size:
                            longest_ec_size = len(parts)
                            longest_ec = ec
                    if longest_ec:
                        ec_hash[longest_ec] = "RCSB"

                # Process UniProt EC numbers
                if prot_row.get("uniprot_ec"):
                    for ec_entry in prot_row["uniprot_ec"]:
                        ec_num = ec_entry.get("number")
                        if ec_num:
                            if ec_num in ec_hash:
                                ec_hash[ec_num] += ";UniProt"
                            else:
                                ec_hash[ec_num] = "UniProt"

                # Build combined EC list and track best hits per EC
                for ec, source in ec_hash.items():
                    prot_row["combined_ec"].append([ec, source])
                    if ec not in ec_numbers or ec_scores[ec] > hit_data["evalue"]:
                        ec_numbers[ec] = hit
                        ec_scores[ec] = hit_data["evalue"]

        # Select best hits by EC number, sorted by e-value
        sorted_ec = sorted(ec_scores, key=ec_scores.get)
        count = 0
        for ec in sorted_ec:
            if ec_numbers[ec] not in retained_hits:
                retained_hits.append(ec_numbers[ec])
                count += 1
                if count >= max_hits:
                    break

        return retained_hits
