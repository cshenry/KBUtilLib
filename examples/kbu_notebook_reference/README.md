# kbu_notebook_reference

A minimal, offline reference project demonstrating the canonical **FLAT** KBUtilLib
notebook layout end-to-end.

## Layout

```
kbu_notebook_reference/
├── notebooks/
│   ├── util.py          # Rendered from cli/templates/util.py.tmpl; run with %run util.py
│   ├── reference.ipynb  # Single-cell exemplar: compute + cache save/load
│   └── .kbcache/
│       ├── .gitignore   # Excludes catalog.sqlite; commits only the blob(s) below
│       └── blobs/
│           └── 265fc4c1...083d0.json  # Committed exemplar artifact (dict, ~110 bytes)
├── data/                # Input data placeholder (empty in this exemplar)
├── figures/             # Output figures placeholder (empty in this exemplar)
└── README.md
```

## Running the notebook

Open `notebooks/reference.ipynb` in JupyterLab using a healthy KBUtilLib
notebook venv (one with `kbutillib` installed):

```bash
# From the repo root:
jupyter lab examples/kbu_notebook_reference/notebooks/reference.ipynb
```

The single cell does:

1. `%run util.py` — bootstraps the session, path constants, and guarded imports.
2. Computes `6 + 7 = 13` (trivial, fully offline; no BERDL / KBase / network).
3. Saves the result dict as `"reference_result"` via `session.cache.save()`.
4. Loads it back and asserts the round-trip is exact.
5. Asserts the flat path constants (`NOTEBOOK_DIR`, `PROJECT_ROOT`, `DATA_DIR`,
   `FIGURES_DIR`) resolve to the expected locations.

## Cache design

The `.kbcache/` directory is the session cache store:

- **`blobs/`** — content-addressed blob files (`.json`, `.arrow`, etc.).
  The blob committed here (`265fc4c1...083d0.json`) is the dict saved by the
  reference cell. Re-running the cell regenerates the same blob in-place
  (deterministic SHA-256 for the same content).
- **`catalog.sqlite`** — SQLite provenance catalog (access_log, object index).
  This is **NOT committed** (see `.gitignore`). It is regenerated automatically
  when `session.cache.save()` runs. Absence of the catalog does not prevent
  reading blobs that were pre-committed — calling `session.cache.load()` on a
  fresh clone will rebuild the catalog and then find the blob by hash.

## `util.py` fidelity

`notebooks/util.py` is a verbatim render of `cli/templates/util.py.tmpl` with
`project_name = "kbu_notebook_reference"`. To verify:

```bash
# From the KBUtilLib repo root:
python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/kbutillib/cli/templates'))
tmpl = env.get_template('util.py.tmpl')
print(tmpl.render(project_name='kbu_notebook_reference'))
" | diff - examples/kbu_notebook_reference/notebooks/util.py
```

No diff means the exemplar stays in sync with the canonical template.
