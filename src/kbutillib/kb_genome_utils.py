"""KBase genome utilities for working with genomic data and annotations."""

from typing import Any, Dict, Optional
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
