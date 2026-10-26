# Work Record: p1-kbutillib-version-typing

**task_id:** p1-kbutillib-version-typing
**role:** developer
**dispatched_by:** Jane (e session 71d48f60), 2026-10-26 — item 1 of 2 in a serial bundle
**issue:** kbaseincubator/KBDLJobRunningPrototype#13 (Refs, not Fixes — the issue
closes only after a production deploy check)

## branch

- KBUtilLib: `maestro/developer/serial-bundle-on-one-kbutillib-b-kbu-prov-typing-ud-client`
- base commit: `0bb8a88` (off `main`)

## summary

Annotators now classify their own version string, so a consumer of
`AnnotationResult` can tell a tool release from an image content digest from a
string that identifies nothing — and can tell an operator-asserted version from
an observed one. No existing `tool_version` string value changed; two optional
fields were added and six annotator call sites set them.

The concrete problem: one free-form `tool_version` mixed semver releases
(`"1.14.6"`, `"1.11.4"`), image identities (`"<image>@sha256:<64 hex>"` or a
bare tag when `docker image inspect` fails), an executable path
(`"/opt/kofam_scan/exec_annotation"`), a runner version
(`"nextflow/24.10.5"`) and a dataset id (`"2018-0531"`). RAST's `"2018-0531"`
is an operator assertion (Chris Henry, 2026-10-15) that was recorded only as a
code comment in `rast_utils.py`, so a reader — or a program — took it for an
observed value.

## what changed

### 1. `src/kbutillib/domains/genome/annotation/annotator_utils.py`

- `AnnotationResult` (dataclass, now at `:102-139`) gained two fields, both
  **appended after `records` with `None` defaults**, so every existing
  positional and keyword construction still works unchanged
  (`annotator_utils.py:138-139`):
  - `version_kind: str | None` — `"tool-version"`, `"artifact-digest"`, or
    `None` meaning `tool_version` (if any) does not identify the tool version.
  - `version_asserted_by: dict | None` — `None` when the version was
    *observed*; otherwise `{"who": str, "date": str | None, "basis": str}`.
  Both are documented in the class docstring.
- New public helper `classify_version(value, *, from_image)` at
  `annotator_utils.py:295-348`, with the module-level `_DIGEST_RE` at `:292`.
  Rules, exactly as specified:
  - `None` / empty / whitespace-only → `None`.
  - `from_image=True`: `re.fullmatch` of `.+@sha256:[0-9a-fA-F]{64}` →
    `"artifact-digest"`; anything else → `None`. The match is a **full** match,
    not a prefix match, which is what rejects `…@sha256:<64 hex>-dirty` and a
    65-hex string.
  - `from_image=False`: `"nextflow/"` prefix → `None` (runner, not tool);
    `os.path.isabs(value)` → `None`; else `"tool-version"`.
  - Never raises: a non-`str` input returns `None` rather than blowing up on
    `.startswith`.
  - The docstring carries an explicit **SCOPE** paragraph: the classifier
    covers the formats the wired annotators emit and does **not** judge
    arbitrary strings — `from_image=False` returns `"tool-version"` for any
    value that is not a runner label or an absolute path.
- Added `import os` (`:34`) for `os.path.isabs`.

### 2. The six wired call sites

| Module | Line | What it sets |
|---|---|---|
| `prokka_utils.py` | `:442` | `version_kind=classify_version(tool_version, from_image=False)` |
| `bakta_utils.py` | `:901` | `version_kind=classify_version(tool_version, from_image=False)` — annotation path only |
| `dram2_utils.py` | `:737` | `version_kind=classify_version(tool_version, from_image=False)` |
| `transyt_utils.py` | `:707` | `version_kind=classify_version(tool_version, from_image=True)` |
| `kofamscan_utils.py` | `:611-634` | docker mode → `classify_version(identity, from_image=True)`; native mode → `None` |
| `domains/external/rast_utils.py` | `:404-409` | `version_kind="tool-version"` plus a `version_asserted_by` copy |

- **kofamscan** required hoisting the inlined `tool_version` expression out of
  the constructor into a local (`kofamscan_utils.py:611-616`) so the kind could
  be derived from the same value. The string produced is byte-identical:
  `(docker_image_identity(...) if self._docker_image else self._kofamscan_exe) or None`.
  Native mode is written as an explicit `None` rather than
  `classify_version(path, from_image=False)`; the two agree for the configured
  default (`/opt/kofam_scan/exec_annotation` is absolute → `None`), but an
  explicit `None` also holds for a *relative* configured executable name,
  which `classify_version` would otherwise call a tool version.
- **bakta**: only the annotation path (`BaktaUtils.annotate`) was touched. The
  structural-call `BaktaCallResult` at `bakta_utils.py:1366` was left alone, as
  instructed.

### 3. RAST operator assertion — `src/kbutillib/domains/external/rast_utils.py`

- `_DEFAULT_KMER_DATASET_ASSERTED_BY` (`:146-153`) sits next to
  `_DEFAULT_KMER_DATASET`, holding
  `{"who": "chris", "date": "2026-10-15", "basis": "kmer dataset id named by the PI; the RAST service exposes no version"}`.
- `_CONFIG_KMER_DATASET_ASSERTED_BY` (`:157-161`) holds
  `{"who": "config", "date": None, "basis": "rast.kmer_dataset override"}`.
- `_UNSET = object()` (`:167`) is a sentinel passed as
  `get_config_value("rast.kmer_dataset", default=_UNSET)` in `__init__`
  (`:261-272`), which records `self._kmer_dataset_from_config`. **Supplied-ness,
  not value comparison** — a deployment that explicitly pins
  `rast.kmer_dataset: "2018-0531"` has asserted that value itself, and a
  value comparison would silently credit the PI for it.
- `annotate` returns `version_asserted_by=dict(...)` — a **copy**, so a caller
  mutating the dict on one result cannot poison every later run.

## tests added

| File | What |
|---|---|
| `tests/annotators/test_annotator_utils.py` | `TestClassifyVersion` — a 29-case parametrised table covering every rule: 64-hex full match (lower and mixed case) → `artifact-digest`; 63-hex, 65-hex, `-dirty` trailing junk, trailing space, 64 non-hex chars, bare `kbutillib/kofamscan:latest`, bare `:latest`, an 8-char abbreviated digest, and a plain release string in image mode → `None`; `nextflow/24.10.5` and `nextflow/` → `None`; `/opt/kofam_scan/exec_annotation` and `/usr/local/bin/prokka` → `None`; `1.14.6` / `1.11.4` / `2.0.0-beta17` / `v2.0.0-beta17` / `2018-0531` → `tool-version`; `None` / `""` / `"   "` → `None` in both modes. Plus: the documented SCOPE limit (`test_an_arbitrary_string_is_called_a_tool_version`), never-raises on a non-string, `from_image` is keyword-only, and the bare-`:latest` asymmetry between the two modes. |
| `tests/annotators/test_annotator_utils.py` | `TestAnnotationResultDataclass` — the two fields default to `None`, the pre-existing **positional** 7-argument construction still works, and the fields round-trip. |
| `tests/annotators/test_prokka_utils.py:498-503` | `version_kind == "tool-version"`, `version_asserted_by is None`. |
| `tests/annotators/test_bakta_utils.py:562-566` | same, on the payload's `version.bakta` = `"1.11.4"`. |
| `tests/annotators/test_dram2_utils.py:582-586` | `"v2.0.0-beta17"` → `tool-version`. |
| `tests/annotators/test_dram2_utils.py:1705-1751` | three new module-level tests driving `annotate` through a stubbed `_run_nextflow`: the **`nextflow/` fallback** case → `version_kind is None`; the bare manifest form `"2.0.0-beta17"` → `tool-version`; `tool_version=None` → kind `None`. |
| `tests/annotators/test_kofamscan_utils.py:500-505` | native mode: `tool_version` is the executable path, kind is `None`. |
| `tests/annotators/test_kofamscan_utils.py:512-547` | docker mode with a patched `docker_image_identity`: a full 64-hex digest → `artifact-digest`; the bare-tag degradation → `None`. |
| `tests/annotators/test_transyt_utils.py:607-644` | full digest → `artifact-digest`; bare tag → `None`; and the module's own existing `mock:latest@sha256:abc` fixture (an abbreviated digest) → `None`. |
| `tests/external/test_rast_utils.py:185-200` | default path: `version_kind == "tool-version"` and `version_asserted_by` is the full PI dict, `who="chris"`, `date="2026-10-15"`. |
| `tests/external/test_rast_utils.py:224-289` | four new tests: config override asserts `who="config"`; the default and override dicts **differ**; an explicit config pin *of the default value* is still the config's assertion (the supplied-ness rule); and the returned dict is a copy, not the module constant. |

## annotators left at the defaults

**None.** Every `AnnotationResult(...)` construction in `src/` is one of the six
wired above — verified with `grep -rn "AnnotationResult(" src/`, which returns
exactly those six plus the dataclass definition. `checkm2_utils.py` does not
construct one. The only other construction anywhere is the test helper at
`tests/annotators/test_annotator_utils.py:147`.

Also checked: nothing in this repo serialises `AnnotationResult` via
`dataclasses.asdict` or enumerates its `fields()`, so appending two fields
cannot change a stored payload shape produced by KBUtilLib itself.
`src/kbutillib/domains/genome/annotator_utils.py` is a star-import alias shim
with its own `__all__`; `classify_version` reaches it through the star import
but was deliberately not added to that `__all__`, matching
`docker_image_identity`, which is also unexported.

## verification

Targeted, per the cached baseline's instruction not to re-run the full suite
(`build-baselines/kbutillib-h100-main-6310d61.json` records the full run at
**3416 s**, 5.7× the 600 s tool ceiling).

```
cd /home/chenry/.maestro/worktrees/kbu-prov-typing-ud-client
TMPDIR=/scratch/chenry/tmp PYTHONPATH=$PWD/src \
  /home/chenry/venvs/kbdl/bin/python -m pytest tests/annotators tests/external \
  -q -p no:cacheprovider --no-header

810 passed, 20 skipped in 39.70s     # exit 0
```

Also run individually while iterating: `tests/annotators/test_annotator_utils.py`
(97 passed), `tests/annotators/test_dram2_utils.py` (105 passed, 1 skipped),
`tests/external/test_rast_utils.py` (38 passed).

**Baseline comparison (by nodeid, never by count).** Neither cached baseline
lists a single failing nodeid under `tests/annotators/` or `tests/external/`:

- `build-baselines/kbutillib.json` (0f84803, 2026-09-13): of its 99
  `known_failing` ids, the only one matching any of my keywords is
  `tests/cli/test_kind.py::TestBundleFiles::test_files_names_cannot_escape_the_app_dir`
  (a substring coincidence, not an annotator test).
- `build-baselines/kbutillib-h100-main-6310d61.json` (6310d61, 2026-10-19):
  its 69 failures are `failures_by_area` biochem 27 / cli 19 / modeling 16 /
  domains/ai 5 / core 2 — **zero** in annotators or external.

So the two directories I touched were fully passing at base and are fully
passing now. **This is not a claim that the suite is green** — both baselines
record a large pre-existing failing set in biochem, cli, modeling, domains/ai
and core, none of which these files are imported by.

## what this prompt asserted that I found to be false

1. **`prokka_utils.py (~:435/:539)`** — there is only **one** `AnnotationResult`
   construction in `prokka_utils.py`, at `:433`. `:539` is
   `tool_version = self._parse_prokka_version(result.stderr)` inside
   `_run_prokka`, which *produces* the string and returns it up to the single
   call site. One edit, not two.
2. **`transyt_utils.py (~:682/:700)`** — same shape: `:682` is
   `tool_version = docker_image_identity(self._docker_image)` and `:698` is the
   one construction. One edit.
3. **`dram2_utils.py (~:730/:952)`** — `:952` is inside `_run_nextflow`
   (`tool_version = self._resolve_tool_version(...)`), not a second
   construction. One edit, at `:728`.
4. **`AnnotationResult (~:99-122)`** — the dataclass ran `:99-122` before my
   change, which matched; worth recording only because the field list the
   prompt gave was exactly right and the positional-compatibility check it
   asked for turned out to be unnecessary in practice: **all six** existing
   constructions use keyword arguments, as does the test helper. Appending the
   fields was still the right call and `test_positional_construction_still_works`
   pins it.
5. **`rast_utils.py ~:118-138 / :139 / :233 / :355`** — all four approximately
   correct pre-change (`_DEFAULT_KMER_DATASET` was at `:139`, the config read at
   `:233`, the result at `:353`). No divergence.

## caveats

- **Ruff was not run.** `pyproject.toml:194` declares `ruff >=0.5.5` as a dev
  dependency, but no `ruff` binary exists on this machine's PATH or in
  `/home/chenry/venvs/kbdl/bin/`, and installing one into a shared service venv
  is out of a developer task's bounds. Import blocks were hand-ordered to match
  ruff/isort's case-sensitive rule as evidenced by the existing code (`_`-prefixed
  names last, e.g. `rast_utils`'s `_chunked, _split_role_terms`). Line lengths
  were kept under the surrounding files' width; `E501` is ignored in this repo's
  ruff config anyway (`pyproject.toml:360`).
- `classify_version` is deliberately permissive in `from_image=False` mode: an
  arbitrary non-path, non-`nextflow/` string is reported as `"tool-version"`.
  That is pinned by a test and stated in the docstring's SCOPE paragraph.
  Widening it means adding the new producer's format there, deliberately.
- `_DEFAULT_KMER_DATASET_ASSERTED_BY` still cannot drift-check itself — the
  pre-existing comment at `rast_utils.py:118-138` explains why (the kmer server
  is unreachable from poplar and `GenomeAnnotation.version` returns the SDK
  module version). This change makes the assertion *legible to a program*; it
  does not make it verifiable.
- `version_asserted_by` is typed `dict | None`, not a `TypedDict`. The prompt
  specified `dict`; the three keys are documented in the `AnnotationResult`
  docstring and asserted whole in the RAST tests.
