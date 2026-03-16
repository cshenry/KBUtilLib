# DataTables Viewer Frontend Configuration Reference

## Overview

The KBDatalakeDashboard frontend is the **DataTables Viewer v3.1.0** - a pre-compiled Vue.js application built with Vite. It renders interactive data tables from GenomeDataLakeTables objects. The frontend is bundled as compiled assets in `data/html/` and is NOT built during the KBase module build - it must be pre-compiled separately.

## Configuration Files

### config/index.json - Main App Configuration

Controls API endpoints, default settings, and feature flags.

```json
{
    "app": {
        "name": "DataTables Viewer",
        "version": "3.1.0",
        "description": "Production-grade configurable data table renderer"
    },
    "apis": {
        "tablescanner": {
            "id": "tablescanner",
            "name": "TableScanner Service",
            "url": "https://appdev.kbase.us/services/berdl_table_scanner",
            "type": "rest"
        },
        "workspace": {
            "id": "workspace",
            "name": "KBase Workspace",
            "url": "https://appdev.kbase.us/services/ws",
            "type": "rest"
        }
    },
    "defaultConfig": {
        "configUrl": "./config/tables/default-config.json",
        "description": "Fallback configuration for unmapped databases"
    },
    "defaults": {
        "pageSize": 50,
        "theme": "system",
        "density": "normal",
        "showRowNumbers": true,
        "defaultSource": "76990/7/2",
        "autoLoad": false,
        "locale": "en-US",
        "dateFormat": "YYYY-MM-DD",
        "numberFormat": {
            "decimals": 2,
            "thousandsSeparator": ",",
            "decimalSeparator": "."
        }
    },
    "features": {
        "schemaExplorer": true,
        "columnSearch": true,
        "exportFormats": ["csv", "json", "tsv"],
        "cellExpansion": true,
        "rowSelection": true,
        "keyboardNavigation": true,
        "columnResizing": true
    },
    "plugins": []
}
```

### config/tables/default-config.json - Default Table Config

Fallback rendering config for tables that don't have type-specific configurations.

```json
{
    "id": "default",
    "name": "Default Configuration",
    "description": "Basic configuration for unmapped databases",
    "version": "1.0.0",
    "icon": "bi-table",
    "color": "#64748b",
    "defaults": {
        "pageSize": 50,
        "density": "normal",
        "showRowNumbers": true,
        "enableSelection": true,
        "enableExport": true
    },
    "sharedCategories": [
        {
            "id": "all",
            "name": "All Columns",
            "icon": "bi-list-columns",
            "defaultVisible": true,
            "order": 1
        }
    ],
    "tables": {}
}
```

### app-config.json - Runtime Injection

Written by the backend at report generation time. Tells the frontend which object to load.

```json
{
    "upa": "76990/7/2"
}
```

## JSON Schema Reference

The full configuration is validated against `config/schemas/config.schema.json` (696 lines). Here are the key schema definitions:

### Data Types

Data type configurations allow mapping specific database types to custom table rendering:

```json
{
    "dataTypes": {
        "genome-datalake": {
            "configUrl": "./config/tables/genome-datalake-config.json",
            "matches": ["GenomeDataLakeTables", "genome_datalake"],
            "priority": 10,
            "autoLoad": true
        }
    }
}
```

### Table Schema (DataTypeConfig)

```json
{
    "id": "genome-datalake",
    "name": "Genome DataLake",
    "version": "1.0.0",
    "icon": "bi-database",
    "color": "#3b82f6",
    "defaults": {
        "pageSize": 50,
        "density": "normal",
        "showRowNumbers": true,
        "enableSelection": true,
        "enableExport": true,
        "enableColumnReorder": false,
        "enableColumnResize": true,
        "defaultSortColumn": "genome_id",
        "defaultSortOrder": "asc"
    },
    "sharedCategories": [
        {"id": "annotation", "name": "Annotation", "icon": "bi-tags", "defaultVisible": true, "order": 1},
        {"id": "modeling", "name": "Modeling", "icon": "bi-diagram-3", "defaultVisible": false, "order": 2}
    ],
    "tables": {
        "user_feature": { ... },
        "genome_reaction": { ... }
    }
}
```

### Column Schema

```json
{
    "column": "feature_id",
    "displayName": "Feature ID",
    "description": "Unique identifier for the genomic feature",
    "dataType": "id",
    "visible": true,
    "sortable": true,
    "filterable": true,
    "searchable": true,
    "copyable": true,
    "width": "150px",
    "align": "left",
    "pin": "left",
    "categories": ["annotation"],
    "transform": { ... },
    "conditionalStyles": [ ... ]
}
```

**Supported data types:**
`string`, `number`, `integer`, `float`, `boolean`, `date`, `datetime`, `timestamp`, `duration`, `id`, `url`, `email`, `phone`, `percentage`, `currency`, `filesize`, `sequence`, `ontology`, `json`, `array`

### Transform Types

Transforms control how cell values are rendered:

```json
// Link transform - make value clickable
{"type": "link", "options": {"urlTemplate": "https://kbase.us/genome/{value}", "target": "_blank"}}

// Badge transform - render as colored badge
{"type": "badge", "options": {"colorMap": {"core": "#22c55e", "accessory": "#f97316"}}}

// Number transform - format numeric values
{"type": "number", "options": {"decimals": 3, "prefix": "", "suffix": "%"}}

// Heatmap transform - color cells by value
{"type": "heatmap", "options": {"min": 0, "max": 1, "colorScale": "viridis"}}

// Sequence transform - render biological sequences
{"type": "sequence", "options": {"type": "protein"}}

// Truncate transform - truncate long text
{"type": "truncate", "options": {"maxLength": 50, "showTooltip": true}}

// Chain transform - apply multiple transforms in sequence
{"type": "chain", "options": [
    {"type": "number", "options": {"decimals": 2}},
    {"type": "heatmap", "options": {"min": 0, "max": 1}}
]}
```

**All transform types:** `link`, `badge`, `number`, `date`, `boolean`, `percentage`, `heatmap`, `sequence`, `ontology`, `copy`, `truncate`, `highlight`, `chain`

### Virtual Columns

Computed columns that derive from existing data:

```json
{
    "column": "full_name",
    "displayName": "Full Name",
    "sourceColumns": ["first_name", "last_name"],
    "compute": {
        "type": "concat",
        "separator": " "
    },
    "visible": true,
    "categories": ["identity"]
}
```

**Compute types:** `merge`, `concat`, `lookup`, `formula`, `conditional`

### Conditional Styles

Apply styling based on cell values:

```json
{
    "condition": {
        "operator": "gt",
        "value": 0.95
    },
    "style": {
        "color": "#16a34a",
        "fontWeight": "bold",
        "backgroundColor": "#f0fdf4",
        "icon": "bi-check-circle-fill"
    }
}
```

**Operators:** `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `contains`, `startsWith`, `endsWith`, `matches`, `empty`, `notEmpty`

### Categories

Group columns into toggleable categories:

```json
{
    "id": "annotation",
    "name": "Annotation",
    "description": "Annotation-related columns",
    "icon": "bi-tags",
    "color": "#3b82f6",
    "defaultVisible": true,
    "order": 1
}
```

### Feature Flags

```json
{
    "features": {
        "schemaExplorer": true,       // Show schema exploration panel
        "columnSearch": true,          // Enable column search
        "exportFormats": ["csv", "json", "tsv", "xlsx"],  // Available export formats
        "cellExpansion": true,         // Click to expand cell contents
        "rowSelection": true,          // Enable row selection checkboxes
        "keyboardNavigation": true,    // Arrow key navigation
        "contextMenu": false,          // Right-click context menu
        "columnReordering": false,     // Drag-and-drop column reorder
        "columnResizing": true,        // Resize column widths
        "virtualScrolling": false,     // Virtual scroll for large datasets
        "infiniteScroll": false        // Infinite scroll pagination
    }
}
```

### Global Settings

```json
{
    "defaults": {
        "pageSize": 50,                // 10, 25, 50, 100, 250, 500
        "theme": "system",             // "light", "dark", "system"
        "density": "normal",           // "compact", "normal", "comfortable"
        "showRowNumbers": true,
        "locale": "en-US",
        "dateFormat": "YYYY-MM-DD",
        "numberFormat": {
            "decimals": 2,
            "thousandsSeparator": ",",
            "decimalSeparator": "."
        }
    }
}
```

## API Configuration

### TableScanner API

The primary API for retrieving table data from GenomeDataLakeTables objects.

```json
{
    "apis": {
        "tablescanner": {
            "id": "tablescanner",
            "name": "TableScanner Service",
            "url": "https://appdev.kbase.us/services/berdl_table_scanner",
            "type": "rest",
            "headers": {},
            "timeout": 30000,
            "retries": 3
        }
    }
}
```

**Important:** For production deployment, the URL should be updated to the production endpoint.

### API Configuration Schema

```json
{
    "id": "string (required)",
    "name": "string (required)",
    "url": "string (required)",
    "type": "rest | graphql | json_server | mock",
    "headers": {"key": "value"},
    "timeout": 30000,
    "retries": 3
}
```

## Creating Custom Table Configurations

To add a custom table configuration for a specific data type:

### Step 1: Create config file

Create `data/html/config/tables/my-custom-config.json`:

```json
{
    "id": "my-custom",
    "name": "My Custom Data",
    "version": "1.0.0",
    "icon": "bi-table",
    "color": "#3b82f6",
    "defaults": {
        "pageSize": 100,
        "density": "compact",
        "showRowNumbers": true,
        "enableSelection": true,
        "enableExport": true
    },
    "sharedCategories": [
        {"id": "core", "name": "Core Fields", "icon": "bi-star", "defaultVisible": true, "order": 1},
        {"id": "extended", "name": "Extended", "icon": "bi-plus-circle", "defaultVisible": false, "order": 2}
    ],
    "tables": {
        "my_table": {
            "displayName": "My Table",
            "description": "Description of this table",
            "icon": "bi-table",
            "columns": [
                {
                    "column": "id",
                    "displayName": "ID",
                    "dataType": "id",
                    "pin": "left",
                    "copyable": true,
                    "categories": ["core"]
                },
                {
                    "column": "value",
                    "displayName": "Value",
                    "dataType": "float",
                    "align": "right",
                    "categories": ["core"],
                    "transform": {"type": "number", "options": {"decimals": 4}},
                    "conditionalStyles": [
                        {"condition": {"operator": "gt", "value": 0.9}, "style": {"color": "#16a34a", "fontWeight": "bold"}}
                    ]
                }
            ]
        }
    }
}
```

### Step 2: Register in index.json

Add a `dataTypes` entry in `config/index.json`:

```json
{
    "dataTypes": {
        "my-custom": {
            "configUrl": "./config/tables/my-custom-config.json",
            "matches": ["MyCustomType"],
            "priority": 10,
            "autoLoad": true
        }
    }
}
```

### Step 3: Rebuild and deploy

Since the frontend is pre-compiled, configuration changes in `data/html/config/` take effect immediately without rebuilding the JavaScript - the config files are loaded at runtime.

## Icons

The frontend uses **Bootstrap Icons** (loaded via CDN in index.html). Reference: https://icons.getbootstrap.com/

Common icons used:
- `bi-table` - Tables
- `bi-database` - Database
- `bi-tags` - Tags/annotations
- `bi-diagram-3` - Models/diagrams
- `bi-star` - Core/important
- `bi-list-columns` - All columns
- `bi-check-circle-fill` - Success
- `bi-x-circle-fill` - Error
- `bi-plus-circle` - Extended/additional

## Fonts

The frontend uses Google Fonts (loaded via CDN in index.html):
- **Inter** - UI text
- **JetBrains Mono** - Monospace/code text
