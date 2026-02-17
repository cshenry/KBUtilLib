# KBDatalakeApps Development Guide

## Common Development Tasks

### Adding a New Parameter to the App

1. **Update KIDL spec** (`KBDatalakeApps.spec`):
```kidl
typedef structure {
    list<string> input_refs;
    string suffix;
    int save_models;
    string workspace_name;
    int my_new_param;          # Add here
} BuildGenomeDatalakeTablesParams;
```

2. **Run `make`** to regenerate server code

3. **Update spec.json** (`ui/narrative/methods/build_genome_datalake_tables/spec.json`):
```json
{
    "id": "my_new_param",
    "optional": true,
    "advanced": true,
    "allow_multiple": false,
    "default_values": ["0"],
    "field_type": "checkbox",
    "checkbox_options": {
        "checked_value": 1,
        "unchecked_value": 0
    }
}
```
And add to `input_mapping`:
```json
{
    "input_parameter": "my_new_param",
    "target_property": "my_new_param",
    "target_type_transform": "int"
}
```

4. **Update display.yaml** with UI label:
```yaml
my_new_param:
    ui-name: |
        My New Parameter
    short-hint: |
        Description of what it does
    long-hint: |
        Longer description
```

5. **Use in Impl** (`KBDatalakeAppsImpl.py`):
```python
my_new_param = params.get('my_new_param', 0) == 1
```

### Adding a New Annotation Source

1. **Install the client** - Add to `kb-sdk install <module>` or manually create client

2. **Initialize client in constructor** (`KBDatalakeAppsImpl.__init__`):
```python
self.new_client = NewModule(self.callback_url, service_ver='beta')
```

3. **Create annotation function** (`lib/KBDatalakeApps/annotation/annotation.py`):
```python
def run_new_annotation(client, genome_file_input, output_file):
    genome = MSGenome.from_fasta(str(genome_file_input))
    proteins = {f.id: f.seq for f in genome.features if f.seq}
    result = client.annotate_proteins(proteins)
    # Parse result and write TSV
    with open(str(output_file), 'w') as fh:
        fh.write('feature_id\tNewAnnotation\n')
        for feature_id, value in result.items():
            fh.write(f'{feature_id}\t{value}\n')
```

4. **Create task wrapper** (`lib/KBDatalakeApps/executor/task.py`):
```python
def task_new_annotation(filename_faa: Path, client):
    print(f'start task_new_annotation: {filename_faa}')
    if filename_faa.exists():
        _parent = filename_faa.parent
        output = _parent / f'{filename_faa.name[:-4]}_annotation_new.tsv'
        run_new_annotation(client, filename_faa, output)
        return output
    raise ValueError(f"invalid: {filename_faa}")
```

5. **Add to parallel execution** (`KBDatalakeAppsImpl.build_genome_datalake_tables`):
```python
tasks_input_genome.append(executor.run_task(
    task_new_annotation, path_user_genome / filename_faa, self.new_client))
```

6. **The table builder will automatically pick up** the new TSV because `collect_annotation()` scans for `*_annotation_*.tsv` files.

### Adding a New SQLite Table

1. **Generate the data** - Create a method in `KBDatalakeUtils`:
```python
def build_my_new_table(self, database_path=None, data_dir=None):
    rows = []  # Build your data
    if database_path is None:
        # TSV mode
        df = pd.DataFrame(rows)
        df.to_csv(os.path.join(data_dir, "my_table.tsv"), sep='\t', index=False)
    else:
        # SQLite mode
        conn = sqlite3.connect(database_path)
        df = pd.DataFrame(rows)
        df.to_sql('my_table', conn, if_exists='replace', index=False)
        conn.close()
```

2. **Call it from the Impl** at the right point in the pipeline:
```python
self.util.build_my_new_table(model_path=str(path_root / 'models'))
```

3. **Or from DatalakeTableBuilder** if it's per-clade data - add a method to `DatalakeTableBuilder.build()`.

### Adding a New Pipeline Stage

If it needs the **berdl_genomes** environment:
1. Create a CLI script in `berdl/berdl/bin/my_pipeline.py` with `main()` + `__main__` block
2. Create a shell wrapper in `scripts/run_my_pipeline.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
source /opt/env/berdl_genomes/bin/activate
/opt/env/berdl_genomes/bin/python /kb/module/berdl/berdl/bin/my_pipeline.py "$@"
```
3. Call from Impl:
```python
@staticmethod
def run_my_pipeline(input_file):
    cmd = ["/kb/module/scripts/run_my_pipeline.sh", str(input_file)]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen(cmd, stdout=None, stderr=None, env=env)
    ret = process.wait()
    if ret != 0:
        raise RuntimeError(f"my pipeline failed with exit code {ret}")
```

If it runs in **SDK Python**:
- Add it directly in `berdl/bin/` or `lib/KBDatalakeApps/`
- Call via `run_model_pipeline.sh` pattern (no venv activation)
- Can use ProcessPoolExecutor for parallelism

---

## Key Patterns

### Subprocess Pipeline Pattern

All heavy pipelines run as subprocesses to isolate Python environments:

```python
@staticmethod
def run_pipeline_x(input_file):
    cmd = ["/kb/module/scripts/run_x.sh", str(input_file)]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)  # Critical: remove SDK PYTHONPATH for berdl venv
    process = subprocess.Popen(cmd, stdout=None, stderr=None, env=env)
    ret = process.wait()
    if ret != 0:
        raise RuntimeError(f"pipeline failed with exit code {ret}")
```

Key points:
- `env.pop("PYTHONPATH", None)` is essential for berdl venv scripts
- `stdout=None, stderr=None` inherits parent stdout/stderr for logging
- Shell scripts handle venv activation

### Parallel Annotation Pattern

```python
executor = TaskExecutor(max_workers=4)
tasks = []
for filename_faa in genome_faa_files:
    tasks.append(executor.run_task(task_rast, faa_path, self.rast_client))
    tasks.append(executor.run_task(task_kofam, faa_path, self.kb_kofam))
    tasks.append(executor.run_task(task_bakta, faa_path, self.kb_bakta))
    tasks.append(executor.run_task(task_psortb, faa_path, '-n', self.kb_psortb))

# Wait for specific tasks before proceeding
for t in rast_tasks:
    t.wait()  # RAST needed before modeling

# Wait for all tasks
for t in tasks:
    t.wait()
    print(t.status, t.result, t.traceback)
executor.shutdown()
```

### ProcessPoolExecutor Pattern (for CPU-heavy work)

```python
from concurrent.futures import ProcessPoolExecutor, as_completed

with ProcessPoolExecutor(max_workers=10) as executor:
    futures = {
        executor.submit(run_model_reconstruction, inp, outp, classifier_dir, kbversion): inp
        for inp, outp in work_items
    }
    for future in as_completed(futures):
        inp = futures[future]
        try:
            result = future.result()
            print(f"OK: {inp}" if result.get('success') else f"FAIL: {result.get('error')}")
        except Exception as e:
            print(f"ERROR: {e}")
```

### KBUtilLib Composable Class Pattern

```python
from kbutillib import KBGenomeUtils, MSReconstructionUtils, MSFBAUtils

class KBDataLakeUtils(KBGenomeUtils, MSReconstructionUtils, MSFBAUtils):
    def __init__(self, reference_path, module_path, **kwargs):
        super().__init__(name="KBDataLakeUtils", **kwargs)
        self.module_path = module_path
        self.reference_path = reference_path
```

This gives access to all methods from: KBGenomeUtils (genome parsing), MSReconstructionUtils (model building, gapfilling), MSFBAUtils (media, FBA).

### Dual-output Pattern (TSV or SQLite)

Many methods support both output modes:
```python
def build_model_tables(self, database_path=None, model_path=None):
    # ... build rows ...
    if database_path is None:
        # TSV output mode
        df.to_csv(os.path.join(output_dir, "file.tsv"), sep='\t', index=False)
    else:
        # SQLite output mode
        conn = sqlite3.connect(database_path)
        df.to_sql('table_name', conn, if_exists='replace', index=False)
        conn.close()
```

---

## Troubleshooting

### Common Issues

**1. "genome pipeline failed with exit code 1"**
- Usually a missing or invalid genome reference
- Check that input_refs resolve to actual Genome objects
- Verify KBase token is valid for the target workspace
- Check berdl_genomes venv has all dependencies

**2. "No RAST annotations found"**
- The model pipeline needs RAST annotations to build models
- Check that `_rast.tsv` files exist in genome/ directory
- Verify the RAST service is responding (callback URL)
- Check the TSV format: simple (id, functions) vs full genome TSV

**3. Modeling pipeline fails silently**
- run_model_reconstruction returns `{'success': False, 'error': ...}` instead of raising
- Check `_data.json` files for success field
- Common cause: genome has no protein-coding features with RAST annotations

**4. PYTHONPATH conflicts**
- The berdl_genomes venv has different packages than SDK Python
- Always `env.pop("PYTHONPATH", None)` before subprocess calls
- The model pipeline does NOT pop PYTHONPATH (it needs SDK packages)

**5. Missing reference data**
- `/data/` mount must contain `reference_data/berdl_db/` and `reference_data/phenotype_data/`
- The module checks for these paths at startup
- Missing data causes silent failures in ANI and fitness lookups

**6. OntologyEnrichment API failures**
- BERDL API requires valid service account token
- KEGG API has rate limits (but loads all 26K KOs in one call)
- COG FTP sometimes goes down; results are cached after first load

### Development Workflow

1. **Local testing with notebooks**: Use `notebooks/test_pipeline_steps.ipynb` to test individual pipeline stages
2. **Skip flags**: Use `skip_genome_pipeline`, `skip_annotation`, `skip_pangenome`, `skip_modeling_pipeline` to bypass stages
3. **Docker build**: `docker build -t test/kbdatalakeapps:latest .`
4. **SDK test**: `kb-sdk test` (needs token in `test_local/test.cfg`)
5. **Export flags for debugging**: Enable `export_folder_models`, `export_folder_phenotypes` to download intermediate results

### Adding Dependencies

**For SDK Python env:**
- Add to `requirements.txt`
- `RUN pip install` in Dockerfile

**For berdl_genomes venv:**
- Add to `berdl/requirements.txt`
- `RUN /root/.local/bin/uv pip install --python /opt/env/berdl_genomes ...` in Dockerfile

**For /deps/ git repos:**
- Add git clone to Dockerfile
- Add to sys.path in `KBDatalakeUtils.py` line 13:
```python
sys.path = ["/deps/KBUtilLib/src", "/deps/cobrakbase", "/deps/ModelSEEDpy", ...] + sys.path
```

---

## Current Known TODOs and WIP

1. **KBUtilLib not pip-installed** - KBUtilLib import is via commented-out code in Impl (lines 31-35) and sys.path manipulation in KBDatalakeUtils (line 13). The Dockerfile clones it to /deps/ but doesn't `pip install -e`.

2. **Output object not saved** - The GenomeDataLakeTables workspace object creation is commented out (Impl lines 461-474, 598-605). Currently only produces reports.

3. **PSORTb organism flag hardcoded** - `task_psortb` uses '-n' (Gram-negative) hardcoded. Should detect organism type first.

4. **HTML viewer UPA hardcoded** - `app_config["upa"]` is set to `"76990/Test2"` (Impl line 617). Should use actual input reference.

5. **build_berdl_db.py exists but unused** - The SQLite building code in this file is not called; the DatalakeTableBuilder in berdl/ is used instead.

6. **BERDL library code commented out** - QueryPangenomeBERDL import is commented out in KBDatalakeUtils.py (lines 26-33).

---

## Testing

### Unit Test Pattern

```python
# test/KBDatalakeApps_server_test.py
class KBDatalakeAppsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        token = environ.get('KB_AUTH_TOKEN')
        config_file = environ.get('KB_DEPLOYMENT_CONFIG')
        cls.cfg = {}
        # ... setup ...
        cls.serviceImpl = KBDatalakeApps(cls.cfg)
        cls.scratch = cls.cfg['scratch']

    def test_build_genome_datalake_tables(self):
        ret = self.serviceImpl.build_genome_datalake_tables(
            self.ctx,
            {
                'workspace_name': self.wsName,
                'input_refs': ['77057/3/1'],
                'suffix': 'test',
                'save_models': 0,
                'skip_annotation': 0,
                'skip_pangenome': 0,
                'skip_genome_pipeline': 0,
                'skip_modeling_pipeline': 0,
                'export_genome_data': 0,
                'export_pangenome_data': 0,
                'export_all_content': 0,
                'export_databases': 1,
                'export_folder_models': 0,
                'export_folder_phenotypes': 0,
            }
        )
        self.assertIsNotNone(ret[0]['report_ref'])
```

### Notebook Testing

The notebooks provide step-by-step testing:
- `RunBERDLTablesPipeline.ipynb` - Full pipeline execution
- `test_pipeline_steps.ipynb` - Individual stage testing
- `module_registration_testing.ipynb` - Registration/deployment testing
