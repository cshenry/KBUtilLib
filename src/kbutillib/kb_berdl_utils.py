"""BERDL Data API utilities for querying KBase data lake.

This module provides utilities for interacting with the BERDL (Biological and
Environmental Research Data Lake) API to query genomic, ontology, and other
scientific data stored in the KBase data lake.

For full documentation, call: utils.print_docs() or see docs/modules/kb_berdl_utils.md
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from .shared_env_utils import SharedEnvUtils


class KBBERDLUtils(SharedEnvUtils):
    """Utilities for querying the BERDL Data API.

    This class provides methods to:
    - Execute SQL queries against BERDL delta tables
    - List available databases, tables, and schemas
    - Paginate through large result sets
    - Test API connectivity

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
            default="/apis/mcp/delta"
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

    def print_docs(self) -> None:
        """Print the BERDL documentation to the console.

        Displays the full module documentation from the docs file.
        This is useful for quick reference in interactive sessions.
        """
        # Try to find the docs file relative to this module
        module_dir = Path(__file__).parent
        docs_path = module_dir.parent.parent / "docs" / "modules" / "kb_berdl_utils.md"

        if docs_path.exists():
            print(docs_path.read_text())
        else:
            # Fallback to inline help
            print("""
KBBERDLUtils - BERDL Data API Utilities

The BERDL API provides SQL access to KBase data stored in Delta Lake format.

Quick Start:
    from kbutillib import KBBERDLUtils
    utils = KBBERDLUtils()

    # Test connection
    utils.test_connection()

    # List databases
    utils.get_database_list()

    # List tables in a database
    utils.get_database_tables("kbase_genomes")

    # Get table columns
    utils.get_table_columns("kbase_genomes", "contig")

    # Run SQL query
    utils.query("SELECT * FROM kbase_genomes.contig LIMIT 10")

Authentication:
    Store your KBase token in ~/.kbase/token

Performance:
    - Logged into BERDL JupyterHub: Uses personal cluster (faster)
    - Not logged in: Uses shared cluster (slower)

Full docs: https://hub.berdl.kbase.us/apis/mcp/docs
""")

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers including authentication.

        Returns:
            Dict of HTTP headers

        Raises:
            ValueError: If no KBase token is available
        """
        token = self.get_token(namespace="berdl")
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
                f"{self.api_url}/tables/query",
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

    def get_database_list(
        self,
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """List all databases available in BERDL.

        Returns:
            Dict containing:
                - success: bool indicating if request succeeded
                - databases: List of database names
                - count: Number of databases
                - error: Error message if success is False

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.get_database_list()
            >>> for db in result["databases"]:
            ...     print(db)
        """
        self.initialize_call("get_database_list", {}, print_params=True)

        if timeout is None:
            timeout = self.berdl_timeout

        try:
            headers = self._get_headers()

            response = requests.post(
                f"{self.api_url}/databases/list",
                headers=headers,
                json={},
                timeout=timeout
            )

            response.raise_for_status()
            result = response.json()

            # Parse response - may be a list directly or wrapped
            databases = result if isinstance(result, list) else result.get("databases", [])

            self.log_info(f"Found {len(databases)} databases")

            return {
                "success": True,
                "databases": databases,
                "count": len(databases)
            }

        except requests.exceptions.RequestException as e:
            self.log_error(f"Request failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "databases": [],
                "count": 0
            }

    def get_database_tables(
        self,
        database: str,
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """List all tables in a specific database.

        Args:
            database: Database name (e.g., "kbase_genomes")
            timeout: Request timeout in seconds

        Returns:
            Dict containing:
                - success: bool
                - database: The database name queried
                - tables: List of table names
                - count: Number of tables
                - error: Error message if success is False

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.get_database_tables("kbase_genomes")
            >>> for table in result["tables"]:
            ...     print(table)
        """
        self.initialize_call(
            "get_database_tables",
            {"database": database},
            print_params=True
        )

        if timeout is None:
            timeout = self.berdl_timeout

        try:
            headers = self._get_headers()

            response = requests.post(
                f"{self.api_url}/databases/tables/list",
                headers=headers,
                json={"database": database},
                timeout=timeout
            )

            response.raise_for_status()
            result = response.json()

            # Parse response
            tables = result if isinstance(result, list) else result.get("tables", [])

            self.log_info(f"Found {len(tables)} tables in {database}")

            return {
                "success": True,
                "database": database,
                "tables": tables,
                "count": len(tables)
            }

        except requests.exceptions.RequestException as e:
            self.log_error(f"Request failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "database": database,
                "tables": [],
                "count": 0
            }

    def get_table_columns(
        self,
        database: str,
        table: str,
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """Get column information for a specific table.

        Args:
            database: Database name (e.g., "kbase_genomes")
            table: Table name (e.g., "contig")
            timeout: Request timeout in seconds

        Returns:
            Dict containing:
                - success: bool
                - database: The database name
                - table: The table name
                - columns: List of column info dicts with 'name' and 'type'
                - error: Error message if success is False

        Example:
            >>> utils = KBBERDLUtils()
            >>> result = utils.get_table_columns("kbase_genomes", "contig")
            >>> for col in result["columns"]:
            ...     print(f"{col['name']}: {col['type']}")
        """
        self.initialize_call(
            "get_table_columns",
            {"database": database, "table": table},
            print_params=True
        )

        if timeout is None:
            timeout = self.berdl_timeout

        try:
            headers = self._get_headers()

            response = requests.post(
                f"{self.api_url}/databases/tables/schema",
                headers=headers,
                json={"database": database, "table": table},
                timeout=timeout
            )

            response.raise_for_status()
            result = response.json()

            # Parse response - expecting column info
            columns = result if isinstance(result, list) else result.get("columns", result.get("schema", []))

            # Normalize column format
            normalized_columns = []
            for col in columns:
                if isinstance(col, str):
                    normalized_columns.append({"name": col, "type": "unknown"})
                elif isinstance(col, dict):
                    normalized_columns.append({
                        "name": col.get("name", col.get("column_name", "")),
                        "type": col.get("type", col.get("data_type", "unknown"))
                    })

            self.log_info(f"Found {len(normalized_columns)} columns in {database}.{table}")

            return {
                "success": True,
                "database": database,
                "table": table,
                "columns": normalized_columns
            }

        except requests.exceptions.RequestException as e:
            self.log_error(f"Request failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "database": database,
                "table": table,
                "columns": []
            }

    def get_database_schema(
        self,
        database: Optional[str] = None,
        include_columns: bool = False,
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """Get the complete schema structure of databases.

        Args:
            database: Specific database to get schema for (None for all)
            include_columns: Whether to include column details for each table
            timeout: Request timeout in seconds

        Returns:
            Dict containing:
                - success: bool
                - schema: Dict mapping database names to table lists
                          (or table names to column lists if include_columns)
                - error: Error message if success is False

        Example:
            >>> utils = KBBERDLUtils()
            >>> # Get all databases and tables
            >>> result = utils.get_database_schema()
            >>> for db, tables in result["schema"].items():
            ...     print(f"{db}: {len(tables)} tables")

            >>> # Get schema with column details
            >>> result = utils.get_database_schema(
            ...     database="kbase_genomes",
            ...     include_columns=True
            ... )
        """
        self.initialize_call(
            "get_database_schema",
            {"database": database, "include_columns": include_columns},
            print_params=True
        )

        if timeout is None:
            timeout = self.berdl_timeout

        try:
            headers = self._get_headers()

            payload = {"include_schemas": include_columns}
            if database:
                payload["database"] = database

            response = requests.post(
                f"{self.api_url}/databases/structure",
                headers=headers,
                json=payload,
                timeout=timeout
            )

            response.raise_for_status()
            result = response.json()

            self.log_info(f"Retrieved database schema structure")

            return {
                "success": True,
                "schema": result
            }

        except requests.exceptions.RequestException as e:
            self.log_error(f"Request failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "schema": {}
            }

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

