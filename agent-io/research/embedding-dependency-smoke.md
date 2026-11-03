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
