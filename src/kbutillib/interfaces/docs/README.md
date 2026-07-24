# kbutillib.interfaces.docs — Documentation Generators

Auto-generates the capability catalog and mkdocs API reference pages from the live registry and module docstrings.

## What lives here

| File | Purpose |
|------|---------|
| `gen_capabilities.py` | Generates `docs/capabilities/index.md` — a table of all `@capability`-decorated functions with summary, tags, and availability |
| `mcp_catalog.py` | Generates an MCP-compatible tool catalog (JSON) from the registry |

## Install

```console
pip install -e ".[apidocs]"
```

This adds `mkdocs`, `mkdocstrings[python]`, and `mkdocs-material` to your environment.

## Generate the capability catalog

```console
python -m kbutillib.interfaces.docs.gen_capabilities
```

This overwrites `docs/capabilities/index.md` with a fresh table. Run it after adding new `@capability`-decorated methods. The output is committed — it is the authoritative catalog for the docs site.

## Build and serve the docs

```console
mkdocs serve      # live-reload at http://127.0.0.1:8000
mkdocs build      # static site → site/
```

The `mkdocs.yml` at the repo root uses `mkdocstrings` to pull API reference from module docstrings. Each `docs/modules/*.md` page contains a `::: kbutillib.domains.<d>.<mod>` directive that renders the module's full API.

## Add a new docs page

1. Create `docs/modules/<name>.md` with the `mkdocstrings` directive:
   ```markdown
   # MyUtils

   ::: kbutillib.domains.mydomain.my_utils
   ```
2. Add the page to `nav:` in `mkdocs.yml`.
3. Run `mkdocs serve` to verify rendering.

## MCP catalog generation

```console
python -m kbutillib.interfaces.docs.mcp_catalog > mcp_tools.json
```

Produces a JSON array of MCP tool descriptors — useful for publishing the tool list to a catalog service or for offline agent reference.
