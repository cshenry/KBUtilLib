# function_to_go council pipeline — the standing, quoted, approved run

Chris, 2026-10-22:

> an automated process in place where functions that lack council runs are
> automatically placed in a job folder in dropbox, then I can kick off a council
> run to get a quote on cost and then approve and submit. Then the output should
> go in a standard place where an automated script on kbhub picks up and loads it.

Five stages. Today, all five are carried by about eight throwaway scripts on
`/scratch/chenry/council/`, a filesystem this machine treats as ephemeral and
shares with other users. One of them — `export_results.py` — is the only record
of how 19,268 verdicts became a loadable file, and it joins on text because the
hash it should have carried was discarded upstream.

**The finding that sets the shape of this PRD: four of the five stages already
exist in KBUtilLib, and the 2026-10-20 run bypassed all of them.**

- Stage 1 (what is un-run) is a read against `function_entity` minus
  `function_result` — and the clearinghouse read path works from h100 today,
  measured, over the BERDL delta API. It does not need kbhub.
- Stage 4 (the run) is `OntologyMappingUtils.function_to_go_council`, which
  already fans deduplicated texts into the council and already carries
  `query_id` — the clearinghouse entity hash — through to every output row. The
  hand-rolled run threw that column away and then spent a session recovering it
  by joining on definition text.
- Stage 5 (the load) is `kbu clearinghouse plan` → `shard` → `load` → `verify`:
  a declarative TOML manifest, bronze parquet shards, a `run.json` carrying a
  sha256 and a row count and a hash range per batch, a five-check integrity gate
  before any byte is used, fill-only idempotent appends, a resumable ledger, and
  a `--dry-run` the code itself calls "the operator's last gate". Sharding runs
  anywhere; only the load is in-pod. **Nobody needs to write a kbhub loader.**

So this PRD is mostly wiring, plus two things that genuinely do not exist:

**The quote.** Nothing in the system can say what a run will cost before it runs.
It can now be made almost exact rather than projected, because `prompt_hash` is a
pure function — every proposer and critic prompt can be rendered locally, counted,
and checked against the store for an existing paid answer, with zero API calls.
Only the meta seat has to be projected, because its prompt does not exist until
the chairs have voted. The quote must say which lines are exact and which are
projected, and it must quote in **tokens**, because neither ARGO nor MAG publishes
a rate card and a dollar figure derived from vendor list prices is the precise
error that let a $350/month cap look affordable until the week it was exhausted.

**The approval gate.** An approval has to be bound to the thing approved — a hash
of the quote, which contains a hash of the plan — or it is a sticky note. It also
has to carry a **token ceiling** the runner halts at, and the store already has
the machinery for that (`endpoint_state.epoch_tokens`, `budget_epoch`,
`halt_kind`).

## One hard prerequisite, and it is worse than it looks

`0825c5a` — "readiness must accept every id a gateway advertises" — exists in no
shared branch. Not on `main`, and the object is not even present in the Dropbox
repo. It lives only in this machine's editable install, which is why the
2026-10-20 run worked here and would fail anywhere else.

Without it, `servable.py` builds its served-model set from the gateway's display
spelling while `/chat/completions` accepts only `internal_id`, so the readiness
test is the exact inverse of the call: **every ARGO seat is refused at submit, and
`kbu council doctor` reports the whole ARGO bench unavailable.** Three of the five
seats in the measured roster are ARGO.

The council on `main` cannot run. That is task one.

## What this is not

Not a redesign of the council's composition or its candidate menu. Dropping the
third chair (~22% of tokens) and letting a 2-of-3 majority settle without a
critic (~19%) are live cost levers with real numbers behind them, and trimming
the menu is now a joint yield/cost question because 25.4% of items declined for
`no_candidate_matched` — the right term was never on the menu. Those belong to
continuation [145]. This PRD makes the cost of each such choice **visible before
it is paid**, which is the thing that was missing when those levers were ranked.
