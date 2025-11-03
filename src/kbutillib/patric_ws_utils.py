"""PATRIC/BV-BRC Workspace utilities for managing model-related objects.

This module provides utilities for interacting with the PATRIC/BV-BRC workspace service,
specifically for saving and loading model-related objects. It is a Python port of the
workspace functionality from ProbModelSEED (Perl).
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Union
import requests
from datetime import datetime

from .shared_env_utils import SharedEnvUtils


class PatricWSClient:
    """Client for PATRIC/BV-BRC Workspace JSON-RPC API.

    This client provides low-level access to the workspace service using JSON-RPC.
    It handles authentication, request formatting, and response parsing.
    """

    def __init__(self, url: str, token: Optional[str] = None):
        """Initialize the workspace client.

        Args:
            url: Workspace service URL
            token: Authentication token
        """
        self.url = url
        self.token = token
        self.timeout = 60 * 30  # 30 minutes

    def _call_method(self, method: str, params: List[Any]) -> Any:
        """Call a workspace method via JSON-RPC.

        Args:
            method: Method name to call
            params: List of parameters for the method

        Returns:
            The result from the workspace service

        Raises:
            Exception: If the RPC call fails
        """
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = self.token

        payload = {
            'version': '1.1',
            'method': f'Workspace.{method}',
            'params': params,
            'id': str(os.getpid())
        }

        try:
            response = requests.post(
                self.url,
                data=json.dumps(payload),
                headers=headers,
                timeout=self.timeout
            )
            response.raise_for_status()

            result = response.json()

            if 'error' in result:
                raise Exception(f"Workspace error: {result['error']}")

            return result.get('result')

        except requests.exceptions.RequestException as e:
            raise Exception(f"Failed to call {method}: {str(e)}")

    def create(self, objects: List[Dict[str, Any]], overwrite: bool = False,
               createUploadNodes: bool = False) -> List[Dict[str, Any]]:
        """Create objects in the workspace.

        Args:
            objects: List of object specifications with 'objects', 'path', 'type', etc.
            overwrite: Whether to overwrite existing objects
            createUploadNodes: Whether to create upload nodes

        Returns:
            List of metadata for created objects
        """
        params = {
            'objects': objects,
            'overwrite': 1 if overwrite else 0,
            'createUploadNodes': 1 if createUploadNodes else 0
        }
        return self._call_method('create', [params])

    def get(self, objects: List[str], metadata_only: bool = False) -> List[Dict[str, Any]]:
        """Retrieve objects from the workspace.

        Args:
            objects: List of object paths to retrieve
            metadata_only: If True, only return metadata

        Returns:
            List of objects with data and metadata
        """
        params = {
            'objects': objects,
            'metadata_only': 1 if metadata_only else 0
        }
        return self._call_method('get', [params])

    def ls(self, paths: List[str], recursive: bool = False,
           query: Optional[Dict[str, Any]] = None) -> Dict[str, List[Any]]:
        """List objects in workspace directories.

        Args:
            paths: List of directory paths to list
            recursive: Whether to list recursively
            query: Optional query parameters for filtering

        Returns:
            Dictionary mapping paths to lists of objects
        """
        params = {
            'paths': paths,
            'recursive': 1 if recursive else 0
        }
        if query:
            params['query'] = query
        return self._call_method('ls', [params])

    def copy(self, objects: List[Dict[str, str]], overwrite: bool = False,
             move: bool = False) -> List[Dict[str, Any]]:
        """Copy or move objects in the workspace.

        Args:
            objects: List of dicts with 'source_path' and 'destination_path'
            overwrite: Whether to overwrite existing objects
            move: If True, move instead of copy

        Returns:
            List of metadata for copied/moved objects
        """
        params = {
            'objects': objects,
            'overwrite': 1 if overwrite else 0,
            'move': 1 if move else 0
        }
        return self._call_method('copy', [params])

    def delete(self, objects: List[str], force: bool = False,
               deleteDirectories: bool = False) -> List[str]:
        """Delete objects from the workspace.

        Args:
            objects: List of object paths to delete
            force: Force deletion even if objects don't exist
            deleteDirectories: Whether to delete directories

        Returns:
            List of deleted object paths
        """
        params = {
            'objects': objects,
            'force': 1 if force else 0,
            'deleteDirectories': 1 if deleteDirectories else 0
        }
        return self._call_method('delete', [params])

    def update_metadata(self, objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Update metadata for workspace objects.

        Args:
            objects: List of dicts with 'path' and 'metadata' to update

        Returns:
            List of updated metadata
        """
        return self._call_method('update_metadata', [{'objects': objects}])

    def set_permissions(self, path: str, permissions: List[Dict[str, str]],
                       new_global_permission: Optional[str] = None) -> Dict[str, Any]:
        """Set permissions for workspace objects.

        Args:
            path: Path to the object
            permissions: List of dicts with 'user' and 'permission' ('r', 'w', 'a', 'n')
            new_global_permission: New global permission setting

        Returns:
            Updated permission information
        """
        params = {
            'path': path,
            'permissions': permissions
        }
        if new_global_permission:
            params['new_global_permission'] = new_global_permission
        return self._call_method('set_permissions', [params])

    def list_permissions(self, objects: List[str]) -> Dict[str, List[Dict[str, str]]]:
        """List permissions for workspace objects.

        Args:
            objects: List of object paths

        Returns:
            Dictionary mapping paths to permission lists
        """
        return self._call_method('list_permissions', [{'objects': objects}])

    def get_download_url(self, objects: List[str]) -> Dict[str, str]:
        """Get download URLs for workspace objects.

        Args:
            objects: List of object paths

        Returns:
            Dictionary mapping paths to download URLs
        """
        return self._call_method('get_download_url', [{'objects': objects}])


class PatricWSUtils(SharedEnvUtils):
    """Utilities for PATRIC/BV-BRC workspace operations.

    This class provides high-level utilities for working with model-related objects
    in the PATRIC/BV-BRC workspace. It is designed to be compatible with the existing
    KBUtilLib framework and ports functionality from ProbModelSEED.

    Example:
        >>> utils = PatricWSUtils()
        >>> # Save a model to workspace
        >>> model_data = {...}  # Model object data
        >>> utils.save_model_object(
        ...     model_data,
        ...     '/username/models/my_model',
        ...     'FBAModel'
        ... )
        >>> # Load a model from workspace
        >>> model = utils.get_model_object('/username/models/my_model')
    """

    # PATRIC workspace URLs
    WORKSPACE_URLS = {
        'prod': 'https://p3.theseed.org/services/Workspace',
        'dev': 'http://p3c.theseed.org/dev1/services/Workspace'
    }

    # ModelSEED service URLs
    MODELSEED_URLS = {
        'prod': 'https://p3.theseed.org/services/ProbModelSEED',
        'dev': 'http://p3c.theseed.org/dev1/services/ProbModelSEED'
    }

    # Standard ModelSEED object types
    OBJECT_TYPES = {
        'model': 'FBAModel',
        'fba': 'FBA',
        'media': 'Media',
        'genome': 'Genome',
        'template': 'ModelTemplate',
        'phenotypeset': 'PhenotypeSet',
        'phenotypesimulationset': 'PhenotypeSimulationSet',
        'biochemistry': 'Biochemistry'
    }

    def __init__(self, version: str = 'prod', **kwargs: Any) -> None:
        """Initialize PATRIC workspace utilities.

        Args:
            version: Workspace version ('prod' or 'dev')
            **kwargs: Additional keyword arguments passed to SharedEnvUtils
        """
        super().__init__(**kwargs)
        self.patric_version = version
        self.workspace_url = self.WORKSPACE_URLS.get(version, self.WORKSPACE_URLS['prod'])
        self.modelseed_url = self.MODELSEED_URLS.get(version, self.MODELSEED_URLS['prod'])

        # Initialize workspace client
        token = self.get_token(namespace='patric')
        self._ws_client = PatricWSClient(self.workspace_url, token)

    def ws_client(self) -> PatricWSClient:
        """Get the workspace client.

        Returns:
            PatricWSClient instance
        """
        return self._ws_client

    def build_ref(self, object_id: str, workspace: Optional[str] = None) -> str:
        """Build a workspace reference path.

        This is equivalent to the Perl buildref() function from ProbModelSEED.

        Args:
            object_id: Object identifier or name
            workspace: Workspace path (optional if object_id is full path)

        Returns:
            Full workspace reference path
        """
        if workspace and not object_id.startswith('/'):
            # Ensure workspace starts with /
            if not workspace.startswith('/'):
                workspace = '/' + workspace
            # Join workspace and object_id
            return os.path.join(workspace, object_id).replace('\\', '/')
        return object_id

    def save_object(self, data: Dict[str, Any], path: str, obj_type: str,
                   metadata: Optional[Dict[str, str]] = None,
                   overwrite: bool = True) -> Dict[str, Any]:
        """Save an object to the workspace.

        This is equivalent to the Perl util_save_object() function from ProbModelSEED.

        Args:
            data: Object data to save
            path: Workspace path for the object
            obj_type: Type of the object (e.g., 'FBAModel', 'Media')
            metadata: Optional user metadata
            overwrite: Whether to overwrite existing objects

        Returns:
            Metadata of the saved object

        Example:
            >>> utils.save_object(
            ...     model_data,
            ...     '/username/models/my_model',
            ...     'FBAModel',
            ...     metadata={'description': 'My metabolic model'}
            ... )
        """
        # Prepare object specification
        obj_spec = {
            'path': path,
            'type': obj_type,
            'data': data
        }

        if metadata:
            obj_spec['metadata'] = metadata

        # Save to workspace
        result = self._ws_client.create([obj_spec], overwrite=overwrite)

        if result and len(result) > 0:
            self.log_info(f"Saved object to {path}")
            return result[0]
        else:
            self.log_error(f"Failed to save object to {path}")
            return {}

    def get_object(self, path: str, metadata_only: bool = False) -> Dict[str, Any]:
        """Retrieve an object from the workspace.

        This is equivalent to the Perl util_get_object() function from ProbModelSEED.

        Args:
            path: Workspace path to the object
            metadata_only: If True, only return metadata

        Returns:
            Object data and metadata

        Example:
            >>> obj = utils.get_object('/username/models/my_model')
            >>> model_data = obj['data']
        """
        result = self._ws_client.get([path], metadata_only=metadata_only)

        if result and len(result) > 0:
            obj = result[0]
            self.log_info(f"Retrieved object from {path}")
            return obj
        else:
            self.log_error(f"Failed to retrieve object from {path}")
            return {}

    def get_ref(self, metadata: List[Any]) -> str:
        """Extract reference string from workspace metadata.

        This is equivalent to the Perl util_get_ref() function from ProbModelSEED.
        Workspace metadata format: [path, type, moddate, ?, size, ?username, ?workspace_id, ??, metadata_dict]
        Reference format: workspace_id/object_id/version

        Args:
            metadata: Workspace object metadata list

        Returns:
            Reference string in format "workspace_id/object_id/version"
        """
        # For PATRIC workspace, we use the path as the reference
        # since it doesn't use the same versioning system as KBase
        if metadata and len(metadata) > 0:
            return metadata[0]  # Return the path
        return ""

    def list_objects(self, directory: str, obj_type: Optional[str] = None,
                    recursive: bool = False) -> List[Dict[str, Any]]:
        """List objects in a workspace directory.

        Args:
            directory: Directory path to list
            obj_type: Optional object type filter
            recursive: Whether to list recursively

        Returns:
            List of object metadata

        Example:
            >>> models = utils.list_objects('/username/models', obj_type='FBAModel')
        """
        query = {}
        if obj_type:
            query['type'] = obj_type

        result = self._ws_client.ls([directory], recursive=recursive, query=query if query else None)

        if directory in result:
            objects = result[directory]
            self.log_info(f"Found {len(objects)} objects in {directory}")
            return objects
        return []

    def delete_object(self, path: str, force: bool = False) -> bool:
        """Delete an object from the workspace.

        Args:
            path: Workspace path to the object
            force: Force deletion even if object doesn't exist

        Returns:
            True if successful, False otherwise
        """
        try:
            result = self._ws_client.delete([path], force=force)
            self.log_info(f"Deleted object at {path}")
            return True
        except Exception as e:
            self.log_error(f"Failed to delete object at {path}: {str(e)}")
            return False

    def copy_object(self, source_path: str, dest_path: str,
                   overwrite: bool = False, move: bool = False) -> Dict[str, Any]:
        """Copy or move an object in the workspace.

        Args:
            source_path: Source object path
            dest_path: Destination object path
            overwrite: Whether to overwrite existing objects
            move: If True, move instead of copy

        Returns:
            Metadata of the copied/moved object
        """
        obj_spec = {
            'source_path': source_path,
            'destination_path': dest_path
        }
        result = self._ws_client.copy([obj_spec], overwrite=overwrite, move=move)

        if result and len(result) > 0:
            action = "Moved" if move else "Copied"
            self.log_info(f"{action} object from {source_path} to {dest_path}")
            return result[0]
        return {}

    # Model-specific convenience methods

    def save_model_object(self, model_data: Dict[str, Any], path: str,
                         metadata: Optional[Dict[str, str]] = None,
                         overwrite: bool = True) -> Dict[str, Any]:
        """Save a metabolic model to the workspace.

        Args:
            model_data: Model object data
            path: Workspace path for the model
            metadata: Optional user metadata
            overwrite: Whether to overwrite existing model

        Returns:
            Metadata of the saved model
        """
        return self.save_object(
            model_data,
            path,
            self.OBJECT_TYPES['model'],
            metadata=metadata,
            overwrite=overwrite
        )

    def get_model_object(self, path: str) -> Dict[str, Any]:
        """Retrieve a metabolic model from the workspace.

        Args:
            path: Workspace path to the model

        Returns:
            Model object data and metadata
        """
        return self.get_object(path)

    def save_fba_object(self, fba_data: Dict[str, Any], path: str,
                       metadata: Optional[Dict[str, str]] = None,
                       overwrite: bool = True) -> Dict[str, Any]:
        """Save an FBA analysis result to the workspace.

        Args:
            fba_data: FBA object data
            path: Workspace path for the FBA result
            metadata: Optional user metadata
            overwrite: Whether to overwrite existing FBA

        Returns:
            Metadata of the saved FBA result
        """
        return self.save_object(
            fba_data,
            path,
            self.OBJECT_TYPES['fba'],
            metadata=metadata,
            overwrite=overwrite
        )

    def get_fba_object(self, path: str) -> Dict[str, Any]:
        """Retrieve an FBA analysis result from the workspace.

        Args:
            path: Workspace path to the FBA result

        Returns:
            FBA object data and metadata
        """
        return self.get_object(path)

    def save_media_object(self, media_data: Dict[str, Any], path: str,
                         metadata: Optional[Dict[str, str]] = None,
                         overwrite: bool = True) -> Dict[str, Any]:
        """Save a media formulation to the workspace.

        Args:
            media_data: Media object data
            path: Workspace path for the media
            metadata: Optional user metadata
            overwrite: Whether to overwrite existing media

        Returns:
            Metadata of the saved media
        """
        return self.save_object(
            media_data,
            path,
            self.OBJECT_TYPES['media'],
            metadata=metadata,
            overwrite=overwrite
        )

    def get_media_object(self, path: str) -> Dict[str, Any]:
        """Retrieve a media formulation from the workspace.

        Args:
            path: Workspace path to the media

        Returns:
            Media object data and metadata
        """
        return self.get_object(path)

    def list_models(self, directory: str, recursive: bool = False) -> List[Dict[str, Any]]:
        """List all models in a workspace directory.

        Args:
            directory: Directory path to search
            recursive: Whether to search recursively

        Returns:
            List of model metadata
        """
        return self.list_objects(directory, obj_type=self.OBJECT_TYPES['model'], recursive=recursive)

    def list_fbas(self, directory: str, recursive: bool = False) -> List[Dict[str, Any]]:
        """List all FBA results in a workspace directory.

        Args:
            directory: Directory path to search
            recursive: Whether to search recursively

        Returns:
            List of FBA metadata
        """
        return self.list_objects(directory, obj_type=self.OBJECT_TYPES['fba'], recursive=recursive)

    def list_media(self, directory: str, recursive: bool = False) -> List[Dict[str, Any]]:
        """List all media formulations in a workspace directory.

        Args:
            directory: Directory path to search
            recursive: Whether to search recursively

        Returns:
            List of media metadata
        """
        return self.list_objects(directory, obj_type=self.OBJECT_TYPES['media'], recursive=recursive)
