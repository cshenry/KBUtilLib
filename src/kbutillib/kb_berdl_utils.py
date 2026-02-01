"""BERDL Data API utilities for querying KBase data lake.

This module provides utilities for interacting with the BERDL (Biological and
Environmental Research Data Lake) API to query genomic, ontology, and other
scientific data stored in the KBase data lake.
"""

import json
from typing import Any, Dict, List, Optional, Union

import requests

from .shared_env_utils import SharedEnvUtils


class KBBERDLUtils(SharedEnvUtils):
    """Utilities for querying the BERDL Data API.

    This class provides methods to:
    - Execute SQL queries against BERDL delta tables
    - Query genome data (contigs, features, etc.)
    - Query ontology data (reactions, compounds, etc.)
    - List available tables and schemas

    The BERDL API requires authentication via a KBase token. Users must have
    the BERDL user role to access the API.

    Note on performance:
    - When logged into BERDL JupyterHub: Uses personal cluster (faster)
    - When not logged in: Uses shared cluster (slower)
    - KBase apps use a service account and always use the shared cluster

    Example:
        >>> from kbutillib import KBBERDLUtils
        >>> utils = KBBERDLUtils()
        >>> result = utils.query("SELECT * FROM kbase_genomes.contig LIMIT 10")
        >>> print(result["data"])
    """

    def __init__(
        self,
        **kwargs: Any
    ) -> None:
        """Initialize BERDL utilities.

        Args:
            **kwargs: Additional keyword arguments passed to SharedEnvUtils
        """
        super().__init__(**kwargs)

        # Get BERDL API configuration from config
        self.berdl_base_url = self.get_config_value(
            "berdl.base_url",
            default="https://hub.berdl.kbase.us"
        )
        self.berdl_api_path = self.get_config_value(
            "berdl.api_path",
            default="/apis/mcp/delta/tables"
        )
        self.berdl_timeout = self.get_config_value(
            "berdl.timeout",
            default=60
        )
        self.berdl_default_limit = self.get_config_value(
            "berdl.default_limit",
            default=100
        )

        # Build the full API URL
        self.api_url = f"{self.berdl_base_url.rstrip('/')}{self.berdl_api_path}"

        self.log_info(f"KBBERDLUtils initialized (API: {self.api_url})")

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers including authentication.

        Returns:
            Dict of HTTP headers

        Raises:
            ValueError: If no KBase token is available
        """
        token = self.get_token("kbase")
        if not token:
            raise ValueError(
                "No KBase token available. Set token via set_token() or "
                "ensure ~/.kbase/token exists."
            )

        return {
            "accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    def query(
        self,
        sql: str,
        limit: Optional[int] = None,
        offset: int = 0,
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """Execute a SQL query against the BERDL data lake.

        Args:
            sql: SQL query string to execute
            limit: Maximum number of rows to return. If None, uses default_limit
                from config (default: 100)
            offset: Number of rows to skip (for pagination). Default: 0
            timeout: Request timeout in seconds. If None, uses config default.

        Returns:
            Dict containing:
                - success: bool indicating if query succeeded
                - data: List of result rows (each row is a dict)
                - columns: List of column names
                - row_count: Number of rows returned
                - query: The executed SQL query
                - error: Error message if success is False

        Raises:
            ValueError: If no KBase token is available

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.query(
            ...     "SELECT * FROM kbase_genomes.contig ORDER BY contig_id",
            ...     limit=10
            ... )
            >>> for row in result["data"]:
            ...     print(row["contig_id"], row["length"])
        """
        self.initialize_call(
            "query",
            {
                "sql": sql[:100] + "..." if len(sql) > 100 else sql,
                "limit": limit,
                "offset": offset
            },
            print_params=True
        )

        if limit is None:
            limit = self.berdl_default_limit

        if timeout is None:
            timeout = self.berdl_timeout

        try:
            headers = self._get_headers()

            payload = {
                "query": sql,
                "limit": limit,
                "offset": offset
            }

            self.log_debug(f"Executing BERDL query: {sql}")

            response = requests.post(
                f"{self.api_url}/query",
                headers=headers,
                json=payload,
                timeout=timeout
            )

            if response.status_code == 401:
                self.log_error("Authentication failed. Check your KBase token.")
                return {
                    "success": False,
                    "error": "Authentication failed. Ensure you have a valid KBase token and BERDL user role.",
                    "data": [],
                    "columns": [],
                    "row_count": 0,
                    "query": sql
                }

            if response.status_code == 403:
                self.log_error("Access denied. You may not have the BERDL user role.")
                return {
                    "success": False,
                    "error": "Access denied. Ensure your account has the BERDL user role.",
                    "data": [],
                    "columns": [],
                    "row_count": 0,
                    "query": sql
                }

            response.raise_for_status()

            result = response.json()

            # Parse the response - structure may vary based on API response format
            data = result if isinstance(result, list) else result.get("data", result.get("rows", []))

            # Extract column names from first row if data exists
            columns = list(data[0].keys()) if data and isinstance(data[0], dict) else []

            self.log_info(f"Query returned {len(data)} rows")

            return {
                "success": True,
                "data": data,
                "columns": columns,
                "row_count": len(data),
                "query": sql
            }

        except requests.exceptions.Timeout:
            self.log_error(f"Query timed out after {timeout} seconds")
            return {
                "success": False,
                "error": f"Query timed out after {timeout} seconds. Try a smaller query or increase timeout.",
                "data": [],
                "columns": [],
                "row_count": 0,
                "query": sql
            }
        except requests.exceptions.RequestException as e:
            self.log_error(f"Request failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "data": [],
                "columns": [],
                "row_count": 0,
                "query": sql
            }
        except json.JSONDecodeError as e:
            self.log_error(f"Failed to parse response: {e}")
            return {
                "success": False,
                "error": f"Invalid JSON response: {e}",
                "data": [],
                "columns": [],
                "row_count": 0,
                "query": sql
            }

    def query_contigs(
        self,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "contig_id",
        filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Query contig data from kbase_genomes.contig table.

        Args:
            limit: Maximum number of rows to return. Default: 100
            offset: Number of rows to skip (for pagination). Default: 0
            order_by: Column to order results by. Default: "contig_id"
            filters: Optional dict of column: value pairs for WHERE clause

        Returns:
            Query result dict (see query() for structure)

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.query_contigs(limit=10)
            >>> for contig in result["data"]:
            ...     print(f"{contig['contig_id']}: {contig['length']} bp")
        """
        sql = f"SELECT * FROM kbase_genomes.contig"

        if filters:
            conditions = []
            for col, val in filters.items():
                if isinstance(val, str):
                    conditions.append(f"{col} = '{val}'")
                else:
                    conditions.append(f"{col} = {val}")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)

        sql += f" ORDER BY {order_by}"

        return self.query(sql, limit=limit, offset=offset)

    def query_ontology_statements(
        self,
        subject_prefix: Optional[str] = None,
        predicate: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Query ontology statements from kbase_ontology_source.statements table.

        Args:
            subject_prefix: Filter subjects starting with this prefix
                (e.g., "seed.reaction:" for SEED reactions)
            predicate: Filter by predicate (e.g., "rdfs:label" for names)
            limit: Maximum number of rows to return. Default: 100
            offset: Number of rows to skip. Default: 0

        Returns:
            Query result dict (see query() for structure)

        Example:
            >>> utils = KBBERDLUtils()
            >>> # Get SEED reaction names
            >>> result = utils.query_ontology_statements(
            ...     subject_prefix="seed.reaction:",
            ...     predicate="rdfs:label",
            ...     limit=10
            ... )
        """
        sql = "SELECT subject, predicate, value FROM kbase_ontology_source.statements"

        conditions = []
        if subject_prefix:
            conditions.append(f"subject LIKE '{subject_prefix}%'")
        if predicate:
            conditions.append(f"predicate = '{predicate}'")

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)

        return self.query(sql, limit=limit, offset=offset)

    def get_reaction_names(
        self,
        reaction_ids: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Get SEED reaction names from the ontology.

        Args:
            reaction_ids: Optional list of specific reaction IDs to fetch
                (e.g., ["rxn00001", "rxn00002"]). If None, returns all.
            limit: Maximum number of rows to return. Default: 100
            offset: Number of rows to skip. Default: 0

        Returns:
            Query result dict with reaction_id and reaction_name columns

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.get_reaction_names(limit=10)
            >>> for rxn in result["data"]:
            ...     print(f"{rxn['reaction_id']}: {rxn['reaction_name']}")
        """
        sql = """
            SELECT
                subject as reaction_id,
                value as reaction_name
            FROM kbase_ontology_source.statements
            WHERE subject LIKE 'seed.reaction:%'
            AND predicate = 'rdfs:label'
        """.strip()

        if reaction_ids:
            # Format IDs with the seed.reaction: prefix if not present
            formatted_ids = []
            for rid in reaction_ids:
                if rid.startswith("seed.reaction:"):
                    formatted_ids.append(f"'{rid}'")
                else:
                    formatted_ids.append(f"'seed.reaction:{rid}'")
            id_list = ", ".join(formatted_ids)
            sql += f" AND subject IN ({id_list})"

        return self.query(sql, limit=limit, offset=offset)

    def get_compound_names(
        self,
        compound_ids: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0
    ) -> Dict[str, Any]:
        """Get SEED compound names from the ontology.

        Args:
            compound_ids: Optional list of specific compound IDs to fetch
                (e.g., ["cpd00001", "cpd00002"]). If None, returns all.
            limit: Maximum number of rows to return. Default: 100
            offset: Number of rows to skip. Default: 0

        Returns:
            Query result dict with compound_id and compound_name columns

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.get_compound_names(["cpd00001", "cpd00002"])
        """
        sql = """
            SELECT
                subject as compound_id,
                value as compound_name
            FROM kbase_ontology_source.statements
            WHERE subject LIKE 'seed.compound:%'
            AND predicate = 'rdfs:label'
        """.strip()

        if compound_ids:
            formatted_ids = []
            for cid in compound_ids:
                if cid.startswith("seed.compound:"):
                    formatted_ids.append(f"'{cid}'")
                else:
                    formatted_ids.append(f"'seed.compound:{cid}'")
            id_list = ", ".join(formatted_ids)
            sql += f" AND subject IN ({id_list})"

        return self.query(sql, limit=limit, offset=offset)

    def paginate_query(
        self,
        sql: str,
        page_size: int = 1000,
        max_pages: Optional[int] = None,
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """Execute a query with automatic pagination to fetch all results.

        This method repeatedly queries the API, incrementing the offset
        until no more results are returned or max_pages is reached.

        Args:
            sql: SQL query string (should not include LIMIT/OFFSET)
            page_size: Number of rows per page. Default: 1000
            max_pages: Maximum number of pages to fetch. None for unlimited.
            timeout: Request timeout per page in seconds.

        Returns:
            Dict containing:
                - success: bool
                - data: List of all result rows
                - columns: List of column names
                - row_count: Total number of rows
                - pages_fetched: Number of pages retrieved
                - query: The base SQL query

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.paginate_query(
            ...     "SELECT * FROM kbase_genomes.contig WHERE length > 10000",
            ...     page_size=500,
            ...     max_pages=10
            ... )
            >>> print(f"Fetched {result['row_count']} contigs")
        """
        self.initialize_call(
            "paginate_query",
            {
                "sql": sql[:100] + "..." if len(sql) > 100 else sql,
                "page_size": page_size,
                "max_pages": max_pages
            },
            print_params=True
        )

        all_data = []
        columns = []
        offset = 0
        pages_fetched = 0

        while True:
            if max_pages and pages_fetched >= max_pages:
                self.log_info(f"Reached max_pages limit ({max_pages})")
                break

            result = self.query(sql, limit=page_size, offset=offset, timeout=timeout)

            if not result["success"]:
                return {
                    "success": False,
                    "error": result.get("error", "Query failed"),
                    "data": all_data,
                    "columns": columns,
                    "row_count": len(all_data),
                    "pages_fetched": pages_fetched,
                    "query": sql
                }

            page_data = result["data"]
            if not page_data:
                break

            if not columns and result["columns"]:
                columns = result["columns"]

            all_data.extend(page_data)
            pages_fetched += 1
            offset += page_size

            self.log_debug(f"Fetched page {pages_fetched}: {len(page_data)} rows")

            # If we got fewer rows than page_size, we've reached the end
            if len(page_data) < page_size:
                break

        self.log_info(f"Pagination complete: {len(all_data)} total rows in {pages_fetched} pages")

        return {
            "success": True,
            "data": all_data,
            "columns": columns,
            "row_count": len(all_data),
            "pages_fetched": pages_fetched,
            "query": sql
        }

    def test_connection(self) -> Dict[str, Any]:
        """Test the connection to the BERDL API.

        Executes a simple query to verify authentication and connectivity.

        Returns:
            Dict containing:
                - success: bool
                - message: Status message
                - api_url: The API URL being used

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.test_connection()
            >>> if result["success"]:
            ...     print("Connected successfully!")
        """
        self.log_info("Testing BERDL API connection...")

        result = self.query("SELECT 1 as test", limit=1)

        if result["success"]:
            return {
                "success": True,
                "message": "Successfully connected to BERDL API",
                "api_url": self.api_url
            }
        else:
            return {
                "success": False,
                "message": f"Connection failed: {result.get('error', 'Unknown error')}",
                "api_url": self.api_url
            }

    def get_table_info(
        self,
        schema: str,
        table: str
    ) -> Dict[str, Any]:
        """Get information about a specific table's columns.

        Note: This uses DESCRIBE or information_schema depending on
        what the underlying database supports.

        Args:
            schema: Schema name (e.g., "kbase_genomes")
            table: Table name (e.g., "contig")

        Returns:
            Query result dict with column information

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.get_table_info("kbase_genomes", "contig")
        """
        # Try to get column info - exact syntax depends on database
        sql = f"DESCRIBE {schema}.{table}"
        result = self.query(sql, limit=1000)

        if not result["success"]:
            # Fallback to information_schema approach
            sql = f"""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = '{schema}'
                AND table_name = '{table}'
            """
            result = self.query(sql, limit=1000)

        return result
