# kbutillib.domains.ai — AI/LLM Utilities

Provides access to Argonne's Argo LLM gateway, AI-assisted curation workflows, and protein language model inference for bioinformatics applications.

## What lives here

Three classes cover distinct AI concerns: `ArgoUtils` wraps the Argo LLM REST gateway (Argonne-hosted models), `AiCurationUtils` orchestrates multi-step curation pipelines using those models, and `KbPlmUtils` runs protein language model (PLM) inference for sequence embeddings and property prediction.

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `argo_utils.py` | `ArgoUtils`, `ArgoUtilsImpl` | Thin client for Argo LLM gateway — model listing, chat completions, streaming |
| `ai_curation_utils.py` | `AiCurationUtils`, `AiCurationUtilsImpl` | Automated curation pipelines: entity extraction, conflict resolution, annotation review |
| `kb_plm_utils.py` | `KbPlmUtils`, `KbPlmUtilsImpl` | Protein language model inference — embeddings, property prediction, sequence scoring |

## Facade access

```python
from kbutillib.toolkit import KBUtilLib

kbu = KBUtilLib()

# LLM inference via Argo gateway
response = kbu.argo.chat("Summarize this KEGG pathway: ...")

# AI-assisted curation
results = kbu.ai_curation.curate_annotations(genome_id="GCF_000195955.2")

# Protein embeddings
embedding = kbu.plm.embed_sequence("MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQTLGQHDFSAGEGLYTHMKALRPDEDRLSPLHSVYVDQWDWERVMGDGERQFSTLKSTVEAIWAGIKATEAAVSEEFGLAPFLPDQIHFVHSQELLSRYPDLDAKGRERAIAKDLGAVFLVGIGGKLSDGHRHDVRAPDYDDWSTPSELGHAGLNGDILVWNPVLEDAFELSSMGIRVDADTLKHQLALTGDEDRLELEWHQALLRGEMPQTIGGGIGQSRLTMLLLQLPHIGQVQAGVWPAAVRESVPSLL")
```

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `requests` | Argo HTTP client | included in core |
| `torch` | PLM inference | `pip install torch` |
| `transformers` | HuggingFace PLM models | `pip install transformers` |

All three classes degrade gracefully: `available` returns `False` and `unavailable_reason` explains which package is missing. No import errors at module load time.

## Adding a capability here

See root `CONTRIBUTING.md` → "Add a domain capability". The short version:

1. Add the method to `ArgoUtilsImpl`, `AiCurationUtilsImpl`, or `KbPlmUtilsImpl`.
2. Decorate with `@capability(domain="ai", summary="...", tags=(...))`.
3. Run `kbu cap list --domain ai` to confirm registration.
4. Add a smoke assert to `tests/domains/test_external_kbase_ai_smoke.py`.

Use `kbu new-capability ai.<name>` to scaffold the stub automatically.
