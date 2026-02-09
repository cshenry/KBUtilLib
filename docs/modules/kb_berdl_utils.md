# KBBERDLUtils Module

The `KBBERDLUtils` class provides utilities for interacting with the BERDL (Biological and Environmental Research Data Lake) API to query genomic, ontology, and other scientific data stored in the KBase data lake.

## Overview

BERDL is a data lake infrastructure that provides SQL-based access to KBase scientific data stored in Delta Lake format. The `KBBERDLUtils` class extends `SharedEnvUtils` to provide comprehensive BERDL API functionality including query execution, database introspection, and pagination.

## Key Features

- **SQL Query Execution**: Run arbitrary SQL queries against BERDL delta tables
- **Database Introspection**: List databases, tables, and column schemas
- **Automatic Pagination**: Handle large result sets with built-in pagination
- **Authentication**: Integrated KBase token-based authentication
- **Performance Awareness**: Automatic handling of cluster fallback scenarios

## Authentication & Access

The BERDL API requires authentication via a KBase token. You must have the BERDL user role assigned to your KBase account.

### Token Setup

1. **Automatic** (recommended): Store your KBase token in `~/.kbase/token`
2. **Programmatic**: Pass token via `set_token()` or constructor

```python
from kbutillib import KBBERDLUtils

# Uses token from ~/.kbase/token automatically
utils = KBBERDLUtils()

# Or set token explicitly
utils = KBBERDLUtils()
utils.set_token("your-kbase-token", namespace="berdl")
```

## Performance Notes

BERDL uses a cluster caching system tied to JupyterHub sessions:

| Scenario | Cluster Used | Performance |
|----------|--------------|-------------|
| Logged into BERDL JupyterHub | Personal cluster | Faster |
| Not logged in / session expired | Shared cluster | Slower |
| KBase app (service account) | Shared cluster | Slower |

The system automatically falls back to the shared cluster if your personal cluster is unavailable. There's no explicit warning when this happens - queries will simply be slower.

## API vs MCP

The Data API and MCP (Model Context Protocol) are hosted on the same server and are essentially the same service:

- **Use the MCP** if you want AI agents (like Claude Code) to interact directly
- **Use the Data API** (this module) to fetch data programmatically

## Class Definition

```python
class KBBERDLUtils(SharedEnvUtils):
    """Utilities for querying the BERDL Data API.

    Provides methods to:
    - Execute SQL queries against BERDL delta tables
    - List available databases and tables
    - Inspect table schemas
    - Paginate through large result sets
    """
```

## Constructor

```python
def __init__(self, **kwargs: Any) -> None:
    """Initialize BERDL utilities.

    Args:
        **kwargs: Additional keyword arguments passed to SharedEnvUtils

    Configuration via config.yaml:
        berdl:
          base_url: https://hub.berdl.kbase.us  # API base URL
          api_path: /apis/mcp/delta/tables       # API path
          timeout: 60                            # Request timeout (seconds)
          default_limit: 100                     # Default query limit
    """
```

## Core Methods

### Database Introspection

```python
def get_database_list(self) -> Dict[str, Any]:
    """List all databases available in BERDL.

    Returns:
        Dict with 'success', 'databases' (list of names), and 'count'
    """

def get_database_tables(self, database: str) -> Dict[str, Any]:
    """List all tables in a specific database.

    Args:
        database: Database name (e.g., "kbase_genomes")

    Returns:
        Dict with 'success', 'database', 'tables', and 'count'
    """

def get_table_columns(self, database: str, table: str) -> Dict[str, Any]:
    """Get column information for a specific table.

    Args:
        database: Database name
        table: Table name

    Returns:
        Dict with 'success', 'database', 'table', and 'columns'
    """

def get_database_schema(self, database: Optional[str] = None, include_columns: bool = False) -> Dict[str, Any]:
    """Get the complete schema structure of databases.

    Args:
        database: Specific database to get schema for (None for all)
        include_columns: Whether to include column details for each table

    Returns:
        Dict with 'success' and 'schema' (nested database/table structure)
    """
```

### Query Execution

```python
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
        limit: Maximum rows to return (default from config)
        offset: Rows to skip for pagination
        timeout: Request timeout in seconds

    Returns:
        Dict containing:
            - success: bool
            - data: List of result rows
            - columns: List of column names
            - row_count: Number of rows returned
            - query: The executed SQL query
            - error: Error message if success is False
    """

def paginate_query(
    self,
    sql: str,
    page_size: int = 1000,
    max_pages: Optional[int] = None,
    timeout: Optional[int] = None
) -> Dict[str, Any]:
    """Execute a query with automatic pagination to fetch all results.

    Args:
        sql: SQL query (should not include LIMIT/OFFSET)
        page_size: Rows per page
        max_pages: Maximum pages to fetch (None for unlimited)
        timeout: Request timeout per page

    Returns:
        Dict with 'success', 'data', 'columns', 'row_count', 'pages_fetched'
    """
```

### Connection Testing

```python
def test_connection(self) -> Dict[str, Any]:
    """Test the connection to the BERDL API.

    Returns:
        Dict with 'success', 'message', and 'api_url'
    """
```

### Documentation

```python
def print_docs(self) -> None:
    """Print the BERDL documentation to the console.

    Displays the full module documentation from the docs file.
    """
```

## Usage Examples

### Basic Setup and Connection Test

```python
from kbutillib import KBBERDLUtils

# Initialize
utils = KBBERDLUtils()

# Test connection
result = utils.test_connection()
if result["success"]:
    print("Connected successfully!")
else:
    print(f"Connection failed: {result['message']}")
```

### Exploring Available Data

```python
# List all databases
result = utils.get_database_list()
if result["success"]:
    print(f"Found {result['count']} databases:")
    for db in result["databases"]:
        print(f"  - {db}")

# List tables in a database
result = utils.get_database_tables("kbase_genomes")
if result["success"]:
    print(f"Tables in kbase_genomes:")
    for table in result["tables"]:
        print(f"  - {table}")

# Get column info for a table
result = utils.get_table_columns("kbase_genomes", "contig")
if result["success"]:
    print("Columns in kbase_genomes.contig:")
    for col in result["columns"]:
        print(f"  - {col['name']}: {col['type']}")
```

### Running SQL Queries

```python
# Simple query
result = utils.query(
    "SELECT * FROM kbase_genomes.contig ORDER BY contig_id",
    limit=10
)
if result["success"]:
    for row in result["data"]:
        print(row)

# Query with filters
result = utils.query("""
    SELECT contig_id, gc_content, length
    FROM kbase_genomes.contig
    WHERE gc_content > 0.5
    ORDER BY gc_content DESC
""", limit=20)

# Query ontology data
result = utils.query("""
    SELECT subject as reaction_id, value as reaction_name
    FROM kbase_ontology_source.statements
    WHERE subject LIKE 'seed.reaction:%'
    AND predicate = 'rdfs:label'
    ORDER BY subject
""", limit=10)
```

### Paginated Queries for Large Results

```python
# Fetch all contigs > 10kb (with pagination)
result = utils.paginate_query(
    sql="SELECT * FROM kbase_genomes.contig WHERE length > 10000 ORDER BY contig_id",
    page_size=1000,
    max_pages=10  # Limit for safety
)
if result["success"]:
    print(f"Retrieved {result['row_count']} contigs in {result['pages_fetched']} pages")
```

### Getting Complete Database Schema

```python
# Get all databases and their tables
result = utils.get_database_schema()
if result["success"]:
    for db_name, tables in result["schema"].items():
        print(f"\n{db_name}:")
        for table in tables:
            print(f"  - {table}")

# Get schema with column details
result = utils.get_database_schema(
    database="kbase_genomes",
    include_columns=True
)
```

## Configuration

Configure BERDL settings in `~/.kbutillib/config.yaml`:

```yaml
berdl:
  base_url: https://hub.berdl.kbase.us  # BERDL API base URL
  api_path: /apis/mcp/delta/tables       # API path
  timeout: 120                            # Request timeout (seconds)
  default_limit: 500                      # Default query limit
```

## Available API Endpoints

The underlying BERDL API provides these endpoints (accessed through this module):

| Endpoint | Description |
|----------|-------------|
| `/delta/databases/list` | List all available databases |
| `/delta/databases/tables/list` | List tables in a database |
| `/delta/databases/tables/schema` | Get table column schema |
| `/delta/databases/structure` | Get complete database structure |
| `/delta/tables/query` | Execute SQL queries |
| `/delta/tables/count` | Get row count for a table |
| `/delta/tables/sample` | Get sample rows from a table |

Full API documentation: https://hub.berdl.kbase.us/apis/mcp/docs

## Known Data Sources

BERDL contains data from various KBase and external sources:

### Genome Data (`kbase_genomes`)
- `contig` - Contig sequences with length, GC content
- `feature` - Genomic features (genes, CDS, etc.)
- (additional tables - use `get_database_tables()` to explore)

### Ontology Data (`kbase_ontology_source`)
- `statements` - RDF-style ontology statements
  - SEED reactions: `subject LIKE 'seed.reaction:%'`
  - SEED compounds: `subject LIKE 'seed.compound:%'`
  - Names via: `predicate = 'rdfs:label'`

## Error Handling

The module handles common errors gracefully:

- **401 Unauthorized**: Invalid or expired KBase token
- **403 Forbidden**: User lacks BERDL role
- **Timeout**: Query took too long (configurable)
- **Network errors**: Connection issues to BERDL server

All methods return a consistent structure with `success` boolean and `error` message when applicable.

## KBase App Deployment Notes

When deploying as a KBase app:

1. A service account is created and registered with BERDL
2. The service account token is stored in an environment variable
3. Since service accounts never "log in" to JupyterHub, apps always use the shared cluster
4. Expect slower query performance compared to interactive JupyterHub sessions

## Dependencies

- `requests` - HTTP client for API calls
- Inherits from: `SharedEnvUtils`

## See Also

- [SharedEnvUtils](shared_env_utils.md) - Base class with configuration and logging
- [BERDL Swagger Docs](https://hub.berdl.kbase.us/apis/mcp/docs) - Full API reference
