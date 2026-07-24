# kbutillib.core — Foundation Layer

`core/` is the foundation every other package builds on. It provides the capability registry, the `@capability` decorator, the error hierarchy, configuration loading, and the base utility classes that all domain modules inherit. Nothing in `core/` imports from any domain package — it is a strict dependency floor.

---

## CapabilityRegistry

`CapabilityRegistry` is a runtime catalog of every callable capability in the library. Each entry is a `CapabilitySpec` — a frozen dataclass holding the capability's name, bound callable, domain, description, tags, optional pydantic input/output models, visibility, and the transports it is exposed on (`lib`, `cli`, `mcp`, `api`).

The registry is a process-wide singleton, accessed via `get_registry()`. Transports (CLI, FastAPI, MCP server) query it at startup to build their routing tables. Capabilities can also be looked up by name, filtered by domain or tag, or introspected for availability before a call is dispatched.

---

## `@capability` decorator

```python
@capability(name="biochem.search_compounds", domain="biochem", summary="Search compounds by name or InChI.")
def search_compounds(self, query: str) -> list[dict]:
    ...
```

The decorator is **completely inert to calls** — it returns the original function unchanged, with no wrapper. Its only effect is attaching a `CapabilityDraft` to `fn.__kbu_capability__`. This means decorated methods behave identically in tests and direct use; the metadata is only read at registration time.

Required keyword: `domain`. Optional: `name` (defaults to `f"{domain}.{fn.__name__}"`), `summary`, `tags`, `input_model`, `output_model`, `visibility`, `transports`, `availability`.

---

## `register_all(app, registry)`

```python
from kbutillib.core.capability import register_all
specs = register_all(app, registry)
```

`register_all` wires a facade's utility objects into the registry in one call. It iterates every attribute on `app` that looks like a `*Impl` utility object, finds methods decorated with `@capability`, builds `CapabilitySpec` instances, and registers them. The bound method stored in each spec preserves any dependency injection already set up on the facade. Util attributes that raise on access are logged and skipped — nothing crashes at import time.

This is called once at app startup (CLI init, FastAPI lifespan, MCP server init).

---

## Error hierarchy

```
KBUtilLibError
├── BackendUnavailableError(backend, reason)   # missing dep or unreachable service
├── CapabilityError                            # base for registry errors
│   └── CapabilityNotFound(name)              # no capability with that dotted name
```

Catch `BackendUnavailableError` to implement graceful degradation — an unavailable optional backend (e.g. `rdkit`, `equilibrator`) must never crash import or registry startup. `CapabilityNotFound` is raised when a transport receives a name that does not exist in the registry.

---

## Config

`load_config(path=None)` reads and validates a YAML config file using pydantic v2. Without a path argument it resolves `config.yaml` from the repo root; if no file exists it returns a `Config` instance with all defaults — no error, no warning beyond a debug log.

`Config.get(dotted_key, default=None)` retrieves nested values using dot notation:

```python
from kbutillib.core.config import load_config
cfg = load_config()
ws_url = cfg.get("workspace.url", "https://ci.kbase.us/services/ws")
```

---

## BaseUtils

`BaseUtils` is the parent class for all utility modules. It sets up structured logging, initializes provenance-tracking attributes (`method`, `params`, `timestamp`, `service`, `obj_created`, `input_objects`), and provides `reset_attributes()` and `initialize_call()` helpers. Domain util classes inherit from `BaseUtils` directly or via `SharedEnvUtils`.

---

## SharedEnvUtils

`SharedEnvUtils(BaseUtils)` centralizes access to configuration files, environment variables, and authentication tokens. Config priority: explicit `config_file` arg → `~/.kbutillib/config.yaml` → repo-root `config.yaml`. Tokens are read from `~/.tokens` and `~/.kbase/token` by default, or injected directly as a string or namespace dict. All 37+ domain utility classes that need env config inherit from or instantiate `SharedEnvUtils`.

---

## Quick example

```python
from kbutillib.core.registry import CapabilityRegistry, get_registry
from kbutillib.core.capability import capability, register_all
from kbutillib.core.errors import CapabilityNotFound

class MyUtils:
    @capability(domain="demo", summary="Return a greeting.")
    def hello(self, name: str) -> str:
        return f"Hello, {name}!"

class MyApp:
    my = MyUtils()

app = MyApp()
registry = get_registry()
register_all(app, registry)

spec = registry.get("demo.hello")          # CapabilitySpec
result = spec.fn(name="world")             # "Hello, world!"
```

---

## Files in this package

| File | Purpose |
|------|---------|
| `registry.py` | `CapabilityRegistry`, `CapabilitySpec` dataclass |
| `capability.py` | `@capability` decorator, `register_all()`, `collect_capabilities()` |
| `errors.py` | `KBUtilLibError`, `BackendUnavailableError`, `CapabilityError`, `CapabilityNotFound` |
| `config.py` | `Config`, `load_config()` — pydantic v2 config schema |
| `base_utils.py` | `BaseUtils` — logging, provenance helpers |
| `shared_env_utils.py` | `SharedEnvUtils` — tokens, env vars, config access |
| `dependency_manager.py` | `DependencyManager` — resolve optional deps from `dependencies.yaml` |
