# kbutillib.domains.kbase — KBase Platform Utilities

Client layer for the KBase platform: workspace API, SDK service calls, narrative auditing, catalog lookups, and endpoint resolution.

## What lives here

All direct KBase platform interactions live here. These utilities authenticate via a KBase token (read from `~/.kbase/token` or the `KB_AUTH_TOKEN` env var by `SharedEnvUtils`) and communicate with KBase microservices over HTTP.

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `kb_ws_utils.py` | `KBWSUtils`, `KBWSUtilsImpl` | Workspace API — get/save/list objects, manage workspace permissions |
| `kb_sdk_utils.py` | `KBSDKUtils`, `KBSDKUtilsImpl` | SDK service client helpers and app run management |
| `kb_callback_utils.py` | `KBCallbackUtils`, `KBCallbackUtilsImpl` | SDK callback service client for app-to-app calls |
| `kb_reads_utils.py` | `KBReadsUtils`, `KBReadsUtilsImpl` | Reads library object handling (paired/single/interleaved) |
| `kb_berdl_utils.py` | `KBBerdlUtils`, `KBBerdlUtilsImpl` | BERDL (Bulk Export/Retrieve/Download) pipeline utilities |
| `kb_narrative_audit.py` | `KBNarrativeAudit` | Parse and audit KBase narrative objects for provenance and completeness |
| `kbase_endpoints.py` | — | Endpoint resolver: returns the correct service URL per KBase environment (CI/next/prod) |
| `kbase_catalog_client.py` | — | KBase catalog service client for app/module metadata |

## Facade access

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()

# Workspace operations
obj = kbu.ws.get_object(workspace="my_ws", ref="my_ws/my_genome/1")
info = kbu.ws.list_objects(workspace="my_ws", type="KBaseGenomes.Genome")

# Endpoint resolution (no auth required)
from kbutillib.domains.kbase.kbase_endpoints import get_endpoint
ws_url = get_endpoint("workspace", env="prod")
```

## Optional dependencies

A valid KBase authentication token is required for all workspace and SDK calls. Set it as:

```bash
export KB_AUTH_TOKEN="your-token-here"
# or place it in ~/.kbase/token
```

No additional Python packages beyond `requests` are needed. `KBWSUtils.available` returns `False` if no token is found, and `unavailable_reason` explains how to get one.

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to the relevant `*Impl` class.
2. Decorate with `@capability(domain="kbase", summary="...", tags=("kbase",))`.
3. Capabilities that require auth should use the `availability=` parameter or rely on the class `available` property (which checks for the token).
4. Run `kbu cap list --domain kbase` to confirm.

Network-dependent tests should use `pytest.mark.network` and skip when offline or when no token is present.
