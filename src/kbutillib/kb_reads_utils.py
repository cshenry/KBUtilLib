"""KBase Reads and Assembly utilities for bulk upload and download.

This module provides utilities for working with KBase reads and assembly objects,
including upload and download functionality using only the Workspace API
and standard Python libraries (no SDK callbacks).
"""

import gzip
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from .kb_ws_utils import KBWSUtils


class Reads:
    """Represents a KBase reads library with JSON serialization support.

    This class models a single reads library (single-end or paired-end)
    and provides methods to serialize/deserialize to/from JSON format.
    """

    def __init__(
        self,
        name: Optional[str] = None,
        read_type: str = "single",
        files: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Initialize a Reads object.

        Args:
            name: Name of the reads library
            read_type: Type of reads - 'single', 'paired', or 'interleaved'
            files: Dictionary with file paths ('fwd', 'rev' keys for paired-end)
            metadata: Additional metadata about the reads
        """
        self.name = name
        self.read_type = read_type  # single, paired, or interleaved
        self.files = files or {}
        self.metadata = metadata or {}

        # KBase-specific fields
        self.workspace_ref = None
        self.shock_ids = {}
        self.handle_ids = {}

        # Sequencing metadata
        self.sequencing_tech = self.metadata.get("sequencing_tech", "Unknown")
        self.read_count = self.metadata.get("read_count", 0)
        self.read_size = self.metadata.get("read_size", 0)
        self.gc_content = self.metadata.get("gc_content", 0.0)

    def to_dict(self) -> Dict[str, Any]:
        """Convert Reads object to dictionary for JSON serialization.

        Returns:
            Dictionary representation of the Reads object
        """
        return {
            "name": self.name,
            "read_type": self.read_type,
            "files": self.files,
            "metadata": self.metadata,
            "workspace_ref": self.workspace_ref,
            "shock_ids": self.shock_ids,
            "handle_ids": self.handle_ids,
            "sequencing_tech": self.sequencing_tech,
            "read_count": self.read_count,
            "read_size": self.read_size,
            "gc_content": self.gc_content,
        }

    def to_json(self, filepath: Optional[str] = None) -> str:
        """Serialize Reads object to JSON.

        Args:
            filepath: Optional path to save JSON file

        Returns:
            JSON string representation
        """
        json_str = json.dumps(self.to_dict(), indent=2)
        if filepath:
            with open(filepath, "w") as f:
                f.write(json_str)
        return json_str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Reads":
        """Create a Reads object from a dictionary.

        Args:
            data: Dictionary containing reads data

        Returns:
            Reads object
        """
        reads = cls(
            name=data.get("name"),
            read_type=data.get("read_type", "single"),
            files=data.get("files", {}),
            metadata=data.get("metadata", {}),
        )
        reads.workspace_ref = data.get("workspace_ref")
        reads.shock_ids = data.get("shock_ids", {})
        reads.handle_ids = data.get("handle_ids", {})
        reads.sequencing_tech = data.get("sequencing_tech", "Unknown")
        reads.read_count = data.get("read_count", 0)
        reads.read_size = data.get("read_size", 0)
        reads.gc_content = data.get("gc_content", 0.0)
        return reads

    @classmethod
    def from_json(cls, json_str_or_file: str) -> "Reads":
        """Create a Reads object from JSON string or file.

        Args:
            json_str_or_file: JSON string or path to JSON file

        Returns:
            Reads object
        """
        if os.path.exists(json_str_or_file):
            with open(json_str_or_file, "r") as f:
                data = json.load(f)
        else:
            data = json.loads(json_str_or_file)
        return cls.from_dict(data)


class ReadSet:
    """Represents a collection of Reads objects.

    This class manages multiple reads libraries and provides
    JSON serialization for the entire collection.
    """

    def __init__(self, name: Optional[str] = None, description: Optional[str] = None):
        """Initialize a ReadSet.

        Args:
            name: Name of the read set
            description: Description of the read set
        """
        self.name = name
        self.description = description
        self.reads: Dict[str, Reads] = {}

    def add_reads(self, reads: Reads) -> None:
        """Add a Reads object to the set.

        Args:
            reads: Reads object to add
        """
        if reads.name:
            self.reads[reads.name] = reads
        else:
            raise ValueError("Reads object must have a name to be added to ReadSet")

    def get_reads(self, name: str) -> Optional[Reads]:
        """Get a Reads object by name.

        Args:
            name: Name of the reads library

        Returns:
            Reads object or None if not found
        """
        return self.reads.get(name)

    def remove_reads(self, name: str) -> None:
        """Remove a Reads object from the set.

        Args:
            name: Name of the reads library to remove
        """
        if name in self.reads:
            del self.reads[name]

    def list_reads(self) -> List[str]:
        """List all reads names in the set.

        Returns:
            List of reads names
        """
        return list(self.reads.keys())

    def to_dict(self) -> Dict[str, Any]:
        """Convert ReadSet to dictionary for JSON serialization.

        Returns:
            Dictionary representation of the ReadSet
        """
        return {
            "name": self.name,
            "description": self.description,
            "reads": {name: reads.to_dict() for name, reads in self.reads.items()},
        }

    def to_json(self, filepath: Optional[str] = None) -> str:
        """Serialize ReadSet to JSON.

        Args:
            filepath: Optional path to save JSON file

        Returns:
            JSON string representation
        """
        json_str = json.dumps(self.to_dict(), indent=2)
        if filepath:
            with open(filepath, "w") as f:
                f.write(json_str)
        return json_str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReadSet":
        """Create a ReadSet from a dictionary.

        Args:
            data: Dictionary containing readset data

        Returns:
            ReadSet object
        """
        readset = cls(name=data.get("name"), description=data.get("description"))
        for name, reads_data in data.get("reads", {}).items():
            reads = Reads.from_dict(reads_data)
            readset.reads[name] = reads
        return readset

    @classmethod
    def from_json(cls, json_str_or_file: str) -> "ReadSet":
        """Create a ReadSet from JSON string or file.

        Args:
            json_str_or_file: JSON string or path to JSON file

        Returns:
            ReadSet object
        """
        if os.path.exists(json_str_or_file):
            with open(json_str_or_file, "r") as f:
                data = json.load(f)
        else:
            data = json.loads(json_str_or_file)
        return cls.from_dict(data)


class Assembly:
    """Represents a KBase assembly with JSON serialization support.

    This class models a genome assembly and provides methods to
    serialize/deserialize to/from JSON format.
    """

    def __init__(
        self,
        name: Optional[str] = None,
        fasta_file: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Initialize an Assembly object.

        Args:
            name: Name of the assembly
            fasta_file: Path to FASTA file
            metadata: Additional metadata about the assembly
        """
        self.name = name
        self.fasta_file = fasta_file
        self.metadata = metadata or {}

        # KBase-specific fields
        self.workspace_ref = None
        self.shock_id = None
        self.handle_id = None

        # Assembly metadata
        self.assembly_id = self.metadata.get("assembly_id", name)
        self.num_contigs = self.metadata.get("num_contigs", 0)
        self.dna_size = self.metadata.get("dna_size", 0)
        self.gc_content = self.metadata.get("gc_content", 0.0)
        self.taxon_ref = self.metadata.get("taxon_ref")
        self.type = self.metadata.get("type", "Unknown")
        self.external_source = self.metadata.get("external_source", "User")
        self.external_source_id = self.metadata.get("external_source_id")

    def to_dict(self) -> Dict[str, Any]:
        """Convert Assembly object to dictionary for JSON serialization.

        Returns:
            Dictionary representation of the Assembly object
        """
        return {
            "name": self.name,
            "fasta_file": self.fasta_file,
            "metadata": self.metadata,
            "workspace_ref": self.workspace_ref,
            "shock_id": self.shock_id,
            "handle_id": self.handle_id,
            "assembly_id": self.assembly_id,
            "num_contigs": self.num_contigs,
            "dna_size": self.dna_size,
            "gc_content": self.gc_content,
            "taxon_ref": self.taxon_ref,
            "type": self.type,
            "external_source": self.external_source,
            "external_source_id": self.external_source_id,
        }

    def to_json(self, filepath: Optional[str] = None) -> str:
        """Serialize Assembly object to JSON.

        Args:
            filepath: Optional path to save JSON file

        Returns:
            JSON string representation
        """
        json_str = json.dumps(self.to_dict(), indent=2)
        if filepath:
            with open(filepath, "w") as f:
                f.write(json_str)
        return json_str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Assembly":
        """Create an Assembly object from a dictionary.

        Args:
            data: Dictionary containing assembly data

        Returns:
            Assembly object
        """
        assembly = cls(
            name=data.get("name"),
            fasta_file=data.get("fasta_file"),
            metadata=data.get("metadata", {}),
        )
        assembly.workspace_ref = data.get("workspace_ref")
        assembly.shock_id = data.get("shock_id")
        assembly.handle_id = data.get("handle_id")
        assembly.assembly_id = data.get("assembly_id", assembly.name)
        assembly.num_contigs = data.get("num_contigs", 0)
        assembly.dna_size = data.get("dna_size", 0)
        assembly.gc_content = data.get("gc_content", 0.0)
        assembly.taxon_ref = data.get("taxon_ref")
        assembly.type = data.get("type", "Unknown")
        assembly.external_source = data.get("external_source", "User")
        assembly.external_source_id = data.get("external_source_id")
        return assembly

    @classmethod
    def from_json(cls, json_str_or_file: str) -> "Assembly":
        """Create an Assembly object from JSON string or file.

        Args:
            json_str_or_file: JSON string or path to JSON file

        Returns:
            Assembly object
        """
        if os.path.exists(json_str_or_file):
            with open(json_str_or_file, "r") as f:
                data = json.load(f)
        else:
            data = json.loads(json_str_or_file)
        return cls.from_dict(data)


class AssemblySet:
    """Represents a collection of Assembly objects.

    This class manages multiple assemblies and provides
    JSON serialization for the entire collection.
    """

    def __init__(self, name: Optional[str] = None, description: Optional[str] = None):
        """Initialize an AssemblySet.

        Args:
            name: Name of the assembly set
            description: Description of the assembly set
        """
        self.name = name
        self.description = description
        self.assemblies: Dict[str, Assembly] = {}

    def add_assembly(self, assembly: Assembly) -> None:
        """Add an Assembly object to the set.

        Args:
            assembly: Assembly object to add
        """
        if assembly.name:
            self.assemblies[assembly.name] = assembly
        else:
            raise ValueError(
                "Assembly object must have a name to be added to AssemblySet"
            )

    def get_assembly(self, name: str) -> Optional[Assembly]:
        """Get an Assembly object by name.

        Args:
            name: Name of the assembly

        Returns:
            Assembly object or None if not found
        """
        return self.assemblies.get(name)

    def remove_assembly(self, name: str) -> None:
        """Remove an Assembly object from the set.

        Args:
            name: Name of the assembly to remove
        """
        if name in self.assemblies:
            del self.assemblies[name]

    def list_assemblies(self) -> List[str]:
        """List all assembly names in the set.

        Returns:
            List of assembly names
        """
        return list(self.assemblies.keys())

    def to_dict(self) -> Dict[str, Any]:
        """Convert AssemblySet to dictionary for JSON serialization.

        Returns:
            Dictionary representation of the AssemblySet
        """
        return {
            "name": self.name,
            "description": self.description,
            "assemblies": {
                name: asm.to_dict() for name, asm in self.assemblies.items()
            },
        }

    def to_json(self, filepath: Optional[str] = None) -> str:
        """Serialize AssemblySet to JSON.

        Args:
            filepath: Optional path to save JSON file

        Returns:
            JSON string representation
        """
        json_str = json.dumps(self.to_dict(), indent=2)
        if filepath:
            with open(filepath, "w") as f:
                f.write(json_str)
        return json_str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssemblySet":
        """Create an AssemblySet from a dictionary.

        Args:
            data: Dictionary containing assembly set data

        Returns:
            AssemblySet object
        """
        assemblyset = cls(name=data.get("name"), description=data.get("description"))
        for name, asm_data in data.get("assemblies", {}).items():
            assembly = Assembly.from_dict(asm_data)
            assemblyset.assemblies[name] = assembly
        return assemblyset

    @classmethod
    def from_json(cls, json_str_or_file: str) -> "AssemblySet":
        """Create an AssemblySet from JSON string or file.

        Args:
            json_str_or_file: JSON string or path to JSON file

        Returns:
            AssemblySet object
        """
        if os.path.exists(json_str_or_file):
            with open(json_str_or_file, "r") as f:
                data = json.load(f)
        else:
            data = json.loads(json_str_or_file)
        return cls.from_dict(data)


class KBReadsUtils(KBWSUtils):
    """Utilities for bulk upload and download of reads to/from KBase workspace.

    This class provides methods to upload and download reads libraries using
    only the Workspace API and standard Python libraries (no SDK callbacks).
    """

    def __init__(self, **kwargs):
        """Initialize KBReadsUtils.

        Args:
            **kwargs: Arguments passed to parent KBWSUtils
        """
        super().__init__(**kwargs)

    def upload_reads(
        self,
        reads: Reads,
        workspace: Union[str, int],
        obj_name: Optional[str] = None,
        sequencing_tech: str = "Unknown",
        insert_size_mean: Optional[float] = None,
        insert_size_std_dev: Optional[float] = None,
        read_orientation_outward: Optional[bool] = None,
    ) -> str:
        """Upload a reads library to the KBase workspace.

        Args:
            reads: Reads object containing file paths and metadata
            workspace: Workspace name or ID
            obj_name: Name for the workspace object (default: reads.name)
            sequencing_tech: Sequencing technology used
            insert_size_mean: Mean insert size for paired-end reads
            insert_size_std_dev: Standard deviation of insert size
            read_orientation_outward: Whether reads are oriented outward

        Returns:
            Workspace reference of the uploaded object
        """
        self.initialize_call("upload_reads", {"workspace": workspace})
        self.set_ws(workspace)

        if obj_name is None:
            obj_name = reads.name

        if not obj_name:
            raise ValueError("Object name must be provided")

        # Upload files to Shock
        self.log_info(f"Uploading reads '{obj_name}' to workspace {workspace}")

        shock_ids = {}
        handle_ids = {}

        # Upload forward reads
        if "fwd" in reads.files and reads.files["fwd"]:
            fwd_file = reads.files["fwd"]
            if not os.path.exists(fwd_file):
                raise FileNotFoundError(f"Forward reads file not found: {fwd_file}")
            shock_id, handle_id = self.upload_blob_file(fwd_file)
            shock_ids["fwd"] = shock_id
            handle_ids["fwd"] = handle_id

        # Upload reverse reads if paired
        if reads.read_type == "paired" and "rev" in reads.files and reads.files["rev"]:
            rev_file = reads.files["rev"]
            if not os.path.exists(rev_file):
                raise FileNotFoundError(f"Reverse reads file not found: {rev_file}")
            shock_id, handle_id = self.upload_blob_file(rev_file)
            shock_ids["rev"] = shock_id
            handle_ids["rev"] = handle_id

        # Build reads library object
        reads_obj = self._build_reads_object(
            reads=reads,
            shock_ids=shock_ids,
            handle_ids=handle_ids,
            sequencing_tech=sequencing_tech,
            insert_size_mean=insert_size_mean,
            insert_size_std_dev=insert_size_std_dev,
            read_orientation_outward=read_orientation_outward,
        )

        # Determine object type based on read type
        if reads.read_type == "single":
            obj_type = "KBaseFile.SingleEndLibrary"
        elif reads.read_type in ["paired", "interleaved"]:
            obj_type = "KBaseFile.PairedEndLibrary"
        else:
            raise ValueError(f"Unknown read type: {reads.read_type}")

        # Save to workspace
        result = self.save_ws_object(obj_name, workspace, reads_obj, obj_type)
        ws_ref = f"{result[0][6]}/{result[0][0]}/{result[0][4]}"

        self.log_info(f"Successfully uploaded reads to {ws_ref}")
        return ws_ref

    def download_reads(
        self,
        reads_ref: str,
        download_dir: str,
        unpack: bool = True,
    ) -> Reads:
        """Download a reads library from the KBase workspace.

        Args:
            reads_ref: Workspace reference to the reads object
            download_dir: Directory to download files to
            unpack: Whether to unpack gzipped files

        Returns:
            Reads object with local file paths
        """
        self.initialize_call("download_reads", {"reads_ref": reads_ref})

        self.log_info(f"Downloading reads from {reads_ref}")

        # Create download directory
        os.makedirs(download_dir, exist_ok=True)

        # Get reads object from workspace
        reads_obj_data = self.get_object(reads_ref)
        if not reads_obj_data:
            raise ValueError(f"Failed to retrieve reads object: {reads_ref}")

        reads_data = reads_obj_data["data"]
        reads_info = reads_obj_data["info"]
        obj_name = reads_info[1]

        # Determine read type
        obj_type = reads_info[2]
        if "SingleEndLibrary" in obj_type:
            read_type = "single"
        elif "PairedEndLibrary" in obj_type:
            if reads_data.get("interleaved"):
                read_type = "interleaved"
            else:
                read_type = "paired"
        else:
            raise ValueError(f"Unknown reads type: {obj_type}")

        # Extract file handles
        files = {}
        handle_ids = {}

        # Download forward reads - handle both "lib" and "lib1" formats
        # "lib" is the standard format, "lib1" is used by some older objects
        lib_key = (
            "lib" if "lib" in reads_data else "lib1" if "lib1" in reads_data else None
        )
        if lib_key and "file" in reads_data[lib_key]:
            fwd_handle = reads_data[lib_key]["file"]
            handle_ids["fwd"] = fwd_handle.get("hid")
            fwd_file = self._download_from_handle(
                fwd_handle, download_dir, f"{obj_name}_fwd.fastq"
            )
            if unpack and fwd_file.endswith(".gz"):
                fwd_file = self._unpack_file(fwd_file)
            files["fwd"] = fwd_file

        # Download reverse reads if paired
        if (
            read_type == "paired"
            and "lib2" in reads_data
            and "file" in reads_data["lib2"]
        ):
            rev_handle = reads_data["lib2"]["file"]
            handle_ids["rev"] = rev_handle.get("hid")
            rev_file = self._download_from_handle(
                rev_handle, download_dir, f"{obj_name}_rev.fastq"
            )
            if unpack and rev_file.endswith(".gz"):
                rev_file = self._unpack_file(rev_file)
            files["rev"] = rev_file

        # Extract metadata
        metadata = {
            "sequencing_tech": reads_data.get("sequencing_tech", "Unknown"),
            "read_count": reads_data.get("read_count", 0),
            "read_size": reads_data.get("read_size", 0),
            "gc_content": reads_data.get("gc_content", 0.0),
            "insert_size_mean": reads_data.get("insert_size_mean"),
            "insert_size_std_dev": reads_data.get("insert_size_std_dev"),
        }

        # Create Reads object
        reads = Reads(
            name=obj_name,
            read_type=read_type,
            files=files,
            metadata=metadata,
        )
        reads.workspace_ref = reads_ref
        reads.handle_ids = handle_ids

        self.log_info(f"Successfully downloaded reads to {download_dir}")
        return reads

    def bulk_upload_reads(
        self,
        readset: ReadSet,
        workspace: Union[str, int],
    ) -> Dict[str, str]:
        """Upload multiple reads libraries in bulk.

        Args:
            readset: ReadSet containing multiple Reads objects
            workspace: Workspace name or ID

        Returns:
            Dictionary mapping reads names to workspace references
        """
        self.log_info(f"Bulk uploading {len(readset.reads)} reads libraries")

        results = {}
        for name, reads in readset.reads.items():
            try:
                ws_ref = self.upload_reads(reads, workspace)
                results[name] = ws_ref
                self.log_info(f"Uploaded {name}: {ws_ref}")
            except Exception as e:
                self.log_error(f"Failed to upload {name}: {str(e)}")
                results[name] = None

        return results

    def bulk_download_reads(
        self,
        reads_refs: List[str],
        download_dir: str,
        unpack: bool = True,
    ) -> ReadSet:
        """Download multiple reads libraries in bulk.

        Args:
            reads_refs: List of workspace references
            download_dir: Directory to download files to
            unpack: Whether to unpack gzipped files

        Returns:
            ReadSet containing all downloaded reads
        """
        self.log_info(f"Bulk downloading {len(reads_refs)} reads libraries")

        readset = ReadSet(name="bulk_download")

        for reads_ref in reads_refs:
            try:
                reads = self.download_reads(reads_ref, download_dir, unpack)
                readset.add_reads(reads)
                self.log_info(f"Downloaded {reads.name}: {reads_ref}")
            except Exception as e:
                self.log_error(f"Failed to download {reads_ref}: {str(e)}")

        return readset

    def _download_from_handle(
        self, handle: Union[str, Dict], download_dir: str, filename: str
    ) -> str:
        """Download a file from a Shock handle.

        Args:
            handle: Handle ID or handle object
            download_dir: Directory to download to
            filename: Name for downloaded file

        Returns:
            Path to downloaded file
        """
        # Extract handle ID
        if isinstance(handle, dict):
            handle_id = handle.get("hid")
        else:
            handle_id = handle

        if not handle_id:
            raise ValueError("Invalid handle provided")

        # Download file using handle
        filepath = os.path.join(download_dir, filename)
        result = self.download_blob_file(handle_id, filepath)

        if result is None:
            raise RuntimeError(f"Failed to download file from handle: {handle_id}")

        return result

    def _unpack_file(self, filepath: str) -> str:
        """Unpack a gzipped file.

        Args:
            filepath: Path to gzipped file

        Returns:
            Path to unpacked file
        """
        if not filepath.endswith(".gz"):
            return filepath

        output_path = filepath[:-3]  # Remove .gz extension
        self.log_info(f"Unpacking {filepath} to {output_path}")

        with gzip.open(filepath, "rb") as f_in:
            with open(output_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

        # Remove the gzipped file
        os.remove(filepath)

        return output_path

    def _build_reads_object(
        self,
        reads: Reads,
        shock_ids: Dict[str, str],
        handle_ids: Dict[str, str],
        sequencing_tech: str,
        insert_size_mean: Optional[float] = None,
        insert_size_std_dev: Optional[float] = None,
        read_orientation_outward: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Build a KBase reads object structure for workspace.

        Args:
            reads: Reads object with metadata
            shock_ids: Dictionary of Shock IDs for files
            handle_ids: Dictionary of handle IDs for files
            sequencing_tech: Sequencing technology
            insert_size_mean: Mean insert size
            insert_size_std_dev: Standard deviation of insert size
            read_orientation_outward: Read orientation

        Returns:
            Dictionary representing KBase reads object
        """
        obj = {
            "sequencing_tech": sequencing_tech or reads.sequencing_tech,
            "single_genome": 1,
        }

        # Add forward reads handle
        if "fwd" in handle_ids:
            fwd_file_path = reads.files.get("fwd", "")
            fwd_file_size = (
                os.path.getsize(fwd_file_path)
                if fwd_file_path and os.path.exists(fwd_file_path)
                else 0
            )
            obj["lib"] = {
                "file": {
                    "hid": handle_ids["fwd"],
                    "id": shock_ids.get("fwd", ""),
                    "file_name": os.path.basename(fwd_file_path),
                    "type": "shock",
                    "url": self.shock_url,
                },
                "encoding": "ascii",
                "type": "fq",
                "size": fwd_file_size,
            }

        # Add reverse reads handle for paired-end
        if reads.read_type == "paired" and "rev" in handle_ids:
            rev_file_path = reads.files.get("rev", "")
            rev_file_size = (
                os.path.getsize(rev_file_path)
                if rev_file_path and os.path.exists(rev_file_path)
                else 0
            )
            obj["lib2"] = {
                "file": {
                    "hid": handle_ids["rev"],
                    "id": shock_ids.get("rev", ""),
                    "file_name": os.path.basename(rev_file_path),
                    "type": "shock",
                    "url": self.shock_url,
                },
                "encoding": "ascii",
                "type": "fq",
                "size": rev_file_size,
            }

            # Add paired-end specific metadata
            if insert_size_mean is not None:
                obj["insert_size_mean"] = insert_size_mean
            if insert_size_std_dev is not None:
                obj["insert_size_std_dev"] = insert_size_std_dev
            if read_orientation_outward is not None:
                obj["read_orientation_outward"] = 1 if read_orientation_outward else 0

        # Add interleaved flag
        if reads.read_type == "interleaved":
            obj["interleaved"] = 1

        # Add additional metadata from reads object
        if reads.read_count:
            obj["read_count"] = reads.read_count
        if reads.read_size:
            obj["read_size"] = reads.read_size
        if reads.gc_content:
            obj["gc_content"] = reads.gc_content

        # Add strain and source info if available
        if "strain" in reads.metadata:
            obj["strain"] = reads.metadata["strain"]
        if "source" in reads.metadata:
            obj["source"] = reads.metadata["source"]

        return obj

    def download_assembly(
        self, assembly_refs: List[str], output_dir: str
    ) -> AssemblySet:
        """Download Assembly or AssemblySet objects from KBase workspace.

        Args:
            assembly_refs: List of assembly or assemblyset workspace references
            output_dir: Directory to save FASTA files and metadata JSON
            workspace_name: Optional workspace name (uses default if not provided)

        Returns:
            AssemblySet object containing all downloaded assemblies

        Example:
            >>> util = KBReadsUtils(token="your_token")
            >>> assemblies = util.download_assembly(
            ...     ["12345/6/1", "12345/7/1"],
            ...     "./assemblies"
            ... )
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        assembly_set = AssemblySet(
            name="downloaded_assemblies", description=f"Downloaded from {assembly_refs}"
        )

        for ref in assembly_refs:
            try:
                # Get object info to determine type
                obj_info = self._ws_client.get_object_info3(
                    {"objects": [{"ref": ref}]}
                )["infos"][0]
                obj_type = obj_info[2]

                if "Assembly" in obj_type and "AssemblySet" not in obj_type:
                    # Single Assembly object
                    assembly = self._download_single_assembly(ref, output_path)
                    if assembly:
                        assembly_set.add_assembly(assembly)

                elif "AssemblySet" in obj_type:
                    # AssemblySet object - download all assemblies in set
                    obj_data = self._ws_client.get_objects2(
                        {"objects": [{"ref": ref}]}
                    )["data"][0]["data"]

                    if "items" in obj_data:
                        for item in obj_data["items"]:
                            assembly_ref = item.get("ref")
                            if assembly_ref:
                                assembly = self._download_single_assembly(
                                    assembly_ref, output_path
                                )
                                if assembly:
                                    assembly_set.add_assembly(assembly)

                else:
                    self.log_warning(
                        f"Object {ref} is not an Assembly or AssemblySet: {obj_type}"
                    )

            except Exception as e:
                self.log_error(f"Failed to download {ref}: {e}")
                continue

        # Save metadata JSON for all assemblies
        metadata_file = output_path / "assemblies_metadata.json"
        with open(metadata_file, "w") as f:
            json.dump(assembly_set.to_dict(), f, indent=2)

        self.log_info(
            f"Downloaded {len(assembly_set.assemblies)} assemblies to {output_dir}"
        )

        return assembly_set

    def _download_single_assembly(
        self, assembly_ref: str, output_path: Path
    ) -> Optional[Assembly]:
        """Download a single Assembly object.

        Args:
            assembly_ref: Workspace reference to assembly
            output_path: Directory to save files

        Returns:
            Assembly object or None if download failed
        """
        try:
            # Get assembly object data
            print("Reference: ", assembly_ref)
            obj_data = self._ws_client.get_objects2(
                {"objects": [{"ref": assembly_ref}]}
            )["data"][0]

            data = obj_data["data"]
            info = obj_data["info"]

            # Extract assembly metadata
            assembly_id = data.get("assembly_id", info[1])

            # Create Assembly object
            assembly = Assembly(
                name=assembly_id,
                fasta_file=None,  # Will be set after download
                metadata={
                    "assembly_id": assembly_id,
                    "num_contigs": data.get("num_contigs", 0),
                    "dna_size": data.get("dna_size", 0),
                    "gc_content": data.get("gc_content", 0.0),
                    "taxon_ref": data.get("taxon_ref"),
                    "type": data.get("type", "Unknown"),
                    "external_source": data.get("external_source", "User"),
                    "external_source_id": data.get("external_source_id"),
                },
            )
            assembly.workspace_ref = assembly_ref

            # Download FASTA file from Shock via handle
            if "fasta_handle_ref" in data:
                fasta_handle = data["fasta_handle_ref"]

                # Download using handle (same pattern as reads download)
                fasta_filename = f"{assembly_id}.fasta"
                try:
                    fasta_file = self._download_from_handle(
                        fasta_handle, str(output_path), fasta_filename
                    )

                    assembly.fasta_file = fasta_file
                    assembly.handle_id = (
                        fasta_handle
                        if isinstance(fasta_handle, str)
                        else fasta_handle.get("hid")
                    )

                    self.log_info(f"Downloaded assembly {assembly_id} to {fasta_file}")
                except Exception as e:
                    self.log_error(f"Failed to download FASTA for {assembly_id}: {e}")

            return assembly

        except Exception as e:
            self.log_error(f"Failed to download assembly {assembly_ref}: {e}")
            return None

    def upload_assembly(
        self,
        input_paths: List[str],
        workspace_name: Optional[str] = None,
        assembly_id_map: Optional[Dict[str, str]] = None,
        assemblyset_id: Optional[str] = None,
        taxon_ref: Optional[str] = None,
        assembly_type: str = "Unknown",
    ) -> Dict[str, Any]:
        """Upload Assembly objects to KBase workspace.

        Args:
            input_paths: List of FASTA files or directories containing FASTA files
            workspace_name: Workspace to upload to (uses default if not provided)
            assembly_id_map: Optional dict mapping filenames to desired assembly IDs
            assemblyset_id: Optional ID for AssemblySet to create with all assemblies
            taxon_ref: Optional taxon reference for all assemblies
            assembly_type: Assembly type (default: "Unknown")

        Returns:
            Dict with upload results:
            {
                "assemblies": [list of uploaded assembly references],
                "assemblyset_ref": "ref" (if assemblyset_id provided)
            }

        Example:
            >>> util = KBReadsUtils(token="your_token")
            >>> result = util.upload_assembly(
            ...     ["./genomes/", "./extra_genome.fasta"],
            ...     assembly_id_map={"genome1.fasta": "MyGenome1"},
            ...     assemblyset_id="MyAssemblySet"
            ... )
        """
        if workspace_name is None:
            workspace_name = self._workspace

        assembly_id_map = assembly_id_map or {}

        # Collect all FASTA files
        fasta_files = []
        fasta_extensions = {".fasta", ".fa", ".fna"}

        for path_str in input_paths:
            path = Path(path_str)

            if path.is_file():
                if path.suffix.lower() in fasta_extensions:
                    fasta_files.append(path)
                else:
                    self.log_warning(
                        f"Skipping {path}: not a recognized FASTA extension"
                    )

            elif path.is_dir():
                # Scan directory for FASTA files
                for fasta_file in path.iterdir():
                    if (
                        fasta_file.is_file()
                        and fasta_file.suffix.lower() in fasta_extensions
                    ):
                        fasta_files.append(fasta_file)
            else:
                self.log_warning(f"Path not found: {path_str}")

        if not fasta_files:
            self.log_error("No FASTA files found in provided paths")
            return {"assemblies": [], "assemblyset_ref": None}

        self.log_info(f"Found {len(fasta_files)} FASTA files to upload")

        # Upload each assembly
        uploaded_refs = []

        for fasta_path in fasta_files:
            try:
                # Determine assembly ID
                filename = fasta_path.name
                if filename in assembly_id_map:
                    assembly_id = assembly_id_map[filename]
                else:
                    # Use filename without extension as ID
                    assembly_id = fasta_path.stem

                # Upload assembly
                assembly_ref = self._upload_single_assembly(
                    fasta_path=fasta_path,
                    assembly_id=assembly_id,
                    workspace_name=workspace_name,
                    taxon_ref=taxon_ref,
                    assembly_type=assembly_type,
                )

                if assembly_ref:
                    uploaded_refs.append(assembly_ref)
                    self.log_info(
                        f"Uploaded {filename} as {assembly_id}: {assembly_ref}"
                    )

            except Exception as e:
                self.log_error(f"Failed to upload {fasta_path}: {e}")
                continue

        result = {"assemblies": uploaded_refs, "assemblyset_ref": None}

        # Create AssemblySet if requested
        if assemblyset_id and uploaded_refs:
            try:
                assemblyset_ref = self._create_assemblyset(
                    assemblyset_id=assemblyset_id,
                    assembly_refs=uploaded_refs,
                    workspace_name=workspace_name,
                )
                result["assemblyset_ref"] = assemblyset_ref
                self.log_info(
                    f"Created AssemblySet {assemblyset_id}: {assemblyset_ref}"
                )
            except Exception as e:
                self.log_error(f"Failed to create AssemblySet: {e}")

        return result

    def _upload_single_assembly(
        self,
        fasta_path: Path,
        assembly_id: str,
        workspace_name: str,
        taxon_ref: Optional[str] = None,
        assembly_type: str = "Unknown",
    ) -> Optional[str]:
        """Upload a single assembly to KBase workspace.

        Args:
            fasta_path: Path to FASTA file
            assembly_id: Desired assembly ID in workspace
            workspace_name: Workspace name
            taxon_ref: Optional taxon reference
            assembly_type: Assembly type

        Returns:
            Workspace reference to uploaded assembly, or None if failed
        """
        try:
            # Upload FASTA file to Shock
            shock_id = self._upload_to_shock(str(fasta_path))

            if not shock_id:
                self.log_error(f"Failed to upload {fasta_path} to Shock")
                return None

            # Create handle for Shock file
            handle_id = self._create_handle(shock_id, fasta_path.name)

            if not handle_id:
                self.log_error(f"Failed to create handle for {fasta_path}")
                return None

            # Parse FASTA to get assembly statistics
            num_contigs = 0
            dna_size = 0
            gc_count = 0

            with open(fasta_path, "r") as f:
                sequence = ""
                for line in f:
                    line = line.strip()
                    if line.startswith(">"):
                        if sequence:
                            # Process previous sequence
                            dna_size += len(sequence)
                            gc_count += sequence.upper().count(
                                "G"
                            ) + sequence.upper().count("C")
                            sequence = ""
                        num_contigs += 1
                    else:
                        sequence += line

                # Process last sequence
                if sequence:
                    dna_size += len(sequence)
                    gc_count += sequence.upper().count("G") + sequence.upper().count(
                        "C"
                    )

            gc_content = (gc_count / dna_size * 100) if dna_size > 0 else 0.0

            # Create assembly object
            assembly_obj = {
                "assembly_id": assembly_id,
                "fasta_handle_ref": handle_id,
                "num_contigs": num_contigs,
                "dna_size": dna_size,
                "gc_content": gc_content,
                "type": assembly_type,
                "external_source": "User upload",
            }

            if taxon_ref:
                assembly_obj["taxon_ref"] = taxon_ref

            # Save to workspace
            save_result = self._ws_client.save_objects(
                {
                    "workspace": workspace_name,
                    "objects": [
                        {
                            "type": "KBaseGenomeAnnotations.Assembly",
                            "data": assembly_obj,
                            "name": assembly_id,
                        }
                    ],
                }
            )

            obj_info = save_result[0]
            assembly_ref = f"{obj_info[6]}/{obj_info[0]}/{obj_info[4]}"

            return assembly_ref

        except Exception as e:
            self.log_error(f"Failed to upload assembly {assembly_id}: {e}")
            return None

    def _create_assemblyset(
        self, assemblyset_id: str, assembly_refs: List[str], workspace_name: str
    ) -> Optional[str]:
        """Create an AssemblySet object containing multiple assemblies.

        Args:
            assemblyset_id: Desired AssemblySet ID
            assembly_refs: List of assembly workspace references
            workspace_name: Workspace name

        Returns:
            Workspace reference to created AssemblySet, or None if failed
        """
        try:
            # Build AssemblySet data structure
            items = []
            for ref in assembly_refs:
                # Get assembly info
                obj_info = self._ws_client.get_object_info3(
                    {"objects": [{"ref": ref}]}
                )["infos"][0]

                items.append({"ref": ref, "label": obj_info[1]})  # Object name

            assemblyset_obj = {
                "description": f"Assembly set containing {len(items)} assemblies",
                "items": items,
            }

            # Save to workspace
            save_result = self._ws_client.save_objects(
                {
                    "workspace": workspace_name,
                    "objects": [
                        {
                            "type": "KBaseSets.AssemblySet",
                            "data": assemblyset_obj,
                            "name": assemblyset_id,
                        }
                    ],
                }
            )

            obj_info = save_result[0]
            assemblyset_ref = f"{obj_info[6]}/{obj_info[0]}/{obj_info[4]}"

            return assemblyset_ref

        except Exception as e:
            self.log_error(f"Failed to create AssemblySet: {e}")
            return None
