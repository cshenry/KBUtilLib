# kbutillib.domains.ai — AI and LLM Utilities

Argonne Argo LLM gateway client, AI-assisted curation workflows, and protein language model
inference for bioinformatics applications.

---

## What lives here

Three classes cover distinct AI concerns: `ArgoUtils` wraps the Argo LLM REST gateway
(Argonne-hosted models), `AiCurationUtils` orchestrates multi-step curation pipelines using
those models, and `KBPLMUtils` runs protein language model inference for sequence embeddings
and property prediction.

## Canonical imports

```python
from kbutillib.domains.ai.argo_utils import ArgoUtils
from kbutillib.domains.ai.ai_curation_utils import AiCurationUtils
from kbutillib.domains.ai.kb_plm_utils import KBPLMUtils
```

---

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `argo_utils.py` | `ArgoUtils`, `ArgoUtilsImpl` | Thin client for Argo LLM gateway — model listing, chat completions, streaming |
| `ai_curation_utils.py` | `AiCurationUtils`, `AiCurationUtilsImpl` | Automated curation pipelines: entity extraction, conflict resolution, annotation review |
| `kb_plm_utils.py` | `KBPLMUtils`, `KBPLMUtilsImpl` | Protein language model inference — embeddings, property prediction, sequence scoring |

---

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `requests` | Argo HTTP client | included in core dependencies |
| `torch` | PLM inference | `pip install torch` |
| `transformers` | HuggingFace PLM models | `pip install transformers` |

All three classes degrade gracefully: `available` returns `False` and `unavailable_reason`
explains which package is missing. No import errors occur at module load time.

The Argo LLM gateway is internal to Argonne National Laboratory. `ArgoUtils.available` returns
`False` in environments without gateway access.

---

## Usage example

```python
from kbutillib.domains.ai.argo_utils import ArgoUtils

argo = ArgoUtils()
print(argo.available)  # True only with Argo gateway access

# Chat completion
response = argo.chat("Summarize this KEGG pathway: glycolysis")

# List available models
models = argo.list_models()
```

```python
from kbutillib.domains.ai.kb_plm_utils import KBPLMUtils

plm = KBPLMUtils()
print(plm.available)  # True if torch + transformers installed

# Embed a protein sequence
embedding = plm.embed_sequence("MKTAYIAKQRQISFVKSHFSRQLEERLGL...")
```

Via the facade:

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
response = kbu.argo.chat("Describe ATP synthase in one paragraph.")
```

---

## Available capabilities

Capabilities are registered on each `*Impl` class where they are present. Run:

```bash
kbu cap list --domain ai
```

---

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability".

1. Add the method to `ArgoUtilsImpl`, `AiCurationUtilsImpl`, or `KBPLMUtilsImpl`.
2. Decorate with `@capability(domain="ai", summary="...", tags=(...))`.
3. Run `kbu cap list --domain ai` to confirm registration.
4. Add a smoke assert to the appropriate test file in `tests/domains/`.

Use `kbu new-capability ai.<name>` to scaffold the stub automatically.
