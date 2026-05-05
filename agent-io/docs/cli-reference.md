# kbu CLI Reference

The `kbu` CLI is the developer interface for KBUtilLib. It bootstraps notebook projects with per-project virtual environments, template files, and Jupyter kernel registration.

---

## Bootstrap (one-time per machine)

The `bin/kbu` shell wrapper sets `PYTHONPATH` so that `kbutillib` is importable without a prior `pip install`. Symlink it into your PATH once:

```bash
ln -sf ~/Dropbox/Projects/KBUtilLib/bin/kbu ~/.local/bin/kbu
```

Verify:

```bash
kbu --version
kbu --help
```

If you prefer a different Python interpreter, set `KBU_PYTHON`:

```bash
KBU_PYTHON=python3.13 kbu --version
```

---

## `kbu init-notebook`

Bootstrap a notebook project with a per-project venv and templates.

### Synopsis

```bash
cd /path/to/NotebookRepo
kbu init-notebook [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--project NAME` | Current directory basename | Project name. Auto-slugified (lowercase, hyphens only). |
| `--python VER` | From machine config (`default_python`) | Python version for the venv. |
| `--alias NAME` | Auto-resolved (see below) | Override machine alias resolution. |
| `--force` | Off | Overwrite util.py header (preserves custom code below marker). Force-pin all notebook kernels, including non-kbu ones. |
| `--no-pin-kernels` | Off | Skip Jupyter kernel registration and `.ipynb` metadata pinning. |
| `--no-venv` | Off | Skip venv creation via venvman. Only generate template files. |

### What it does

1. **Resolves project name**: slugifies the directory basename (e.g., `My Project` becomes `my-project`).
2. **Resolves machine alias**: 4-level fallback (AgentForge import, YAML parse, hardware UUID, interactive prompt).
3. **Loads merged machine config**: `KBUtilLib/machine_configs/_default.yaml` deep-merged with `<alias>.yaml`.
4. **Creates venv**: `venvman create --project kbu.nb-<project> --dir <cwd> --python <ver>`.
5. **Installs editable dependencies**: `pip install -e` for each path in `editable_installs`.
6. **Installs notebook dependencies**: `pip install` for each package in `notebook_deps`.
7. **Renders `notebooks/util.py`**: Jinja template with NotebookSession, common imports, and a marker for custom code.
8. **Registers Jupyter kernel**: `ipykernel install --user --name kbu.nb-<project>`.
9. **Pins notebook kernels**: Updates `kernelspec` in all `*.ipynb` files (only kbu.nb-* and unset kernels unless `--force`).

### Examples

Basic bootstrap:

```bash
cd ~/Dropbox/Projects/ADP1Notebooks
kbu init-notebook
source activate.sh
jupyter lab
```

With explicit options:

```bash
kbu init-notebook --project adp1 --python 3.13 --alias emailmac
```

Template-only mode (no venv):

```bash
kbu init-notebook --no-venv --alias primary-laptop
```

Force-refresh after KBUtilLib template update:

```bash
kbu init-notebook --force
```

---

## Machine Configuration

Per-machine config lives in `KBUtilLib/machine_configs/`:

```
machine_configs/
  _default.yaml       # baseline config (all machines)
  primary-laptop.yaml  # overrides for primary-laptop
  emailmac.yaml        # overrides for emailmac
  h100.yaml            # overrides for h100
```

The `_default.yaml` is deep-merged with `<alias>.yaml` (alias wins on conflicts).

### Config keys

| Key | Type | Description |
|---|---|---|
| `default_python` | string | Python version for venvs (e.g., `"3.12"`) |
| `editable_installs` | list[str] | Paths to repos installed as editable (`pip install -e`) |
| `notebook_deps` | list[str] | PyPI packages installed into the venv |
| `hardware_uuids` | list[str] | Hardware UUIDs for auto-detection (optional) |

---

## Troubleshooting

### Broken venv

If you see `Broken venv detected: <path> exists but <path>/bin/python is missing`:

```bash
venvman destroy kbu.nb-<project>
kbu init-notebook
```

### Hardware UUID detection fails

The CLI falls back to interactive prompt. To pre-configure:

1. Find your UUID:
   - macOS: `ioreg -d2 -c IOPlatformExpertDevice | grep IOPlatformUUID`
   - Linux: `cat /etc/machine-id`
2. Add it to `machine_configs/<alias>.yaml`:
   ```yaml
   hardware_uuids:
     - "YOUR-UUID-HERE"
   ```

### Alias not in any machine_config

Set `worker.machine_alias` in `~/.agentforge/config.yaml`:

```yaml
worker:
  machine_alias: "my-machine"
```

Or create a new `machine_configs/my-machine.yaml`.

### venvman not found

Install venvman:

```bash
pip install venvman
```

Or see: https://github.com/cshenry/EnvironmentManager
