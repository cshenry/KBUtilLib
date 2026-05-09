# KBUtilLib Architecture — Composition Over SharedEnvUtils

## Overview

KBUtilLib uses **composition over inheritance**. Every utility class (`*Impl`)
holds a `SharedEnvUtils` instance and zero or more sibling `*Impl` instances
instead of inheriting from `SharedEnvUtils`.

The `KBUtilLib` facade provides lazy-property access to all sub-utilities.

## Quick Start

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()

# Access sub-utilities via lazy properties
kbu.fba.run_fba(model)
kbu.biochem.search_compounds("glucose")
kbu.ws.get_object("12345/6/7")
kbu.genome.reverse_complement("ATCG")
```

## KBUtilLib Facade

```python
class KBUtilLib:
    def __init__(self, env: SharedEnvUtils | None = None, **env_kwargs):
        self.env = env or SharedEnvUtils(**env_kwargs)
```

### Sub-Utility Attribute Namespace

| Attribute     | Impl Class                    | Dependencies              |
|--------------|-------------------------------|---------------------------|
| `ws`         | KBWSUtilsImpl                 | env                       |
| `callback`   | KBCallbackUtilsImpl           | env, ws                   |
| `annotation` | KBAnnotationUtilsImpl         | env, ws, callback         |
| `biochem`    | MSBiochemUtilsImpl            | env                       |
| `model`      | KBModelUtilsImpl              | env, ws, annotation, biochem |
| `fba`        | MSFBAUtilsImpl                | env, model                |
| `recon`      | MSReconstructionUtilsImpl     | env, model                |
| `escher`     | EscherUtilsImpl               | env, model, biochem       |
| `standardize`| ModelStandardizationUtilsImpl | env, biochem              |
| `genome`     | KBGenomeUtilsImpl             | env, ws                   |
| `plm`        | KBPLMUtilsImpl                | env, genome               |
| `bvbrc`      | BVBRCUtilsImpl                | env, genome, annotation   |
| `reads`      | KBReadsUtilsImpl              | env, ws                   |
| `sdk`        | KBSDKUtilsImpl                | env, ws                   |
| `argo`       | ArgoUtilsImpl                 | env                       |
| `curation`   | AICurationUtilsImpl           | env, argo                 |
| `thermo`     | ThermoUtilsImpl               | env, biochem              |
| `mmseqs`     | MMSeqsUtilsImpl               | env                       |
| `skani`      | SKANIUtilsImpl                | env                       |
| `berdl`      | KBBERDLUtilsImpl              | env                       |
| `patric`     | PatricWSUtilsImpl             | env                       |
| `uniprot`    | KBUniProtUtilsImpl            | env                       |
| `pdb`        | RCSBPDBUtilsImpl              | env                       |
| `catalog`    | CatalogClient                 | (standalone)              |
| `jobs`       | KBJobUtils                    | env                       |

## Notebook Integration

```python
class NotebookSession:
    @property
    def kbu(self) -> KBUtilLib:
        if self._kbu is None:
            self._kbu = KBUtilLib(env=self._env)
        return self._kbu
```

Usage:
```python
session = NotebookSession.for_notebook()
session.kbu.fba.run_fba(model)
session.kbu.biochem.search_compounds("glucose")
```

## Legacy Import Compatibility

Legacy class names are exported as aliases in `__init__.py`:

```python
from kbutillib import MSFBAUtils      # still works (legacy alias)
from kbutillib import MSFBAUtilsImpl  # new composition-based class
from kbutillib import KBUtilLib       # facade
```

Constructor signatures have changed (composition takes deps explicitly).

## Flat Modules

Three new flat modules consolidate previously duplicated code:

- **`compartments.py`** — `compartment_types` dict + `normalize_compartment()`
- **`model_helpers.py`** — `_parse_id()` + `_check_and_convert_model()`
- **`model_directionality.py`** — `direction_conversion` dict + analysis functions

## Migration Guide

### From legacy classes to the facade

Old (inheritance-based):

```python
from kbutillib import MSFBAUtils
fba = MSFBAUtils(config_file=False, token_file=None, kbase_token_file=None)
sol = fba.run_fba(model)
```

New (composition-based):

```python
from kbutillib import KBUtilLib
kbu = KBUtilLib()
sol = kbu.fba.run_fba(model)
```

### From notebook god class to session.kbu

Old (inheritance-based god class in ADP1Notebooks `util.py`):

```python
class Util(MSFBAUtils, AICurationUtils, NotebookUtils, KBPLMUtils, EscherUtils):
    ...
```

New:

```python
session = NotebookSession.for_notebook()
session.kbu.fba.run_fba(model)
session.kbu.curation.compare_annotations(...)
session.kbu.plm.embed_sequences(...)
```

### Legacy aliases policy

Legacy class names (`MSFBAUtils`, `KBWSUtils`, etc.) remain importable from `kbutillib` for backward compatibility. These are the **original inheritance-based classes** and still work as before. They will be maintained but not extended.

New code should use the `KBUtilLib` facade or the `*Impl` classes directly.

### AP3 carve-outs

Some `*Impl` classes wrap a legacy delegate internally rather than reimplementing all methods from scratch. These are "AP3 carve-outs" documented in the PRD:

- `ArgoUtilsImpl` — wraps legacy `ArgoUtils` with a lazy delegate (httpx.Client creation is deferred until first method call)
- `MSFBAUtilsImpl` — wraps legacy `MSFBAUtils`
- `KBSDKUtilsImpl` — wraps legacy `KBSDKUtils`

These will be converted to native composition in a future phase.

## Reference Implementations

- `kb_job_utils/` — already composition-based (the reference shape)
- `thermo_utils.py` — lazy-property pattern for biochem_utils
