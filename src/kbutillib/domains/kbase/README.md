# kbutillib.domains.kbase — KBase Platform Utilities

Client layer for the KBase platform: workspace API, SDK service calls, narrative auditing,
catalog lookups, and endpoint resolution.

---

## What lives here

All direct KBase platform interactions live here. These utilities authenticate via a KBase token
(read from `~/.kbase/token` or the `KB_AUTH_TOKEN` env var by `SharedEnvUtils`) and communicate
with KBase microservices over HTTP. No heavy Python dependencies beyond `requests`.

## Canonical imports

```python
from kbutillib.domains.kbase.kb_ws_utils import KBWSUtils
from kbutillib.domains.kbase.kb_sdk_utils import KBSDKUtils
from kbutillib.domains.kbase.kbase_endpoints import get_endpoint
```

Or via top-level re-exports:

```python
from kbutillib import KBWSUtils, KBSDKUtils
```

---

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `kb_ws_utils.py` | `KBWSUtils`, `KBWSUtilsImpl` | Workspace API — get/save/list objects, manage permissions |
| `kb_sdk_utils.py` | `KBSDKUtils`, `KBSDKUtilsImpl` | SDK service client helpers and app run management |
| `kb_callback_utils.py` | `KBCallbackUtils`, `KBCallbackUtilsImpl` | SDK callback service client for app-to-app calls |
| `kb_reads_utils.py` | `KBReadsUtils`, `KBReadsUtilsImpl` | Reads library object handling (paired/single/interleaved) |
| `kb_berdl_utils.py` | `KBBerdlUtils`, `KBBerdlUtilsImpl` | BERDL (Bulk Export/Retrieve/Download) pipeline utilities |
| `kb_narrative_audit.py` | `KBNarrativeAudit` | Parse and audit KBase narrative objects for provenance |
| `kbase_endpoints.py` | — | Endpoint resolver: returns service URLs for CI/next/prod environments |
| `kbase_catalog_client.py` | — | KBase catalog service client for app/module metadata |

---

## Optional dependencies

A valid KBase authentication token is required for all workspace and SDK calls:

```bash
export KB_AUTH_TOKEN="your-token-here"
# or place the token in ~/.kbase/token
```

`KBWSUtils.available` returns `False` when no token is found, and `unavailable_reason` explains
how to obtain one. No additional Python packages beyond `requests` are needed.

---

## Usage example

```python
from kbutillib.domains.kbase.kb_ws_utils import KBWSUtils
from kbutillib.domains.kbase.kbase_endpoints import get_endpoint

# Workspace operations (requires KB_AUTH_TOKEN)
ws = KBWSUtils()
print(ws.available)  # False if no token found

obj = ws.get_object(workspace="my_ws", ref="my_ws/my_genome/1")
objects = ws.list_objects(workspace="my_ws", type="KBaseGenomes.Genome")

# Endpoint resolution — no auth required
ws_url = get_endpoint("workspace", env="prod")
print(ws_url)  # https://kbase.us/services/ws
```

Via the facade:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
obj = kbu.ws.get_object(workspace="my_ws", ref="my_ws/my_genome/1")
```

---

## Available capabilities

Capabilities are registered on each `*Impl` class where they are present. Run:

```bash
kbu cap list --domain kbase
```

---

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to the relevant `*Impl` class.
2. Decorate with `@capability(domain="kbase", summary="...", tags=("kbase",))`.
3. Capabilities requiring auth should use the `availability=` parameter or rely on the class
   `available` property (which checks for the token).
4. Run `kbu cap list --domain kbase` to confirm.

Network-dependent tests should use `pytest.mark.network` and skip when offline or when no token
is present.
