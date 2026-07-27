# Contributing to KBUtilLib

KBUtilLib uses a **one-registry, many-transports** architecture. You add a capability once — decorated with `@capability` — and it automatically appears in the CLI (`kbu cap list`), the MCP server (`kbu-mcp`), and the HTTP API (`kbu-api`). No transport wiring needed.

---

## Repo layout

```
src/kbutillib/
├── core/           ← registry, @capability decorator, errors, base classes
├── domains/        ← all scientific implementations
│   └── <domain>/   ← __init__.py, README.md, <mod>.py, schemas.py (optional)
├── interfaces/     ← mcp/, api/, cli/, docs/ (transport adapters)
├── agents/         ← KING bundles
└── toolkit.py      ← KBUtilLib facade
tests/
├── domains/        ← smoke tests per domain
└── core/           ← registry and core tests
```

---

## Add a new domain capability

This is the most common contribution. Follow these steps exactly — biochem is the reference implementation.

### Step 1 — Find or create the domain

Capabilities live in `src/kbutillib/domains/<domain>/<module>.py`. The `*Impl` class is the concrete implementation; the un-suffixed class is the public-facing alias.

### Step 2 — Write the method on the `*Impl` class, then decorate with `@capability`

```python
from ...core.capability import capability

@capability(
    domain="biochem",
    summary="Return all database aliases for a ModelSEED compound ID.",
    tags=("biochem", "lookup", "readonly"),
    visibility="public",
)
def get_compound_aliases(self, compound_id: str) -> list[str]:
    ...
```

The decorator is **inert at call time** — it only attaches metadata and registers the capability. `name` defaults to `f"{domain}.{fn.__name__}"`. `transports` defaults to `{"lib", "cli", "mcp", "api"}`.

### Step 4 — Handle optional dependencies

For complex return types, add a pydantic v2 model to `schemas.py` and pass `output_model=MyResult` in the decorator.

If the method requires a package that may not be installed, check availability inside the method — not at import time:

```python
@property
def available(self) -> bool:
    try:
        import rdkit  # noqa
        return True
    except ImportError:
        return False

@property
def unavailable_reason(self) -> str | None:
    return None if self.available else "rdkit is required: pip install rdkit"
```

Raise `BackendUnavailableError` (from `kbutillib.core.errors`) when a capability is called without its deps. Never raise `ImportError` at module level.

### Step 6 — Write a smoke test

```python
# tests/domains/test_biochem_smoke.py
def test_get_compound_aliases_capability():
    kbu = KBUtilLib()
    fn = kbu.biochem.get_compound_aliases
    assert hasattr(fn, "__kbu_capability__")
```

### Step 7 — Verify

```console
kbu cap list --domain biochem    # new capability appears
python -m pytest tests/domains/test_biochem_smoke.py -q
```

---

## Add a new domain

1. Create `src/kbutillib/domains/<d>/` with:
   - `__init__.py` — module docstring describing the domain; re-export public classes
   - `README.md` — follow the template in existing domains
   - `<mod>.py` — `*Impl` class with `@capability`-decorated methods
2. Add a lazy facade property to `toolkit.py`:
   ```python
   @cached_property
   def my_domain(self) -> MyDomainImpl:
       from .domains.my_domain.my_mod import MyDomainImpl
       return MyDomainImpl(config=self.config)
   ```
3. Export the public class in `src/kbutillib/__init__.py`.
4. Create `tests/domains/test_my_domain_smoke.py`.

---

## Testing

```console
python -m pytest tests/ -q
```

- Guard optional-dep tests with `pytest.importorskip("rdkit")` (not `--ignore`).
- Network tests should use `pytest.mark.network` and be skippable offline.
- Gate: ≥2204 passing, 0 new failures, 0 collection errors.

---

## Code style

```console
ruff check src/kbutillib      # linting
ruff format src/kbutillib     # formatting
mypy src/kbutillib            # type checking
```

- Type-annotate all public method arguments and return types.
- Every public method needs a one-line docstring at minimum.
- Module-level imports of optional packages are forbidden — use lazy imports inside methods.
- File length: 250 LOC ceiling for pure-logic modules; split if larger.

---

## Docs

After adding a new capability, regenerate the catalog:

```console
python -m kbutillib.interfaces.docs.gen_capabilities
```

Add a `docs/modules/<domain>_<mod>.md` mkdocstrings page and reference it in `mkdocs.yml`.
