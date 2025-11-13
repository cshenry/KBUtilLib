"""KBase UniProt utilities for fetching protein information from UniProt.

This module provides utilities for interfacing with the UniProt REST API
to retrieve protein sequences, annotations, publications, cross-references,
and UniRef cluster IDs.
"""

import time
from typing import Any, Dict, List, Optional, Set, Union

import requests

from .base_utils import BaseUtils


class KBUniProtUtils(BaseUtils):
    """Utilities for retrieving protein information from UniProt.

    This class provides methods to:
    - Fetch protein sequences
    - Retrieve annotations and features
    - Get publication/literature references
    - Access cross-references (Rhea IDs, PDB IDs, etc.)
    - Map UniProt IDs to UniRef cluster IDs
    - Perform flexible field-based queries
    """

    def __init__(
        self,
        uniprot_api_url: str = "https://rest.uniprot.org",
        **kwargs: Any
    ) -> None:
        """Initialize KBase UniProt utilities.

        Args:
            uniprot_api_url: Base URL for the UniProt REST API
            **kwargs: Additional keyword arguments passed to BaseUtils
        """
        super().__init__(**kwargs)
        self.uniprot_api_url = uniprot_api_url.rstrip("/")
        self.uniprotkb_endpoint = f"{self.uniprot_api_url}/uniprotkb"
        self.idmapping_endpoint = f"{self.uniprot_api_url}/idmapping"

        # Set up default headers for API requests
        self.headers = {
            "User-Agent": "KBUniProtUtils/0.1.0 (KBase; https://kbase.us)",
            "Accept": "application/json"
        }

    def get_uniprot_entry(
        self,
        uniprot_id: str,
        fields: Optional[List[str]] = None,
        format: str = "json"
    ) -> Dict[str, Any]:
        """Fetch a UniProt entry with specified fields.

        Args:
            uniprot_id: UniProt accession or ID
            fields: List of field names to retrieve. If None, returns default fields.
                   See https://www.uniprot.org/help/return_fields for available fields.
            format: Response format (json, tsv, fasta, etc.)

        Returns:
            Dict containing the UniProt entry data

        Raises:
            ValueError: If uniprot_id is empty
            requests.RequestException: If API request fails
        """
        if not uniprot_id:
            raise ValueError("uniprot_id cannot be empty")

        self.log_info(f"Fetching UniProt entry for {uniprot_id}")

        url = f"{self.uniprotkb_endpoint}/{uniprot_id}"
        params = {"format": format}

        if fields:
            params["fields"] = ",".join(fields)

        try:
            response = requests.get(
                url,
                params=params,
                headers=self.headers,
                timeout=30,
                verify=False
            )
            response.raise_for_status()

            if format == "json":
                return response.json()
            else:
                return {"raw_response": response.text}

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                self.log_error(f"UniProt entry not found: {uniprot_id}")
                raise ValueError(f"UniProt entry not found: {uniprot_id}")
            else:
                self.log_error(f"Failed to fetch UniProt entry: {str(e)}")
                raise
        except requests.exceptions.RequestException as e:
            self.log_error(f"UniProt API request failed: {str(e)}")
            raise

    def get_protein_sequence(
        self,
        uniprot_id: str,
        format: str = "fasta"
    ) -> str:
        """Retrieve the protein sequence for a UniProt entry.

        Args:
            uniprot_id: UniProt accession or ID
            format: Sequence format (fasta or raw)

        Returns:
            Protein sequence string (FASTA format or raw sequence)

        Raises:
            ValueError: If uniprot_id is empty or entry not found
            requests.RequestException: If API request fails
        """
        self.log_info(f"Fetching protein sequence for {uniprot_id}")

        if format == "fasta":
            result = self.get_uniprot_entry(uniprot_id, format="fasta")
            return result.get("raw_response", "")
        else:
            # Get JSON entry and extract sequence
            entry = self.get_uniprot_entry(uniprot_id, fields=["sequence"])
            sequence = entry.get("sequence", {}).get("value", "")
            return sequence

    def get_annotations(
        self,
        uniprot_id: str,
        annotation_types: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Retrieve annotations for a UniProt entry.

        Args:
            uniprot_id: UniProt accession or ID
            annotation_types: List of annotation types to retrieve. If None, returns
                            common annotations (features, function, keywords, etc.)
                            Available types include: features, comments, keywords,
                            gene_ontology, ec, protein_name, etc.

        Returns:
            Dict containing the requested annotations

        Raises:
            ValueError: If uniprot_id is empty
            requests.RequestException: If API request fails
        """
        self.log_info(f"Fetching annotations for {uniprot_id}")

        # Default annotation fields if none specified
        if annotation_types is None:
            annotation_types = [
                "protein_name",
                "gene_names",
                "organism_name",
                "cc_function",
                "cc_catalytic_activity",
                "ft_domain",
                "ft_region",
                "ft_site",
                "keyword",
                "go",
                "ec"
            ]

        entry = self.get_uniprot_entry(uniprot_id, fields=annotation_types)
        return entry

    def get_publications(
        self,
        uniprot_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieve publication references for a UniProt entry.

        Args:
            uniprot_id: UniProt accession or ID

        Returns:
            List of publication dictionaries containing citation information

        Raises:
            ValueError: If uniprot_id is empty
            requests.RequestException: If API request fails
        """
        self.log_info(f"Fetching publications for {uniprot_id}")

        fields = [
            "lit_pubmed_id",
            "lit_doi_id",
            "cc_interaction"
        ]

        entry = self.get_uniprot_entry(uniprot_id, fields=fields)

        # Extract references from the entry
        references = entry.get("references", [])

        publications = []
        for ref in references:
            citation = ref.get("citation", {})
            pub_info = {
                "title": citation.get("title", ""),
                "journal": citation.get("journal", ""),
                "pubmed_id": None,
                "doi": None,
                "year": citation.get("publicationDate", "")
            }

            # Extract PubMed ID and DOI from citationCrossReferences
            for xref in citation.get("citationCrossReferences", []):
                if xref.get("database") == "PubMed":
                    pub_info["pubmed_id"] = xref.get("id")
                elif xref.get("database") == "DOI":
                    pub_info["doi"] = xref.get("id")

            publications.append(pub_info)

        return publications

    def get_rhea_ids(
        self,
        uniprot_id: str
    ) -> List[str]:
        """Retrieve Rhea reaction IDs for a UniProt entry.

        Args:
            uniprot_id: UniProt accession or ID

        Returns:
            List of Rhea IDs associated with the protein

        Raises:
            ValueError: If uniprot_id is empty
            requests.RequestException: If API request fails
        """
        self.log_info(f"Fetching Rhea IDs for {uniprot_id}")

        entry = self.get_uniprot_entry(
            uniprot_id,
            fields=["cc_catalytic_activity", "xref_rhea"]
        )

        rhea_ids = []

        # Extract from catalytic activity comments
        comments = entry.get("comments", [])
        for comment in comments:
            if comment.get("commentType") == "CATALYTIC ACTIVITY":
                reaction = comment.get("reaction", {})
                rhea_id = reaction.get("reactionCrossReference", {}).get("id")
                if rhea_id:
                    rhea_ids.append(rhea_id)

        # Remove duplicates while preserving order
        seen = set()
        unique_rhea_ids = []
        for rhea_id in rhea_ids:
            if rhea_id not in seen:
                seen.add(rhea_id)
                unique_rhea_ids.append(rhea_id)

        self.log_info(f"Found {len(unique_rhea_ids)} Rhea IDs for {uniprot_id}")
        return unique_rhea_ids

    def get_pdb_ids(
        self,
        uniprot_id: str,
        full_info: bool = False
    ) -> Union[List[str], List[Dict[str, Any]]]:
        """Retrieve PDB structure IDs for a UniProt entry.

        Args:
            uniprot_id: UniProt accession or ID
            full_info: If True, returns full PDB cross-reference information
                      including method, resolution, and chains. If False,
                      returns just PDB IDs.

        Returns:
            List of PDB IDs or list of dicts with full PDB information

        Raises:
            ValueError: If uniprot_id is empty
            requests.RequestException: If API request fails
        """
        self.log_info(f"Fetching PDB IDs for {uniprot_id}")

        field = "xref_pdb_full" if full_info else "xref_pdb"
        entry = self.get_uniprot_entry(uniprot_id, fields=[field])

        pdb_refs = []

        # Extract from uniProtKBCrossReferences
        cross_refs = entry.get("uniProtKBCrossReferences", [])
        for xref in cross_refs:
            if xref.get("database") == "PDB":
                if full_info:
                    pdb_info = {
                        "id": xref.get("id"),
                        "properties": {}
                    }
                    # Extract properties like method, resolution, chains
                    for prop in xref.get("properties", []):
                        key = prop.get("key")
                        value = prop.get("value")
                        if key and value:
                            pdb_info["properties"][key] = value

                    pdb_refs.append(pdb_info)
                else:
                    pdb_refs.append(xref.get("id"))

        self.log_info(f"Found {len(pdb_refs)} PDB entries for {uniprot_id}")
        return pdb_refs

    def get_uniref_ids(
        self,
        uniprot_ids: Union[str, List[str]],
        uniref_type: str = "UniRef50",
        poll_interval: float = 1.0,
        max_wait_time: float = 60.0
    ) -> Dict[str, Optional[str]]:
        """Map UniProt IDs to UniRef cluster IDs.

        This is the most critical function for getting UniRef cluster membership.
        Uses the UniProt ID mapping service to convert UniProt accessions to
        UniRef50, UniRef90, or UniRef100 cluster representatives.

        Args:
            uniprot_ids: Single UniProt ID or list of UniProt IDs
            uniref_type: Type of UniRef cluster (UniRef50, UniRef90, or UniRef100)
            poll_interval: Time in seconds between polling attempts
            max_wait_time: Maximum time in seconds to wait for results

        Returns:
            Dict mapping UniProt IDs to their UniRef cluster IDs
            (None if no mapping found)

        Raises:
            ValueError: If inputs are invalid
            requests.RequestException: If API request fails
            TimeoutError: If mapping job doesn't complete within max_wait_time
        """
        # Normalize input to list
        if isinstance(uniprot_ids, str):
            uniprot_ids = [uniprot_ids]

        if not uniprot_ids:
            raise ValueError("uniprot_ids cannot be empty")

        valid_types = ["UniRef50", "UniRef90", "UniRef100"]
        if uniref_type not in valid_types:
            raise ValueError(
                f"uniref_type must be one of {valid_types}, got: {uniref_type}"
            )

        self.log_info(
            f"Mapping {len(uniprot_ids)} UniProt IDs to {uniref_type} clusters"
        )

        # Step 1: Submit ID mapping job
        submit_url = f"{self.idmapping_endpoint}/run"

        payload = {
            "ids": ",".join(uniprot_ids),
            "from": "UniProtKB_AC-ID",
            "to": uniref_type
        }

        try:
            response = requests.post(
                submit_url,
                data=payload,
                headers=self.headers,
                timeout=30,
                verify=False
            )
            response.raise_for_status()

            job_data = response.json()
            job_id = job_data.get("jobId")

            if not job_id:
                raise ValueError(
                    f"Expected 'jobId' in response, got: {job_data.keys()}"
                )

            self.log_info(f"ID mapping job submitted with job_id: {job_id}")

        except requests.exceptions.RequestException as e:
            self.log_error(f"ID mapping job submission failed: {str(e)}")
            raise

        # Step 2: Poll for results
        results_url = f"{self.idmapping_endpoint}/status/{job_id}"
        elapsed_time = 0.0

        self.log_info(
            f"Polling for ID mapping results (interval: {poll_interval}s, "
            f"max_wait: {max_wait_time}s)"
        )

        while elapsed_time < max_wait_time:
            time.sleep(poll_interval)
            elapsed_time += poll_interval

            try:
                status_response = requests.get(
                    results_url,
                    headers=self.headers,
                    timeout=30,
                    verify=False
                )

                # Job is complete when we get redirected to results
                if status_response.status_code == 303:
                    # Follow redirect to get results
                    results_location = status_response.headers.get("Location")
                    if not results_location:
                        raise RuntimeError("No Location header in redirect response")

                    # Fetch the actual results
                    results_response = requests.get(
                        results_location,
                        headers=self.headers,
                        timeout=30,
                        verify=False
                    )
                    results_response.raise_for_status()

                    results_data = results_response.json()

                    self.log_info(
                        f"ID mapping completed successfully after {elapsed_time:.1f}s"
                    )

                    # Parse results into mapping dict
                    mapping = {}
                    for result in results_data.get("results", []):
                        from_id = result.get("from")
                        to_id = result.get("to", {}).get("id")
                        if from_id:
                            mapping[from_id] = to_id

                    # Ensure all requested IDs are in the result, even if unmapped
                    for uniprot_id in uniprot_ids:
                        if uniprot_id not in mapping:
                            mapping[uniprot_id] = None

                    self.log_info(
                        f"Mapped {len([v for v in mapping.values() if v])} out of "
                        f"{len(uniprot_ids)} UniProt IDs to {uniref_type}"
                    )

                    return mapping

                elif status_response.status_code == 200:
                    # Job still running
                    self.log_debug(
                        f"Job still running after {elapsed_time:.1f}s"
                    )
                    continue

                else:
                    status_response.raise_for_status()

            except requests.exceptions.RequestException as e:
                self.log_warning(f"Error polling for results: {str(e)}")
                continue

        # Timeout reached
        raise TimeoutError(
            f"ID mapping job {job_id} did not complete within {max_wait_time}s"
        )

    def get_uniprot_info(
        self,
        uniprot_id: str,
        include_sequence: bool = True,
        include_annotations: bool = True,
        include_publications: bool = True,
        include_rhea_ids: bool = True,
        include_pdb_ids: bool = True,
        include_uniref_ids: bool = True,
        uniref_type: str = "UniRef50",
        additional_fields: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Comprehensive method to fetch all requested information for a UniProt entry.

        This is a convenience method that fetches multiple types of information
        in one call. Individual methods can be used for more fine-grained control.

        Args:
            uniprot_id: UniProt accession or ID
            include_sequence: Whether to fetch protein sequence
            include_annotations: Whether to fetch annotations
            include_publications: Whether to fetch publication references
            include_rhea_ids: Whether to fetch Rhea reaction IDs
            include_pdb_ids: Whether to fetch PDB structure IDs
            include_uniref_ids: Whether to fetch UniRef cluster IDs (most critical)
            uniref_type: Type of UniRef cluster (UniRef50, UniRef90, or UniRef100)
            additional_fields: Additional field names to fetch

        Returns:
            Dict containing all requested information organized by category

        Raises:
            ValueError: If uniprot_id is empty
            requests.RequestException: If API requests fail
        """
        self.initialize_call(
            "get_uniprot_info",
            {
                "uniprot_id": uniprot_id,
                "include_sequence": include_sequence,
                "include_annotations": include_annotations,
                "include_publications": include_publications,
                "include_rhea_ids": include_rhea_ids,
                "include_pdb_ids": include_pdb_ids,
                "include_uniref_ids": include_uniref_ids,
                "uniref_type": uniref_type,
            },
            print_params=True
        )

        result = {
            "uniprot_id": uniprot_id,
            "sequence": None,
            "annotations": None,
            "publications": None,
            "rhea_ids": None,
            "pdb_ids": None,
            "uniref_ids": None,
            "additional_data": None
        }

        try:
            if include_sequence:
                self.log_info("Fetching sequence...")
                result["sequence"] = self.get_protein_sequence(uniprot_id, format="raw")

            if include_annotations:
                self.log_info("Fetching annotations...")
                result["annotations"] = self.get_annotations(uniprot_id)

            if include_publications:
                self.log_info("Fetching publications...")
                result["publications"] = self.get_publications(uniprot_id)

            if include_rhea_ids:
                self.log_info("Fetching Rhea IDs...")
                result["rhea_ids"] = self.get_rhea_ids(uniprot_id)

            if include_pdb_ids:
                self.log_info("Fetching PDB IDs...")
                result["pdb_ids"] = self.get_pdb_ids(uniprot_id)

            if include_uniref_ids:
                self.log_info(f"Fetching {uniref_type} cluster IDs...")
                uniref_mapping = self.get_uniref_ids(uniprot_id, uniref_type=uniref_type)
                result["uniref_ids"] = uniref_mapping.get(uniprot_id)

            if additional_fields:
                self.log_info("Fetching additional fields...")
                result["additional_data"] = self.get_uniprot_entry(
                    uniprot_id,
                    fields=additional_fields
                )

            self.log_info(f"Successfully fetched all requested information for {uniprot_id}")
            return result

        except Exception as e:
            self.log_error(f"Error fetching information for {uniprot_id}: {str(e)}")
            raise

    def get_batch_uniprot_info(
        self,
        uniprot_ids: List[str],
        **kwargs: Any
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch information for multiple UniProt entries.

        This method processes multiple UniProt IDs efficiently, using batch
        operations where possible (e.g., for UniRef mapping).

        Args:
            uniprot_ids: List of UniProt accessions or IDs
            **kwargs: Arguments passed to get_uniprot_info for each entry

        Returns:
            Dict mapping UniProt IDs to their information dicts

        Raises:
            ValueError: If uniprot_ids is empty
        """
        if not uniprot_ids:
            raise ValueError("uniprot_ids cannot be empty")

        self.log_info(f"Fetching information for {len(uniprot_ids)} UniProt entries")

        results = {}

        # If UniRef IDs are requested, do batch mapping first
        include_uniref = kwargs.get("include_uniref_ids", True)
        uniref_type = kwargs.get("uniref_type", "UniRef50")

        uniref_mapping = {}
        if include_uniref:
            self.log_info(f"Performing batch {uniref_type} mapping...")
            uniref_mapping = self.get_uniref_ids(uniprot_ids, uniref_type=uniref_type)

        # Fetch information for each entry
        for uniprot_id in uniprot_ids:
            try:
                self.log_info(f"Processing {uniprot_id}...")

                # Temporarily disable UniRef fetching since we already have it
                fetch_kwargs = kwargs.copy()
                fetch_kwargs["include_uniref_ids"] = False

                entry_info = self.get_uniprot_info(uniprot_id, **fetch_kwargs)

                # Add the pre-fetched UniRef ID
                if include_uniref:
                    entry_info["uniref_ids"] = uniref_mapping.get(uniprot_id)

                results[uniprot_id] = entry_info

            except Exception as e:
                self.log_error(f"Failed to fetch info for {uniprot_id}: {str(e)}")
                results[uniprot_id] = {
                    "error": str(e),
                    "uniprot_id": uniprot_id
                }

        self.log_info(
            f"Successfully processed {len([r for r in results.values() if 'error' not in r])} "
            f"out of {len(uniprot_ids)} entries"
        )

        return results
