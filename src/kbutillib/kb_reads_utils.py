"""KBase Reads utilities for bulk upload and download of reads data.

This module provides utilities for working with KBase reads objects,
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
        super().__init__(name="KBReadsUtils", **kwargs)

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
            shock_id, handle_id = self._upload_file_to_shock(fwd_file)
            shock_ids["fwd"] = shock_id
            handle_ids["fwd"] = handle_id

        # Upload reverse reads if paired
        if reads.read_type == "paired" and "rev" in reads.files and reads.files["rev"]:
            rev_file = reads.files["rev"]
            if not os.path.exists(rev_file):
                raise FileNotFoundError(f"Reverse reads file not found: {rev_file}")
            shock_id, handle_id = self._upload_file_to_shock(rev_file)
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
        lib_key = "lib" if "lib" in reads_data else "lib1" if "lib1" in reads_data else None
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
        if read_type == "paired" and "lib2" in reads_data and "file" in reads_data["lib2"]:
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

    def _upload_file_to_shock(self, filepath: str) -> tuple:
        """Upload a file to Shock and get handle.

        Args:
            filepath: Path to file to upload

        Returns:
            Tuple of (shock_id, handle_id)
        """
        self.log_info(f"Uploading file to Shock: {filepath}")

        # Upload to Shock
        headers = {"Authorization": "OAuth " + self.get_token(namespace="kbase")}

        # Get file size for Content-Length
        file_size = os.path.getsize(filepath)
        filename = os.path.basename(filepath)

        with open(filepath, "rb") as f:
            # Use multipart form with file and explicit content-type/size
            # The tuple format is (filename, fileobj, content_type, headers)
            files = {
                "upload": (
                    filename,
                    f,
                    "application/octet-stream",
                    {"Content-Length": str(file_size)}
                )
            }

            r = requests.post(
                self.shock_url + "/node",
                headers=headers,
                files=files,
                allow_redirects=True,
            )

            if not r.ok:
                error_msg = r.text
                try:
                    error_data = r.json()
                    error_msg = error_data.get("error", [r.text])[0]
                except:
                    pass
                raise RuntimeError(f"Failed to upload file to Shock: {error_msg}")

            shock_node = r.json()["data"]
            shock_id = shock_node["id"]

        # Create handle
        hs = self.hs_client
        handle = hs.persist_handle(
            {
                "id": shock_id,
                "type": "shock",
                "url": self.shock_url,
            }
        )
        handle_id = handle

        self.log_info(f"File uploaded to Shock: {shock_id}, Handle: {handle_id}")
        return shock_id, handle_id

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
            fwd_file_size = os.path.getsize(fwd_file_path) if fwd_file_path and os.path.exists(fwd_file_path) else 0
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
            rev_file_size = os.path.getsize(rev_file_path) if rev_file_path and os.path.exists(rev_file_path) else 0
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
