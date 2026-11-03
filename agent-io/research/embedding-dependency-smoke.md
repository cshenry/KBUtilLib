# Embedding dependency smoke report: ESM2 and ESM-C against the real packages

Real-dependency smoke test for `kbdl-protein-embeddings-v1` PHASE 1. Nothing in KBUtilLib or
KBDLJobRunningPrototype exercises ESM2 or ESM-C today: a grep for `esm2|esmc|fair-esm|genslm|EsmModel`
over `src/` in this worktree returns **zero hits**, confirmed on the base commit. Only ProtT5 is
implemented (`src/kbutillib/domains/ai/prott5_utils.py`). This report exists so that backend tasks are
written against *measured* per-model constants rather than assumed ones.

Every number below was produced by loading the real checkpoint through the real package on this machine.
Nothing is mocked, injected, or copied from the expectation table. Where a measurement contradicts the
table in the task prompt, the measurement is reported and **the table is called wrong**.

- Measured: 2026-11-03, on h100 (poplar.cels.anl.gov), **CPU only** — the GPU was left alone.
- Base commit: `83e71b1` ("fix(modeling): rebuild spontaneous/universal reactions after gene mapping").
- No source file was changed by this task. The probe scripts live on scratch, deliberately outside the repo.

## Environment measured (the build actually smoked)

Scratch venv at `/scratch/chenry/esm-smoke/venv`, built with `uv venv --python 3.12`. **No service
virtualenv was touched** — the 80 production `kbdl-worker` units and `kbdl.service` import `kbutillib`
from the h100 install trees and were not involved. `TMPDIR`, `HF_HOME` and `UV_CACHE_DIR` were all
pointed at `/scratch/chenry/` (home is 94% full, 24G free; scratch has 1.3T).

| component | version | `__file__` |
|---|---|---|
| python | 3.12.3 (main, Aug 31 2026, 10:18:26) [GCC 13.3.0] | `/scratch/chenry/esm-smoke/venv/bin/python` |
| `transformers` | **5.22.1** | `/scratch/chenry/esm-smoke/venv/lib/python3.12/site-packages/transformers/__init__.py` |
| `torch` | **2.15.0+cpu** | `.../site-packages/torch/__init__.py` |
| `sentencepiece` | 0.2.2 | `.../site-packages/sentencepiece/__init__.py` |
| `tokenizers` | 0.23.3 | — |
| `huggingface_hub` | 2.4.0 | — |

Install commands (both completed in seconds, not the budget-dominating cost the dispatch note predicted):

```
uv pip install --python $VP/bin/python torch --index-url https://download.pytorch.org/whl/cpu   # 18.3s
uv pip install --python $VP/bin/python 'transformers>=5.16' sentencepiece                        # 5.1s
```

Deviations from the install instruction, both deliberate and both recorded rather than hidden:

1. **`python3 -m venv` cannot work on this machine.** `/usr/bin/python3 -m venv` fails with
   `ensurepip is not available ... You may need to use sudo` — the `python3-venv` package is absent and
   we are unprivileged on this devops-managed host. `uv` 0.11.16 was used instead. The resulting venv is
   a plain CPython 3.12.3 venv.
2. **torch was installed as the CPU-only wheel** (`2.15.0+cpu`, 199.8 MiB) from
   `download.pytorch.org/whl/cpu` rather than the default CUDA wheel (~2.5 GB). The task requires CPU
   execution and small downloads, and this keeps the GPU out of the picture entirely.

Honoured as instructed: the EvolutionaryScale `esm` package was **not** installed (it needs Python
>=3.12 and collides with fair-esm on the `esm` import namespace), `fair-esm` was **not** installed
(archived 2024-08-01), and GenSLM was **not** attempted at all.

`transformers >= 5.16` resolved to **5.22.1**, matching the prompt's "observed current release" exactly.
Agreement.

---

## Model 1 — `facebook/esm2_t6_8M_UR50D`

Loaded with `from transformers import EsmModel, EsmTokenizer` — `EsmModel`, **not** `EsmForMaskedLM`,
as instructed. Sequence: `MKVLAAGIVL` (10 residues).

### Resolved revision

| field | value |
|---|---|
| **resolved revision SHA** | **`c731040fcd8d73dceaa04b0a8e6329b345b0f5df`** |
| confirmed independently by | `HfApi().model_info(repo).sha` — identical to the on-disk snapshot dir |
| snapshot path | `/scratch/chenry/esm-smoke/hf/hub/models--facebook--esm2_t6_8M_UR50D/snapshots/c731040fcd8d73dceaa04b0a8e6329b345b0f5df` |
| checkpoint lastModified | 2023-03-21 15:05:17+00:00 |

**Provenance gotcha for the backend tasks:** `model.config._commit_hash` is **`None`** in transformers
5.22.1 — the commit hash is *not* exposed on the config object. Any provenance field that intends to
record "which snapshot produced this embedding" must get the SHA from `HfApi().model_info(repo).sha` or
by parsing the cache snapshot directory. Reading it off the config silently yields `None`.

### Measured values vs. the expectation table

| value | expected by task table | **measured** | verdict |
|---|---|---|---|
| trailing hidden-state dim | 320 | **320** | **AGREES** |
| layers | 6 | **6** (`config.num_hidden_layers`) | **AGREES** |
| leading special token | BOS/`<cls>` | **`<cls>`, id 0, at position 0** | **AGREES** (with a caveat, below) |
| trailing special token | EOS | **`<eos>`, id 2, at final position** | **AGREES** |

`last_hidden_state.shape = (1, 12, 320)`, dtype `torch.float32`, device `cpu`.

**Caveat on "BOS/`<cls>`": the leading token is `<cls>`, and `tokenizer.bos_token` is `None`.** The
table's "BOS/`<cls>`" phrasing is only half right. Code that reaches for `tokenizer.bos_token_id`
expecting the leading token gets `None`, not 0. Use `cls_token_id` (= 0).

### Tokenization — measured, with positions decoded

10 residues in, **12 tokens out, delta = +2** (one leading, one trailing).

```
input_ids : [0, 20, 15, 7, 4, 5, 5, 6, 12, 7, 4, 2]
tokens    : ['<cls>', 'M', 'K', 'V', 'L', 'A', 'A', 'G', 'I', 'V', 'L', '<eos>']
decode()  : '<cls> M K V L A A G I V L <eos>'
special_tokens_mask : [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]
```

| pos | id | token | special |
|---|---|---|---|
| 0 | 0 | `<cls>` | **yes (LEADING)** |
| 1–10 | 20,15,7,4,5,5,6,12,7,4 | M K V L A A G I V L | no |
| 11 | 2 | `<eos>` | **yes (TRAILING)** |

So **there IS a leading special token**, and reduction over residues must strip position 0 and the final
position (`emb[1:-1]`), not just the final one.

Special-token inventory: `all_special_tokens = ['<eos>', '<unk>', '<pad>', '<cls>', '<mask>']`,
`all_special_ids = [2, 3, 1, 0, 32]`. `cls=<cls>/0`, `bos=None`, `eos=<eos>/2`, `pad=<pad>/1`.

### `hidden_states` length — 7, not 6

`len(out.hidden_states) == 7` for a 6-layer model: entry 0 is the **embedding output**, entries 1..6 are
the 6 transformer layers. All have shape `(1, 12, 320)`.

This does not contradict the table (which says 6 *layers*, correctly), but it is the off-by-one that
bites: `hidden_states[-1]` is the last layer and equals `last_hidden_state`; `hidden_states[6]` is the
same thing; `hidden_states[0]` is pre-transformer embeddings. A backend that wants "layer N of 6" must
index `hidden_states[N]`, and a repr-layer config value of `6` is valid here while `7` is out of range.

### Config constants as shipped

| key | value |
|---|---|
| `hidden_size` | 320 |
| `num_hidden_layers` | 6 |
| `num_attention_heads` | 20 |
| `max_position_embeddings` | 1026 |
| `vocab_size` | 33 |
| `model_type` | `esm` |
| `token_dropout` | `True` |
| `position_embedding_type` | `rotary` |

**`tokenizer.model_max_length` is the sentinel `1000000000000000019884624838656`**, i.e. the tokenizer
declares no usable maximum. Truncation length must come from `max_position_embeddings` (1026, which
includes the two special tokens → 1024 residues) or from an explicit constant. Trusting
`model_max_length` means never truncating and OOMing on a long sequence.

### FINDING: `EsmModel` adds a randomly-initialized pooler — `pooler_output` is garbage

The load report from `EsmModel.from_pretrained` on this checkpoint:

```
lm_head.dense.weight / .bias / .layer_norm.weight / .layer_norm.bias / lm_head.bias  | UNEXPECTED
pooler.dense.weight, pooler.dense.bias                                               | MISSING
```

- The `UNEXPECTED` lm_head keys confirm the task's premise: using `EsmModel` instead of
  `EsmForMaskedLM` discards the masked-LM head as intended. Correct and harmless.
- The `MISSING` pooler keys mean `pooler.dense` is **newly initialized with random weights** — the
  checkpoint has no pooler. `EsmModel` still returns a `pooler_output`, and it is meaningless.

Proven by loading the checkpoint twice in one process and comparing:

| tensor | identical across two independent loads? |
|---|---|
| `pooler.dense.weight` | **False** — load A `[0.01392, 0.03081, 0.00560, 0.00996]`, load B `[-0.01242, -0.00844, -0.01108, 0.00652]` |
| `word_embeddings.weight` (control) | True |
| `pooler_output` (shape `(1, 320)`) | **False** |
| `last_hidden_state` (control) | True |

**Consequence for every backend task in this PRD: never use `pooler_output` as a sequence embedding.**
It is non-deterministic across processes, so the same sequence embedded twice would produce different
vectors and any cache keyed on sequence hash would be silently wrong. Pool from `last_hidden_state`
over `[1:-1]`. `output.keys()` is `['last_hidden_state', 'pooler_output']`, so the bad field is the
second thing an author sees — this is an easy mistake to make.

---

## Model 2 — `biohub/ESMC-300M`

Loaded through **native Transformers with an explicit model class**, never `AutoModel`, as instructed.

### FINDING: the class is `EsmcModel` / `EsmcTokenizer` — note the casing

The explicit classes exported by transformers 5.22.1 are `EsmcModel`, `EsmcTokenizer`, `EsmcConfig`,
`EsmcForMaskedLM`, `EsmcForSequenceClassification`, `EsmcForTokenClassification`, `EsmcPreTrainedModel`
— spelled **`Esmc`**, not `ESMC` and not `EsmC`. `EsmcModel.__module__` is
`transformers.models.esmc.modeling_esmc`. A backend author who guesses `from transformers import
EsmCModel` gets an `ImportError`. The ESM-C support lives in its own `transformers/models/esmc`
package, separate from `esm` (ESM2) and `esmfold2`.

This also confirms, concretely, the task's reason for banning `AutoModel` for this repo: the shipped
`config.json` declares `architectures: ['EsmcForMaskedLM']` and carries an `auto_map` key, so automatic
resolution would hand back a masked-LM head (and consult remote code mappings) instead of the bare
encoder we want.

### Resolved revision

| field | value |
|---|---|
| **resolved revision SHA** | **`e4bac860f0c502cd80f3aeac3d0ce6524c6627cb`** |
| source | `HfApi().model_info(repo).sha`, identical to the on-disk snapshot dir |
| snapshot path | `/scratch/chenry/esm-smoke/hf/hub/models--biohub--ESMC-300M/snapshots/e4bac860f0c502cd80f3aeac3d0ce6524c6627cb` |
| checkpoint lastModified | 2026-09-16 18:34:59+00:00 |

`config._commit_hash` is **`None`** here too — same provenance gotcha as ESM2, so it is a property of
transformers 5.22.1 rather than of one checkpoint.

### Measured values vs. the expectation table

| value | expected by task table | **measured** | verdict |
|---|---|---|---|
| leading special token | `<cls>` | **`<cls>`, id 0, at position 0** | **AGREES** |
| trailing special token | `<eos>` | **`<eos>`, id 2, at final position** | **AGREES** |
| max_tokens 2048 | `max_tokens` = 2048 | **2048, but the key is `max_position_embeddings`; `config.max_tokens` is ABSENT** | **value AGREES, key name in table is WRONG** |
| trailing hidden dim | *table gives none* | **960** | newly measured |
| layers | *table gives none* | **30** (`hidden_states` length 31) | newly measured |

**Disagreement, stated explicitly: the table's `max_tokens` key does not exist on this config.**
`getattr(config, "max_tokens")` returns absent. The 2048 figure itself is correct, but it is carried by
`max_position_embeddings`. Any backend reading `config.max_tokens` raises `AttributeError` or silently
takes a default. The table should say `max_position_embeddings = 2048`.

The table also omitted the hidden width and layer count for this model entirely. They are **960** and
**30**. Backend tasks written against a guessed ESM-C width (e.g. 1152, the ESM-C-600M figure) would be
wrong; storage sizing must use 960 for this checkpoint.

`last_hidden_state.shape = (1, 12, 960)`, dtype `torch.float32`, device `cpu`.

### Tokenization — measured, with positions decoded

10 residues in, **12 tokens out, delta = +2**. Identical id layout to ESM2 for this sequence:

```
input_ids : [0, 20, 15, 7, 4, 5, 5, 6, 12, 7, 4, 2]
tokens    : ['<cls>', 'M', 'K', 'V', 'L', 'A', 'A', 'G', 'I', 'V', 'L', '<eos>']
decode()  : '<cls> M K V L A A G I V L <eos>'
special_tokens_mask : [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1]
```

| pos | id | token | special |
|---|---|---|---|
| 0 | 0 | `<cls>` | **yes (LEADING)** |
| 1–10 | 20,15,7,4,5,5,6,12,7,4 | M K V L A A G I V L | no |
| 11 | 2 | `<eos>` | **yes (TRAILING)** |

**There IS a leading special token.** Same `emb[1:-1]` stripping rule as ESM2.

Special-token inventory: `all_special_tokens = ['<cls>', '<eos>', '<unk>', '<pad>', '<mask>', '|']`,
`all_special_ids = [0, 2, 3, 1, 32, 31]`. Note the extra **`|` chainbreak token (id 31)** that ESM2 does
not have; a multi-chain input would gain a further non-residue position that `emb[1:-1]` alone does not
remove. `attention_mask` is returned; `token_type_ids` is not.

`tokenizer.model_max_length` is the same `1e30` sentinel as ESM2 — unusable, derive truncation from
`max_position_embeddings` (2048, inclusive of the two special tokens → 2046 residues).

### Config constants as shipped

| key | value |
|---|---|
| `hidden_size` | **960** |
| `num_hidden_layers` | **30** |
| `num_attention_heads` | 15 |
| `max_position_embeddings` | **2048** |
| `intermediate_size` | 2560 |
| `vocab_size` | 64 |
| `model_type` | `esmc` |
| `max_tokens` | **ABSENT** |

### `hidden_states` length — 31 for 30 layers

Same embedding-plus-layers convention as ESM2: entry 0 is the embedding output, 1..30 are the layers.

### FINDING: `EsmcModel` returns NO `pooler_output` — ESM2 and ESM-C are asymmetric here

| | `EsmModel` (ESM2) | `EsmcModel` (ESM-C) |
|---|---|---|
| `output.keys()` | `['last_hidden_state', 'pooler_output']` | `['last_hidden_state', 'hidden_states']` |
| pooler in checkpoint | absent → **randomly initialized** | **no pooler at all** |
| `tokenizer.bos_token` | **`None`** | **`<cls>`** |

Two consequences for the backend, both of which would be found the hard way otherwise:

1. **A shared code path that reads `output.pooler_output` works on ESM2 (returning garbage) and raises
   on ESM-C.** The only portable sequence embedding is pooling `last_hidden_state[:, 1:-1, :]`.
2. **`bos_token` is `None` on ESM2 but `<cls>` on ESM-C.** Special-token handling must not be written
   against `bos_token_id`; use the measured positional rule or `cls_token_id`.

The `lm_head.*` keys report `UNEXPECTED` on the ESM-C load too, which is the intended discard of the
masked-LM head. Unlike ESM2, **nothing is reported `MISSING`** — so the ESM-C encoder load is fully
deterministic, no randomly-initialized parameters at all.

---

## Step 6 — does `local_files_only=True` raise cleanly when the snapshot is absent?

Method: a fresh empty temp directory under `/scratch/chenry/tmp` was used as both `HF_HOME` and
`cache_dir`, then each class was asked to load with `local_files_only=True`. The directory was verified
empty before the attempts.

**Answer: NO — not uniformly. Three different behaviours across four call sites.**

| call | raises? | exception type | message |
|---|---|---|---|
| `EsmModel.from_pretrained(..., local_files_only=True)` | yes, **cleanly** | `builtins.OSError` | `We couldn't connect to 'https://huggingface.co' to load the files, and couldn't find them in the cached files. Check your internet connection or see how to run the library in offline mode at 'https://huggingface.co/docs/transformers/installation#offline-mode'.` |
| `EsmcModel.from_pretrained(..., local_files_only=True)` | yes, **cleanly** | `builtins.OSError` | *identical message to the above* |
| `EsmTokenizer.from_pretrained(..., local_files_only=True)` | yes, but **not cleanly** | `builtins.TypeError` | `expected str, bytes or os.PathLike object, not NoneType` |
| `EsmcTokenizer.from_pretrained(..., local_files_only=True)` | **NO EXCEPTION — it succeeded** | — | — |

### The two model classes behave well

Both raise a plain `OSError` (`isinstance(e, OSError)` is `True`; the MRO is
`OSError <- Exception <- BaseException <- object`) with an actionable message that names offline mode.
A preflight can catch `OSError` and report "snapshot not cached" accurately. Note the message says
"couldn't connect", which is misleading — there was working network here and `local_files_only=True`
suppressed the fetch by design — but the exception type and behaviour are correct.

### FINDING: `EsmTokenizer` leaks a `TypeError`, not an `OSError`

`EsmTokenizer` fails with `TypeError: expected str, bytes or os.PathLike object, not NoneType` — an
internal failure where a resolved-path of `None` is passed onward, rather than a deliberate "not
cached" error. **A preflight written as `except OSError:` will not catch this** and the task will die
with an opaque `TypeError` instead of a clear diagnostic. Catch `(OSError, TypeError)`, or probe the
cache directly.

### FINDING: `EsmcTokenizer` silently succeeds with NO files present at all

This is the most consequential result of step 6. `EsmcTokenizer.from_pretrained("biohub/ESMC-300M",
local_files_only=True, cache_dir=<empty>)` returned a **fully working tokenizer**:

- `vocab_size` = 33; `name_or_path` = `biohub/ESMC-300M`
- `tk("MKVLAAGIVL")["input_ids"]` → `[0, 20, 15, 7, 4, 5, 5, 6, 12, 7, 4, 2]` — the correct ids
- **no `hub` directory was even created** in the empty cache; nothing was read from or written to disk

The cause is that the vocabulary is **hardcoded in the module source**,
`transformers/models/esmc/tokenization_esmc.py` (verified: the file contains a literal residue/special
token vocab list). The repo id is therefore decorative. Proof: the same call with a deliberately
nonexistent repo id, `biohub/THIS-REPO-DOES-NOT-EXIST-xyz`, **also succeeded**, returning a working
tokenizer with `vocab_size` 33 and `name_or_path` set to the bogus name.

**Consequence for the backend preflight: successfully loading the tokenizer is NOT evidence that the
checkpoint is present, and for ESM-C it is not evidence that the repo even exists.** A
"can we run offline?" check must load the **model** (or inspect the snapshot/weight files on disk).
Using tokenizer construction as the cheap proxy — the obvious optimization — gives a false green for
ESM-C and would fail only later, at weight-load time, inside a worker.

This is recorded as measured third-party behaviour. Per the no-injection criterion nothing was patched,
pinned or worked around.

### Incidental: ESM-C tokenizer vocab 33 vs config vocab 64

`EsmcTokenizer.vocab_size` is **33** while `EsmcConfig.vocab_size` is **64** (ESM2 is self-consistent at
33/33). The embedding matrix is wider than the tokenizer's vocabulary. Harmless for embedding
extraction, but a validation assert of `tokenizer.vocab_size == config.vocab_size` would fail on ESM-C
while passing on ESM2 — do not write that check.

---

## Step 7 — biotrainer 2.1.1 embedding API (reading task, no code)

Read from the actual published artifacts, not from memory. `biotrainer-2.1.1-py3-none-any.whl` and
`biotrainer_core-2.1.1-py3-none-any.whl` were downloaded from PyPI and unzipped under
`/scratch/chenry/esm-smoke/read/` purely to read. **Neither was installed**, and no dependency of this
repo changed. The `EmbeddingAPI` surface lives in `biotrainer`, but the hashing and HDF5 primitives live
in the separate **`biotrainer-core==2.1.1`** distribution — a detail worth knowing before anyone goes
looking for `get_hash` in the wrong wheel.

Confirmed from PyPI metadata: biotrainer 2.1.1 `requires_python = <3.14,>=3.12` — exactly as the task
stated. Agreement.

### FINDING: biotrainer pins `transformers<5.20.0`, so it cannot coexist with the build we measured

`Requires-Dist: transformers<5.20.0,>=5.17.0`. We measured on **5.22.1**. Adopting biotrainer as a
dependency would force transformers *down* below 5.20, i.e. below the version this smoke test
validated. This is an additional, independent reason not to adopt it — one not listed in the task's
reasons — and it reinforces the decision already made.

### The API surface

`biotrainer/embedding/embedding_api.py` exposes exactly two methods on `EmbeddingAPI`:

- `compute_embeddings(input_data, output_dir, protocol, force_output_dir=False, force_recomputing=False, store_by_hash=True, embedding_stats_tracker=None) -> str` — writes an HDF5 file, returns its path.
- `generate_embeddings(input_data, reduce: bool) -> Generator[SequenceData, None, None]` — yields in-memory records as they are computed, nothing written.

**Note the asymmetry in how reduction is selected.** `generate_embeddings` takes an explicit
`reduce: bool`. `compute_embeddings` has **no** `reduce` parameter — it derives reduction from the
`protocol` argument via `use_reduced_embeddings = protocol in Protocol.using_per_sequence_embeddings()`
(`embedding_service.py:89`). Both paths converge on the same generator,
`_embeddings_generator(seq_records, use_reduced_embeddings)`.

### Pooling / reduction

Reduction is a **plain unweighted arithmetic mean over the residue axis**, one line
(`embedder_interfaces.py:111-118`):

```python
@staticmethod
def reduce_per_protein(embedding: torch.Tensor) -> torch.Tensor:
    return embedding.mean(axis=0)
```

Applied in `_embeddings_generator` (`embedding_service.py:283-285`) only when the reduce flag is set;
otherwise the full per-residue matrix is passed through. Properties worth noting:

- **`reduce` is a boolean, not a strategy.** There is no `max`, `sum`, `cls`, or attention-weighted
  option anywhere in the embedding package, and no hook to supply one. A design that needs CLS-token
  pooling or masked-mean must add that concept itself; copying biotrainer gives mean-only.
- **The mean is taken *after* special tokens are stripped**, so it is a true residue mean — provided
  the stripping is right. There is no `attention_mask` weighting at the reduce step, because padding
  has already been removed per-sequence before this point.

### Two different special-token stripping strategies — and a latent ESM-C bug

biotrainer strips special tokens in two different ways, which is directly relevant to the measurements
above:

1. **Generic path** (`huggingface_transformer_embedder.py:85-100`, `_get_keep_mask`): calls
   `tokenizer.get_special_tokens_mask(input_id, already_has_special_tokens=True)`, drops every
   flagged index *except* the mask token (deliberately kept for MLM use), plus a per-model
   `_custom_indices_to_remove`, and indexes with a boolean keep-mask. This is the robust approach — it
   would correctly drop ESM-C's `|` chainbreak token, since I measured `|` as id 31 in
   `all_special_ids`.
2. **ESM2 fast path** (`ESM2.py:46-79`): skips the scan entirely and slices positionally —
   `embedding = embeddings[seq_num, 1:num_real_tokens - 1]` where
   `num_real_tokens = attention_mask[seq_num].sum()`. The docstring states the assumed layout
   `[BOS, AA1..AAn, EOS, PAD...]`. This exactly matches what I measured for both models (leading
   `<cls>`, trailing `<eos>`), and the comment explains the motive: avoid "costly scanning for special
   tokens each time".

**`class ESMC(ESM2)` inherits that fast path and does not override it** (`ESM2.py:87`). For a
single-chain sequence that is correct. But because ESM-C's tokenizer carries the extra `|` chainbreak
special token that ESM2 does not have, a multi-chain input would leave `|` positions **inside** the
residue slice and therefore inside the mean. This is prior art to copy *carefully*: take the
positional-slice optimization only if single-chain input is guaranteed, otherwise use the keep-mask.

### FINDING: the task's claim about biotrainer's ESM routing is WRONG for 2.1.1

The task prompt says biotrainer "routes any model name containing 'esm' to `EsmModel` which is the wrong
architecture for ESMC". **That is not what 2.1.1 does.** Measured from the source:

- There is a **dedicated `ESMC` class** (`biotrainer/embedding/huggingface/ESM2.py:87`), registered in
  `OPTIMIZED_EMBEDDERS` alongside `ESM2` (`huggingface/__init__.py:7-13`).
- Routing is by **exact dictionary membership**, not substring matching:
  `if embedder_name in _esm2_family_dict.keys()` and `if embedder_name in _esmc_family_dict.keys()`.
  A name merely *containing* "esm" matches neither and falls through to the generic embedder.
- `ESMC.detect` does **not** use `EsmModel`.

So the rationale "routes any model name containing 'esm' to EsmModel" should be struck from the PRD's
list of reasons. **The decision not to adopt biotrainer is unaffected** — the python-version pin, the
`transformers<5.20.0` pin, eager loading, and the missing GenSLM adapter all stand on their own. Only
this one stated reason is factually wrong, and it is recorded here so the taskplan can be corrected.

What `ESMC.detect` actually does is its own cautionary tale: it loads via **`AutoTokenizer` /
`AutoModel`** (`ESM2.py:93-95`) — the very thing our task forbids — and then *guards after the fact* by
checking the resolved class names:

```python
if "ESMC" in model_name.upper() and "ESMC" in tokenizer_name.upper():
    return cls(...)
else:
    raise ImportError(f"Trying to load ESMC model {embedder_name}, but resolved as {model_name}! ...")
```

That guard is a good pattern in itself (fail loudly when `Auto*` resolves to the wrong architecture),
but using the explicit `EsmcModel` class as our task requires makes it unnecessary.

### Independent corroboration of our measured widths

biotrainer hardcodes per-model widths, and they **agree with what I measured**:

| model | biotrainer's table | **measured here** | agree? |
|---|---|---|---|
| `facebook/esm2_t6_8M_UR50D` | 320 | **320** | yes |
| `biohub/ESMC-300M` | 960 | **960** | yes |

Two independent sources now give 960 for ESMC-300M, which strengthens the finding that the task's
expectation table simply omitted it. For reference, biotrainer's other entries (not verified by me):
ESM2 35M→480, 150M→640, 650M→1280, 3B→2560, 15B→5120; ESMC-600M→1152, ESMC-6B→2560.

### The task's "loads the model at construction" claim is CORRECT

Confirmed. `ESM2.detect` / `ESMC.detect` call `from_pretrained(...).to(device)` inline
(`ESM2.py:31-35`, `93-95`) and hand an already-materialized model into
`HuggingfaceTransformerEmbedder.__init__`, which immediately calls `self._set_model_precision()`
(`huggingface_transformer_embedder.py:20-28`). There is no deferral: constructing the embedder downloads
and loads weights. Agreement with the task.

### Sequence hashing for provenance

`biotrainer_core/functions/hashing.py:10-13`:

```python
def calculate_sequence_hash(sequence: str) -> str:
    suffix = len(sequence)
    sequence = f"{sequence}_{suffix}"
    return hashlib.sha256(sequence.encode()).hexdigest()
```

- **SHA-256 of the sequence with its own length appended** as `"{seq}_{len}"`. Verified concretely:
  `calculate_sequence_hash("MKVLAAGIVL")` = `d71c29bafc8de8c396e37ad9a29d30f93d5cfb9499f3093cb980a53f524c05fe`
  (plain `sha256("MKVLAAGIVL")` is `a45eb063...`, i.e. the length suffix genuinely changes the digest).
  The length suffix is cheap belt-and-braces against collisions and makes the key self-describing.
- Full 64-hex digest, not truncated. `SequenceData.get_hash()` is a thin wrapper
  (`sequence_data.py:152`). `get_id_for_id2emb()` falls back to `seq_id` only when the sequence is
  empty.
- **The hash covers the SEQUENCE ONLY.** It does not include the embedder name, the model revision, the
  `reduce` flag, or the precision. biotrainer compensates entirely through *file naming* (below), so
  embeddings from different models never share a file.

**This is the single most important pattern NOT to copy verbatim for KBDL.** If per-model embeddings are
ever pooled into one store — which a KBDL object store naturally invites — a sequence-only key silently
collides across ESM2 / ESM-C / ProtT5, and across reduced vs per-residue. Our key must incorporate at
minimum the model id, the resolved revision SHA (which step 4 proves must be fetched deliberately,
since `config._commit_hash` is `None`), and the reduction mode.

A second, genuinely good hashing pattern worth copying — `get_embedding_hash()`
(`sequence_data.py:160-185`) hashes the embedding's raw bytes **after normalizing to float64**:

> "We normalize to float64 before hashing to avoid false cache misses caused purely by dtype
> differences (e.g. float32 vs float64) when the underlying values are identical."

That is exactly the right instinct for a cache key over float tensors, and worth lifting directly.

### HDF5 output layout

Writer: `biotrainer_core/h5_files/h5_handling.py:38-42`.

```python
def store_embedding_to_handle(h5_file_handle, seq_hash, seq_id, embedding_data):
    h5_file_handle.create_dataset(seq_hash, data=embedding_data, compression="gzip", chunks=True)
    h5_file_handle[seq_hash].attrs["original_id"] = seq_id
```

| aspect | layout |
|---|---|
| structure | **flat** — one dataset per sequence at the file root, no groups, no nesting |
| dataset key | the **sequence hash** when `store_by_hash=True` (default), else the raw `seq_id` |
| human-readable id | carried as the dataset attribute **`attrs["original_id"]`** — the only place `seq_id` survives |
| compression | `compression="gzip"` with `chunks=True` (auto-chunking), on every dataset |
| dataset shape | `(n_residues, width)` unreduced; `(width,)` reduced — the *same* key either way |
| dtype | whatever numpy array arrives; float16 only if half precision was used |

Path and file name (`embedding_service.py:289-323`):

```
<output_dir>/embeddings/<embedder_name_last_path_segment>/[reduced_]embeddings_file_<embedder>[_half].h5
```

`embedder_name.split("/")[-1]` strips the HF org, so `biohub/ESMC-300M` becomes `ESMC-300M`. **The
reduction mode, the embedder, and the precision are encoded in the filename and directory — never in the
dataset key.** That is what makes the sequence-only hash safe *for biotrainer* and unsafe for a single
shared store.

Reader: `read_id2emb` (`h5_handling.py:12-28`) returns `{key: embedding[:]}`, loading everything when
`ids_to_load is None`, and **raises `ValueError` on a requested-but-missing key** rather than skipping.
`EmbeddingService.load_embeddings` then converts to `torch.tensor(...).float()` — note this **upcasts
float16 back to float32 on read**, so half precision is a storage saving only.

Operational patterns in `compute_embeddings` worth copying:

- **Resumability by key-set difference** (`:105-122`): opens an existing file, reads `existing_keys`,
  and embeds only records whose hash (or id) is absent; returns early if nothing is missing. This is
  the right shape for a long-running KBDL job that gets killed.
- **`force_recomputing=True` unlinks the file** (`:99-100`) rather than overwriting keys — because
  `create_dataset` on an existing key would raise.
- **Sequences sorted by length descending** before embedding (`:132-135`) so the largest batch is
  attempted first and OOM surfaces immediately rather than hours in.
- **`/` is rejected in sequence ids** when `store_by_hash=False` (`:127-130`), since HDF5 treats it as a
  group path separator. Hash keys are immune, which is a real argument for hash keys.
- **I/O is offloaded to a separate `spawn` process** fed by an `mp.Queue(maxsize=30)`
  (`:24-33`, `:152-190`), so gzip compression does not block the GPU; a `None` sentinel shuts it down,
  and it is pushed in a `finally`/`except` so the writer cannot hang on an exception.

### OOM handling (biotrainer, inherited from bio-embeddings)

`EmbedderWithFallback._embed_batch` (`embedder_interfaces.py:137-200`) is a **three-stage cascade** on
`RuntimeError`: full batch on GPU → each sequence individually on GPU → that sequence on CPU. It is
skipped entirely when the device is already CPU. Half precision disables the CPU fallback with an
explicit `NotImplementedError` pointing at huggingface/transformers#11546.

`estimate_batch_size` (`embedder_interfaces.py:57-74`) sizes batches from device memory rather than a
fixed constant: `batch_size = device_memory_gb * 1024 residues_per_gb * 0.8 safety_factor`, clamped to
`8192 * 6` with the comment that this is a good trade-off "even on an H100". Batching is by **total
residue count**, not sequence count (`embed_many`, `:76-109`), which is the correct unit for a
variable-length workload, and an over-long single sequence is passed through alone with a warning.

### bio-embeddings 0.2.2 — OOM/storage prior art only

Unmaintained since 2021 and, confirmed from PyPI, `requires_python = ">=3.7.1,<3.10"` — it **cannot be
installed on any python this fleet runs**. Skimmed purely as prior art, as instructed. biotrainer's
`embedder_interfaces.py` header explicitly credits it as the file's origin, so the lineage is direct.

- **Same reduction**: `reduce_per_protein` is `embedding.mean(0)` (`embed/esm_embedder.py:82-84`), but
  declared `@abc.abstractmethod` so every embedder must opt in — several override it (CPCProt is
  already per-protein and no-ops; UniRep, Bepler, PLUS-RNN, GloVe, fastText each differ). biotrainer
  made it a concrete default instead. The abstract version is arguably the safer design: it forces each
  new model to state its pooling rather than silently inheriting mean.
- **Keys are plain `md5(sequence)`** (`utilities/helpers.py:59`) — no length suffix — with
  `attrs["original_id"]` carrying the real id, the same convention biotrainer still uses. Notably it
  ships a **dedicated MD5-clash exception** (`utilities/exceptions.py:27`) that halts execution, plus a
  check that the mapping has as many unique `original_id`s as hashes. biotrainer's length-suffixed
  SHA-256 is the evolved answer to exactly that worry.
- **Storage is split across three files**, not one: per-residue, `reduced_`, and `transformed_`
  (`embed/pipeline.py:229-248`), each written in the same loop with its own `create_dataset`. The
  `discard_per_amino_acid_embeddings` flag exists specifically so the large per-residue file can be
  skipped when only per-protein vectors are wanted — and the config validation refuses the combination
  that would produce nothing (`:116-119`).
- **Half precision is applied at storage time**, `embedding.astype(numpy.float16)` (`:234-235`),
  separately from `half_precision_model` (which is restricted to the ProtT5 models). Splitting
  "compute in fp16" from "store in fp16" is a useful distinction biotrainer partly loses.
- **Disk-space preflight — the most directly reusable idea here.**
  `_print_expected_file_sizes` (`embed/pipeline.py:40-76`) predicts output size *before* embedding:

  ```
  per_amino_acid_size_in_bytes = 4 * embedding_dimension * number_of_layers
  per_protein_size_in_bytes    = 4 * embedding_dimension
  embeddings_file_size         = per_amino_acid_size_in_bytes * total_residues
  reduced_embeddings_file_size = per_protein_size_in_bytes * n_proteins
  ```

  then compares the total against `shutil.disk_usage(prefix)` and logs a loud warning advising the user
  to "stop execution NOW" if it will not fit. For KBDL this is worth copying almost verbatim — with the
  measured widths (320 for ESM2-8M, 960 for ESMC-300M) it is immediately computable, and note the
  formula multiplies by layer count, which matters if more than the last hidden state is ever stored.
  Unreduced ESMC-300M is `4 * 960 = 3840` bytes per residue before gzip.
- **`max_amino_acids`** is a user-supplied batch cap threaded into `embed_many`
  (`embed/pipeline.py:227`) rather than biotrainer's automatic memory-derived estimate.

---

## Summary of disagreements with the task's expectation table

Reported, not reconciled. Per the task's instruction, the measurement stands and the table is called
wrong where it is wrong.

| # | item | status |
|---|---|---|
| 1 | ESM2 width 320, 6 layers, leading `<cls>`, trailing `<eos>` | **table AGREES with measurement** |
| 2 | ESM-C leading `<cls>`, trailing `<eos>` | **table AGREES with measurement** |
| 3 | ESM-C `max_tokens 2048` | **TABLE WRONG on the key.** Value 2048 is right; the key is `max_position_embeddings`. `config.max_tokens` does not exist. |
| 4 | ESM-C hidden width and layer count | **TABLE INCOMPLETE** — omitted entirely. Measured 960 and 30 (corroborated by biotrainer's own table for the width). |
| 5 | biotrainer "routes any model name containing 'esm' to EsmModel" | **CLAIM WRONG for 2.1.1.** A dedicated `ESMC` class exists; routing is exact dict membership. Other reasons to reject biotrainer stand. |
| 6 | biotrainer "loads the model at construction" | **CLAIM CORRECT** — verified in `ESM2.detect`. |
| 7 | biotrainer requires python `>=3.12,<3.14` | **CLAIM CORRECT** — verified from PyPI metadata. |
| 8 | bio-embeddings unmaintained | **CLAIM CORRECT** — last release 0.2.2, `requires_python >=3.7.1,<3.10`. |
| 9 | `transformers>=5.16` for ESM-C; current release 5.22.1 | **CLAIM CORRECT** — resolved to exactly 5.22.1. |

### Additional constants and hazards the table did not cover

1. `EsmModel` randomly initializes `pooler.dense`; **`pooler_output` is non-deterministic and must never
   be used**. `EsmcModel` returns no `pooler_output` at all.
2. `config._commit_hash` is `None` on both models — fetch the revision SHA from `HfApi` or the cache path.
3. `len(hidden_states)` is layers **+ 1** (7 and 31); entry 0 is the embedding output.
4. `tokenizer.model_max_length` is the `1e30` sentinel on both — derive truncation from
   `max_position_embeddings` (1026 / 2048, both inclusive of the 2 special tokens).
5. `local_files_only=True` is **not** uniformly clean: models raise `OSError`, `EsmTokenizer` raises
   `TypeError`, `EsmcTokenizer` raises nothing at all and succeeds even for a nonexistent repo.
6. ESM-C's tokenizer has an extra `|` chainbreak special token (id 31) that ESM2 lacks.
7. ESM-C: `tokenizer.vocab_size` 33 vs `config.vocab_size` 64 — do not assert these are equal.
8. `tokenizer.bos_token` is `None` on ESM2 but `<cls>` on ESM-C.
9. The explicit class is spelled **`Esmc`**Model, not `EsmC`/`ESMC`.
10. biotrainer pins `transformers<5.20.0`, below the 5.22.1 validated here.

## Steps not reached

None. Steps 1–7 were all completed and are recorded above. GenSLM was deliberately not attempted, per
the task instruction; no claim about where its weights live is made here.
