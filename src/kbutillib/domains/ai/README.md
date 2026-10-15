# kbutillib.domains.ai — AI and LLM Utilities

Argonne Argo LLM gateway client, AI-assisted curation workflows, the closed-menu LLM
**council**, and protein language model inference for bioinformatics applications.

---

## What lives here

Four classes cover distinct AI concerns: `ArgoUtils` wraps the Argo LLM REST gateway
(Argonne-hosted models), `AiCurationUtils` orchestrates multi-step curation pipelines using
those models, `CouncilUtils` runs the closed-menu LLM council (several independent models
voting on one closed candidate menu, audited and adjudicated), and `KBPLMUtils` runs protein
language model inference for sequence embeddings and property prediction.

## Canonical imports

```python
from kbutillib.domains.ai.argo_utils import ArgoUtils
from kbutillib.domains.ai.ai_curation_utils import AiCurationUtils
from kbutillib.domains.ai.council_utils import CouncilUtils
from kbutillib.domains.ai.kb_plm_utils import KBPLMUtils
```

---

## Modules

| File | Class(es) | Purpose |
|------|-----------|---------|
| `argo_utils.py` | `ArgoUtils`, `ArgoUtilsImpl` | Thin client for Argo LLM gateway — model listing, chat completions, streaming |
| `ai_curation_utils.py` | `AiCurationUtils`, `AiCurationUtilsImpl` | Automated curation pipelines: entity extraction, conflict resolution, annotation review |
| `council_utils.py` | `CouncilUtils`, `CouncilUtilsImpl` | The LLM council: configuration, store, jobs, foreground worker, `doctor` |
| `council/` | (package) | The council protocol itself — types, backends, store, query types, engine |
| `kb_plm_utils.py` | `KBPLMUtils`, `KBPLMUtilsImpl` | Protein language model inference — embeddings, property prediction, sequence scoring |

---

## Optional dependencies

| Package | Enables | Install |
|---------|---------|---------|
| `requests` | Argo HTTP client | included in core dependencies |
| `torch` | PLM inference | `pip install torch` |
| `transformers` | HuggingFace PLM models | `pip install transformers` |
| `httpx` | Council HTTP backends (ARGO, OpenAI-compatible) | `pip install 'kbutillib[ai]'` |
| `SQLAlchemy`, `psycopg` | Council store (SQLite / shared PostgreSQL) | `pip install 'kbutillib[council]'` |

`ArgoUtils`, `AiCurationUtils` and `KBPLMUtils` degrade gracefully: `available` returns
`False` and `unavailable_reason` explains which package is missing. No import errors occur at
module load time. The council is lazy rather than degrading — nothing under `council/` is
imported until a name from it is touched, and a missing driver then raises one clean
`ImportError` naming the extra to install.

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

## The LLM council

A **council** asks N≥2 independent *proposer* models to pick ids from a **closed candidate
menu** for one item, has a *critic* audit the resulting vote table, and invokes a *meta*
adjudicator only when the proposers disagree or the critic objects. The protocol is a port of
Jose Faria's CONCORDIA GO council V3, generalised so the engine knows nothing about GO — or
any other vocabulary. That knowledge lives in a **query type**.

Nothing about a council is domain-specific except its query type, and nothing about it is
tied to one provider except its backends.

### Backends

One per way of reaching models. A backend never raises for a call failure: the council runs
N seats per item, so every failure comes back typed on the result instead of aborting the
item.

| Kind | Reaches | Billing |
|---|---|---|
| `argo` | ANL's ARGO gateway (OpenAI-compatible; the only route to Gemini) | API key |
| `openai` | Any OpenAI-compatible server — the self-hosted vLLM ports on poplar | none / local |
| `claude_cli` | The headless `claude` CLI | interactive **subscription** |
| `codex_cli` | The headless `codex exec` CLI | interactive **subscription** |

The two CLI backends are deliberately subscription-only. They start from an empty
environment and copy an allowlist into the child, so an exported `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY` is **dropped** rather than silently moving the call onto metered API
billing — `kbu council doctor` reports the NAMES of anything it dropped, so a user who
wondered why their key had no effect is told.

### Profiles

A **profile** is a named seating: which model alias sits in each proposer seat, which is the
critic, which is meta. Three ship in the packaged default configuration:

| Profile | Seating |
|---|---|
| `argo` | Every seat on ARGO — one API key's worth of billing, frontier models throughout |
| `mixed-subscription` (default) | Proposers and adjudicators spread across an ARGO key and two interactive subscriptions, so a batch costs almost nothing in API tokens |
| `poplar-local` | Entirely self-hosted: no external billing, no data leaving the lab |

A seating must be **independent**: two seats on the same canonical model are one opinion
counted twice, and the roster refuses to be constructed. Override any seat per call
(`critic="opus-cli"`, `proposers=[...]`) without editing the configuration.

Configuration comes from the packaged `council/default_config.yaml` overlaid with the
`ai_council` section of `~/.kbutillib/config.yaml` — the user's keys win per endpoint, model
and profile entry, and entries they do not name are untouched.

### Query types

A query type is the only place a domain vocabulary exists. It says what an item's evidence
looks like, what a menu entry looks like, how each role's prompt is written, what shape the
answer must take, and how to rename that answer's domain field names to the generic ones the
engine counts votes on. Two ship as builtins (both verbatim ports of CONCORDIA councils):

- **`go_mf@1`** — assign Gene Ontology molecular-function terms to one protein annotation
  string from a closed menu of retrieved GO terms.
- **`reaction_pickup@1`** — pick reactions for an EC/annotation from a closed candidate set.

Register your own with `kbu council query register spec.yaml`. A saved query type is
**immutable**: editing one means registering a new version, because a finished job's verdicts
only mean anything against the exact spec that produced them.

### Jobs

Work is organised as **jobs**: a batch of items against one saved query type. Every model
invocation is a **call row** in the store, and those rows *are* the queue — a worker claims
pending calls, runs them through the submitting user's own backends, and advances each item
`proposers → critic → meta (only if needed) → verdict`. Submitting therefore spends nothing;
`run` is what spends.

The store is a local SQLite file by default (`~/.kbutillib/council/council.sqlite`) or a
shared PostgreSQL database every KBUtilLib installation can point at — in which case a
question one user has already paid for is not asked again by the next, because reuse is keyed
on content hashes rather than on anything user-local.

**The schema is never created implicitly.** Run `kbu council db init` once per store; every
other verb refuses an unstamped store and says so, so a mistyped `--store` cannot quietly
become a second, empty council.

### Example

```python
from kbutillib import KBUtilLib

kbu = KBUtilLib()
kbu.council.db_init()          # once per store: create the schema, seed the builtins

items = [
    {
        "item_id": "fig|83333.1.peg.1",
        "evidence": {"text": "glutamine synthetase", "source": "RAST"},
        "menu": [
            {"id": "GO:0004356", "label": "glutamine synthetase activity", "rank": 1, "score": 0.91},
            {"id": "GO:0016874", "label": "ligase activity", "rank": 2, "score": 0.44},
        ],
    }
]

job_id = kbu.council.submit("go_mf", items, label="peg annotations")
status = kbu.council.run(job_id)          # foreground worker, until the job is idle
for row in kbu.council.results(job_id):
    print(row["item_id"], row["verdict"]["selected_ids"], row["verdict"]["decided_by"])

# One item, start to finish, without managing a job:
verdict = kbu.council.evaluate(items[0], query="go_mf")
```

Every asynchronous method has an `*_async` twin to await from a notebook — and the
synchronous form works inside a running event loop too, by handing the coroutine to a worker
thread rather than refusing.

### Start with `doctor`

```bash
kbu council doctor                 # or: --profile poplar-local, --json
```

`doctor` **spends nothing and sends no prompt**: HTTP readiness is the free `/models` GET,
CLI readiness is `--version` plus an offline billing preflight that only reads config files.
It reports every seat as `ready`, `unknown` ("nobody has confirmed this") or `unavailable`
("I checked, and no"), plus the store's reachability, the submitting user, and any dropped
billing variable names. It exits **1** if any seat is unavailable and **2** if none is but
some are unknown — so it is usable as a gate before a batch that will cost money.

The rest of the surface:

```bash
kbu council submit --query go_mf --input items.jsonl --label batch-1
kbu council run --job <id>                    # or --query ... --input ... to do both
kbu council jobs                              # --all-users, --status, --include-deleted
kbu council jobs status <id>
kbu council jobs results <id> --out r.jsonl   # --format jsonl|parquet, --include-pending
kbu council jobs rerun <id> --profile poplar-local
kbu council query list | show go_mf@1 | register spec.yaml | retire go_mf@1
kbu council calls search --query go_mf --entity "glutamine synthetase"
kbu council endpoints status | reset-halt <endpoint> | reset-budget <endpoint>
kbu council db init | migrate | check
kbu council models list                       # --endpoint <name> for a live listing
```

Every verb supports `--json`, which emits exactly one envelope object
(`schema_version`, `verb`, `generated_at`, `data`, `warnings`) on stdout and nothing else;
the human output is a rendering of that same object.

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
