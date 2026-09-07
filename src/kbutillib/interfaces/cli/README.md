# kbutillib.interfaces.cli — CLI (`kbu`)

The `kbu` command-line interface. This is the canonical implementation; `src/kbutillib/cli/` is a back-compat shim that re-exports everything from here.

## Install

```console
pip install -e .
```

The `kbu` script is registered automatically — no extras required.

## Command reference

```console
kbu --help          # top-level help and command list
kbu --version       # print kbutillib version
```

### Capability commands

```console
kbu cap list                    # table of all registered capabilities
kbu cap list --domain biochem   # filter by domain
kbu cap info <name>             # signature, summary, tags, availability
kbu cap run <name> [--arg val]  # invoke a capability from the shell
```

### Developer tools

```console
kbu new-capability              # interactive scaffold for a new @capability
kbu doctor                      # check all backend availability (exits 0 if all ok)
```

### KIND agent bundle commands

```console
kbu kind status                 # show installed KIND bundles
kbu kind install <bundle>       # install a KIND bundle
kbu kind uninstall <bundle>     # remove a KIND bundle
```

## What lives here

| File | Purpose |
|------|---------|
| `__init__.py` | Click group root (`kbu`); entry point for the `kbu` script |
| `cap.py` | `kbu cap` subgroup — list, info, run |
| `bootstrap.py` | `kbu bootstrap` / `kbu new-project` project scaffolding |
|  `kind.py` | `kbu kind` subgroup — bundle install/uninstall/status |
| `doctor.py` | `kbu doctor` — per-backend availability diagnostics |
| `new_capability.py` | `kbu new-capability` — interactive capability scaffolder |
| `templates/` | Jinja2 templates used by `new-capability` |

## Back-compat note

`from kbutillib.cli import main` still works — `src/kbutillib/cli/__init__.py` re-exports from this package. Prefer `from kbutillib.interfaces.cli import main` for new code.
