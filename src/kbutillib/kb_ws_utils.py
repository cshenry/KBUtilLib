"""KBase API utilities for interacting with KBase services and data."""

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Union

import requests

from .installed_clients.AbstractHandleClient import AbstractHandle as HandleService
from .installed_clients.WorkspaceClient import Workspace
from .shared_env_utils import SharedEnvUtils


class KBWSUtils(SharedEnvUtils):
    """Utilities for interacting with KBase (Department of Energy Systems Biology
    Knowledgebase) APIs and services.

    Provides methods for authentication, data retrieval, workspace operations,
    type discovery, and other KBase-specific functionality.

    Key features:
    - Workspace object retrieval and storage
    - Type discovery and specification retrieval
    - Handle service integration for file downloads
    - Provenance tracking and logging
    """

    def __init__(
        self, kb_version: Optional[str] = "prod", max_retry: int = 3,  kbendpoint: Optional[str] = None, **kwargs: Any
    ) -> None:
        """Initialize KBase Workspace utilities."""
        super().__init__(**kwargs)
        self.kb_version = kb_version
        #Overrides KBVersion based on kbendpoint, which is generally how SDK modules are configured
        if kbendpoint:
            from urllib.parse import urlparse
            hostname = urlparse(kbendpoint).hostname or ""
            if hostname.startswith("ci."):
                kb_version = "ci"
            elif hostname.startswith("appdev."):
                kb_version = "appdev"
            else:
                kb_version = "prod"
            self.kb_version = kb_version
        base_url = self.get_base_url_from_version(kb_version)
        self.workspace_url = f"{base_url}/ws"
        self.shock_url = f"{base_url}/shock-api"
        self.hs_url = f"{base_url}/handle_service"
        self.cached_to_obj_path = {}
        self._ws_client = Workspace(
            self.workspace_url, token=self.get_token(namespace="kbase")
        )
        self.max_retry = max_retry
        self.hs_client = HandleService(
            self.hs_url, token=self.get_token(namespace="kbase")
        )
        self.ws_id = None
        self.ws_name = None
        self.method = None
        self.input_objects = None
        self.params = None
        self.service = None
        self.version = None
        self.description = None

    def reset_attributes(self):
        """Resetting workspace elements related to a new method call."""
        super().reset_attributes()
        self.ws_id = None
        self.ws_name = None

    def initialize_call(
        self,
        method: str,
        params: Dict[str, Any],
        print_params: bool = False,
        no_print: List[str] = None,
        no_prov_params: List[str] = None,
    ) -> None:
        """Initialize workspace elements related to a new method call."""
        if not self.initialized:
            # Set workspace if provided
            if "workspace" in params:
                self.set_ws(params["workspace"])
            elif "output_workspace" in params:
                self.set_ws(params["output_workspace"])
        super().initialize_call(method, params, print_params, no_print, no_prov_params)

    def ws_client(self) -> Workspace:
        """Get the Workspace client.

        Returns:
            Workspace client instance
        """
        return self._ws_client

    def set_ws(self, workspace):
        if self.ws_id == workspace or self.ws_name == workspace:
            return
        if not isinstance(workspace, str) or re.search("^\\d+$", workspace) != None:
            if isinstance(workspace, str):
                workspace = int(workspace)
            self.ws_id = workspace
            info = self.ws_client().get_workspace_info({"id": workspace})
            self.ws_name = info[1]
        else:
            self.ws_name = workspace
            info = self.ws_client().get_workspace_info({"workspace": workspace})
            self.ws_id = info[0]

    def get_base_url_from_version(self, version):
        if version == "prod":
            return "https://kbase.us/services"
        elif version == "appdev":
            return "https://appdev.kbase.us/services"
        elif version == "ci":
            return "https://ci.kbase.us/services"
        else:
            self.log_critical("Unknown workspace version: " + version)
            return "https://kbase.us/services"

    def download_blob_file(self, handle_id, file_path):
        headers = {"Authorization": "OAuth " + self.get_token(namespace="kbase")}
        hs = self.hs_client
        handles = hs.hids_to_handles([handle_id])
        shock_id = handles[0]["id"]
        node_url = self.shock_url + "/node/" + shock_id
        r = requests.get(node_url, headers=headers, allow_redirects=True)
        errtxt = ("Error downloading file from shock " + "node {}: ").format(shock_id)
        if not r.ok:
            print(json.loads(r.content)["error"][0])
            return None
        resp_obj = r.json()
        size = resp_obj["data"]["file"]["size"]
        if not size:
            print(f"Node {shock_id} has no file")
            return None
        node_file_name = resp_obj["data"]["file"]["name"]
        attributes = resp_obj["data"]["attributes"]
        # Making the directory if it doesn't exist
        dir = os.path.dirname(file_path)
        os.makedirs(dir, exist_ok=True)
        # Adding filename to the end of the directory
        if os.path.isdir(file_path):
            file_path = os.path.join(file_path, node_file_name)
        with open(file_path, "wb") as fhandle:
            with requests.get(
                node_url + "?download_raw",
                stream=True,
                headers=headers,
                allow_redirects=True,
            ) as r:
                if not r.ok:
                    print(json.loads(r.content)["error"][0])
                    return None
                for chunk in r.iter_content(1024):
                    if not chunk:
                        break
                    fhandle.write(chunk)
        return file_path

    def upload_blob_file(self, filepath):
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
                    {"Content-Length": str(file_size)},
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

    def save_ws_object(self, objid, workspace, obj_json, obj_type):
        self.set_ws(workspace)
        params = {
            "id": self.ws_id,
            "objects": [
                {
                    "data": obj_json,
                    "name": objid,
                    "type": obj_type,
                    "meta": {},
                    "provenance": []#self.get_provenance(),
                }
            ],
        }
        self.obj_created.append(
            {"ref": self.create_ref(objid, self.ws_name), "description": ""}
        )
        return self.ws_client().save_objects(params)

    def set_provenance(self,method="unknown",description=None,input_objects=[],params={},service="unknown",version=0):
        self.method = method
        self.input_objects = input_objects
        self.params = params
        self.service = service
        self.version = version
        if description:
            self.description = description
        else:
            self.description = method

    def get_provenance(self) -> List[Dict[str, Any]]:
        """Generate provenance information for workspace operations.

        Returns:
            Provenance data list
        """
        return [
            {
                "description": self.description,
                "input_ws_objects": self.input_objects,
                "method": self.method,
                "script_command_line": "",
                "method_params": [self.params],
                "service": self.name,
                "service_ver": self.version,
            }
        ]

    def list_ws_objects(self, wsid_or_ref, type=None, include_metadata=True):
        """List objects in a workspace"""
        ws_client = self.ws_client()
        done = False
        skip = 0
        full_output = {}
        start_after = None
        while not done:
            input = {}
            if type:
                input["type"] = type
            if include_metadata:
                input["includeMetadata"] = 1
            else:
                input["includeMetadata"] = 0
            if isinstance(wsid_or_ref, int):
                input["ids"] = [wsid_or_ref]
                wsid_or_ref = str(wsid_or_ref)
            else:
                input["workspaces"] = [wsid_or_ref]

            if start_after:
                input["startafter"] = start_after
            output = ws_client.list_objects(input)
            start_after = wsid_or_ref + "/" + str(output[-1][0])
            for item in output:
                full_output[item[1]] = item
            if len(output) < 5000:
                done = True
        return full_output

    def process_ws_ids(self, id_or_ref, workspace=None, no_ref=False):
        """IDs should always be processed through this function so we can interchangeably use
        refs, IDs, and names for workspaces and objects
        """
        objspec = {}
        if len(id_or_ref.split(";")) > 1:
            objspec["to_obj_ref_path"] = id_or_ref.split(";")[0:-1]
            id_or_ref = id_or_ref.split(";")[-1]

        if len(id_or_ref.split("/")) > 1:
            if no_ref:
                array = id_or_ref.split("/")
                workspace = array[0]
                id_or_ref = array[1]
            else:
                objspec["ref"] = id_or_ref

        if "ref" not in objspec:
            if isinstance(workspace, int):
                objspec["wsid"] = workspace
            else:
                objspec["workspace"] = workspace
            if isinstance(id_or_ref, int):
                objspec["objid"] = id_or_ref
            else:
                objspec["name"] = id_or_ref
        return objspec

    def get_object_info(self, id_or_ref, ws=None):
        ws_identities = [self.process_ws_ids(id_or_ref, ws)]
        return self.ws_client().get_object_info(ws_identities, 1)[0]

    def get_object(self, id_or_ref, ws=None):
        res = self.ws_get_objects({"objects": [self.process_ws_ids(id_or_ref, ws)]})
        if res is None:
            return None
        return res["data"][0]

    def ws_get_objects(self, args):
        """All functions calling get_objects2 should call this function to ensure they get the retry
        code because workspace periodically times out
        :param args:
        :return:
        """
        tries = 0
        while tries < self.max_retry:
            try:
                return self.ws_client().get_objects2(args)
            except Exception as e:
                self.log_warning(
                    "Workspace get_objects2 call failed [%s:%s - %s]. Trying again! Error: %s"
                    % (sys.exc_info()[0], sys.exc_info()[1], sys.exc_info()[2], str(e))
                )
                tries += 1
                time.sleep(10)  # Give half second
        self.log_warning(
            "get_objects2 failed after multiple tries: %s" % sys.exc_info()[0]
        )
        raise

    def wsinfo_to_ref(self, info):
        return str(info[6]) + "/" + str(info[0]) + "/" + str(info[4])

    def create_ref(self, id_or_ref, ws=None):
        if isinstance(id_or_ref, int):
            id_or_ref = str(id_or_ref)
        if len(id_or_ref.split("/")) > 1:
            return id_or_ref
        if isinstance(ws, int):
            ws = str(ws)
        return ws + "/" + id_or_ref

    def is_ref(self, ref_string: str) -> bool:
        """Check if a string is a valid KBase workspace reference.

        A valid KBase reference has one of the following formats:
        - wsid/objid (e.g., "123/456")
        - wsid/objid/version (e.g., "123/456/1")
        - wsname/objname (e.g., "MyWorkspace/MyObject")
        - wsname/objname/version (e.g., "MyWorkspace/MyObject/1")

        Args:
            ref_string: String to check

        Returns:
            bool: True if the string appears to be a valid KBase reference
        """
        if not isinstance(ref_string, str):
            return False

        parts = ref_string.split("/")

        # Must have 2 or 3 parts (ws/obj or ws/obj/ver)
        if len(parts) < 2 or len(parts) > 3:
            return False

        # Each part must be non-empty
        if any(not part.strip() for part in parts):
            return False

        # If 3 parts, the version (last part) should be numeric
        if len(parts) == 3:
            try:
                int(parts[2])
            except ValueError:
                return False

        return True

    def list_all_types(
        self, include_empty_modules: bool = False, track_provenance: bool = False
    ) -> List[str]:
        """List all released types from all modules in the KBase Workspace.

        Retrieves a complete list of all available datatypes in the KBase type system,
        returning them as a flat list of type strings in the format "Module.TypeName".

        Args:
            include_empty_modules (bool): If True, include modules with no released types.
                                          Defaults to False.
            track_provenance (bool): If True, track this operation in provenance.
                                    Defaults to False.

        Returns:
            list: List of type strings (e.g., ['KBaseGenomes.Genome',
                  'KBaseGenomeAnnotations.Assembly', 'KBaseFBA.FBAModel', ...])

        Raises:
            Exception: If workspace API call fails or authentication is invalid

        Example:
            >>> utils = KBWSUtils()
            >>> all_types = utils.list_all_types()
            >>> print(f"Found {len(all_types)} types")
            >>> # Filter for genome-related types
            >>> genome_types = [t for t in all_types if 'Genome' in t]
        """
        if track_provenance:
            self.initialize_call(
                "list_all_types", {"include_empty_modules": include_empty_modules}
            )

        self.logger.info(
            f"Retrieving all types from workspace (include_empty_modules={include_empty_modules})"
        )
        try:
            # Call the workspace API to retrieve all types
            # Convert boolean to integer (0 or 1) as the API expects a Long type
            result = self._ws_client.list_all_types(
                {"with_empty_modules": 1 if include_empty_modules else 0}
            )
            self.logger.debug(f"Workspace API returned {len(result)} modules")

            # Transform nested dict to flat list of type strings
            type_list = []
            for module, types in result.items():
                for typename, version in types.items():
                    type_list.append(f"{module}.{typename}")

            self.logger.info(f"Successfully retrieved {len(type_list)} types")
            return type_list
        except Exception as e:
            raise Exception(f"Failed to list all types from workspace: {str(e)}")

    def get_type_specs(
        self, type_list: List[str], track_provenance: bool = False
    ) -> Dict[str, Any]:
        """Retrieve detailed specifications for specific datatypes.

        Fetches complete type information including JSON schemas, descriptions,
        and metadata for a list of KBase datatypes.

        Args:
            type_list (list): List of type strings to retrieve specs for.
                             Can be with or without versions (e.g., ['KBaseGenomes.Genome',
                             'KBaseGenomeAnnotations.Assembly-5.0'])
            track_provenance (bool): If True, track this operation in provenance.
                                    Defaults to False.

        Returns:
            dict: Dictionary mapping type strings to their full specifications.
                  Each specification includes:
                  - type_def: Type definition string
                  - description: Type description
                  - spec_def: KIDL specification text
                  - json_schema: JSON schema for validation
                  - module_vers: List of module versions
                  - type_vers: List of type versions
                  - Other metadata from get_type_info

        Raises:
            ValueError: If type_list is empty or not a list
            Exception: If any type doesn't exist or API call fails

        Example:
            >>> utils = KBWSUtils()
            >>> specs = utils.get_type_specs(['KBaseGenomes.Genome', 'KBaseFBA.FBAModel'])
            >>> genome_spec = specs['KBaseGenomes.Genome']
            >>> print(genome_spec['description'])
            >>> print(genome_spec['json_schema'])
        """
        # Validate input parameters
        if not isinstance(type_list, list):
            raise ValueError("type_list must be a list")
        if len(type_list) == 0:
            raise ValueError("type_list cannot be empty")

        if track_provenance:
            self.initialize_call("get_type_specs", {"type_list": type_list})

        self.logger.info(f"Retrieving type specifications for {len(type_list)} types")

        # Initialize empty result dictionary
        specs = {}

        try:
            # Implement loop: for each type_string, call get_type_info and store in specs dict
            for type_string in type_list:
                try:
                    self.logger.debug(f"Retrieving spec for type: {type_string}")
                    type_info = self._ws_client.get_type_info(type_string)
                    specs[type_string] = type_info
                    self.logger.debug(f"Successfully retrieved spec for: {type_string}")
                except Exception as e:
                    # Provide clear error message indicating which type failed
                    raise Exception(
                        f"Failed to retrieve spec for type '{type_string}': {str(e)}"
                    )

            self.logger.info(f"Successfully retrieved {len(specs)} type specifications")
            return specs
        except Exception as e:
            if "Failed to retrieve spec for type" in str(e):
                # Re-raise the specific error from the loop
                raise
            else:
                # Wrap other errors with context
                raise Exception(f"Failed to retrieve type specifications: {str(e)}")

    def object_url(self, id_or_ref, ws=None):
        """Get the data viewer URL for a KBase object.

        Args:
            id_or_ref: Object ID or reference
            ws: Workspace ID (optional)

        Returns:
            Object URL
        """
        if ws != None:
            return f"https://narrative.kbase.us/legacy/dataview/{ws}/{id_or_ref}"
        else:
            return f"https://narrative.kbase.us/legacy/dataview/{id_or_ref}"

    # Function to register a typespec (requires admin permissions)
    def register_typespec_dryrun(self, typespec, new_types, dryrun=True):
        """Register a typespec module with the workspace.

        Args:
            ws_client: Workspace client
            typespec: KIDL typespec string
            new_types: List of type names to make available
            dryrun: If True, only test compilation without saving

        Returns:
            JSON schemas for the types if successful
        """
        print("Note: Registering new typespecs requires:")
        print("  1. Module ownership (request via request_module_ownership)")
        print("  2. Admin approval for new modules")
        print("  3. Valid KIDL syntax")
        params = {
            "spec": typespec,
            "new_types": new_types,
            "dryrun": 1 if dryrun else 0,
        }

        try:
            result = self._ws_client.register_typespec(params)
            return result
        except Exception as e:
            return {"error": str(e)}

    def request_module_ownership(self, module_name):
        """Request ownership of a module name.

        Args:
            ws_client: Workspace client
            module_name: Name of the module to request

        Returns:
            Success status
        """
        try:
            self._ws_client.request_module_ownership(module_name)
            return {
                "status": "success",
                "message": f"Requested ownership of {module_name}",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def list_module_versions(self, module_name):
        """List all versions of a module.

        Args:
            ws_client: Workspace client
            module_name: Module name

        Returns:
            Module version information
        """
        try:
            result = self._ws_client.list_module_versions({"mod": module_name})
            return result
        except Exception as e:
            return {"error": str(e)}

    def get_module_info(self, module_name, version=None):
        """Get detailed information about a module.

        Args:
            ws_client: Workspace client
            module_name: Module name
            version: Optional specific version

        Returns:
            Module information including spec and types
        """
        params = {"mod": module_name}
        if version:
            params["ver"] = version

        try:
            result = self._ws_client.get_module_info(params)
            return result
        except Exception as e:
            return {"error": str(e)}

    def release_module(self, module_name):
        """Release a module for general use.

        Args:
            ws_client: Workspace client
            module_name: Module name to release

        Returns:
            List of released types
        """
        try:
            result = self._ws_client.release_module(module_name)
            return {"status": "success", "released_types": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}
