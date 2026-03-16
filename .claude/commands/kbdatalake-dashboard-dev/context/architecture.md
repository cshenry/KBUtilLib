# KBDatalakeDashboard Architecture Reference

## Module Identity

- **Module Name (kbase.yml):** `KBDatalakeDashboard2` (renamed from KBDatalakeDashboard due to registration conflict)
- **KIDL Module Name:** `KBDatalakeDashboard` (used in spec and code)
- **Version:** 0.0.1
- **Service Language:** Python
- **Owners:** chenry, jplfaria
- **Repository:** `/Users/chenry/Dropbox/Projects/KBDatalakeDashboard`

## Architecture Pattern

KBDatalakeDashboard is a **Simple Report Generator** - the simplest KBase SDK app pattern. The backend does minimal work (copy files, write config, upload, create report), while the frontend handles all data retrieval and rendering.

### Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│ KBase Narrative                                             │
│                                                             │
│  User selects GenomeDataLakeTables object                   │
│  User clicks "Run"                                          │
│       │                                                     │
│       ▼                                                     │
│  ┌─────────────────────────────────────────────────┐       │
│  │ Backend (KBDatalakeDashboardImpl.py)             │       │
│  │                                                   │       │
│  │  1. Validate params (input_ref, workspace_name)   │       │
│  │  2. Copy /kb/module/data/html → scratch/<uuid>    │       │
│  │  3. Write app-config.json = {"upa": input_ref}    │       │
│  │  4. Upload directory to Shock (ZIP via DFU)       │       │
│  │  5. Create KBaseReport with HTML link             │       │
│  │  6. Return {report_name, report_ref}              │       │
│  └─────────────────────────────────────────────────┘       │
│       │                                                     │
│       ▼                                                     │
│  ┌─────────────────────────────────────────────────┐       │
│  │ Report Viewer (iframe in Narrative)              │       │
│  │                                                   │       │
│  │  Frontend (DataTables Viewer v3.1.0)              │       │
│  │  1. Load index.html from Shock ZIP                │       │
│  │  2. Read app-config.json for UPA                  │       │
│  │  3. Read config/index.json for API endpoints      │       │
│  │  4. Call TableScanner API with UPA                 │       │
│  │  5. Render interactive data tables                 │       │
│  └─────────────────────────────────────────────────┘       │
│       │                                                     │
│       ▼                                                     │
│  ┌─────────────────────────────────────────────────┐       │
│  │ TableScanner Service (BERDL)                     │       │
│  │  https://appdev.kbase.us/services/                │       │
│  │         berdl_table_scanner                       │       │
│  │                                                   │       │
│  │  Retrieves data from GenomeDataLakeTables object  │       │
│  │  Returns table data as JSON                       │       │
│  └─────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

## Complete File Map

### Core Module Files

| File | Purpose | Size | Edit Frequency |
|------|---------|------|---------------|
| `kbase.yml` | Module metadata (name, version, owners) | Small | Rarely |
| `KBDatalakeDashboard.spec` | KIDL type/function definitions | 30 lines | When adding methods |
| `Makefile` | Build targets (compile, test, scripts) | ~60 lines | Rarely |
| `Dockerfile` | Container build (Python 3.10, deps) | 72 lines | When adding deps |
| `requirements.txt` | Runtime Python packages | 12 lines | When adding deps |
| `requirements_kbase.txt` | Test/build Python packages | 7 lines | Rarely |
| `deploy.cfg` | Deployment config template (Jinja2) | 10 lines | Rarely |
| `sdk.cfg` | SDK catalog config | Small | Rarely |
| `dependencies.json` | External KBase module deps | 3 entries | When adding services |

### Implementation

| File | Purpose | Key Details |
|------|---------|-------------|
| `lib/KBDatalakeDashboard/KBDatalakeDashboardImpl.py` | Main implementation | ~143 lines, single method |
| `lib/KBDatalakeDashboard/__init__.py` | Package marker | Empty |
| `lib/installed_clients/KBaseReportClient.py` | Report service client | Auto-generated, 20KB |
| `lib/installed_clients/DataFileUtilClient.py` | File utility client | Auto-generated, 34KB |
| `lib/installed_clients/WorkspaceClient.py` | Workspace client | Auto-generated, 388KB |
| `lib/installed_clients/authclient.py` | Auth client | Auto-generated |
| `lib/installed_clients/baseclient.py` | Base client class | Auto-generated |

### UI Specification

| File | Purpose |
|------|---------|
| `ui/narrative/methods/run_genome_datalake_dashboard/spec.json` | Parameter definitions, service mapping, type transforms |
| `ui/narrative/methods/run_genome_datalake_dashboard/display.yaml` | UI labels, hints, app description |

### Frontend Assets (Pre-built, in `data/html/`)

| File | Purpose |
|------|---------|
| `data/html/index.html` | Entry point, loads Vite bundles |
| `data/html/vite.svg` | App icon |
| `data/html/assets/main-CLpjqEaD.js` | Main JavaScript bundle (Vue.js app) |
| `data/html/assets/main-B6Alf4rU.css` | Main stylesheet |
| `data/html/assets/filter-parser-HXU3E1Qm.js` | Filter parsing module |
| `data/html/assets/AdvancedFilterPanel-8fPeCncD.js` | Advanced filter UI component |
| `data/html/config/index.json` | App configuration (APIs, defaults, features) |
| `data/html/config/tables/default-config.json` | Default table rendering config |
| `data/html/config/schemas/config.schema.json` | JSON Schema for config validation (696 lines) |

### Scripts

| File | Purpose |
|------|---------|
| `scripts/entrypoint.sh` | Docker container entry point (start server, test, async, init, bash, report modes) |
| `scripts/prepare_deploy_cfg.py` | Processes Jinja2 template → runtime config |

### Testing

| File | Purpose |
|------|---------|
| `test/KBDatalakeDashboard_server_test.py` | Test suite (skeleton only - needs implementation) |
| `notebooks/util.py` | NotebookUtil class for local testing (extends NotebookUtils + SharedEnvUtils) |

### Legacy

| File | Purpose |
|------|---------|
| `biokbase/auth.py` | Legacy Globus auth utilities (not actively used) |
| `biokbase/log.py` | Legacy syslog logging utilities |
| `biokbase/user-env.sh` | Environment setup for container |

## KIDL Specification Details

```kidl
module KBDatalakeDashboard {
    typedef structure {
        string report_name;
        string report_ref;
    } ReportResults;

    typedef structure {
        string workspace_name;
        string input_ref;
    } RunGenomeDatalakeDashboardParams;

    funcdef run_genome_datalake_dashboard(RunGenomeDatalakeDashboardParams params)
        returns (ReportResults output)
        authentication required;
};
```

**Types:**
- `ReportResults` - Standard KBase report output (report_name + report_ref)
- `RunGenomeDatalakeDashboardParams` - workspace_name (auto-injected) + input_ref (GenomeDataLakeTables UPA)

**Functions:**
- `run_genome_datalake_dashboard` - Single public method, authentication required

## UI Specification Details

### spec.json Key Points

- **Single parameter:** `input_ref` mapped to `KBaseFBA.GenomeDataLakeTables` type
- **Workspace auto-injected:** `narrative_system_variable: workspace` → `workspace_name`
- **Type transform:** `resolved-ref` converts user selection to full UPA (ws/obj/ver)
- **Output mapping:** Standard report_name and report_ref extraction
- **No custom widgets:** Uses default input (null) and no-display output

### display.yaml Key Points

- **App name:** "Run Genome Datalake Dashboard"
- **Parameter label:** "Genome DataLake Tables"
- **Description:** Explains that input comes from "Build Genome Datalake Tables" app
- **No publications listed**
- **No icon file present** (references icon.png but file doesn't exist)

## Docker Architecture

### Base Image
`python:3.10-slim-bullseye` with KB SDK 1.2.1 copied from `kbase/kb-sdk:1.2.1`

### Dependency Installation Order
1. System packages (build-essential, git, openjdk-11, wget, curl, gcc)
2. KB SDK binaries (copied from sdk image)
3. KBase test requirements (requests, pytest, nose, sphinx)
4. Legacy biokbase package
5. Module requirements (pandas, cobra, polars, etc.)
6. Custom forks (ModelSEEDpy, cobrakbase at specific commit, KBUtilLib)
7. Module compile (`make all`)

### Important: Custom Dependency Commits

```dockerfile
# ModelSEEDpy - latest from cshenry fork
RUN git clone https://github.com/cshenry/ModelSEEDpy.git && pip install -e ModelSEEDpy

# cobrakbase - PINNED to specific commit
RUN git clone https://github.com/cshenry/cobrakbase.git && \
    cd cobrakbase && git checkout 68444e46fe3b68482da80798642461af2605e349

# KBUtilLib - latest from cshenry fork
RUN git clone https://github.com/cshenry/KBUtilLib.git && \
    cd KBUtilLib && pip install -e .
```

## External Service Dependencies

### KBase Services (via SDK clients)

| Service | Client Class | Used For |
|---------|-------------|----------|
| DataFileUtil | `DataFileUtilClient` | `file_to_shock()` - upload HTML directory as ZIP |
| KBaseReport | `KBaseReportClient` | `create_extended_report()` - generate report with HTML links |
| Workspace | `WorkspaceClient` | Available but not directly used by backend |

### External APIs (called by frontend)

| Service | URL | Purpose |
|---------|-----|---------|
| TableScanner | `https://appdev.kbase.us/services/berdl_table_scanner` | Retrieve data from GenomeDataLakeTables |
| Workspace | `https://appdev.kbase.us/services/ws` | Workspace operations |

## Relationship to KBDatalakeApps

KBDatalakeDashboard is the **viewer** for data produced by KBDatalakeApps:

```
KBDatalakeApps                        KBDatalakeDashboard
─────────────                          ────────────────────
build_genome_datalake_tables()    →    run_genome_datalake_dashboard()
  Input: Genome/GenomeSet refs          Input: GenomeDataLakeTables ref
  Pipeline: genome → annotation →       Output: HTML report with
    pangenome → modeling → tables         interactive data viewer
  Output: GenomeDataLakeTables object
```

The GenomeDataLakeTables object contains SQLite databases with tables for:
- Genome features and annotations (RAST, KOfam, Bakta, PSORTb)
- ANI distances between genomes
- Pangenome clusters
- Metabolic model reactions and fluxes
- Phenotype simulation results
- Gene-phenotype associations
- Ontology term enrichments

## Configuration Hierarchy

```
config/index.json         → App metadata, API endpoints, defaults, feature flags
config/tables/*.json      → Table rendering configurations per data type
config/schemas/*.json     → JSON Schema for config validation
app-config.json           → Runtime injection: {"upa": "WS/OBJ/VER"}
```

The frontend reads these in order:
1. `app-config.json` (injected by backend) → which object to load
2. `config/index.json` → where to find APIs, what features are enabled
3. `config/tables/default-config.json` → how to render tables by default
4. Data type-specific configs → custom table/column definitions (via `dataTypes` in index.json)
