# kbu model: auto-annotate unannotated genomes, and make GLPK the configurable default solver

## Research Report

### Important findings

**1. The reconstruct defect is `ontology_terms`, not `function` — and the mechanism
is fully traced.**
A prior session diagnosed this as "`.function` is None on all 4285 features".
`MSFeature` has no `.function` attribute at all. The empty surface is
`MSFeature.ontology_terms`, and its consumer is `_aaaa(genome, ontology_term)` in
`modelseedpy/core/msbuilder.py`, which builds `search_name_to_genes` by reading
`f.ontology_terms[ontology_term]`. `MSBuilder.__init__` defaults
`ontology_term="RAST"`. `MSGenome.from_fasta` constructs `MSFeature(id, seq,
description)` and leaves `ontology_terms = {}`.
Measured on kbhub 2026-09-30 against
`GCF_000005845.2_ASM584v2_protein.faa`: **4,285 features, 0 with any ontology
term, 0 with a RAST term, `search_name_to_genes` size 0.** No complex matches, so
no gene-associated reaction is added, so the build returns the template biomass
scaffold — 5 reactions / 82 metabolites / 0 genes, at exit 0.
Same defect the prior session found; the corrected mechanism is what makes the fix
a one-line mutation rather than a translation pipeline.

**2. The fix already exists twice over, and one of the two is the right one.**
`modelseedpy.core.rast_client.RastClient.annotate_genome(genome)` mutates an
MSGenome in place, calling `feature.add_ontology_term("RAST", function)`
(`rast_client.py:77-79`). That is the precedent, and it is one line.
`kbutillib.domains.external.rast_utils.RastUtils` wraps the same service as a
proper `AnnotatorUtils`: protein-alphabet guard, a configurable timeout, opt-in
chunking, a structured `RastServiceError` distinguishing network failure from a
server-side error from a malformed response, and an explicit privacy notice. We
use `RastUtils` and write a thin adapter to the MSGenome mutation, because the raw
`RastClient` path gives us bare `requests` exceptions and a hardcoded timeout —
and because the `AnnotatorUtils` abstraction is what lets a different annotator
drop in later without touching the CLI.

**3. No SSO/reaction translation step is needed.** The prior session pointed at
`domains/genome/annotation`'s `translate_function_to_reactions()`,
`translate_rast_function_to_sso()` and `convert_role_to_searchrole()`. None of
them is on this path. `MSBuilder` consumes raw RAST **role strings** directly,
normalising them with `normalize_role` and matching against template role names.
`RastUtils` already returns exactly those role strings, already split on the
multi-role delimiter `RastClient.annotate_genome` uses. The adapter is a direct
hand-off. This removes an entire subsystem from the design.

**4. Nothing in KBUtilLib sets a cobra solver, anywhere.** A repo-wide grep for
`cobra.Configuration`, `Configuration().solver` and `.solver =` across
`src/kbutillib/` returns exactly one hit — `ms_remote_solve_utils.py:70`, which
*reads* a solver name out of a response payload. So solver selection is left
entirely to optlang's default preference order, which ranks commercial backends
first. Measured on kbhub: `cobra.Configuration().solver` resolves to
`optlang.gurobi_interface`, and `optlang.available_solvers` reports
`{GUROBI: True, GLPK: True, SCIPY: True}`. The gurobi in that venv is a
restricted size-capped licence, so gapfilling's ~9,400-reaction candidate set
exceeds the cap and the verb dies.

**5. GLPK is measured-sufficient, not hoped-sufficient.** Miles ran the same
`MSReconstructionUtils.build_metabolic_model -> gapfill_metabolic_model ->
run_multi_gapfill` path on h100 (reply to trigger envelope
`0c210a2a-abb5-42ef-9a0c-c0e4009b42e2`, answered 2026-09-28 19:52 UTC), on an
E. coli-sized proteome against the gram_neg template's 9,434 candidate reactions,
on 17-compound glucose-minimal media. **GLPK: 28.1s, `reactions_added=437`,
post-gapfill FBA `objective_value=1.168`. Unrestricted gurobi: 27.4s, 438,
1.168.** That an unrestricted commercial solver is no faster proves the LP is not
genuinely large; kbhub's failure is the licence cap and nothing else. Miles also
confirmed `gurobipy` is not a declared dependency of kbutillib, modelseedpy,
cobra, cobrakbase or optlang — so its presence in `~/venvs/kbu-modeling` is an
explicit install that then shadowed GLPK by accident.

**6. THE TRAP: `get_config_value` is inert on the `kbu model` CLI path.** This is
the single finding that most shapes the solver half, and copying the
`remote_solver.*` precedent would have walked straight into it.
`interfaces/cli/model.py::_construct_offline` constructs every modeling utility
with `config_file=False`. `SharedEnvUtils.get_config_value` reads *only*
`self._config_hash`, which is `{}` under `config_file=False`, and it has **no
environment-variable fallback** — it returns the caller's default and logs at
debug. A `modeling.solver` key read through `get_config_value` would therefore be
honoured on the notebook path and **silently ignored on every CLI invocation**.
The solver must be resolved in the CLI layer instead, against the config file
directly. `model.py` already establishes exactly that shape in
`_resolve_modelseed_db_path()` (environment override, then candidate paths, then
`None`).

**7. `modeling.*` is an existing config namespace, and `~/.kbutillib/` is
per-machine.** `~/.kbutillib/config.yaml` already carries a `modeling:` section
(`default_objective`, `fba_timeout`), so `modeling.solver` needs no new namespace.
`_find_config_file` resolves `~/.kbutillib/config.yaml` at priority 2 and a
project-root `config.yaml` at priority 3 — and the repo *does* ship a root
`config.yaml`. So a default committed to the repo would leak to every machine,
whereas `~/.kbutillib/config.yaml` is per-machine, untracked, and is therefore the
correct h100 override surface with no hostname detection anywhere.

**8. The RAST endpoint is reachable from the kbhub pod.** Measured 2026-09-30:
`tutorial.theseed.org` resolves (8 addresses, Cloudflare-fronted) and completes a
TLS handshake in 0.02s from inside the pod. The annotation half is therefore
demoable on kbhub. A full annotation RPC on a real proteome was **not** performed —
see the quality report.

**9. The remote LP-solver service is not an alternative here.**
`domains/modeling/ms_remote_solver_utils.py` is an LP-text-in / solution-out HTTP
client (POST gzipped LP, poll for a six-key result). It is not an optlang
interface and cannot be assigned to `cobra.Configuration().solver`. Making it one
is a separate project of real size. GLPK removes the need.

### Links to research output

No external literature scan was dispatched — see the quality report for why.
Miles's measured solver answer, which this design rests on, is at
`~/Dropbox/AIPlatform/channels/trigger-inbox/replies/0c210a2a-abb5-42ef-9a0c-c0e4009b42e2.json`.

### Quality report

**Searched, and read in full:** `interfaces/cli/model.py` (module docstring,
`_resolve_modelseed_db_path`, `_construct_offline`, `_recon_utils`, `_fba_utils`,
`_resolve_template`, `_load_media`, `reconstruct_cmd`, `gapfill_cmd`);
`domains/modeling/ms_reconstruction_utils.py` (`build_metabolic_model` in full,
`compute_ontology_model_changes` and `kb_build_metabolic_models` by signature and
annotation references); `domains/external/rast_utils.py` in full;
`domains/genome/annotation/annotator_utils.py` (class and dataclass surface);
`core/shared_env_utils.py` (`_find_config_file`, `read_config`,
`get_config_value`, `set_environment_variable`, the credential bootstrap);
`domains/modeling/ms_remote_solver_utils.py` (config pattern and `solve_lp`
signature); `modelseedpy/core/msgenome.py` (`MSFeature`, `MSGenome`, `from_fasta`,
`from_annotation_ontology`) and `modelseedpy/core/msbuilder.py` (`_aaaa`,
`MSBuilder.__init__`, complex matching) in the relevant regions;
`modelseedpy/core/rast_client.py` (`annotate_genome`); `kind_app/skill.md`;
`agent-io/docs/kbu-model-cli.md`; and
`/home/chenry/albert/agent-io/docs/kind-apikey-and-kbutillib-skills.md` Step 4,
which already carries the numeric acceptance criteria this PRD adopts.

**Measured on kbhub, not assumed:** the 4,285 / 0 / 0 / 0 annotation-coverage
numbers; `search_name_to_genes` size 0; `cobra.Configuration().solver` resolving
to gurobi; `optlang.available_solvers`; the absence of any solver assignment in
`src/kbutillib/`; DNS + TLS reachability of `tutorial.theseed.org`; the presence
of a `modeling:` section in `~/.kbutillib/config.yaml`; the presence of a
repo-root `config.yaml`.

**Not searched, and why.** No external literature or prior-art scan was
dispatched to Maestro: kbhub has no `maestro` client installed (verified — only
`assistant` is on PATH) and is not an AgentForge/Maestro worker, so the rail is
unavailable from this machine. The confront step is unavailable for the same
reason and is recorded as skipped rather than silently dropped. The design is
also a poor candidate for an external scan — it is entirely internal
integration against two libraries already in the tree, with no design space that
published prior art would narrow.

**Not verified, and it matters.** No live RAST annotation call was made. TLS
reachability is not the same as a successful ~4,300-protein
`GenomeAnnotation.run_pipeline` round trip, and the wall-clock cost of that call
from this pod is unknown. If the call is slow enough to approach the default
30-minute RPC deadline, `chunk_size` becomes operationally necessary rather than
optional — `RastUtils` already supports it, so this changes an operational
default, not the design. Also unverified: that GLPK completes the gapfill *on
kbhub specifically*. Miles measured it on h100 with an identical optlang solver
set; the cross-machine inference is strong but it is an inference, and the kbhub
end-to-end run is deliberately carried as an acceptance step outside the
taskplan (see Q7).

**Confidence the design should carry: high on both root causes** (each traced to
a named line and confirmed with measured numbers), **high on the solver fix**
(measured on h100 by Miles, and the config-inertness trap is measured here),
**medium on the annotation fix's operational cost** (correctness is certain; RAST
latency and chunking behaviour from this pod are not).

## Revision Log

**Round 0 — 2026-09-30 (initial single-pass draft).**
First draft. Both root causes traced to named lines and confirmed with measured
numbers on kbhub. Solver default settled on GLPK from Miles's h100 measurements.
The `config_file=False` inertness trap (finding 6) is new to this round and is
what moved solver resolution out of `get_config_value` and into the CLI layer.
The SSO-translation subsystem the prior session expected was found to be
unnecessary and is removed from scope.

## Problem Statement

A researcher on kbhub runs the documented modelling arc —
`reconstruct -> gapfill -> fba -> fva` — on an *E. coli* protein FASTA, and gets
a result that looks like a success and is worthless.

`reconstruct` exits 0 and reports a model with 5 reactions, 82 metabolites and
**0 genes** built from 4,285 proteins. Nothing in the output says the genome was
never annotated; the researcher has to know that a genome-scale *E. coli* model
should have a few thousand reactions to notice anything is wrong. `gapfill` then
fails outright with `Model too large for size-limited license`, an error that
points at model size when the actual cause is a restricted licence being picked
ahead of a perfectly adequate free solver. `fba` on the scaffold reports
`solver_status: optimal` with `objective_value: 0.0` — again a passing-looking
signal over a broken result.

The through-line is that every one of these failures reports success or a
misleading cause. Four separate checks in this stack returned a passing signal on
a broken system on 2026-09-28. A researcher who trusts the exit code ships a
conclusion drawn from a template biomass scaffold.

## Solution

From the researcher's point of view, three things change.

**`reconstruct` annotates the genome when it needs to.** Given a bare protein
FASTA, `reconstruct` notices that no feature carries a functional role, sends the
proteome to RAST once, writes the returned roles back onto the genome, and builds
from the annotated genome. The reported reaction and gene counts become real
numbers in the thousands instead of a scaffold. A genome that arrives already
annotated is detected as such and is never re-sent.

**A reconstruct that cannot annotate fails instead of lying.** If the genome needs
annotation and RAST is unreachable, the verb fails with a message naming the
missing annotation and the two ways forward. The 5-reaction scaffold is still
obtainable — `--annotate never` produces exactly today's behaviour — but it has to
be asked for, and even then stderr warns when the result has fewer than a few
hundred reactions or zero genes.

**Gapfilling works, because GLPK is the default.** All four verbs accept
`--solver`, and in its absence resolve to GLPK rather than to whatever optlang
happens to prefer. On h100, where an unrestricted commercial licence exists, a
single line in that machine's own `~/.kbutillib/config.yaml` overrides the default
to gurobi. Nothing detects a hostname; the override lives on the machine it
applies to.

The honesty cost is stated rather than hidden: `reconstruct` on an unannotated
genome now makes one network call to a third-party public SEED server. It still
needs **no KBase credential**. The documentation says exactly that, in place of
the current blanket "fully offline" claim.

## User Stories

1. As a researcher, I want `kbu model reconstruct` on a bare protein FASTA to
   produce a genome-scale model with thousands of gene-associated reactions, so
   that the model I gapfill and simulate reflects my organism rather than a
   template scaffold.
2. As a researcher, I want `reconstruct` to tell me how many of my features were
   annotated and by which tool, so that I can judge the model before I build on
   it.
3. As a researcher whose genome is already annotated, I want `reconstruct` not to
   re-send my proteome over the network, so that I neither wait for a redundant
   call nor leak sequences I did not need to share.
4. As a researcher with no network access, I want `--annotate never` to reproduce
   exactly today's offline behaviour, so that an air-gapped or reproducibility run
   still works.
5. As a researcher, I want a reconstruct that *needed* annotation and could not get
   it to **fail**, so that I never again mistake a 5-reaction scaffold for a
   model.
6. As a researcher who deliberately asked for `--annotate never`, I want a stderr
   warning when the resulting model has suspiciously few reactions or zero genes,
   so that an explicitly-requested scaffold is still visible as a scaffold.
7. As a researcher handling unpublished collaborator sequences, I want the CLI to
   name the third-party endpoint on every annotating run, so that the privacy
   consequence of the default is in front of me at the moment it happens.
8. As a researcher on kbhub, I want `kbu model gapfill` to complete rather than
   fail with `Model too large for size-limited license`, so that the gapfill stage
   of the documented arc is usable on this pod at all.
9. As a researcher, I want `--solver` on `reconstruct`, `gapfill`, `fba` and `fva`,
   so that I can choose a backend per run without editing any configuration.
10. As an operator on h100, I want to set `modeling.solver: gurobi` once in that
    machine's `~/.kbutillib/config.yaml` and have every verb honour it, so that a
    machine with an unrestricted licence uses it without any code change.
11. As an operator, I want an environment override to outrank the config file, so
    that a one-off run or a CI job can pin a solver without touching machine
    state.
12. As a researcher, I want the resolved solver name reported in `--json` output,
    so that a result I archive records which backend produced it.
13. As a notebook user calling `build_metabolic_model` directly, I want the same
    GLPK default the CLI gets, so that the library and the CLI do not disagree
    about which solver is in use.
14. As a KOROS/KIND session reading the injected `skill.md`, I want the offline
    claim to be accurate about the annotation network call, so that I do not
    promise a user an offline capability that is about to make an HTTP request.
15. As a reviewer of an archived run, I want the annotation tool, run id and
    coverage recorded in the provenance stamp, so that I can tell whether a model
    was built from RAST roles or from a pre-annotated genome.
16. As a maintainer, I want the MSGenome annotation adapter to accept any
    `AnnotatorUtils`, so that bakta or prokka can be wired in later without
    changing the CLI.
17. As a maintainer, I want the solver precedence chain to live in exactly one
    function, so that the CLI and the library constructors cannot drift apart.
18. As a maintainer, I want a test that fails if `get_config_value` is ever made
    the solver's config source on the CLI path, so that the
    `config_file=False` inertness trap cannot be reintroduced.

## Implementation Decisions

### Module 1 — `domains/modeling/ms_genome_annotation.py` (new)

The annotation-to-MSGenome bridge. A deep module: a two-function interface over
proteome extraction, annotator dispatch, term mapping, coverage accounting and
input guards.

```
@dataclass
class AnnotationCoverage:
    tool: str                  # AnnotationResult.tool, e.g. "rast"
    run_id: str | None         # AnnotationResult.run_id
    features_total: int        # len(ms_genome.features)
    features_annotated: int    # features that gained >=1 term
    terms_total: int           # total roles written
    namespace: str             # the ontology namespace written, e.g. "RAST"

def is_annotated(ms_genome, namespace: str = "RAST") -> bool

def annotate_msgenome(
    ms_genome,
    annotator=None,                  # any AnnotatorUtils; default RastUtils()
    *,
    namespace: str = "RAST",
    chunk_size: int | None = None,
) -> AnnotationCoverage
```

- `is_annotated` returns True iff at least one feature has a non-empty
  `ontology_terms[namespace]`. It is the *measurement* that drives
  `--annotate auto`; the trigger is never inferred from the input file's type or
  extension.
- `annotate_msgenome` builds `{f.id: f.seq}` from `ms_genome.features`, skipping
  features with an empty sequence, calls
  `annotator.annotate(proteins, chunk_size=chunk_size)`, and for each returned
  `AnnotationRecord` writes every `Term.value` back with
  `feature.add_ontology_term(namespace, value)` — the same mutation
  `RastClient.annotate_genome` performs. It mutates in place and returns coverage.
- It raises `ValueError` if the genome has no features, or no feature with a
  non-empty sequence, rather than issuing an annotation call that cannot succeed.
- It does **not** catch `RastServiceError` or `ToolUnavailableError`. Those
  propagate to the CLI, which owns the user-facing error text. A library that
  swallowed them would reintroduce the silent-scaffold failure one layer down.
- `Term.value`, not `Term.id`, is what gets written: `MSBuilder` matches role
  *names* through `normalize_role`, and `RastUtils` returns roles with
  `id=None, value=<role string>`.

Placed under `domains/modeling/` rather than `domains/genome/annotation/`: its sole
purpose is to prepare an MSGenome for `MSBuilder`, MSGenome is modelseedpy's
modeling-side object, and putting the adapter in the annotation package would make
that package depend on a modeling concept. It imports `RastUtils` lazily inside
the function so the modeling domain does not gain an import-time dependency on the
external domain.

### Module 2 — `domains/modeling/ms_solver_config.py` (new)

The single home of solver precedence. Two functions, one constant.

```
DEFAULT_SOLVER = "glpk"

def resolve_solver(explicit: str | None = None,
                   config_getter=None) -> str

def apply_solver(name: str, model=None) -> str
```

- `resolve_solver` implements exactly one precedence chain:
  **`explicit` > environment override > `modeling.solver` from config >
  `DEFAULT_SOLVER`.** The environment override is a single documented variable in
  the same `KBU_*` family `model.py` already reads for the ModelSEED database
  path. `config_getter` is an optional callable `(key, default) -> value`; callers
  that have a live config-backed instance pass `instance.get_config_value`, and
  callers that do not (the CLI) pass a getter that reads
  `~/.kbutillib/config.yaml` directly. The chain itself exists in one place
  regardless.
- `apply_solver` sets `cobra.Configuration().solver = name` and, when a `model` is
  given, also sets `model.solver = name`. Both are required: cobra resolves the
  global configuration at model-*construction* time, so a global set alone does
  nothing for a model already loaded from JSON, and a per-model set alone does
  nothing for models `MSBuilder` constructs internally. It returns the resolved
  name so callers can report it.
- An unavailable solver name surfaces cobra/optlang's own error unmodified, with
  the resolved name and the precedence source named in the message. We do not
  fall back to another solver: silent cross-solver fallback is how the current
  defect became invisible.

### Module 3 — `interfaces/cli/model.py` (modify)

- **A module-level config-getter helper** that reads `~/.kbutillib/config.yaml`
  directly and returns a `(key, default)` getter. It mirrors
  `_resolve_modelseed_db_path()`'s existing shape in this same file.
  **It must not route through any `_construct_offline`-built instance's
  `get_config_value`**, which is inert because `_construct_offline` passes
  `config_file=False` (Research Report finding 6).
- **`--solver TEXT` on all four verbs** (`reconstruct`, `gapfill`, `fba`, `fva`),
  default `None`. Each verb calls
  `resolve_solver(explicit=solver, config_getter=...)` then `apply_solver(name)`
  **before** any model is built or loaded, and calls
  `apply_solver(name, model=<the model>)` again once it holds one. The resolved
  name appears in `--json` output as `"solver"` and in the human-readable line.
- **`--annotate [auto|always|never]` on `reconstruct`**, default `auto`,
  implemented as `click.Choice`. After `MSGenome.from_fasta`:
  - `never`: proceed unchanged.
  - `auto`: call `annotate_msgenome` iff `is_annotated(ms_genome)` is False.
  - `always`: call `annotate_msgenome` unconditionally.
- **On every annotating run**, before the call, emit one stderr line naming the
  tool and the endpoint host, and the fact that sequences leave the machine.
  Stderr, never stdout, so `--json` output stays byte-identical to the documented
  schema.
- **The hard-error path.** When annotation was required and `annotate_msgenome`
  raises, `reconstruct` fails with a `ClickException` that states the genome is
  unannotated, names the underlying cause, and names both ways forward
  (`--annotate never` to accept a scaffold; pre-annotate the genome). It must
  **not** fall through to a build. The failure is stamped via the existing
  `_stamp("failed")` path.
- **Coverage in the result.** `--json` output gains an `"annotation"` object —
  `{tool, run_id, features_total, features_annotated, terms_total}` — or `null`
  when no annotation ran. The existing `reactions`/`metabolites`/`genes` keys and
  their order are unchanged; the two new keys (`solver`, `annotation`) are added,
  because the documented schema is additive and existing consumers read by key.
- **The scaffold warning.** After a successful build, if `len(model.genes) == 0`
  or `len(model.reactions) < 100`, emit a stderr warning naming both counts and
  stating that this is the shape of an unannotated build. This fires regardless of
  `--annotate` mode, so it also catches an annotation that succeeded but matched
  nothing.
- **Provenance.** The `_record_reconstruct` stamp gains the annotation tool, run
  id and coverage counts, and the resolved solver, so an archived analysis records
  which backend and which annotation produced it.

### Module 4 — library-path solver default

`MSReconstructionUtils.__init__` and `MSFBAUtils.__init__` call
`resolve_solver(config_getter=self.get_config_value)` then `apply_solver(name)`,
storing the name on the instance. On the notebook path `get_config_value` works
normally, so `modeling.solver` is honoured natively there; on the CLI path the
value resolved by the constructor is then overridden by the verb's explicit
`apply_solver` call, which is correct because `--solver` outranks everything.

Rejected: a module-import-time side effect setting the global cobra solver. An
`import` that silently changes the caller's solver configuration is a worse
surprise than a constructor that does, and it would fire even for callers using
none of this.

### Module 5 — documentation

Three prose surfaces must change in the same commit as the behaviour:

- **`src/kbutillib/kind_app/skill.md`** — the sentence at line 9 claiming every
  verb "runs fully offline (no KBase token, no network)". This is what a KOROS
  session actually reads, and it is the only one a user never sees being wrong.
  Restated: verbs need no KBase credential; `reconstruct` on an *unannotated*
  genome makes one call to the public SEED RAST service; `--annotate never`
  restores fully-offline. Document `--annotate` and `--solver` in the usage block.
- **`agent-io/docs/kbu-model-cli.md`** — the same restatement, plus the new flags
  and the new `--json` keys.
- **`interfaces/cli/model.py`'s module docstring** — the "Local/offline
  construction" section, same restatement.

After landing, `kbu kind install` must be re-run to recompose the injected skill
text. No KIND server restart is needed. Verification on a spare port, never by
bouncing the live server.

## Testing Decisions

Test external behaviour. The annotation path's behaviour is *coverage on the
genome object*, and the solver path's is *which name gets resolved* — both
observable without a network call or a live LP.

- **`annotate_msgenome` with a stub annotator.** A fake `AnnotatorUtils` returning
  a fixed `AnnotationResult` for a small synthetic MSGenome. Assert
  `feature.ontology_terms["RAST"]` contents, the multi-term case, that a feature
  RAST returned nothing for is left untouched, and every field of the returned
  `AnnotationCoverage`. This is the test that would have caught the original
  defect.
- **`is_annotated` detection.** False for a `from_fasta` genome; True after one
  term is added; False for a genome carrying only a *different* namespace. The
  last case is what keeps `--annotate auto` from being fooled by a bakta-annotated
  genome whose terms MSBuilder cannot match.
- **Propagation, not swallowing.** A stub annotator raising `RastServiceError`
  must propagate out of `annotate_msgenome` unchanged.
- **`resolve_solver` precedence table.** One parametrised test per row of
  `explicit > environment > config > default`, including all four with only the
  default present, and the environment override set to the empty string (which
  must not win).
- **The inertness regression guard.** A test constructing a modeling utility the
  way `_construct_offline` does — `config_file=False` — and asserting that
  `get_config_value("modeling.solver", None)` returns `None` even when
  `~/.kbutillib/config.yaml` sets it. This pins finding 6 as a fact about the
  codebase, so a later refactor that routes CLI solver resolution through
  `get_config_value` fails a test rather than silently going inert.
- **CLI wiring via click's `CliRunner`.** `reconstruct --annotate never` on a
  fixture FASTA behaves as today; `--annotate auto` with a patched annotator
  reports coverage in `--json`; a patched annotator that raises produces a
  **non-zero exit** and a message naming the missing annotation; `--solver` shows
  up in `--json`.
- **Prior art:** `tests/test_ms_fba_utils_eval.py` already establishes the
  offline-construction pattern these tests need (`_make_fba_utils`), and the
  existing `tests/fixtures/model/glucose_minimal.json` is the media fixture.
  Follow both rather than inventing new harnesses.

**Not unit-tested, by design:** a live RAST call, and a live gapfill. Both are
carried as the kbhub acceptance step (Q7), asserted on numbers.

## Open Questions and Judgement Calls

Every item below is **already decided and reflected in the PRD body and
taskplan**. The PRD is buildable as it stands. Changing a decision means editing
its `DECIDED:` line and following the `If you disagree` consequences.

### Q1. What happens when a genome needs annotation and RAST is unreachable? -- DECIDED: hard error; `reconstruct` fails and names the missing annotation.
**Blast radius:** MEDIUM
**Why:** The behaviour being replaced is a 5-reaction scaffold returned at exit 0,
which is how this defect survived. Any outcome that still exits 0 preserves the
original sin. A hard error is the only result that cannot be mistaken for
success, and the escape hatch (`--annotate never`) keeps the scaffold reachable
for anyone who genuinely wants it.
**If you disagree:** the alternative is exit 0 with a loud stderr warning. That
would remove the hard-error branch from `reconstruct_cmd` in Module 3, remove the
non-zero-exit assertion from the `CliRunner` test in Testing Decisions, and
change user story 5. It would also mean a scripted pipeline on a pod that lost
network silently produces scaffolds again — the exact 2026-09-28 failure.
**Confidence:** high -- Chris's ruling was that unannotated genomes get
annotated; a silent scaffold contradicts the intent of that ruling, not just its
letter.

### Q2. Where does the solver default come from, given `get_config_value` is inert on the CLI path? -- DECIDED: resolve in the CLI layer against `~/.kbutillib/config.yaml` directly, via a single `resolve_solver()` shared with the library path.
**Blast radius:** MEDIUM
**Why:** `_construct_offline` passes `config_file=False`, so `_config_hash` is
`{}` and `get_config_value` has no environment fallback — a `modeling.solver` key
read that way is honoured in notebooks and **silently ignored on every CLI run**.
That is the same failure class as an allow-rule that matches nothing. Resolving in
the CLI, in the shape `_resolve_modelseed_db_path()` already uses in that file,
makes the key actually live.
**If you disagree:** the alternative is changing `_construct_offline` to enable
config discovery (`config_file=None`). That changes the behaviour of *every*
`get_config_value` call on the CLI path — the RAST timeout, the `remote_solver.*`
keys, anything added later — and `_find_config_file`'s priority-3 rule would let a
stray `config.yaml` in the caller's `cwd` (searched up 5 levels) silently
reconfigure the modelling verbs. Much larger blast radius for no gain.
**Confidence:** high -- the inertness is measured, not inferred:
`get_config_value`'s body reads only `self._config_hash`.

### Q3. How does h100 get gurobi without a machine-detection branch? -- DECIDED: h100's own `~/.kbutillib/config.yaml` sets `modeling.solver: gurobi`.
**Blast radius:** LOW
**Why:** `~/.kbutillib/` is per-machine, untracked, and already resolved at
priority 2 by `_find_config_file`, which makes machine scoping a property of the
filesystem rather than a branch in the code. `modeling:` already exists as a
section there. Hostname detection would encode fleet topology into a library that
colleagues also run; a repo-committed default would leak to every machine, because
the repo ships a root `config.yaml` at priority 3.
**If you disagree:** the alternative is exporting the environment override in
h100's shell profile — which still works and needs no code change, since the
environment outranks the config file. Only a hostname-detection alternative would
move code, and it would add a branch to `resolve_solver` and a fleet-topology
constant to `ms_solver_config.py`.
**Confidence:** high

### Q4. Do we use `RastUtils` or modelseedpy's one-line `RastClient.annotate_genome`? -- DECIDED: `RastUtils`, through a thin adapter.
**Blast radius:** LOW
**Why:** `RastClient.annotate_genome(genome)` would do the job in one line, but it
raises bare `requests` exceptions, hardcodes its timeout, has no protein-alphabet
guard, and offers no chunking. `RastUtils` wraps all of that and carries the
privacy notice, and it is an `AnnotatorUtils` — so the adapter accepting any
annotator is what lets bakta/prokka be added later without touching the CLI. The
adapter that buys this is roughly thirty lines.
**If you disagree:** drop Module 1's `annotator` parameter and call
`RastClient().annotate_genome(ms_genome)` in `reconstruct_cmd` directly. You lose
the structured `RastServiceError` the hard-error message in Q1 relies on, the
configurable RAST timeout, and `chunk_size` — which the quality report flags as
possibly operationally necessary from this pod. User story 16 goes away.
**Confidence:** high

### Q5. Does `--annotate auto` trigger on file type or on measured absence? -- DECIDED: measured absence -- `is_annotated()` is False.
**Blast radius:** LOW
**Why:** `MSGenome` can arrive annotated by several routes —
`from_annotation_ontology` populates both SSO and RAST terms, and a caller may
have annotated a `from_fasta` genome by hand. Triggering on "the input was a
`.faa`" would re-send those proteomes over the network for nothing, which is both
slow and a needless disclosure. Measuring the actual surface MSBuilder reads is
strictly better and is one line.
**If you disagree:** nothing else in the design moves, but user story 3 goes away
and the `is_annotated` namespace-isolation test in Testing Decisions becomes
meaningless.
**Confidence:** high

### Q6. Auto-annotation makes a third-party network call the DEFAULT. Is that acceptable? -- DECIDED: yes, per Chris's ruling, with a mandatory stderr disclosure on every annotating run.
**Blast radius:** MEDIUM
**Why:** Chris ruled on 2026-09-28 that an unannotated genome should be annotated
automatically via the RAST API, and this PRD does not relitigate that. But the
consequence is real and is not stated anywhere today: every default `reconstruct`
of an unannotated genome sends the caller's protein sequences over plain HTTPS to
`tutorial.theseed.org`, a public third-party SEED server that is neither
KBase-authenticated nor Argonne-hosted. On kbhub this matters more than
elsewhere — it is Berkeley-operated infrastructure and BERDL tenant data may
include unpublished collaborator material. The mitigation is disclosure at the
moment of the call plus `--annotate never`, not a changed default.
**If you disagree:** flipping the default to `--annotate never` would change one
`click.option` default and the `skill.md` wording, and would leave every naive
`reconstruct` producing today's scaffold — so it would need Q1's warning to become
a hard error on the `never` path too, which is contradictory. The coherent
alternative is default `auto` with an interactive confirmation, which is
unavailable: these verbs are run non-interactively by KOROS sessions and scripts.
**Confidence:** medium -- the decision follows Chris's ruling and I am confident in
it as a default; I am less confident that stderr disclosure is *sufficient* for
NDA-covered sequences on this pod specifically. What would raise it: an explicit
ruling on whether a sensitive-data guard (e.g. refusing to annotate when a
namespace is marked restricted) belongs in scope. I have not built one.

### Q7. How is this validated on kbhub, given the conductor cannot run work there? -- DECIDED: unit tests in the taskplan; the kbhub end-to-end run is carried OUTSIDE it, as an Albert acceptance step asserting numbers.
**Blast radius:** LOW
**Why:** kbhub is not an AgentForge or Maestro worker and has no `maestro` client,
so no taskplan task can execute there. An `in_context` task runs where the
conductor runs, which is not the pod with the restricted gurobi licence.
Pretending otherwise would put an unrunnable task in the plan. The plan therefore
carries tests that pass anywhere, and the pod validation is an explicit acceptance
step with the numeric criteria already recorded in
`/home/chenry/albert/agent-io/docs/kind-apikey-and-kbutillib-skills.md` Step 4:
**reconstruct > 500 reactions AND > 500 genes on E. coli; gapfill reports a
non-empty `reactions_added`; post-gapfill FBA `objective_value` > 0.** Assert
numbers, never exit codes — four checks in this stack returned passing signals on
a broken system on 2026-09-28.
**If you disagree:** if you want the pod run inside the plan, kbhub must first
become a Maestro worker, which is an infra change on another machine and outside
this PRD entirely.
**Confidence:** high

### Q8. Should the remote LP-solver service be the answer for kbhub instead of GLPK? -- DECIDED: no; it is architecturally unrelated and GLPK is measured-sufficient.
**Blast radius:** NONE
**Why:** `ms_remote_solver_utils` is an LP-text-in / solution-out HTTP client. It
is not an optlang interface and cannot be assigned to
`cobra.Configuration().solver`; making it one means writing an optlang backend,
a separate project. And Miles measured GLPK completing the gapfill in 28.1s with
`reactions_added=437` and FBA objective 1.168 — versus unrestricted gurobi's
27.4s / 438 / 1.168 — so there is nothing left for a remote commercial solver to
buy on this problem class. The poplar tunnel stays out of the modelling arc's
default path.
**If you disagree:** nothing in this PRD changes; you would open a separate PRD
for an optlang-over-HTTP backend.
**Confidence:** high -- this rests on measured numbers from h100, not on judgement.

### Q9. Does the `--json` schema change break existing consumers? -- DECIDED: add `solver` and `annotation` keys; change nothing existing.
**Blast radius:** LOW
**Why:** `reactions`, `metabolites`, `genes` and `model_path` keep their names,
types and values. Consumers read by key, and the documented contract is a JSON
object, so addition is compatible. The alternative — nesting the old keys under a
new envelope — would break every current consumer for cosmetic gain.
**If you disagree:** the `--json` assertions in the `CliRunner` tests and the
schema block in `agent-io/docs/kbu-model-cli.md` change.
**Confidence:** high

### Q10. Where does the annotation adapter live? -- DECIDED: `domains/modeling/ms_genome_annotation.py`.
**Blast radius:** NONE
**Why:** Its only purpose is preparing an MSGenome for `MSBuilder`. MSGenome is
modelseedpy's modeling-side object, and putting the adapter under
`domains/genome/annotation/` would make the annotation package depend on a
modeling concept, inverting the dependency. It imports `RastUtils` lazily so the
modeling domain gains no import-time dependency on `domains/external/`.
**If you disagree:** one file moves and its import sites change. Nothing else.
**Confidence:** medium -- this is a genuine judgement call about package boundaries
and a maintainer who owns the annotation package may reasonably prefer it there.
Nothing depends on the answer.

### Q11. What I could not decide and did not guess
Two external facts remain unreconciled, and neither blocks the build:

1. **The wall-clock cost of a ~4,300-protein RAST call from the kbhub pod.** DNS
   and TLS reachability are measured (0.02s handshake); the RPC itself was not
   attempted. If it approaches the default 30-minute RPC deadline, `chunk_size`
   becomes an operational necessity rather than an option. `RastUtils` already
   supports chunking, so this changes a recommended default, not the design. It
   is answered by the Q7 acceptance run, which is why that run is scoped to
   report timings.
2. **Whether GLPK completes the gapfill on kbhub specifically.** Miles measured it
   on h100 against an identical optlang solver set (`{GUROBI, GLPK, SCIPY}`), so
   the inference is strong — but it is an inference, on a different machine, with
   a different venv. The Q7 acceptance run settles it. I have not claimed it as
   measured-on-kbhub anywhere in this document.

There is one further thing I did not decide and deliberately did not build: a
sensitive-data guard on RAST submission (Q6). I do not know whether Chris wants
one, and inventing a namespace-restriction mechanism unasked would be a larger
change than this PRD.

## Gotchas and Unintuitive Consequences

1. **Setting `cobra.Configuration().solver` after a model exists does nothing to
   that model.** cobra resolves the global configuration at model-*construction*
   time. So `gapfill`/`fba`/`fva`, which load a model from JSON, need
   `apply_solver` called **before** the load *and* `model.solver` set on the
   loaded object. A reviewer seeing both calls may read the second as redundant;
   it is not, and removing either one reintroduces the bug on a different verb.

2. **`get_config_value` fails by returning your default and logging at debug.** It
   does not raise, and it does not warn. A config key that is never read looks
   identical to a config key set to the default value. This is why the inertness
   regression guard in Testing Decisions is a *test* and not a comment.

3. **`_find_config_file` searches the caller's `cwd` and up to five parent
   directories for `config.yaml`.** Any repo checkout with a root `config.yaml` —
   including KBUtilLib itself — can therefore become the active config for a
   `kbu` invocation run from inside it, whenever `~/.kbutillib/config.yaml` is
   absent. This is pre-existing behaviour that the solver key now rides on, and it
   is a reason the *default* lives in code (`DEFAULT_SOLVER`) rather than in the
   repo's `config.yaml`.

4. **Annotation coverage can be high and the model still a scaffold.** Coverage
   counts roles written to the genome; MSBuilder only adds a reaction when a
   normalised role matches a *template* role and completes a complex. A genome
   annotated against a template that does not cover it yields high coverage and
   few reactions. This is why the scaffold warning fires on the reaction/gene
   counts and not on coverage, and why it fires even when annotation succeeded.

5. **`--annotate always` on an already-annotated genome does not replace terms, it
   adds them.** `MSFeature.add_ontology_term` appends and de-duplicates by value;
   there is no clear-then-write. Re-annotating with a tool that names roles
   differently leaves both namings on the feature, and MSBuilder will match
   whichever hits the template. This is usually harmless and occasionally
   surprising; it is not worth a clear-first semantics that would silently discard
   a caller's curated annotation.

6. **Other annotators will populate a namespace MSBuilder ignores.** `MSBuilder`
   reads `ontology_terms["RAST"]` by default. A bakta or prokka run writes its own
   namespace, so wiring one in without an SSO translation step produces a genome
   that `is_annotated("RAST")` reports as **False** and that reconstruct will then
   try to RAST-annotate anyway. The adapter's `namespace` parameter makes the
   coupling visible; it does not remove it. This is the concrete reason non-RAST
   annotators are out of scope rather than "just another dict entry".

7. **The privacy posture of the default changes on the machine where it matters
   most.** kbhub is third-party-operated Berkeley infrastructure, and the pod's
   own guidance says not to route sensitive material through it. Auto-annotation
   makes outbound transmission of protein sequences to a public SEED server the
   *default* behaviour of the most commonly-run verb on that pod. Nothing in the
   design detects sensitivity; the only guards are the stderr notice and
   `--annotate never`.

8. **`kbu kind install` must be re-run after the docs change, and its absence is
   invisible.** `skill.md` is composed into the text a KOROS session reads at
   session start. Landing the code without recomposing leaves live sessions being
   told every verb runs with no network while `reconstruct` makes an HTTP call.
   No KIND restart is needed — and verification must use a spare port, never by
   bouncing the live server, because `serve.sh` is port-scoped and
   `kbu kind status` cannot see an unwired server.

9. **Fixing the solver makes `fba` newly capable of reporting failure.** Today
   `fba` on a scaffold reports `solver_status: optimal` with
   `objective_value: 0.0`. Once reconstruct produces a real model and gapfill
   succeeds, an `objective_value` of 0 becomes a genuine biological finding rather
   than an artifact — and `solver_status: optimal` remains a statement about the
   LP, not about viability. Any consumer keying on `solver_status` is still
   wrong, and this change does not fix it.

10. **`gurobipy`'s presence in `~/venvs/kbu-modeling` is an accident nobody is
    removing.** Miles verified it is not a declared dependency of kbutillib,
    modelseedpy, cobra, cobrakbase or optlang. This PRD makes selection explicit
    so the accident stops having an effect; it does not uninstall anything. A
    future venv rebuild that drops gurobipy will therefore change nothing, and a
    future one that adds a *different* commercial backend will also change
    nothing — which is the point.

## Sources Consulted

**Read and informative:**
- `src/kbutillib/interfaces/cli/model.py` — module docstring;
  `_resolve_modelseed_db_path` (l.~99); `_construct_offline` (l.~118, the
  `config_file=False` trap); `_recon_utils`/`_fba_utils`; `_resolve_template`;
  `_load_media`; `reconstruct_cmd` (the `MSGenome.from_fasta` line and the result
  dict); `gapfill_cmd`.
- `src/kbutillib/domains/modeling/ms_reconstruction_utils.py` —
  `build_metabolic_model` in full (the `MSBuilder(genome, template)` call);
  `compute_ontology_model_changes`; `kb_build_metabolic_models`
  (`annotation_priority` handling).
- `src/kbutillib/domains/external/rast_utils.py` — in full: `RastUtils.annotate`,
  `is_available`, `_split_role_terms`, `_call_rast`, `_records_from_payload`,
  `RastServiceError`, the privacy notice, the timeout config key, `chunk_size`.
- `src/kbutillib/domains/genome/annotation/annotator_utils.py` — `Term`,
  `AnnotationRecord`, `AnnotationResult`, `AnnotatorUtils`,
  `ToolUnavailableError`, `_guard_protein`, `_require_available`.
- `src/kbutillib/core/shared_env_utils.py` — `_find_config_file` (the 3-priority
  order and the 5-level `cwd` walk), `read_config`, `get_config_value` (reads only
  `_config_hash`; no environment fallback), `set_environment_variable`, the
  credential bootstrap.
- `src/kbutillib/domains/modeling/ms_remote_solver_utils.py` — the
  `get_config_value("remote_solver.*", DEFAULT)` pattern; `solve_lp` signature
  (LP text in, solution out — not an optlang interface).
- `modelseedpy/core/msgenome.py` — `MSFeature.__init__`
  (`ontology_terms = {}`), `add_ontology_term`, `MSGenome.from_fasta`,
  `from_annotation_ontology`, `from_gbff_*`.
- `modelseedpy/core/msbuilder.py` — `_aaaa` (reads
  `f.ontology_terms[ontology_term]`), `MSBuilder.__init__`
  (`ontology_term="RAST"` default), complex/role matching via `normalize_role`.
- `modelseedpy/core/rast_client.py` — `annotate_genome` (the
  `add_ontology_term("RAST", ...)` precedent at l.77-79).
- `src/kbutillib/kind_app/skill.md` — l.9, the "fully offline" claim; the usage
  block.
- `agent-io/docs/kbu-model-cli.md` — the offline claims and the `--json` schema.
- `/home/chenry/albert/agent-io/docs/kind-apikey-and-kbutillib-skills.md` —
  Step 4, which already carries the numeric acceptance criteria adopted in Q7,
  and the troubleshooting table naming all three symptoms.
- `~/Dropbox/AIPlatform/channels/trigger-inbox/replies/0c210a2a-abb5-42ef-9a0c-c0e4009b42e2.json`
  — Miles's measured GLPK-vs-gurobi answer (2026-09-28 19:52 UTC).
- `~/.kbutillib/config.yaml` — the existing `modeling:` section.
- `agent-io/prds/remote-lp-solver/data.json` — key decisions, to confirm the
  service's shape and that it is not an optlang backend.

**Read and uninformative (recorded so the absence is visible):**
- `mem search "RAST annotation kbu model reconstruct solver glpk"` — **zero
  results.** No Merlin memory node covers this design.
- A repo-wide grep for `cobra.Configuration` / `Configuration().solver` /
  `.solver =` across `src/kbutillib/` — one hit, and it is a payload read, not an
  assignment. The absence *is* the finding.
- `find` for `CONTEXT.md` in KBUtilLib — **no such file exists.** The prior
  session cited "CONTEXT.md:200" as the home of the offline promise; the actual
  home is `src/kbutillib/kind_app/skill.md:9`.
- `which maestro agentforge` — neither is installed on kbhub, which is what makes
  the confront step and the external scan unavailable.

**Measured on kbhub 2026-09-30 (commands run, not files read):**
- `MSGenome.from_fasta` on `GCF_000005845.2_ASM584v2_protein.faa`: 4,285 features
  / 0 with any ontology term / 0 with RAST / `_aaaa` → `search_name_to_genes`
  size 0.
- `cobra.Configuration().solver` → `optlang.gurobi_interface`;
  `optlang.available_solvers` → `{GUROBI: True, GLPK: True, SCIPY: True}`.
- DNS + TLS to `tutorial.theseed.org:443` — 8 addresses, handshake in 0.02s.

## Out of Scope

- **An optlang backend for the remote LP-solver service.** See Q8. Would make
  `ms_remote_solver_utils` assignable to `cobra.Configuration().solver`; it is a
  separate PRD, and GLPK removes the motivation.
- **Non-RAST annotators wired into the build path.** bakta, prokka, DRAM2 and
  kofamscan all exist as `AnnotatorUtils` in the tree, and Module 1 accepts any
  of them — but MSBuilder reads only `ontology_terms["RAST"]`, so using them
  requires the SSO/role translation step (`translate_rast_function_to_sso`,
  `convert_role_to_searchrole`). Gotcha 6 is why this is a project rather than a
  dict entry.
- **Annotation caching.** A 4,300-protein RAST call is minutes, and the same
  genome gets reconstructed repeatedly during development, so a cache is genuinely
  attractive. It is out because a stale cache is a correctness hazard of exactly
  the kind this PRD exists to remove, and because the cheap substitute already
  exists: annotate once, then use `--annotate never`. Revisit once the Q7
  acceptance run reports real RAST latency.
- **A sensitive-data guard on RAST submission.** See Q6 and Q11. Not built, not
  designed, and named rather than silently omitted.
- **`--atp-safe` behaviour, the `auto` template, and the genome classifier.**
  Untouched. `--atp-safe` stays off by default and still needs a local ModelSEED
  biochemistry database.
- **Uninstalling or repinning `gurobipy` in `~/venvs/kbu-modeling`.** See gotcha
  10. Making selection explicit is sufficient; changing the venv is a separate
  operational decision on a machine whose pip discipline is deliberately strict.
- **Making kbhub a Maestro worker.** Named in Q7 as the thing that would let the
  pod acceptance run live inside a taskplan. An infra change elsewhere.

## Further Notes

**Repo hazard for whoever implements this.** There are two KBUtilLib checkouts on
kbhub. `~/Dropbox/Projects/KBUtilLib` (branch `wip`) is the canonical development
tree and owns this PRD. `/global_share/KBaseUtilities/KBUtilLib` (branch
`deploy/main-20260926`, at `73d6197`) is a **shared** checkout that colleagues
read. Never `pip install` from the shared path — it builds in place and leaves
hundreds of gitignored files that `git status` does not show.

**Four surfaces change together**, and three of them are in the taskplan: the CLI
verb, the solver configuration, and the prose (`skill.md`,
`agent-io/docs/kbu-model-cli.md`, the `model.py` docstring). The fourth,
`/home/chenry/albert/agent-io/docs/kind-apikey-and-kbutillib-skills.md`, is not in
any git repository and is Albert's to update after the Q7 acceptance run lands —
its Step 4 and its troubleshooting table both currently describe the broken
behaviour as expected.

**The taskplan is five sequential single-task phases, and that is forced rather
than chosen.** The annotation adapter and the solver module are genuinely
independent files and would naturally be one parallel group, as would the library
constructors and the prose. `load_taskplan` rejects both: a `parallel_group` whose
tasks share a repo is refused outright, because concurrent branches in one repo
collide. So the work is serialised in dependency order
(`annotation-adapter` → `solver-config` → `cli-flags` →
`library-solver-default` → `docs-offline-restatement`). Nothing about the design
depends on this; do not read the sequencing as a claim that the steps are
logically dependent on each other, because two pairs of them are not.

**Confront was not run.** kbhub has no `maestro` client and is not a
Maestro/AgentForge worker, so the cross-family adversary rail is unreachable from
this machine. Recorded in `data.json` as
`"confront": "skipped: no maestro client on kbhub (not a Maestro worker)"` rather
than silently omitted. If this PRD is worth attacking before build, it should be
confronted from primary-laptop or h100.
