# KBDatalakeDashboard Development Guide

## Common Development Tasks

### 1. Adding a New Backend Method

If you need to add a new KIDL method (e.g., a second app entry point):

**Step 1: Update the KIDL spec**

Edit `KBDatalakeDashboard.spec`:
```kidl
typedef structure {
    string workspace_name;
    string input_ref;
    string config_name;  // new parameter
} RunCustomDashboardParams;

funcdef run_custom_dashboard(RunCustomDashboardParams params)
    returns (ReportResults output)
    authentication required;
```

**Step 2: Recompile**
```bash
make compile
# Or: kb-sdk compile KBDatalakeDashboard.spec --out lib --pysrvname KBDatalakeDashboard.KBDatalakeDashboardServer --pyimplname KBDatalakeDashboard.KBDatalakeDashboardImpl
```

**Step 3: Implement in KBDatalakeDashboardImpl.py**

Add the new method between `#BEGIN` and `#END` markers:
```python
def run_custom_dashboard(self, ctx, params):
    #BEGIN run_custom_dashboard
    # Your implementation here
    #END run_custom_dashboard
```

**Step 4: Add UI files**

Create `ui/narrative/methods/run_custom_dashboard/spec.json` and `display.yaml`.

**Step 5: Rebuild Docker image**
```bash
docker build -t kbdatalakedashboard:latest .
```

### 2. Adding a New UI Parameter

To add a parameter to the existing `run_genome_datalake_dashboard` method:

**Step 1: Update KIDL spec** - Add field to `RunGenomeDatalakeDashboardParams`

**Step 2: Update spec.json** - Add parameter definition and input mapping

Example - adding an optional "config_preset" dropdown:
```json
{
    "id": "config_preset",
    "optional": true,
    "advanced": true,
    "allow_multiple": false,
    "default_values": ["default"],
    "field_type": "dropdown",
    "dropdown_options": {
        "options": [
            {"display": "Default", "value": "default"},
            {"display": "Compact", "value": "compact"},
            {"display": "Detailed", "value": "detailed"}
        ]
    }
}
```

Add input mapping:
```json
{
    "input_parameter": "config_preset",
    "target_property": "config_preset"
}
```

**Step 3: Update display.yaml** - Add parameter UI labels

**Step 4: Update Impl** - Handle the new parameter in `run_genome_datalake_dashboard`

**Step 5: Recompile** - Run `make compile` after spec changes

### 3. Modifying the Frontend Configuration

The frontend configuration files in `data/html/config/` are loaded at runtime, not compiled into the JavaScript bundles. This means you can modify them without rebuilding the frontend.

**Changing API endpoints:**
Edit `data/html/config/index.json` → `apis` section. For production, change the TableScanner URL.

**Adding table column definitions:**
Create or edit files in `data/html/config/tables/`. See the frontend-config context file for the full schema.

**Enabling/disabling features:**
Edit `data/html/config/index.json` → `features` section.

### 4. Modifying the app-config.json Injection

The backend writes `app-config.json` to tell the frontend which object to display. To pass additional configuration:

Edit `KBDatalakeDashboardImpl.py`, find the `app_config` dictionary:
```python
app_config = {
    "upa": input_ref,
    # Add additional config here:
    "theme": params.get("theme", "system"),
    "preset": params.get("config_preset", "default")
}
```

The frontend must also be updated to read these new fields from `app-config.json`.

### 5. Adding Python Dependencies

**Step 1: Add to requirements.txt**
```
new-package==1.2.3
```

**Step 2: Update Dockerfile if needed**

For packages requiring system libraries:
```dockerfile
RUN apt-get update && apt-get install -y libfoo-dev
```

**Step 3: Rebuild Docker image**
```bash
docker build -t kbdatalakedashboard:latest .
```

### 6. Adding a KBase Service Dependency

**Step 1: Update dependencies.json**
```json
{
    "module_name": "NewService",
    "type": "sdk",
    "version_tag": "release"
}
```

**Step 2: Generate client**
The client will be auto-generated when running `make compile` or `kb-sdk compile`.

**Step 3: Import and use in Impl**
```python
from installed_clients.NewServiceClient import NewService

# In __init__:
self.new_service = NewService(self.callback_url)
```

### 7. Using KBUtilLib in the Implementation

The current implementation uses raw SDK clients. To leverage KBUtilLib:

**Update the Impl header:**
```python
#BEGIN_HEADER
import os
import json
import uuid
import shutil
import logging

from installed_clients.KBaseReportClient import KBaseReport
from installed_clients.DataFileUtilClient import DataFileUtil
from kbutillib import KBWSUtils, KBCallbackUtils, SharedEnvUtils

class DashboardUtils(KBWSUtils, KBCallbackUtils, SharedEnvUtils):
    """Composable utility class for dashboard operations."""
    pass
#END_HEADER
```

**Use in constructor:**
```python
def __init__(self, config):
    #BEGIN_CONSTRUCTOR
    self.callback_url = os.environ['SDK_CALLBACK_URL']
    self.shared_folder = config['scratch']
    self.config = config
    self.utils = DashboardUtils(callback_url=self.callback_url)
    self.dfu = DataFileUtil(self.callback_url)
    #END_CONSTRUCTOR
```

**Available KBUtilLib methods:**
- `self.utils.get_object(ws_id, ref)` - Get workspace object
- `self.utils.save_object(ws_id, obj_type, obj_data, name)` - Save object
- `self.utils.create_extended_report(params)` - Create report (wraps KBaseReport)

### 8. Writing Tests

The test suite at `test/KBDatalakeDashboard_server_test.py` is currently a skeleton. To add real tests:

```python
def test_run_genome_datalake_dashboard(self):
    # You need a real GenomeDataLakeTables object in your test workspace
    # Option 1: Create one via KBDatalakeApps in a test Narrative
    # Option 2: Upload a test object programmatically

    result = self.serviceImpl.run_genome_datalake_dashboard(
        self.ctx,
        {
            'workspace_name': self.wsName,
            'input_ref': 'YOUR_TEST_OBJECT_REF'  # e.g., "76990/7/2"
        }
    )

    self.assertIn('report_name', result[0])
    self.assertIn('report_ref', result[0])

    # Verify report was created
    report = self.wsClient.get_objects2({
        'objects': [{'ref': result[0]['report_ref']}]
    })['data'][0]['data']

    self.assertTrue(len(report['html_links']) > 0)
    self.assertEqual(report['html_links'][0]['name'], 'index.html')

def test_missing_params(self):
    with self.assertRaises(ValueError):
        self.serviceImpl.run_genome_datalake_dashboard(
            self.ctx,
            {'workspace_name': self.wsName}  # missing input_ref
        )

def test_invalid_ref(self):
    with self.assertRaises(Exception):
        self.serviceImpl.run_genome_datalake_dashboard(
            self.ctx,
            {
                'workspace_name': self.wsName,
                'input_ref': 'invalid/ref'
            }
        )
```

**Running tests:**
```bash
# In Docker:
kb-sdk test

# Or via entrypoint:
docker run kbdatalakedashboard:latest test
```

### 9. Local Notebook Testing

Use `notebooks/util.py` for testing outside Docker:

```python
# In a Jupyter notebook:
from notebooks.util import util

# The util instance is pre-initialized with NotebookUtils + SharedEnvUtils
# It loads tokens from ~/kbutillib_config.yaml

# Create KBDataLakeUtils (mirrors Impl constructor)
kbdl = util.create_kbdl_utils(
    reference_path="./data/reference_data",
    module_path="/Users/chenry/Dropbox/Projects/KBDatalakeApps",
    kb_version="appdev"
)

# Get catalog client to check module status
catalog = util.get_catalog_client("appdev")
```

### 10. Updating the Pre-built Frontend

The frontend JavaScript/CSS bundles in `data/html/assets/` are pre-compiled. To update them:

1. The frontend source is maintained separately (not in this repo)
2. Build the Vue.js app with Vite: `npm run build`
3. Copy the built `dist/` contents to `data/html/`
4. Commit the new bundles
5. Rebuild the Docker image

**Note:** The asset filenames include content hashes (e.g., `main-CLpjqEaD.js`). When updating, `index.html` must reference the new filenames.

## Build and Deployment

### Local Docker Build
```bash
cd /Users/chenry/Dropbox/Projects/KBDatalakeDashboard
docker build -t kbdatalakedashboard:latest .
```

### Running Locally
```bash
# Start server
docker run -p 5000:5000 kbdatalakedashboard:latest

# Interactive shell
docker run -it kbdatalakedashboard:latest bash

# Run tests
docker run -e KB_AUTH_TOKEN=$KB_AUTH_TOKEN kbdatalakedashboard:latest test
```

### SDK Test
```bash
kb-sdk test
```

### Deploying to KBase
1. Push changes to GitHub
2. Register with KBase catalog (or update existing registration)
3. The module name in kbase.yml is `KBDatalakeDashboard2`

## Troubleshooting

### "HTML report is blank"
- Check that `data/html/index.html` exists and references correct asset filenames
- Verify the Shock upload succeeded (check logs for shock_id)
- Check that `app-config.json` was written with correct UPA

### "TableScanner API returns error"
- Verify the GenomeDataLakeTables object exists and is accessible
- Check the API URL in `config/index.json` (appdev vs prod)
- Ensure the user's auth token is valid for the workspace

### "Module won't compile"
- Run `make clean` then `make all`
- Ensure KIDL spec syntax is correct
- Check that module name in spec matches `kbase.yml`
- Note: kbase.yml says `KBDatalakeDashboard2` but KIDL module is `KBDatalakeDashboard`

### "Docker build fails on dependencies"
- Check `requirements.txt` for version conflicts
- The cobrakbase checkout is pinned to a specific commit - verify it exists
- ModelSEEDpy and KBUtilLib use latest from cshenry fork

### "Tests fail with SDK_CALLBACK_URL error"
- Tests must run inside the SDK test harness (via `kb-sdk test`)
- The `SDK_CALLBACK_URL` environment variable is set by the test framework
- For notebook testing, use `notebooks/util.py` instead

## Architecture Decisions and Rationale

### Why pre-built frontend?
The frontend is a complex Vue.js application. Building it requires Node.js and npm, which would bloat the Docker image. Pre-compiling keeps the image lean and the build fast.

### Why minimal backend?
The GenomeDataLakeTables data is accessed via the TableScanner service, not through the backend. The backend only needs to package the viewer and tell it what to display. This keeps the module simple and fast.

### Why app-config.json?
The frontend runs in an iframe with no direct communication to the KBase SDK backend. The app-config.json file is the simplest way to pass runtime parameters (the UPA) to the frontend.

### Why so many Python dependencies?
The requirements.txt includes pandas, cobra, ModelSEEDpy, etc. - these are inherited from the project's broader ecosystem. The dashboard module itself only uses the standard library + DataFileUtil + KBaseReport. The extra deps are present because the Docker image is shared with notebook testing workflows.

## Key Code Locations Quick Reference

| What | Where |
|------|-------|
| Implementation entry point | `lib/KBDatalakeDashboard/KBDatalakeDashboardImpl.py:58` |
| Parameter validation | `lib/KBDatalakeDashboard/KBDatalakeDashboardImpl.py:37-41` |
| HTML copy step | `lib/KBDatalakeDashboard/KBDatalakeDashboardImpl.py:80-81` |
| app-config.json write | `lib/KBDatalakeDashboard/KBDatalakeDashboardImpl.py:83-89` |
| Shock upload | `lib/KBDatalakeDashboard/KBDatalakeDashboardImpl.py:92-96` |
| Report creation | `lib/KBDatalakeDashboard/KBDatalakeDashboardImpl.py:108-119` |
| KIDL spec | `KBDatalakeDashboard.spec` |
| UI spec | `ui/narrative/methods/run_genome_datalake_dashboard/spec.json` |
| UI display | `ui/narrative/methods/run_genome_datalake_dashboard/display.yaml` |
| Frontend entry | `data/html/index.html` |
| Frontend config | `data/html/config/index.json` |
| Table config | `data/html/config/tables/default-config.json` |
| Config schema | `data/html/config/schemas/config.schema.json` |
| Docker | `Dockerfile` |
| Test suite | `test/KBDatalakeDashboard_server_test.py` |
| Notebook utils | `notebooks/util.py` |
