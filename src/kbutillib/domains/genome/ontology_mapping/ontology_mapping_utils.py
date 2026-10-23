"""OntologyMapping: function description -> GO, as a fixed KBUtilLib API.

Two capabilities, meant to be run in sequence:

* :meth:`OntologyMappingUtils.function_to_go` -- a RANKED CANDIDATE LIST of GO
  molecular-function terms per description. Today's backend is the retrieval
  stage of the 2026-09-10 GO council (see :mod:`.go_text`); it abstains on
  placeholder functions (see :mod:`.placeholder`).
* :meth:`OntologyMappingUtils.function_to_go_council` -- runs KBUtilLib's AI
  council (query ``go_mf@1``) over those ranked lists to pick the final GO
  term(s) per description.

The API is the contract; the backend underneath is expected to change (e.g.
to Vibhav's Type 4 fusion once it can score new text). Every result therefore
carries ``mapping_version`` and ``mapping_hash``: a backend swap changes them,
and anything that caches or hashes results (the function clearinghouse) must
fold them into its own key.

On top of those two capabilities sit the STAGES of the standing, quoted,
operator-approved ``function_to_go`` pipeline (``kbu ontomap``), which live
here because they are those same two capabilities plus bookkeeping:

* :meth:`OntologyMappingUtils.council_plan` -- stage 1. Derives the un-run
  set from the clearinghouse, ranks it, partitions it three ways, chunks the
  submit side and writes ``PLAN.json``. The partition arithmetic itself is
  pure and lives in :mod:`.unrun`.
* :meth:`OntologyMappingUtils.council_export` -- stage 5's first half. Joins
  the plan's abstain and fanout records to the council store's verdicts and
  writes PARQUET, one row per input ``query_id``, on local scratch. The row
  derivation itself is pure and lives in :mod:`.verdicts`, for the same
  reason :mod:`.unrun` is separate.

Stage 2 (the quote) is :func:`kbutillib.domains.ai.council.quote.quote_plan`,
which READS the ``PLAN.json`` this module writes -- so the plan's contract is
that module's :func:`~kbutillib.domains.ai.council.quote.load_plan`, and
``plan_hash`` is its :attr:`~kbutillib.domains.ai.council.quote.Plan.plan_hash`
rather than a second hashing rule that could disagree with it.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from kbutillib.core.capability import capability
from kbutillib.core.shared_env_utils import SharedEnvUtils

from . import go_text
from .placeholder import PLACEHOLDER_RULES_VERSION, placeholder_reason

# Re-exported so `unrun_set` is reachable beside the stage that uses it,
# while the pure derivation itself stays in a module that imports no numpy
# (see `unrun`'s docstring): a partition test living here would be skipped
# on every host without the retrieval stack.
from .unrun import (  # noqa: F401 - public re-export
    UNRUN_PARTITIONS,
    unrun_items,
    unrun_set,
)

#: The backend name. Bump on any change to retrieval behaviour.
BACKEND = "ontomap-go-text-union"
BACKEND_VERSION = "1.0.0"
DEFAULT_COUNCIL_QUERY = "go_mf@1"

#: The clearinghouse ``source`` a council run publishes under:
#: ``"council/<query_name>@<version>"``. It is the partition value of
#: ``function_result``, and it is also what stage 1 filters ``current_state``
#: by -- so the two must be spelled in exactly one place.
COUNCIL_SOURCE_PREFIX = "council/"

#: Submit items per chunk, written INTO ``PLAN.json`` as the run's own
#: setting rather than read from here by the run stage.
#:
#: THIS IS A WORKAROUND FOR KBUtilLib ISSUE 84, NOT A DESIGN. The council
#: store's ``claim_calls`` is synchronous on the worker event loop and its
#: cost is linear in the pending set: measured 217 ms at 19k pending rows
#: against 10.4 ms at 2k. That inflated service times 6-9x on BOTH gateways
#: at once -- with queue wait at 0.00, which is how it was identified -- and
#: collapsed throughput 10.8x. Chunking to ~2,000 items with ONE chunk in
#: flight keeps the pending set small enough that the claim stays cheap.
#:
#: It is a DEFAULT here and a VALUE in the plan precisely because it must
#: change the day issue 84 is fixed: a workaround baked in as a code constant
#: becomes permanent silently, with nothing recording why it was ever there.
PLAN_CHUNK_SIZE = 2000

#: The file STAGE 4 appends per chunk inside the run directory. Stage 5 reads
#: it for one thing only -- which council jobs the run used -- so that
#: ``council_export`` can be pointed at a run directory rather than told the
#: job ids by hand. Stage 4 owns everything else about its contract.
RUN_FILENAME = "RUN.json"

#: Chunks the run stage may have outstanding at once. One, for the same
#: reason: the daemon drains every claimable job, so two chunks in flight is
#: one large pending set again (see :data:`PLAN_CHUNK_SIZE`).
PLAN_CHUNKS_IN_FLIGHT = 1

FunctionsArg = Union[Sequence[str], Sequence[Dict[str, Any]], Dict[str, str]]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalise_functions(functions: FunctionsArg) -> List[Dict[str, str]]:
    """Accept ``["text", ...]``, ``{id: text}`` or ``[{"id"/"query_id", "text", "source"?}]``."""
    if isinstance(functions, str):
        functions = [functions]
    if isinstance(functions, dict):
        return [{"query_id": str(k), "text": str(v), "source": ""} for k, v in functions.items()]
    out: List[Dict[str, str]] = []
    for i, f in enumerate(functions):
        if isinstance(f, str):
            out.append({"query_id": f"Q{i + 1}", "text": f, "source": ""})
        else:
            qid = f.get("query_id", f.get("id", f"Q{i + 1}"))
            out.append({"query_id": str(qid), "text": str(f["text"]), "source": str(f.get("source") or "")})
    ids = [f["query_id"] for f in out]
    if len(set(ids)) != len(ids):
        raise ValueError("function_to_go: query ids must be unique")
    return out


class OntologyMappingUtils(SharedEnvUtils):
    """Function-description -> GO mapping (ranked candidates, then council)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._version: Optional[Dict[str, Any]] = None

    # -- provenance --------------------------------------------------------

    def mapping_version(self) -> Dict[str, Any]:
        """The identity of the retrieval backend: what produced a ranked list."""
        if self._version is None:
            models = go_text.load_union_models()
            meta = go_text.load_metadata()
            parts = {
                "backend": BACKEND,
                "backend_version": BACKEND_VERSION,
                "models": [
                    {"artifact": m.artifact_version,
                     "ontomap_method_version": go_text.METHOD_VERSIONS[m.artifact_version],
                     "sha256": m.sha256}
                    for m in models
                ],
                "go_release": meta.obo_version,
                "go_metadata_sha256": _sha256_file(go_text.GO_METADATA_PATH),
                "placeholder_rules": PLACEHOLDER_RULES_VERSION,
                "params": {"export_top_n": go_text.EXPORT_TOP_N, "per_model_top_k": go_text.PER_MODEL_TOP_K,
                           "max_height": go_text.MAX_HEIGHT, "union_max_candidates": go_text.UNION_MAX_CANDIDATES},
            }
            digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
            versions = "+".join(go_text.METHOD_VERSIONS[m.artifact_version] for m in models)
            self._version = {
                "mapping_version": f"{BACKEND}@{BACKEND_VERSION} (go-text {versions}; GO {meta.obo_version})",
                "mapping_hash": digest,
                "provenance": parts,
            }
        return self._version

    # -- stage 1: ranked candidates -----------------------------------------

    @capability(
        domain="ontology_mapping",
        summary="Rank candidate GO molecular-function terms for function descriptions.",
        tags=("ontology", "go", "annotation"),
        visibility="public",
    )
    def function_to_go(
        self,
        functions: FunctionsArg,
        max_candidates: int = go_text.UNION_MAX_CANDIDATES,
    ) -> Dict[str, Any]:
        """Return a ranked GO candidate list for each function description.

        Args:
            functions: descriptions, as a list of strings, a ``{query_id: text}``
                mapping, or a list of ``{"query_id"|"id", "text", "source"?}``.
            max_candidates: cap on each merged list (the council used 40).

        Returns:
            ``{"mapping_version", "mapping_hash", "provenance", "results": [...]}``
            with one result per input, in input order::

                {"query_id", "text", "source",
                 "abstained": bool, "abstain_reason": str | None,
                 "candidates": [{"go_id", "name", "def", "score", "height", "rank"}]}

            Placeholder functions abstain with an empty candidate list. Scores
            are ranking evidence from two models and are NOT comparable across
            candidates from different models -- use ``rank``.
        """
        items = _normalise_functions(functions)
        version = self.mapping_version()
        models = go_text.load_union_models()
        meta = go_text.load_metadata()

        reasons = {f["query_id"]: placeholder_reason(f["text"]) for f in items}
        texts = sorted({f["text"] for f in items if reasons[f["query_id"]] is None})
        per_model = [dict(zip(texts, m.rank(texts))) if texts else {} for m in models]

        results = []
        for f in items:
            reason = reasons[f["query_id"]]
            cands: List[Dict[str, Any]] = []
            if reason is None:
                lists = [go_text.council_cut(ranked[f["text"]], meta) for ranked in per_model]
                cands = go_text.interleave(lists, max_candidates)
                if not cands:
                    reason = "no candidate survived the GO height filter"
            results.append({**f, "abstained": reason is not None, "abstain_reason": reason, "candidates": cands})
        return {**version, "results": results}

    # -- stage 2: council vote ----------------------------------------------

    @capability(
        domain="ontology_mapping",
        summary="Run the AI council (go_mf@1) over function_to_go's ranked GO candidates.",
        tags=("ontology", "go", "annotation", "council", "llm"),
        visibility="public",
    )
    def function_to_go_council(
        self,
        ranked: Optional[Dict[str, Any]] = None,
        functions: Optional[FunctionsArg] = None,
        profile: Optional[str] = None,
        query: str = DEFAULT_COUNCIL_QUERY,
        allow_unverified: bool = False,
        label: Optional[str] = None,
        job_id: Optional[str] = None,
        **seat_overrides: Any,
    ) -> Dict[str, Any]:
        """Have the AI council choose GO term(s) from each ranked candidate list.

        Pass the output of :meth:`function_to_go` as ``ranked`` (or pass
        ``functions`` and it is computed). Abstained descriptions are not sent
        to the council. Identical texts are sent once and fanned back out.
        To resume an interrupted run, pass the ``job_id`` it returned.

        Spends LLM tokens. The council store, roster and credentials come from
        the ``ai_council`` config section (``kbu council doctor`` checks them).

        Returns:
            The ranked result's ``mapping_version``/``mapping_hash`` plus
            ``council`` (job id, query, profile, final job status) and, per
            input, ``selected_go_ids``, ``final_confidence``, ``decline_reason``,
            ``decided_by`` and the raw council ``verdict`` row.
        """
        if ranked is None:
            if functions is None:
                raise ValueError("function_to_go_council needs ranked= or functions=")
            ranked = self.function_to_go(functions)
        council = self._council()

        by_text: Dict[str, Dict[str, Any]] = {}
        for r in ranked["results"]:
            if not r["abstained"] and r["candidates"] and r["text"] not in by_text:
                by_text[r["text"]] = r
        item_of_text = {t: f"t{i}" for i, t in enumerate(sorted(by_text))}
        tasks = [
            {
                "item_id": item_of_text[t],
                "evidence": {"text": t, "source": r.get("source") or ""},
                "menu": [
                    {"id": c["go_id"], "label": c["name"], "description": c.get("def") or "",
                     "rank": c["rank"], "score": c["score"]}
                    for c in r["candidates"]
                ],
            }
            for t, r in sorted(by_text.items())
        ]

        status: Dict[str, Any] = {}
        verdicts: Dict[str, Dict[str, Any]] = {}
        if tasks:
            if job_id is None:
                job_id = council.submit(query, tasks, label=label or "function_to_go_council",
                                        profile=profile, allow_unverified=allow_unverified, **seat_overrides)
            status = council.run(job_id)
            for row in council.results(job_id):
                verdicts[str(row.get("item_id"))] = row

        # A council item whose calls all failed settles with status "error" and an
        # EMPTY verdict, while the job still reports "done" with no blockers. An
        # empty pick must never be mistaken for "the council chose nothing", so an
        # errored item is labelled council_error and counted, never left blank.
        results = []
        errored = 0
        for r in ranked["results"]:
            out = {k: r[k] for k in ("query_id", "text", "source", "abstained", "abstain_reason")}
            row = verdicts.get(item_of_text.get(r["text"], ""))
            verdict = _verdict_of(row)
            item_status = (row or {}).get("status")
            if row is None:
                decline = "abstained" if r["abstained"] else ("no_candidates" if not r["candidates"] else "not_decided")
            elif item_status not in (None, "ok"):
                decline = f"council_error:{item_status}"
                errored += 1
                verdict = {}
            else:
                decline = verdict.get("decline_reason")
            out.update({
                "selected_go_ids": list(verdict.get("selected_ids") or []),
                "final_confidence": verdict.get("final_confidence"),
                "decline_reason": decline,
                "decided_by": verdict.get("decided_by"),
                "council_item_status": item_status,
                "names": {g: go_text.load_metadata().name.get(g) for g in (verdict.get("selected_ids") or [])},
                "verdict": row,
            })
            results.append(out)
        job = {k: v for k, v in status.items() if k != "worker"}
        by_status = dict(job.get("by_status") or {})
        warnings = []
        if errored or by_status.get("error"):
            warnings.append(
                f"{max(errored, by_status.get('error', 0))} council item(s) ERRORED (no verdict); "
                "they are marked decline_reason=council_error:* -- check `kbu council jobs status "
                f"{job_id}` and resume with job_id= once the seat is fixed"
            )
        return {
            "mapping_version": ranked["mapping_version"],
            "mapping_hash": ranked["mapping_hash"],
            "council": {"job_id": job_id, "query": query, "profile": profile,
                        "status": job, "worker": status.get("worker"),
                        "errored_items": errored, "warnings": warnings},
            "results": results,
        }

    # -- hand-off files ------------------------------------------------------
    #
    # Stage 1 and stage 2 may run on different machines (kbhub ranks, poplar
    # runs the council). A ranked result is written COMPACTLY -- ids, scores,
    # ranks; no names or definitions, which are 95% of its bytes and are a pure
    # function of the vendored GO table -- and rehydrated on read. The header
    # carries mapping_hash, and a reader whose installed backend differs
    # refuses the file rather than silently mixing two mapping versions.

    def write_ranked(self, ranked: Dict[str, Any], path: Union[str, Path]) -> Dict[str, Any]:
        """Write :meth:`function_to_go` output as gzipped JSONL (header line first)."""
        import gzip

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        n = 0
        with gzip.open(tmp, "wt") as fh:
            header = {k: ranked[k] for k in ("mapping_version", "mapping_hash", "provenance")}
            fh.write(json.dumps({"header": header}) + "\n")
            for r in ranked["results"]:
                fh.write(json.dumps({
                    "q": r["query_id"], "t": r["text"], "s": r.get("source") or "",
                    "a": r["abstain_reason"] if r["abstained"] else None,
                    "c": [[c["go_id"], c["score"]] for c in r["candidates"]],
                }) + "\n")
                n += 1
        tmp.replace(path)  # atomic: a reader never sees a half-written file
        return {"path": str(path), "rows": n, "mapping_hash": ranked["mapping_hash"]}

    def read_ranked(self, path: Union[str, Path], allow_version_mismatch: bool = False) -> Dict[str, Any]:
        """Read a :meth:`write_ranked` file back into :meth:`function_to_go`'s shape."""
        import gzip

        meta = go_text.load_metadata()
        with gzip.open(Path(path), "rt") as fh:
            header = json.loads(fh.readline())["header"]
            mine = self.mapping_version()["mapping_hash"]
            if header["mapping_hash"] != mine and not allow_version_mismatch:
                raise ValueError(
                    f"{path} was ranked by mapping_hash {header['mapping_hash'][:12]} "
                    f"({header['mapping_version']}); this install is {mine[:12]}. "
                    "Re-rank, or deploy the same KBUtilLib on both machines."
                )
            results = []
            for line in fh:
                row = json.loads(line)
                cands = [{"go_id": g, "name": meta.name.get(g, ""), "def": meta.definition.get(g, ""),
                          "score": s, "height": meta.height.get(g), "rank": i}
                         for i, (g, s) in enumerate(row["c"], 1)]
                results.append({"query_id": row["q"], "text": row["t"], "source": row["s"],
                                "abstained": row["a"] is not None, "abstain_reason": row["a"],
                                "candidates": cands})
        return {**header, "results": results}

    def write_council(self, council_out: Dict[str, Any], path: Union[str, Path]) -> Dict[str, Any]:
        """Write :meth:`function_to_go_council` output as gzipped JSONL, one row per input."""
        import gzip

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        with gzip.open(tmp, "wt") as fh:
            header = {k: council_out[k] for k in ("mapping_version", "mapping_hash")}
            header["council"] = council_out["council"]
            fh.write(json.dumps({"header": header}, default=str) + "\n")
            for r in council_out["results"]:
                fh.write(json.dumps({k: v for k, v in r.items() if k != "verdict"}, default=str) + "\n")
        tmp.replace(path)
        return {"path": str(path), "rows": len(council_out["results"])}

    # -- pipeline stage 1: the run plan --------------------------------------

    def council_plan(
        self,
        run_dir: Union[str, Path],
        *,
        query: str,
        parameter_set: Optional[Dict[str, Any]] = None,
        chunk_size: int = PLAN_CHUNK_SIZE,
        profile: Optional[str] = None,
        scratch_dir: Optional[Union[str, Path]] = None,
        concurrency: Optional[Dict[str, Any]] = None,
        max_candidates: int = go_text.UNION_MAX_CANDIDATES,
    ) -> Dict[str, Any]:
        """Stage 1: derive the un-run set, rank it, chunk it, write ``PLAN.json``.

        Spends NO LLM tokens. It reads the clearinghouse, runs the retrieval
        stage locally and writes files; the first token is spent in stage 4,
        behind a quote and an approval.

        THE STEPS, each one a reuse of something that already exists:

        1. **Universe** -- :meth:`ClearinghouseCapability.universe`
           (``function_entity`` joined to ``function_content``). This works
           from h100 OFF-POD over the BERDL delta API, measured 2026-10-22, so
           it is deliberately NOT gated on the locus: only ``load`` and
           ``verify`` are in-pod verbs.
        2. **Done** -- :meth:`ClearinghouseCapability.current_state` for
           ``function``, filtered to this run's ``source`` AND its
           ``parameter_set_hash``. A result at a DIFFERENT parameter set is a
           different experiment and leaves the function un-run.
        3. **Un-run** = universe - done.
        4. **Rank** -- :meth:`function_to_go` over the un-run set,
           :meth:`write_ranked` to a LOCAL scratch path.
        5. **Partition** -- :func:`~.unrun.unrun_set`, three ways, all three
           recorded (the abstained set is never dropped).
        6. **Chunk** -- the submit set into ordered chunks of ``chunk_size``.
        7. **Write** ``PLAN.json`` into ``run_dir``.

        WHAT CROSSES DROPBOX AND WHAT DOES NOT. ``run_dir`` is a synced
        directory, and ``PLAN.json`` -- small, operator-readable -- is the only
        thing this stage puts there. The ranked file, the plan's items file and
        the abstain/fanout records go to ``scratch_dir``: the items of a
        19k-item plan are ~130 MB of evidence and menus (6.9 kB each,
        measured), nothing off-machine reads them, and every byte on the sync
        surface needs an integrity gate. ``PLAN.json`` names them by ABSOLUTE
        path, which is the ``items_file`` spelling
        :func:`~kbutillib.domains.ai.council.quote.load_plan` supports for
        exactly this reason.

        THE PARAMETER SET IS ``{query, bench_fingerprint, mapping_hash}`` and
        is hashed by the CLEARINGHOUSE's own
        :func:`~kbutillib.domains.kbase.berdl.clearinghouse_parameters.result_parameter_set_identity`,
        never by a hashing rule of this module's own. ``bench_fingerprint``
        and NOT ``roster_fingerprint``: the bench partitions the corpus, the
        transport does not. ``Roster.fingerprint()`` covers the endpoint kind,
        the route spelling, the token ceiling, the temperature and the extras,
        so moving one seat from the codex CLI to MAG moves it -- which would
        re-partition all 47,341 clearinghouse functions and re-charge ~199M
        tokens for a pure cost optimisation. ``roster_fingerprint`` and the
        per-seat ``route_fingerprint`` are still RECORDED in the plan, as
        provenance; they are simply not keys. See
        :func:`~kbutillib.domains.ai.council.types.bench_fingerprint`.

        Args:
            run_dir: Where ``PLAN.json`` is written (created if absent).
            query: ``"<name>@<version>"``. The version is REQUIRED: it is part
                of the parameter set and of every prompt hash, so a plan that
                left it to be guessed could be quoted against one spec and run
                against another.
            parameter_set: Override the resolved parameter set. For tests and
                for re-deriving a historical partition; the default is the one
                the pipeline means.
            chunk_size: Submit items per chunk. See :data:`PLAN_CHUNK_SIZE` --
                it is a workaround for KBUtilLib issue 84 and the plan records
                why.
            profile: The bench to plan against. Default: the configured one.
            scratch_dir: Local ROOT for the ranked file, the items file and
                the abstain/fanout records; ``function-to-go/<run-id>/`` is
                appended. Default: ``$KBU_SCRATCH_DIR``, else
                ``/scratch/<user>``, else the system temporary directory. Must
                not be inside ``run_dir``.
            concurrency: Override the concurrency block written into the plan.
            max_candidates: Cap on each ranked candidate list.

        Returns:
            The plan, exactly as written to ``PLAN.json``, plus
            ``plan_file`` / ``counts`` / the three partition records' paths.

        Raises:
            ValueError: ``query`` names no version, ``chunk_size`` is below 1,
                the scratch path is inside ``run_dir``, or the un-run set and
                the ranking disagree (see :func:`~.unrun.unrun_set`).
        """
        from kbutillib.domains.ai.council.quote import (
            PLAN_FILENAME,
            PLAN_SCHEMA_VERSION,
            Plan,
            PlanItem,
        )
        from kbutillib.domains.ai.council.types import (
            bench_fingerprint,
            route_fingerprint,
            seat_generation_params,
        )
        from kbutillib.domains.kbase.berdl.clearinghouse_parameters import (
            result_parameter_set_identity,
        )

        if int(chunk_size) < 1:
            raise ValueError(
                f"council_plan: chunk_size={chunk_size}; a chunk must hold at "
                "least one item"
            )
        chunk_size = int(chunk_size)
        name, version = _split_query_ref(query)
        query_ref = f"{name}@{version}"
        source = f"{COUNCIL_SOURCE_PREFIX}{query_ref}"

        run_dir = Path(run_dir).expanduser()
        scratch = _scratch_dir(run_dir, scratch_dir)

        # -- identity: the bench partitions, the transport is provenance ----
        council = self._council()
        config = council.config()
        roster = council.roster(profile)
        resolved_profile = str(profile or getattr(config, "default_profile", "") or "") or None
        transport = [
            {
                "role": role,
                "seat_name": seat.seat_name,
                "endpoint": seat.endpoint,
                "endpoint_kind": seat.endpoint_kind,
                "gateway": _gateway_of(config, seat),
                "model": seat.model,
                "canonical_model": seat.canonical_model,
                "route_fingerprint": route_fingerprint(
                    seat, config.endpoint(seat.endpoint), seat_generation_params(seat)
                ),
            }
            for role, seat in roster.seats()
        ]

        version_report = self.mapping_version()
        mapping_hash = str(version_report["mapping_hash"])
        bench = bench_fingerprint(roster)
        if parameter_set is None:
            parameter_set = {
                "query": query_ref,
                "bench_fingerprint": bench,
                "mapping_hash": mapping_hash,
            }
        else:
            parameter_set = dict(parameter_set)
        canonical_json, parameter_set_hash = result_parameter_set_identity(
            parameter_set, source=source, where="council_plan parameter_set"
        )

        # -- the two clearinghouse reads, thin, around a pure derivation ----
        clearinghouse = self._clearinghouse()
        universe = list(clearinghouse.universe("function"))
        done = list(
            clearinghouse.current_state(
                "function",
                sources=[source],
                parameter_set_hashes=[parameter_set_hash],
            )
        )

        items = unrun_items(universe, done)
        ranked = self.function_to_go(items, max_candidates=max_candidates)
        ranked_written = self.write_ranked(ranked, scratch / "ranked.jsonl.gz")
        parts = unrun_set(universe, done, ranked)

        # -- the plan ---------------------------------------------------------
        plan_items = tuple(
            PlanItem(
                item_id=row["item_id"],
                evidence={"text": row["text"], "source": row["source"]},
                menu=_menu_of(row["candidates"]),
                query_id=row["query_id"],
            )
            for row in parts["submit"]
        )
        plan_object = Plan(
            query_name=name,
            query_version=version,
            items=plan_items,
            mapping_hash=mapping_hash,
            profile=resolved_profile,
        )

        items_file = scratch / "items.jsonl"
        _write_jsonl(
            items_file,
            (
                {
                    "item_id": item.item_id,
                    "query_id": item.query_id,
                    "evidence": dict(item.evidence),
                    "menu": [dict(entry) for entry in item.menu],
                }
                for item in plan_items
            ),
        )
        abstain_file = scratch / "abstain.jsonl"
        _write_jsonl(abstain_file, parts["abstain"])
        fanout_file = scratch / "fanout.jsonl"
        _write_jsonl(fanout_file, parts["fanout"])

        chunks = [
            {
                "index": index,
                "offset": offset,
                "items": len(plan_items[offset : offset + chunk_size]),
                "first_item_id": plan_items[offset].item_id,
                "last_item_id": plan_items[
                    min(offset + chunk_size, len(plan_items)) - 1
                ].item_id,
            }
            for index, offset in enumerate(range(0, len(plan_items), chunk_size))
        ]

        plan: Dict[str, Any] = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "run_dir": str(run_dir),
            "run_id": run_dir.name,
            "query": query_ref,
            "source": source,
            "profile": resolved_profile,
            "mapping_version": version_report["mapping_version"],
            "mapping_hash": mapping_hash,
            # The one in the PARAMETER SET, which is the one that partitions
            # the corpus. It is the roster's own unless the caller overrode
            # the parameter set to re-derive a historical partition; the
            # seated bench is recorded either way, in transport_allocation.
            "bench_fingerprint": str(parameter_set.get("bench_fingerprint") or bench),
            "parameter_set": dict(parameter_set),
            "parameter_set_hash": parameter_set_hash,
            "parameter_set_canonical_json": canonical_json,
            # PROVENANCE, NOT KEYS. roster_fingerprint and route_fingerprint
            # answer "was this configured identically" and "where did this
            # come from"; bench_fingerprint answers "is this the same
            # experiment", and only that one partitions the corpus.
            "roster_fingerprint": roster.fingerprint(),
            # EXPECTED TO DIFFER BETWEEN RUNS. Which gateway each seat is
            # routed over is the thing we iterate on to allocate token cost
            # (Chris, 2026-10-22), so it is written down per run rather than
            # inferred from whatever the config happened to say later.
            "transport_allocation": transport,
            "counts": {
                "universe": len(universe),
                "done": len(done),
                "unrun": len(items),
                "submit": len(parts["submit"]),
                "abstain": len(parts["abstain"]),
                "fanout": len(parts["fanout"]),
            },
            "chunks": chunks,
            "concurrency": (
                dict(concurrency)
                if concurrency is not None
                else _plan_concurrency(chunk_size, config)
            ),
            "items_file": str(items_file),
            "abstain_file": str(abstain_file),
            "fanout_file": str(fanout_file),
            "ranked_file": str(ranked_written["path"]),
            "plan_hash": plan_object.plan_hash,
        }

        run_dir.mkdir(parents=True, exist_ok=True)
        plan_file = run_dir / PLAN_FILENAME
        tmp = plan_file.with_name(plan_file.name + ".partial")
        tmp.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        tmp.replace(plan_file)  # atomic: a reader never sees a half-written plan
        return {**plan, "plan_file": str(plan_file)}

    # -- pipeline stage 5: the verdict export --------------------------------

    def council_export(
        self,
        run_dir: Union[str, Path],
        out_path: Optional[Union[str, Path]] = None,
        *,
        job_ids: Optional[Sequence[str]] = None,
        retry_job_ids: Optional[Sequence[str]] = None,
        genomes: Optional[Dict[str, Sequence[str]]] = None,
        scratch_dir: Optional[Union[str, Path]] = None,
    ) -> Dict[str, Any]:
        """Stage 5: write the run's verdicts as parquet, one row per input.

        Spends NO LLM tokens: it reads ``PLAN.json``, the plan's abstain and
        fanout records and the council store, and writes one file.

        ONE ROW PER INPUT ``query_id`` -- the whole un-run set the plan
        recorded, which is ``abstain`` plus ``fanout`` (a text shared by three
        ``query_id``s is answered once and published three times). All three
        decline classes are written with ``go_ids = []``; the only thing
        withheld is an item with NO VERDICT, which is still un-run. The row
        derivation and the reasons for each cell live in :mod:`.verdicts`,
        which is pure and testable without the retrieval stack.

        THE JOIN KEY IS ``query_id``, taken from the plan's fanout and abstain
        records -- which carry it through from the ranked file, because
        :func:`~.unrun.unrun_items` reads it off the clearinghouse universe
        row and never discards it. It is NOT resolved through a text join and
        NOT the council's own ``items.entity_hash``: measured 2026-10-21, that
        hash matched 0 of 19,268 clearinghouse hashes while the definition
        text matched 19,268 of 19,268, and the fix is to stop discarding the
        key rather than to join on the text.

        Args:
            run_dir: The run directory holding ``PLAN.json``.
            out_path: Where the parquet goes. Default:
                ``<scratch>/verdicts.parquet``. A path inside ``run_dir`` is
                REFUSED -- the published artifact is the shard run, and the
                verdict parquet is its input, not a sync artifact.
            job_ids: The council jobs to read verdicts from, in order; a later
                job's verdict for an ``item_id`` supersedes an earlier one's.
                Default: whatever ``RUN.json`` names (see
                :func:`_run_job_ids`).
            retry_job_ids: Which of those belong to the retry pass. A row
                whose verdict came from one is marked ``retried``. Default:
                ``RUN.json``'s.
            genomes: Optional ``query_id -> [genome_id, ...]``. See
                :func:`~.verdicts.export_rows` -- nothing in-pipeline can fill
                this today, and the column is an empty list without it.
            scratch_dir: Local ROOT for the default ``out_path``, resolved the
                same way :meth:`council_plan` resolves it.

        Returns:
            The export record: ``out_path``, ``rows``, ``bytes``, ``counts``,
            the run identity, the job ids read, and ``warnings`` -- non-empty
            whenever anything was withheld.

        Raises:
            FileNotFoundError: ``run_dir`` holds no ``PLAN.json``, or the plan
                names an abstain/fanout file that is gone (``/scratch`` is
                ephemeral on this host and is not backed up).
            ValueError: No job ids were given and ``RUN.json`` names none,
                ``out_path`` is inside ``run_dir``, or the abstain/fanout
                records disagree with the counts ``PLAN.json`` recorded.
        """
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        from .verdicts import export_rows, verdicts_by_item, write_verdict_parquet

        run_dir = Path(run_dir).expanduser()
        plan_file = run_dir / PLAN_FILENAME
        if not plan_file.exists():
            raise FileNotFoundError(
                f"no plan at {plan_file}. Stage 1 writes it: "
                "`kbu ontomap council-plan`"
            )
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        name, version = _split_query_ref(str(plan.get("query") or ""))

        abstain = _read_jsonl(_plan_path(plan, "abstain_file", run_dir))
        fanout = _read_jsonl(_plan_path(plan, "fanout_file", run_dir))
        _check_plan_counts(plan, abstain=len(abstain), fanout=len(fanout))

        declared = _run_job_ids(run_dir)
        resolved_jobs = (
            [str(jid) for jid in job_ids] if job_ids is not None else declared["job_ids"]
        )
        resolved_retries = (
            [str(jid) for jid in retry_job_ids]
            if retry_job_ids is not None
            else declared["retry_job_ids"]
        )
        if not resolved_jobs:
            raise ValueError(
                f"council_export: no council job ids. {run_dir / RUN_FILENAME} "
                "names none (stage 4 writes it), so pass job_ids= explicitly. "
                "Exporting with no job would write every item as withheld and "
                "publish nothing, which reads as a completed export."
            )
        # Retry jobs LAST, so their verdicts supersede the chunk jobs' --
        # measured: 105 of the 113 items that failed on a silently truncated
        # ARGO response came back on a plain retry, and the retry's answer is
        # the run's answer for those items.
        retries = set(resolved_retries)
        ordered = [jid for jid in resolved_jobs if jid not in retries]
        ordered.extend(jid for jid in resolved_retries if jid not in set(ordered))

        council = self._council()
        settled: Dict[str, Dict[str, Any]] = {}
        for job in ordered:
            verdicts_by_item(council.results(job), job_id=job, into=settled)

        if out_path is None:
            out_path = _scratch_dir(run_dir, scratch_dir) / "verdicts.parquet"
        out_path = Path(out_path).expanduser()
        _refuse_inside_run_dir(out_path, run_dir)

        built = export_rows(
            abstain=abstain,
            fanout=fanout,
            verdicts=settled,
            identity={
                "query_name": name,
                "query_version": version,
                "roster_fingerprint": str(plan.get("roster_fingerprint") or ""),
                "mapping_hash": str(plan.get("mapping_hash") or ""),
            },
            job_ids=ordered,
            # The time RETRIEVAL declined, which is when the plan was written.
            # The shard SKIPS a row whose observed_at is null, so an abstained
            # row needs a real timestamp or it would vanish at shard time.
            abstained_at=str(plan.get("generated_at") or "") or None,
            genomes=genomes,
            retry_job_ids=resolved_retries,
        )
        written = write_verdict_parquet(built["rows"], out_path)

        counts = dict(built["counts"])
        warnings: List[str] = []
        if counts["withheld"]:
            warnings.append(
                f"{counts['withheld']} of {counts['inputs']} input function(s) "
                "were WITHHELD: their council item has no verdict (every call "
                "failed, or it never settled). They are not results, so they "
                "are not in the export and the next `council-plan` will "
                "re-select them -- run the retry pass before publishing if "
                "that is not what you want."
            )
        return {
            "out_path": written["path"],
            "rows": written["rows"],
            "bytes": written["bytes"],
            "counts": counts,
            "plan_file": str(plan_file),
            "plan_hash": str(plan.get("plan_hash") or ""),
            "query": f"{name}@{version}",
            "query_name": name,
            "query_version": version,
            "source": str(plan.get("source") or ""),
            "parameter_set_hash": str(plan.get("parameter_set_hash") or ""),
            "mapping_hash": str(plan.get("mapping_hash") or ""),
            "roster_fingerprint": str(plan.get("roster_fingerprint") or ""),
            "job_ids": ordered,
            "retry_job_ids": resolved_retries,
            "warnings": warnings,
        }

    def _clearinghouse(self) -> Any:
        """The clearinghouse READ surface this stage derives its universe from.

        One factory, so a test can inject a fake by patching one name, and a
        lazy import, so neither ``kbu ontomap version`` nor the two retrieval
        capabilities pay for the BERDL stack.

        NOT gated on the locus. The clearinghouse reads work from h100
        off-pod over the BERDL delta API (measured 2026-10-22); only ``load``
        and ``verify`` are in-pod, and ``_require_in_pod_or_refuse`` guards
        exactly those two.
        """
        from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
            ClearinghouseCapability,
        )

        return ClearinghouseCapability()

    def _council(self) -> Any:
        from kbutillib.domains.ai.council_utils import CouncilUtils

        return CouncilUtils(config_file=False, token_file=None, kbase_token_file=None)


def _split_query_ref(query: str) -> tuple:
    """``("go_mf", "1")`` from ``"go_mf@1"``, refusing a version-less query.

    The same rule :func:`~kbutillib.domains.ai.council.quote._plan_query_ref`
    enforces when it reads the plan back, stated here so the refusal lands
    BEFORE the clearinghouse is read rather than at quote time: the query
    version is part of the parameter set and of every prompt hash, so a plan
    that left it to be guessed could be quoted against one spec and run
    against another. Used by stage 1, which gets it from the operator, and by
    stage 5, which gets it back off ``PLAN.json`` -- so the message names the
    query rather than a stage.
    """
    name, _, version = str(query or "").strip().partition("@")
    name, version = name.strip(), version.strip()
    if not name:
        raise ValueError('query is empty; expected "<name>@<version>"')
    if not version:
        raise ValueError(
            f"query {name!r} names no VERSION. A query version "
            "is part of the clearinghouse parameter set and of every prompt "
            'hash; pass "<name>@<version>".'
        )
    return name, version


def _scratch_dir(run_dir: Path, scratch_dir: Optional[Union[str, Path]]) -> Path:
    """Resolve, create and CHECK the local directory the bulk files go in.

    The ROOT is the caller's ``scratch_dir``, else ``$KBU_SCRATCH_DIR``, else
    ``/scratch/<user>`` where ``/scratch`` exists (h100's convention), else the
    system temporary directory. ``function-to-go/<run-id>/`` is appended to
    whichever it is, so two runs never share a path and a caller naming a
    scratch root does not have to know the layout under it.

    REFUSES a path inside ``run_dir``. The run directory is a Dropbox-synced
    surface and these files are 100+ MB of machine-local intermediates that
    nothing off-machine reads; a default that quietly landed them there is
    exactly the mistake worth failing on rather than discovering from a sync
    backlog.
    """
    import getpass
    import tempfile

    if scratch_dir is not None:
        root = Path(scratch_dir).expanduser()
    elif os.environ.get("KBU_SCRATCH_DIR"):
        root = Path(str(os.environ["KBU_SCRATCH_DIR"])).expanduser()
    elif Path("/scratch").is_dir():
        root = Path("/scratch") / getpass.getuser()
    else:
        root = Path(tempfile.gettempdir())
    root = root / "function-to-go" / (run_dir.name or "run")
    resolved_run_dir = run_dir.expanduser().resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    if resolved_root == resolved_run_dir or resolved_run_dir in resolved_root.parents:
        raise ValueError(
            f"council_plan: the scratch path {resolved_root} is inside the run "
            f"directory {resolved_run_dir}. The run directory is a synced "
            "surface and carries PLAN.json only; the ranked file and the items "
            "file are machine-local intermediates and must not cross it."
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _plan_path(plan: Dict[str, Any], key: str, run_dir: Path) -> Path:
    """One of the plan's named side files, absolute or relative to ``run_dir``.

    ``council_plan`` writes these keys as ABSOLUTE paths under the scratch
    root, which is the spelling
    :func:`~kbutillib.domains.ai.council.quote.load_plan` supports for the
    items file. A relative one is resolved against the run directory, the same
    rule that function applies, so a plan hand-edited to use relative paths
    reads the same here as it does there.
    """
    raw = plan.get(key)
    if not raw:
        raise ValueError(
            f"the plan declares no {key}; stage 1 writes it, and without it "
            "this stage cannot tell which functions the run was given"
        )
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = run_dir / path
    return path


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read a :func:`_write_jsonl` file back, skipping blank lines."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. Stage 1 wrote it under the scratch root, and "
            "`/scratch` on this host is shared, ephemeral and not backed up -- "
            "re-run `kbu ontomap council-plan` to regenerate the partition "
            "records rather than exporting a partition with a hole in it."
        )
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except ValueError as exc:
                raise ValueError(f"{path}:{number}: not valid JSON: {exc}") from exc
    return rows


def _check_plan_counts(plan: Dict[str, Any], *, abstain: int, fanout: int) -> None:
    """Refuse an export whose input set differs from the plan's own record.

    ``PLAN.json`` records the partition sizes it wrote, so a disagreement
    means the records and the plan are from different runs. Publishing that
    would land result rows for a partition no plan recorded, and "one row per
    input" would be a claim about an input set nobody can reconstruct.
    """
    recorded = dict(plan.get("counts") or {})
    disagreements = [
        f"{key}: PLAN.json says {recorded[key]}, the file holds {observed}"
        for key, observed in (("abstain", abstain), ("fanout", fanout))
        if key in recorded and int(recorded[key]) != observed
    ]
    if disagreements:
        raise ValueError(
            "council_export: the plan's partition records disagree with "
            f"{plan.get('run_id') or 'the plan'}'s own counts -- "
            + "; ".join(disagreements)
            + ". Re-run stage 1 rather than exporting against records from a "
            "different plan."
        )


def _run_job_ids(run_dir: Path) -> Dict[str, List[str]]:
    """The council job ids of a run, from ``RUN.json`` when there is one.

    STAGE 4 OWNS ``RUN.json``'s contract; this reads only the job ids out of
    it, and a caller passing ``job_ids=`` bypasses it entirely. Both spellings
    the PRD uses are accepted -- a top-level ``job_id``/``job_ids`` and a
    per-chunk ``job_id`` in a ``chunks`` list -- because the run appends that
    file per chunk and a run that halted mid-way still has the chunks it did.

    Returns ``{"job_ids": [...], "retry_job_ids": [...]}``, in file order with
    repeats collapsed. An absent or unreadable ``RUN.json`` yields two empty
    lists, and the caller refuses rather than exporting nothing silently.
    """
    path = run_dir / RUN_FILENAME
    if not path.exists():
        return {"job_ids": [], "retry_job_ids": []}
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(run, dict):
        raise ValueError(
            f"{path}: expected a JSON object, got {type(run).__name__}"
        )

    def _ids(value: Any) -> List[str]:
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    job_ids = _ids(run.get("job_id")) + _ids(run.get("job_ids"))
    retries = _ids(run.get("retry_job_id")) + _ids(run.get("retry_job_ids"))
    for chunk in run.get("chunks") or ():
        if not isinstance(chunk, dict):
            continue
        target = retries if chunk.get("retry") else job_ids
        target.extend(_ids(chunk.get("job_id")))
    return {"job_ids": _unique(job_ids), "retry_job_ids": _unique(retries)}


def _unique(values: Sequence[str]) -> List[str]:
    """*values* with repeats collapsed, first occurrence's order kept."""
    seen: set = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _refuse_inside_run_dir(out_path: Path, run_dir: Path) -> None:
    """Refuse an export written into the Dropbox-synced run directory.

    The run directory carries ``PLAN.json``, ``QUOTE.json``, ``APPROVED.json``
    and ``RUN.json`` -- small, operator-readable JSON -- plus the published
    ``shard/``. The verdict parquet is the shard's INPUT, not a published
    artifact, and every byte on the sync surface needs an integrity gate; the
    same refusal :func:`_scratch_dir` makes for the ranked and items files.
    """
    resolved_out = out_path.expanduser().resolve(strict=False)
    resolved_run_dir = run_dir.expanduser().resolve(strict=False)
    if resolved_run_dir == resolved_out or resolved_run_dir in resolved_out.parents:
        raise ValueError(
            f"council_export: {resolved_out} is inside the run directory "
            f"{resolved_run_dir}. The run directory is a synced surface "
            "carrying the small JSON stages and the published shard; the "
            "verdict parquet is the shard's input and belongs on local "
            "scratch. Pass out_path= under a scratch root, or omit it."
        )


def _menu_of(candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A ranked candidate list as the council's closed menu.

    The SAME projection
    :meth:`OntologyMappingUtils.function_to_go_council` builds when it calls
    the council directly, so a planned item and a directly-submitted item
    render to the same prompt -- which is what lets stage 2 count reuse
    against answers the store already holds instead of quoting a re-run.
    """
    return [
        {
            "id": c["go_id"],
            "label": c["name"],
            "description": c.get("def") or "",
            "rank": c["rank"],
            "score": c["score"],
        }
        for c in candidates
    ]


def _gateway_of(config: Any, seat: Any) -> str:
    """Which PAYER a seat's spend lands on: its account, else its alias.

    Account first because the limits and the bill are account-wide: MAG's 20
    parallel and 120 requests per minute are enforced across every endpoint on
    the account, so two aliases on one account are one gateway.
    """
    try:
        cfg = dict(config.endpoint(seat.endpoint))
    except Exception:  # noqa: BLE001 - an unknown endpoint is still plannable
        cfg = {}
    return str(cfg.get("account") or seat.endpoint)


def _plan_concurrency(chunk_size: int, config: Any) -> Dict[str, Any]:
    """The concurrency block written INTO ``PLAN.json``.

    Every number the run stage obeys lives in the plan rather than in a code
    constant here, and the KBUtilLib-issue-84 rationale travels with it. A
    workaround baked in as a constant becomes permanent silently; this one
    must change the day the issue is fixed, and when it does the reason it
    existed is recorded in the plans it shaped.

    ``declared_queues`` is the live configuration's own queue table, recorded
    so an operator can see whether the config this run will execute under
    matches the shape the plan assumes.
    """
    declared = dict(getattr(config, "queues", {}) or {})
    return {
        "chunk_size": chunk_size,
        "chunks_in_flight": PLAN_CHUNKS_IN_FLIGHT,
        "issue": "KBUtilLib issue 84",
        "rationale": (
            "WORKAROUND for KBUtilLib issue 84, not a design. The council "
            "store's claim_calls is synchronous on the worker event loop and "
            "its cost is linear in the pending set: 217 ms at 19k pending "
            "rows against 10.4 ms at 2k. That inflated service times 6-9x on "
            "BOTH gateways at once -- with queue wait at 0.00 -- and "
            "collapsed throughput 10.8x. ~2,000-item chunks with ONE chunk "
            "in flight keep the pending set small, because the daemon drains "
            "every claimable job. When issue 84 is fixed, this block changes."
        ),
        "gateways": {
            "mag": {
                "queues_sum_to": 6,
                "max_parallel_requests": 20,
                "requests_per_minute": 120,
                "scope": "account-wide",
                "note": (
                    "BOTH limits are account-wide and both bind. A config "
                    "asking 24 parallel errored 427 of 500 pilot items over "
                    "1,925 transport attempts; queues summing to 6 is the "
                    "configuration measured to work."
                ),
            },
            "argo": {
                "chair": 24,
                "critic": 8,
                "meta": 8,
                "note": (
                    "ARGO saturates at ~24 concurrent and showed zero rate "
                    "limiting at 40 concurrent across three models."
                ),
            },
        },
        "declared_queues": [
            {
                "queue": qname,
                "seats": list(getattr(queue, "seats", ()) or ()),
                "concurrency": int(getattr(queue, "concurrency", 0) or 0),
                "workers": int(getattr(queue, "workers", 1) or 1),
            }
            for qname, queue in sorted(declared.items())
        ],
    }


def _write_jsonl(path: Path, rows: Any) -> int:
    """Write *rows* as JSON lines, atomically, returning the row count.

    Streamed a row at a time and swapped into place through ``.partial``, the
    same discipline :meth:`OntologyMappingUtils.write_ranked` uses: a plan's
    items file is ~130 MB, so neither a reader nor this process wants the
    whole thing in memory or half of it on disk under the final name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    count = 0
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
            count += 1
    tmp.replace(path)
    return count


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _verdict_of(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pull the verdict dict out of a council results row, however it is nested."""
    if not row:
        return {}
    v = row.get("verdict", row)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


class OntologyMappingUtilsImpl:
    """Composition wrapper registered on :class:`kbutillib.toolkit.KBUtilLib`."""

    def __init__(self, env: Any, **kwargs: Any) -> None:
        self._env = env
        _kwargs = {"config_file": False, "token_file": None, "kbase_token_file": None}
        _kwargs.update(kwargs)
        self._delegate = OntologyMappingUtils(**_kwargs)

    @property
    def env(self) -> Any:
        return self._env

    @property
    def available(self) -> bool:
        """True when scikit-learn/joblib import and the vendored data is present."""
        return self.unavailable_reason is None

    @property
    def unavailable_reason(self) -> Optional[str]:
        try:
            import joblib  # noqa: F401
            import sklearn  # noqa: F401
        except ImportError as exc:
            return f"ontology_mapping needs scikit-learn and joblib: {exc}"
        if not go_text.GO_METADATA_PATH.exists():
            return f"GO metadata table missing at {go_text.GO_METADATA_PATH}"
        return None

    def __dir__(self) -> list:
        base = list(super().__dir__())
        base.extend(a for a in dir(self._delegate) if not a.startswith("__") and a not in base)
        return base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)
