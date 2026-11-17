"""Build ``go_mf_adjudicate@1`` items from a manifest, and run them in tranches.

This is the step that SPENDS money, so almost everything here is about not
spending it twice and not spending it blind.

THE ITEMS ARE BLIND BY CONSTRUCTION, NOT BY CONVENTION
======================================================
An adjudication item carries the annotation, its source tool, the retrieved
menu and the two rival answers rendered as bare ``GO:id | label`` lines.
Which side is ALCF and which is ``go_mf@4`` appears NOWHERE in the evidence --
not in a field, not in a label, not in the item id.  The orientation lives in
the MANIFEST (``alcf_is_A``), is applied here when the two answers are
assigned to A and B, and is read back only when a verdict is mapped to a side
(:func:`verdict_side`).  :func:`blinding_problems` is run over every built
item before anything is submitted, so a leak is a refusal rather than a
corpus nobody can use.

THE SUBMISSION TOKEN IS WHAT MAKES A RESTART SAFE
=================================================
A tranche is one council job.  The window that loses money is the one between
``submit`` returning a job id and that id reaching durable storage: crash
there and the job exists, nothing records it, and a resume pays for it again.
So each tranche gets a CONTENT-KEYED submission token -- a digest over the
run, the manifest, the tranche's index, the query version, the roster
fingerprint and every planned item's evidence and menu hashes -- which is
written to the ledger BEFORE the submit and carried in the job's label.  A
restart recomputes the same token from the same plan, finds the job in the
store by label, and adopts it.  Nothing is re-submitted, with or without the
ledger record that was being written when the power went out.

The token is computed over the tranche's PLANNED membership and never over
what was actually sent.  Items already answered are dropped at submit time
(see below), so a token keyed on the sent subset would change between the
first run and the resume -- which is precisely the case it exists to survive.

THE TOKEN CEILING IS A SOFT ADMISSION THRESHOLD, OVER ARGO SEATS ONLY
=====================================================================
``token_ceiling`` is not a cap on a tranche and not a forecast.  Before each
tranche this invocation's COMPLETED ARGO-billed usage is summed from the
store, and the tranche starts only while that total is still below the
ceiling.  One tranche may therefore overshoot; no second one can, and the
overshoot is recorded.  Nothing is estimated, so nothing can be estimated
wrong -- an earlier draft of this design metered a per-item forecast, and a
forecast that is 30% low spends 30% more than the operator authorised.

WHICH SEATS ARE METERED IS DECIDED BY ENDPOINT KIND, NEVER BY A NULL
--------------------------------------------------------------------
``argo`` seats are metered.  ``claude_cli`` and ``codex_cli`` seats are a
subscription: their usage is REPORTED in the ledger and never counted against
the ceiling, because no number of tokens on them moves the ARGO bill.  This
is read off the call row's ``endpoint_kind`` and never off whether a usage
figure is null: since the per-seat capture work landed, a successful CLI call
reports real token counts (measured on h100: codex ~17K prompt tokens, claude
20-50K), and only a FAILED CLI call records ``usage: null``.  A rule that read
"null means a CLI seat" would therefore have metered every successful CLI call
against the ARGO ceiling and stopped the run early.

Missing usage on an ARGO-billed ``ok`` call is a different matter and refuses
further submission: the ceiling cannot be enforced against a spend nobody
measured.  A reused answer (paid for by someone else) and a non-anchor member
of a batched invocation (whose cost is on the anchor's row) are exempt --
their absence of a token count is a fact about the billing, not a gap in it.

ERROR ITEMS HALT; THEY ARE NEVER "DONE"
=======================================
A tranche whose job finished with ``error`` or ``cancelled`` items is NOT
complete, however green its job status looks: a job every item of which
errored still reads ``done`` with no blockers.  Such a tranche halts the run
and names the items, for an explicit operator ``--retry-failed``.  Treating it
as complete would carry a hole in the corpus forward into the scoring.

THE SPOT-CHECK GATE
===================
Tranche N+1 does not start until tranche N's recorded spot-check ids have
imported human marks and the cumulative band is not ``not_trusted`` -- and it
does not start at all without a calibration record whose manifest, adjudicate
spec hash and roster all match.  A tuning run that outran its own calibration
is a run whose later tranches nobody can interpret.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from ..types import (
    CLI_KINDS,
    CouncilTask,
    MenuEntry,
    canonical_sha256,
    data_hash,
    entity_hash,
)
from .conflicts import MatchedItem, MatchResult, Refused, _opened
from .policy import MAPPING_POLICY
from .sample import POOLS

__all__ = [
    "ARGO_BILLED_KINDS",
    "BLINDING_FORBIDDEN",
    "CONFIDENCE_ORDER",
    "DEFAULT_SPOT_CHECK",
    "DEFAULT_TOKEN_CEILING",
    "DEFAULT_TRANCHE_SIZE",
    "ItemResult",
    "NO_TERMS",
    "QUERY_NAME",
    "QUERY_REF",
    "QUERY_VERSION",
    "SPOT_CHECK_BY_BAND",
    "UNMETERED_KINDS",
    "adjudicate_spec_hash",
    "blinding_problems",
    "build_items",
    "calibration_gate",
    "derived_verdict",
    "item_key",
    "parse_answer",
    "read_results",
    "render_answer",
    "run_tranches",
    "spot_check_ids",
    "submission_token",
    "usage_report",
    "verdict_side",
]

#: The query type these items are built for.  Pinned by VERSION: the
#: adjudication metadata this module reads back (``prefers``,
#: ``failure_type``, ``rules_cited``) is this version's vocabulary, and a
#: later version free to rename a field must be a deliberate change here.
QUERY_NAME = "go_mf_adjudicate"
QUERY_VERSION = "1"
QUERY_REF = f"{QUERY_NAME}@{QUERY_VERSION}"

#: What an answer that named no terms is rendered as.  The adjudicators must
#: be able to tell "this side declined" from "this field is empty", and an
#: empty string in a delimited block is indistinguishable from a rendering
#: bug.
NO_TERMS = "(no terms)"

#: Rendered for a selected id that is NOT on the item's own menu.  It should
#: not happen -- the engine filters off-menu picks -- but a silently dropped
#: term would change what the adjudicators were shown about a side's answer.
OFF_MENU_LABEL = "(not on the menu)"

#: Substrings that must not appear anywhere in a built item's evidence, in
#: any case.  These two words are the entire content of the blinding: either
#: one in an answer block tells the panel which pipeline it is judging.
BLINDING_FORBIDDEN = ("alcf", "go_mf", "gomf")

#: Items per tranche, and the ARGO-billed token ceiling per invocation.
DEFAULT_TRANCHE_SIZE = 100
DEFAULT_TOKEN_CEILING = 3_000_000

#: Endpoint kinds whose usage is METERED against ``token_ceiling``.
#:
#: ``argo`` and nothing else.  ``openai``/``anthropic``/``alcf`` seats are
#: API-billed too, but C10 governs the ARGO bill and says so; their usage is
#: reported under ``other_tokens`` rather than silently folded into a ceiling
#: that was not set for them.
ARGO_BILLED_KINDS = ("argo",)

#: Endpoint kinds whose real, non-null usage is reported and NEVER metered:
#: an interactive subscription, where tokens do not convert into ARGO spend.
UNMETERED_KINDS = tuple(CLI_KINDS)

#: Worst-first, so ``min`` over this order is "the lowest confidence any
#: contributing voice expressed".
CONFIDENCE_ORDER = ("low", "medium", "high")

#: Spot-check sample size by the band the previous calibration reported.
SPOT_CHECK_BY_BAND = {"trusted": 5, "conditional": 20}

#: Used for the FIRST tranche and whenever no band has been recorded yet: the
#: cautious size, because an unknown band is not a good one.
DEFAULT_SPOT_CHECK = 20

#: Bands a calibration record may report, and which of them admit a tranche.
_BANDS_ADMITTED = ("trusted", "conditional")


# ---------------------------------------------------------------------------
# rendering the two answers
# ---------------------------------------------------------------------------


def render_answer(
    menu: Sequence[Mapping[str, Any]], selected_ids: Iterable[str]
) -> str:
    """One side's answer as ``GO:id | label`` lines, in MENU ORDER.

    Menu order rather than id order or set order: the menu is ranked, the
    panel reads both answers against it, and a set's iteration order is not
    reproducible between processes -- two runs of the same manifest would
    render two different prompts and lose reuse for no reason.

    Returns :data:`NO_TERMS` for an empty selection.  An id that is not on
    the item's menu is rendered with :data:`OFF_MENU_LABEL` and sorted after
    the menu terms, because dropping it would misreport what the side said.
    """
    wanted = {str(i) for i in selected_ids}
    if not wanted:
        return NO_TERMS
    lines: List[str] = []
    seen: Set[str] = set()
    for entry in menu:
        entry_id = str(entry.get("id") or "")
        if entry_id in wanted and entry_id not in seen:
            seen.add(entry_id)
            lines.append(f"{entry_id} | {entry.get('label') or ''}")
    for entry_id in sorted(wanted - seen):
        lines.append(f"{entry_id} | {OFF_MENU_LABEL}")
    return "\n".join(lines)


def parse_answer(text: Any) -> Tuple[str, ...]:
    """The ids back out of a rendered answer block, in the order rendered.

    The inverse of :func:`render_answer`, and the reason
    :func:`read_results` needs no second input to know what A and B were:
    the item row in the store holds the very text the panel was shown, so
    the two sets being compared are the two sets that were actually judged
    rather than a re-derivation that could have drifted.
    """
    body = str(text or "").strip()
    if not body or body == NO_TERMS:
        return ()
    ids: List[str] = []
    for line in body.splitlines():
        head = line.split("|", 1)[0].strip()
        if head:
            ids.append(head)
    return tuple(ids)


# ---------------------------------------------------------------------------
# building the items
# ---------------------------------------------------------------------------


def _manifest_parts(
    manifest: Any,
) -> Tuple[str, List[Dict[str, Any]], Dict[str, List[str]]]:
    """``(manifest_id, entries, execution_order)`` from a Manifest or its JSON."""
    payload = manifest.as_dict() if hasattr(manifest, "as_dict") else dict(manifest)
    entries = [dict(entry) for entry in payload.get("entries") or ()]
    if not entries:
        raise Refused("the manifest holds no entries; there is nothing to adjudicate")
    order = {
        str(pool): [str(i) for i in ids]
        for pool, ids in (payload.get("execution_order") or {}).items()
    }
    return str(payload.get("manifest_id") or ""), entries, order


def _matched_by_item_id(matched: Any) -> Dict[str, MatchedItem]:
    items = matched.items if isinstance(matched, MatchResult) else matched
    out: Dict[str, MatchedItem] = {}
    for item in items:
        out[item.item_id] = item
    return out


def build_items(
    manifest: Any,
    matched: Any,
    pools: Any = None,
) -> List[CouncilTask]:
    """``go_mf_adjudicate@1`` tasks for the manifest's *pools*, in run order.

    Args:
        manifest: a :class:`~.sample.Manifest`, or the dict/JSON it wrote.
        matched: the :class:`~.conflicts.MatchResult` or
            :class:`~.conflicts.MatchedItem` list the manifest was sampled
            from -- the manifest records WHICH rows and in what orientation,
            and this is where the terms, the menu and the annotation come
            from.
        pools: one pool name, a sequence of them, or ``None`` for every pool
            the manifest holds.

    Each task's ``item_id`` is the MANIFEST ENTRY's id, which is the item id
    for an ordinary entry and ``"<item_id>:swap"`` for its position-swapped
    twin; evidence is ``{text, source, answer_a, answer_b}`` with the two
    answers assigned by the entry's ``alcf_is_A``; the menu is the item's own,
    copied unchanged.

    Order is the manifest's own ``execution_order`` for each pool, which is
    the seeded interleaving the sampler froze -- working the items in class
    order instead would correlate queue position with class.

    Raises:
        Refused: the manifest names no entries, names a pool it does not
            hold, or names an item *matched* does not contain.
    """
    manifest_id, entries, order = _manifest_parts(manifest)
    available = {str(entry["pool"]) for entry in entries}
    if pools is None:
        # The SAMPLER's own pool order, imported rather than restated: a
        # copy of it here would silently stop covering a pool the sampler
        # learned to draw, and `pools=None` would quietly build fewer items
        # than the manifest holds.
        wanted = [p for p in POOLS if p in available] + sorted(
            available - set(POOLS)
        )
    else:
        wanted = [pools] if isinstance(pools, str) else [str(p) for p in pools]
        unknown = [p for p in wanted if p not in available]
        if unknown:
            raise Refused(
                f"manifest {manifest_id or '(unidentified)'} holds no entries for "
                f"pool(s) {', '.join(sorted(unknown))}; it holds "
                f"{', '.join(sorted(available))}",
                sorted(unknown),
            )

    by_item_id = _matched_by_item_id(matched)
    by_entry_id = {str(entry["id"]): entry for entry in entries}
    tasks: List[CouncilTask] = []
    missing: List[str] = []
    for pool in wanted:
        pool_entries = [e for e in entries if str(e["pool"]) == pool]
        sequence = [
            by_entry_id[entry_id]
            for entry_id in order.get(pool, ())
            if entry_id in by_entry_id and str(by_entry_id[entry_id]["pool"]) == pool
        ]
        # Anything the recorded order does not mention is appended rather
        # than dropped: an order that lost an entry is a manifest bug, and
        # silently not adjudicating the row would be the expensive way to
        # find out.
        named = {str(e["id"]) for e in sequence}
        sequence.extend(e for e in pool_entries if str(e["id"]) not in named)
        for entry in sequence:
            item = by_item_id.get(str(entry["item_id"]))
            if item is None:
                missing.append(str(entry["item_id"]))
                continue
            tasks.append(_task_for(entry, item))
    if missing:
        raise Refused(
            f"{len(missing)} manifest entr(ies) name an item the matched input does "
            "not hold; sample and adjudicate must read the SAME extract",
            sorted(set(missing)),
        )
    return tasks


def _task_for(entry: Mapping[str, Any], item: MatchedItem) -> CouncilTask:
    """One blinded adjudication task from a manifest entry and its row."""
    alcf_is_a = bool(entry.get("alcf_is_A"))
    alcf = render_answer(item.menu, item.candidate_selected)
    gomf = render_answer(item.menu, item.baseline_selected)
    return CouncilTask(
        item_id=str(entry["id"]),
        evidence={
            "text": item.evidence_text,
            "source": item.source,
            "answer_a": alcf if alcf_is_a else gomf,
            "answer_b": gomf if alcf_is_a else alcf,
        },
        menu=[MenuEntry.model_validate(dict(e)) for e in item.menu],
    )


def blinding_problems(items: Sequence[CouncilTask]) -> List[Dict[str, Any]]:
    """Every place a built item's EVIDENCE names a side.  Empty is the pass.

    Checked over the evidence the renderer will splice into the prompts, and
    over the item id, which the panel never sees but a reader of the store
    does.  Case-insensitive, substring: ``"ALCF"``, ``"alcf_bench"`` and
    ``"go_mf@4"`` are all the same leak.
    """
    problems: List[Dict[str, Any]] = []
    for task in items:
        fields: List[Tuple[str, Any]] = [("item_id", task.item_id)]
        fields.extend(sorted((str(k), v) for k, v in (task.evidence or {}).items()))
        for name, value in fields:
            lowered = str(value).lower()
            for word in BLINDING_FORBIDDEN:
                if word in lowered:
                    problems.append(
                        {"item_id": task.item_id, "field": name, "found": word}
                    )
    return problems


# ---------------------------------------------------------------------------
# identity: the spec hash and the submission token
# ---------------------------------------------------------------------------


def adjudicate_spec_hash(query: Any) -> str:
    """A digest of WHAT WAS ASKED: the query spec plus the mapping policy.

    The policy is folded in because it reaches the models through the
    renderer rather than through the stored spec, so a reworded rule changes
    the question without changing the spec, the version number or any
    ``prompt_hash`` (:mod:`.policy` says so in its own docstring).  A
    calibration record that matched on the spec alone would therefore keep
    admitting tranches after the standard under judgement had moved.
    """
    payload = (
        query.model_dump(mode="json")
        if hasattr(query, "model_dump")
        else dict(query)
    )
    return canonical_sha256({"spec": payload, "policy": MAPPING_POLICY})


def _item_fingerprint(task: CouncilTask) -> Dict[str, str]:
    """``{item_id, evidence, menu}`` hashes -- orientation included.

    The evidence hash is what makes ORIENTATION part of a token: a swapped
    twin has the same annotation and the same menu and the opposite ``answer_a``,
    so only a hash over the evidence tells the two apart.
    """
    return {
        "item_id": task.item_id,
        "evidence": canonical_sha256(dict(task.evidence or {})),
        "menu": canonical_sha256(
            [entry.model_dump(mode="json") for entry in task.menu or []]
        ),
    }


def submission_token(
    *,
    run: str,
    manifest_id: str,
    tranche_index: int,
    query_ref: str,
    roster_fingerprint: str,
    items: Sequence[CouncilTask],
) -> str:
    """The content-keyed token that identifies one tranche's submission.

    Deterministic: the same plan recomputes the same token in a later
    process, which is the whole mechanism by which a restart recognises a job
    it has already paid for.  Covers run, manifest, tranche index, query
    version, roster identity and every planned item's evidence and menu hash
    (C9's key, plus the tranche's own place in the plan).
    """
    return canonical_sha256(
        {
            "run": str(run),
            "manifest_id": str(manifest_id),
            "tranche_index": int(tranche_index),
            "query": str(query_ref),
            "roster_fingerprint": str(roster_fingerprint),
            "items": [_item_fingerprint(task) for task in items],
        }
    )[:32]


def _label_for(token: str, manifest_id: str, tranche_index: int) -> str:
    """The job label the token travels in.  ``token=`` is what is parsed back."""
    return (
        f"{QUERY_REF} manifest={manifest_id or '-'} "
        f"tranche={int(tranche_index)} token={token}"
    )


def _token_of(label: Any) -> Optional[str]:
    for part in str(label or "").split():
        if part.startswith("token="):
            return part[len("token=") :] or None
    return None


def spot_check_ids(
    token: str, item_ids: Sequence[str], size: int
) -> Tuple[str, ...]:
    """A seeded sample of *size* ids from *item_ids*, sorted for the record.

    Seeded from the tranche's own token, so the sample is a property of the
    tranche rather than of when somebody asked for it: the operator can
    recompute which items they were meant to mark, and a lost ledger record
    does not lose the obligation.
    """
    pool = sorted({str(i) for i in item_ids})
    k = min(max(int(size), 0), len(pool))
    if not k:
        return ()
    rng = random.Random(f"{token}|spot_check")
    return tuple(sorted(rng.sample(pool, k)))


# ---------------------------------------------------------------------------
# usage: what has been spent, and on which kind of seat
# ---------------------------------------------------------------------------


def usage_report(store: Any, job_ids: Sequence[str]) -> Dict[str, Any]:
    """Token usage over *job_ids*, split by whether it is ARGO-billed.

    Read from ``attempts.usage`` rather than ``calls.fresh_usage``, because a
    call that was retried paid for every attempt and only the attempts table
    holds them all.  Uncharged attempts (the negative ``attempt_no`` a
    stopped daemon files) are excluded: they are audit rows for calls that
    were never answered.

    Returns:
        ``argo_tokens`` (the only figure the ceiling meters),
        ``unmetered_tokens`` (CLI seats' real usage -- reported, never
        metered), ``other_tokens``, ``by_endpoint_kind``, ``attempts``,
        and ``missing_usage``: the ids of ARGO-billed ``ok`` calls that
        carry no token count and are not exempt.  A non-empty
        ``missing_usage`` is what refuses further submission.

    Raises:
        TypeError: *store* exposes no ``.engine``.  Same requirement, and
            same reason, as :func:`..quote.fit_token_model`: this is a read
            of the ``attempts`` x ``calls`` join, which the store contract
            does not publish a method for.
    """
    report: Dict[str, Any] = {
        "jobs": list(job_ids),
        "argo_tokens": 0,
        "unmetered_tokens": 0,
        "other_tokens": 0,
        "by_endpoint_kind": {},
        "attempts": 0,
        "missing_usage": [],
    }
    if not job_ids:
        return report
    engine = getattr(store, "engine", None)
    if engine is None:
        raise TypeError(
            "usage_report needs a store exposing .engine (SqliteStore); the "
            "per-attempt token counts it meters are not on the store contract"
        )
    import sqlalchemy as sa

    from ..store import schema as sch

    attempts, calls = sch.attempts, sch.calls
    join = attempts.join(calls, calls.c.call_id == attempts.c.call_id)
    statement = (
        sa.select(
            calls.c.call_id,
            calls.c.endpoint,
            calls.c.endpoint_kind,
            calls.c.status,
            calls.c.reused_from,
            attempts.c.usage,
            attempts.c.outcome_kind,
        )
        .select_from(join)
        .where(calls.c.job_id.in_(list(job_ids)), attempts.c.attempt_no >= 0)
    )

    totals: Dict[str, int] = {}
    accounted: Set[str] = set()
    with engine.connect() as conn:
        for row in conn.execute(statement):
            report["attempts"] += 1
            kind = str(row.endpoint_kind or "")
            usage = row.usage if isinstance(row.usage, Mapping) else {}
            has_count = "prompt_tokens" in usage or "completion_tokens" in usage
            tokens = int(usage.get("prompt_tokens") or 0) + int(
                usage.get("completion_tokens") or 0
            )
            if has_count:
                accounted.add(str(row.call_id))
                totals[kind] = totals.get(kind, 0) + tokens
            elif str(row.outcome_kind or "") == "reused":
                accounted.add(str(row.call_id))
            elif usage.get("anchor_call_id") not in (None, str(row.call_id)):
                # A non-anchor member of a batched invocation: the real
                # counts are on the anchor's row, deliberately not copied
                # here (see the worker's ``_batch_usage_payload``).
                accounted.add(str(row.call_id))

    report["by_endpoint_kind"] = dict(sorted(totals.items()))
    for kind, tokens in totals.items():
        if kind in ARGO_BILLED_KINDS:
            report["argo_tokens"] += tokens
        elif kind in UNMETERED_KINDS:
            report["unmetered_tokens"] += tokens
        else:
            report["other_tokens"] += tokens

    missing: List[str] = []
    for job_id in job_ids:
        for call in store.get_calls(job_id):
            if str(call.get("status")) != "ok":
                continue
            if str(call.get("endpoint_kind")) not in ARGO_BILLED_KINDS:
                continue
            if call.get("reused_from"):
                continue
            if str(call["call_id"]) not in accounted:
                missing.append(str(call["call_id"]))
    report["missing_usage"] = sorted(missing)
    return report


# ---------------------------------------------------------------------------
# the calibration / spot-check gate
# ---------------------------------------------------------------------------


def calibration_gate(
    ledger: Any,
    *,
    manifest_id: str,
    spec_hash: str,
    roster_fingerprint: str,
    required_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Whether the next tranche may start, and what the current band is.

    A calibration record only counts when its ``manifest_id``,
    ``adjudicate_spec_hash`` and ``roster_fingerprint`` all match this run:
    marks collected against another sample, another wording of the policy or
    another bench say nothing about this one.  Marks accumulate across
    matching records; the BAND is the newest matching record's, which is the
    cumulative judgement the operator recorded.

    Returns ``{"ok", "reason", "band", "marks", "missing"}``.
    """
    records = [
        r
        for r in ledger.records("calibration")
        if str(r.get("manifest_id") or "") == str(manifest_id)
        and str(r.get("adjudicate_spec_hash") or "") == str(spec_hash)
        and str(r.get("roster_fingerprint") or "") == str(roster_fingerprint)
    ]
    marks: Dict[str, Any] = {}
    for record in records:
        marks.update(dict(record.get("marks") or {}))
    band = str(records[-1].get("band") or "") if records else ""
    out: Dict[str, Any] = {
        "ok": False,
        "reason": "",
        "band": band or None,
        "marks": len(marks),
        "missing": [],
        "records": len(records),
    }
    if not records:
        out["reason"] = (
            f"no calibration record matches manifest {manifest_id!r}, adjudicate "
            f"spec {spec_hash[:12]}... and roster {roster_fingerprint[:12]}...; a "
            "tranche after the first is not admitted without one"
        )
        return out
    unmarked = sorted(str(i) for i in required_ids if str(i) not in marks)
    if unmarked:
        out["missing"] = unmarked
        out["reason"] = (
            f"{len(unmarked)} of the previous tranche's {len(set(required_ids))} "
            "spot-check items have no imported mark"
        )
        return out
    if band == "not_trusted":
        out["reason"] = "the cumulative calibration band is not_trusted"
        return out
    if band not in _BANDS_ADMITTED:
        out["reason"] = (
            f"the newest matching calibration record reports band {band!r}, which "
            f"is not one of {', '.join(_BANDS_ADMITTED)}"
        )
        return out
    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# running the tranches
# ---------------------------------------------------------------------------


def _chunks(items: Sequence[Any], size: int) -> Iterator[List[Any]]:
    step = max(int(size), 1)
    for start in range(0, len(items), step):
        yield list(items[start : start + step])


def item_key(query: Any, task: CouncilTask) -> Tuple[str, str, str]:
    """``(item_id, entity_hash, data_hash)`` -- C9's reuse key for one task.

    The two hashes are computed by the ENGINE's own functions over the
    engine's own split of the evidence, so they are the very values the item
    row carries rather than a second hash that could drift from them.
    Between them they cover the annotation, the source, both rendered answers
    (so ORIENTATION) and the menu in order.
    """
    evidence = dict(task.evidence or {})
    entity = evidence.get(query.entity_field, "")
    rest = {k: v for k, v in evidence.items() if k != query.entity_field}
    return (
        task.item_id,
        entity_hash(entity),
        data_hash(rest, list(task.menu or [])),
    )


def _already_ok(
    store: Any, name: str, version: str, roster_fingerprint: str
) -> Set[Tuple[str, str, str]]:
    """Every :func:`item_key` this store already holds an ``ok`` result for.

    Across JOBS, because a resume's whole point is that an earlier job
    answered some of these items; tombstoned jobs are excluded by
    ``list_jobs``, and a cancelled job's items are ``cancelled`` rather than
    ``ok``, so neither needs special-casing here.

    Keyed on the CONTENT HASHES and the ROSTER, not on the item id alone
    (C9).  An id is not the question: two manifests can name the same row,
    and the same row in the opposite orientation is a different question
    asked of the same protein -- skipping it because an id matched would
    silently answer one manifest's item with another's result.  A job seated
    on a different bench is likewise not an answer to this one.
    """
    out: Set[Tuple[str, str, str]] = set()
    for job in store.list_jobs():
        if str(job.get("query_name")) != name or str(job.get("query_version")) != version:
            continue
        if str(job.get("roster_fingerprint") or "") != str(roster_fingerprint):
            continue
        for row in store.get_items(job["job_id"], status="ok"):
            out.add(
                (
                    str(row["item_id"]),
                    str(row.get("entity_hash") or ""),
                    str(row.get("data_hash") or ""),
                )
            )
    return out


def _jobs_by_token(store: Any, name: str, version: str) -> Dict[str, Dict[str, Any]]:
    """``token -> job row`` for every job of this query type carrying one.

    THE STORE IS AUTHORITATIVE for "was this tranche submitted?".  The ledger
    records the token before the submit precisely so that this lookup can
    find a job the ledger never got to mention.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for job in store.list_jobs():
        if str(job.get("query_name")) != name or str(job.get("query_version")) != version:
            continue
        token = _token_of(job.get("label"))
        if token and token not in out:
            out[token] = dict(job)
    return out


def _item_outcomes(store: Any, job_id: str) -> Dict[str, List[str]]:
    """Item ids of *job_id* grouped by status."""
    out: Dict[str, List[str]] = {}
    for row in store.get_items(job_id):
        out.setdefault(str(row["status"]), []).append(str(row["item_id"]))
    return out


def run_tranches(
    store_url: Any,
    profile: Optional[str],
    items: Sequence[CouncilTask],
    tranche_size: int = DEFAULT_TRANCHE_SIZE,
    token_ceiling: int = DEFAULT_TOKEN_CEILING,
    *,
    ledger: Any,
    manifest_id: str = "",
    council: Any = None,
    query_ref: str = QUERY_REF,
    work: bool = True,
    retry_failed: bool = False,
) -> Dict[str, Any]:
    """Submit *items* as tranches of one council job each, resumably.

    Args:
        store_url: the target store.  Ignored when *council* is given.
        profile: the seating profile.  The ROSTER IS THE OPERATOR'S -- it is
            resolved from the configuration by this profile name and nothing
            here constructs or edits one (the adjudication roster on h100
            needs ``allow_meta_overlap: true``, which is a fact about that
            machine's config and not about this code).
        items: the tasks :func:`build_items` produced, in run order.
        tranche_size: items per council job.
        token_ceiling: the soft ARGO-billed admission threshold, per
            invocation.  See the module docstring.
        ledger: the run's :class:`~.ledger.Ledger`.  Required: the submission
            token has to be durable BEFORE the submit, so there is no
            ledger-free mode that is safe.
        manifest_id: the manifest these items came from, recorded on every
            ledger record and folded into every token.
        council: a ``CouncilUtils``-shaped facade to submit through.  Built
            from *store_url* when not given; supplied by the tests and by any
            caller that already has one open.
        query_ref: the query type, ``name@version``.
        work: work each submitted job to idle before starting the next, which
            is what keeps tranches from overlapping.  ``False`` submits and
            leaves the calls for a daemon -- in which case the caller, not
            this function, owns the no-overlap rule.
        retry_failed: resubmit the ``error``/``cancelled`` items of a tranche
            that halted, as one extra job per tranche.  The operator's
            explicit act; off by default.

    Returns:
        A report: ``tranches`` (one entry each, with its token, outcome, job
        id, spot-check ids and usage), ``job_ids``, ``usage``, ``stopped``
        (``None``, or why the run stopped early) and ``invocation_id``.

    Raises:
        Refused: an item's evidence leaks which side is which; a tranche's
            job settled with error or cancelled items and *retry_failed* is
            off; or an ARGO-billed ``ok`` call carries no usage, so the
            ceiling cannot be enforced.
    """
    leaks = blinding_problems(items)
    if leaks:
        raise Refused(
            f"{len(leaks)} built item field(s) name a side; the panel must not be "
            "able to tell which answer is whose",
            sorted({str(p["item_id"]) for p in leaks}),
        )

    if council is None:
        from ...council_utils import CouncilUtils

        council = CouncilUtils(store_url=str(store_url))
    store = council.require_schema()
    spec = council.query(query_ref)
    name, version = str(spec.name), str(spec.version)
    roster = council.roster(profile)
    roster_fingerprint = str(roster.fingerprint())
    spec_hash = adjudicate_spec_hash(spec)
    run_name = Path(ledger.run_dir).name

    planned = list(_chunks(list(items), tranche_size))
    ledger.append(
        "manifest",
        manifest_id=manifest_id,
        store=str(council.store_url()),
        query=f"{name}@{version}",
        adjudicate_spec_hash=spec_hash,
        profile=profile,
        roster_fingerprint=roster_fingerprint,
        n_items=len(items),
        tranche_size=int(tranche_size),
        tranches=len(planned),
        token_ceiling=int(token_ceiling),
    )

    already_ok = _already_ok(store, name, version, roster_fingerprint)
    by_token = _jobs_by_token(store, name, version)
    report: Dict[str, Any] = {
        "invocation_id": ledger.invocation_id,
        "manifest_id": manifest_id,
        "store": str(council.store_url()),
        "query": f"{name}@{version}",
        "profile": profile,
        "roster_fingerprint": roster_fingerprint,
        "adjudicate_spec_hash": spec_hash,
        "token_ceiling": int(token_ceiling),
        "planned_tranches": len(planned),
        "tranches": [],
        "job_ids": [],
        "stopped": None,
        "usage": {},
    }
    #: Jobs THIS invocation submitted.  The ceiling meters this invocation's
    #: spend, so a job adopted from a previous one is reported and not
    #: metered.
    invocation_jobs: List[str] = []
    previous_spot_check: Tuple[str, ...] = ()
    #: The band the run's calibration already reports, read BEFORE the loop
    #: and with no required ids.  The first tranche is not itself gated, but
    #: its spot check is what gates the second, so it has to be sized by the
    #: band that is already known rather than always by the cautious default
    #: -- a run resuming under a `trusted` band would otherwise ask its
    #: operator to mark 20 items it had already earned the right to sample 5
    #: of.
    band = str(
        calibration_gate(
            ledger,
            manifest_id=manifest_id,
            spec_hash=spec_hash,
            roster_fingerprint=roster_fingerprint,
        ).get("band")
        or ""
    )

    for index, tranche in enumerate(planned):
        token = submission_token(
            run=run_name,
            manifest_id=manifest_id,
            tranche_index=index,
            query_ref=f"{name}@{version}",
            roster_fingerprint=roster_fingerprint,
            items=tranche,
        )
        entry: Dict[str, Any] = {
            "index": index,
            "token": token,
            "planned_items": [task.item_id for task in tranche],
            "job_id": None,
            "outcome": "",
            "detail": "",
            "spot_check_ids": [],
            "usage": {},
        }
        existing = by_token.get(token)

        if existing is None:
            gate = _gates(
                ledger=ledger,
                store=store,
                index=index,
                manifest_id=manifest_id,
                spec_hash=spec_hash,
                roster_fingerprint=roster_fingerprint,
                previous_spot_check=previous_spot_check,
                invocation_jobs=invocation_jobs,
                token_ceiling=token_ceiling,
            )
            band = str(gate.get("band") or band)
            if not gate["ok"]:
                entry["outcome"] = gate["outcome"]
                entry["detail"] = gate["reason"]
                entry["usage"] = dict(gate.get("usage") or {})
                report["tranches"].append(entry)
                report["stopped"] = {
                    "tranche": index,
                    "reason": gate["outcome"],
                    "detail": gate["reason"],
                }
                ledger.append(
                    "stop",
                    manifest_id=manifest_id,
                    tranche_index=index,
                    submission_token=token,
                    reason=gate["outcome"],
                    detail=gate["reason"],
                    usage=dict(gate.get("usage") or {}),
                )
                break

            send = [
                task for task in tranche if item_key(spec, task) not in already_ok
            ]
            entry["spot_check_ids"] = list(
                spot_check_ids(
                    token,
                    [task.item_id for task in tranche],
                    SPOT_CHECK_BY_BAND.get(band, DEFAULT_SPOT_CHECK),
                )
            )
            if not send:
                entry["outcome"] = "skipped_already_ok"
                entry["detail"] = (
                    f"all {len(tranche)} item(s) already have an ok result in this store"
                )
                report["tranches"].append(entry)
                previous_spot_check = tuple(entry["spot_check_ids"])
                continue

            # BEFORE the submit, and fsync-ed: everything after this line can
            # be reconciled from the token even if this process dies.
            ledger.append(
                "tranche",
                phase="planned",
                manifest_id=manifest_id,
                tranche_index=index,
                submission_token=token,
                store=str(council.store_url()),
                query=f"{name}@{version}",
                adjudicate_spec_hash=spec_hash,
                profile=profile,
                roster_fingerprint=roster_fingerprint,
                planned_item_ids=[task.item_id for task in tranche],
                submitted_item_ids=[task.item_id for task in send],
                skipped_already_ok=[
                    task.item_id
                    for task in tranche
                    if item_key(spec, task) in already_ok
                ],
                spot_check_ids=list(entry["spot_check_ids"]),
                spot_check_band=band or None,
                usage_before=dict(usage_report(store, invocation_jobs)),
            )
            job_id = council.submit(
                query_ref,
                send,
                label=_label_for(token, manifest_id, index),
                profile=profile,
            )
            entry["job_id"] = job_id
            invocation_jobs.append(job_id)
            ledger.append(
                "tranche",
                phase="submitted",
                manifest_id=manifest_id,
                tranche_index=index,
                submission_token=token,
                job_id=job_id,
                n_submitted=len(send),
            )
            entry["outcome"] = "submitted"
        else:
            job_id = str(existing["job_id"])
            entry["job_id"] = job_id
            entry["outcome"] = "adopted"
            entry["detail"] = (
                f"job {job_id} already carries this tranche's submission token"
            )
            known = {
                str(r.get("job_id"))
                for r in ledger.records("tranche")
                if str(r.get("submission_token") or "") == token and r.get("job_id")
            }
            if job_id not in known:
                # The crash window the token exists to close: the job was
                # created and no record of its id ever landed.
                ledger.append(
                    "tranche",
                    phase="reconciled",
                    manifest_id=manifest_id,
                    tranche_index=index,
                    submission_token=token,
                    job_id=job_id,
                    detail=(
                        "the store holds a job for this token that the ledger never "
                        "recorded; adopted rather than re-submitted"
                    ),
                )
            recorded = next(
                (
                    r
                    for r in ledger.records("tranche")
                    if str(r.get("submission_token") or "") == token
                    and r.get("spot_check_ids")
                ),
                None,
            )
            entry["spot_check_ids"] = list(
                (recorded or {}).get("spot_check_ids")
                or spot_check_ids(
                    token,
                    [task.item_id for task in tranche],
                    SPOT_CHECK_BY_BAND.get(band, DEFAULT_SPOT_CHECK),
                )
            )

        status = dict(store.job_status(job_id))
        if work and status.get("status") not in ("done", "cancelled"):
            status = dict(council.run(job_id))
        entry["status"] = status.get("status")

        outcomes = _item_outcomes(store, job_id)
        failed = sorted(outcomes.get("error", []) + outcomes.get("cancelled", []))
        entry["usage"] = dict(usage_report(store, [job_id]))
        if failed:
            entry["outcome"] = "halted_failed_items"
            entry["detail"] = (
                f"{len(failed)} item(s) of job {job_id} settled error/cancelled; the "
                "tranche is NOT complete"
            )
            entry["failed_items"] = failed
            report["tranches"].append(entry)
            if not retry_failed:
                report["stopped"] = {
                    "tranche": index,
                    "reason": "halted_failed_items",
                    "detail": entry["detail"],
                }
                ledger.append(
                    "stop",
                    manifest_id=manifest_id,
                    tranche_index=index,
                    submission_token=token,
                    job_id=job_id,
                    reason="halted_failed_items",
                    detail=entry["detail"],
                    failed_item_ids=failed,
                )
                raise Refused(
                    f"tranche {index} (job {job_id}) settled with {len(failed)} "
                    "error/cancelled item(s); re-run with --retry-failed to resubmit "
                    "them, but do not treat this tranche as complete",
                    failed,
                )
            retry = _retry_failed(
                council=council,
                store=store,
                ledger=ledger,
                tranche=tranche,
                failed=failed,
                token=token,
                index=index,
                manifest_id=manifest_id,
                query_ref=query_ref,
                profile=profile,
                work=work,
            )
            entry["retry"] = retry
            # The halted job is NOT complete, but its ok items are real and a
            # reader needs the job id to see them.  It is reported here and
            # NEVER recorded `complete` in the ledger, which is the record a
            # later invocation reads.
            report["job_ids"].append(job_id)
            if retry.get("job_id"):
                invocation_jobs.append(str(retry["job_id"]))
                report["job_ids"].append(str(retry["job_id"]))
            previous_spot_check = tuple(entry["spot_check_ids"])
            continue

        report["tranches"].append(entry)
        if entry["job_id"]:
            report["job_ids"].append(job_id)
        ledger.append(
            "tranche",
            phase="complete",
            manifest_id=manifest_id,
            tranche_index=index,
            submission_token=token,
            job_id=job_id,
            job_status=status.get("status"),
            ok_items=len(outcomes.get("ok", [])),
            usage=dict(entry["usage"]),
        )
        previous_spot_check = tuple(entry["spot_check_ids"])

    report["usage"] = dict(usage_report(store, invocation_jobs))
    report["overshoot"] = report["usage"]["argo_tokens"] > int(token_ceiling)
    if report["overshoot"]:
        ledger.append(
            "stop",
            manifest_id=manifest_id,
            reason="token_ceiling_overshoot",
            detail=(
                f"this invocation's ARGO-billed usage {report['usage']['argo_tokens']} "
                f"is above the ceiling {int(token_ceiling)}; the overshooting tranche "
                "was admitted while the total was still below it and no further "
                "tranche will start"
            ),
            usage=dict(report["usage"]),
        )
    return report


def _gates(
    *,
    ledger: Any,
    store: Any,
    index: int,
    manifest_id: str,
    spec_hash: str,
    roster_fingerprint: str,
    previous_spot_check: Sequence[str],
    invocation_jobs: Sequence[str],
    token_ceiling: int,
) -> Dict[str, Any]:
    """Whether tranche *index* may be CREATED.  Calibration first, then spend.

    Calibration first because it is the harder refusal: a run that outran its
    calibration must not start a tranche even with budget to spare, and
    reporting "out of budget" for a run that was really out of calibration
    would send the operator to raise the wrong number.
    """
    out: Dict[str, Any] = {"ok": True, "outcome": "", "reason": "", "band": None}
    if index > 0:
        gate = calibration_gate(
            ledger,
            manifest_id=manifest_id,
            spec_hash=spec_hash,
            roster_fingerprint=roster_fingerprint,
            required_ids=previous_spot_check,
        )
        out["band"] = gate.get("band")
        if not gate["ok"]:
            out.update(ok=False, outcome="refused_spot_check", reason=gate["reason"])
            return out

    usage = usage_report(store, list(invocation_jobs))
    out["usage"] = usage
    if usage["missing_usage"]:
        # Not a soft stop: an unmeasurable spend makes the ceiling
        # unenforceable, and carrying on would be spending an amount nobody
        # can add up.
        raise Refused(
            f"{len(usage['missing_usage'])} ARGO-billed ok call(s) in this "
            "invocation carry no token usage, so the ceiling cannot be enforced; "
            "no further tranche will be submitted",
            usage["missing_usage"],
        )
    if usage["argo_tokens"] >= int(token_ceiling):
        out.update(
            ok=False,
            outcome="refused_token_ceiling",
            reason=(
                f"this invocation's completed ARGO-billed usage "
                f"{usage['argo_tokens']} has reached the ceiling "
                f"{int(token_ceiling)}; CLI seats' "
                f"{usage['unmetered_tokens']} reported token(s) are not counted"
            ),
        )
    return out


def _retry_failed(
    *,
    council: Any,
    store: Any,
    ledger: Any,
    tranche: Sequence[CouncilTask],
    failed: Sequence[str],
    token: str,
    index: int,
    manifest_id: str,
    query_ref: str,
    profile: Optional[str],
    work: bool,
) -> Dict[str, Any]:
    """Resubmit one tranche's failed items as their own job, once.

    Its token is the tranche's with a ``:retry`` suffix, so the retry is as
    reconcilable as the original and a second ``--retry-failed`` adopts it
    instead of paying again.
    """
    wanted = {str(i) for i in failed}
    send = [task for task in tranche if task.item_id in wanted]
    retry_token = f"{token}:retry"
    out: Dict[str, Any] = {"token": retry_token, "items": [t.item_id for t in send]}
    if not send:
        out["detail"] = "no planned item matches the failed ids; nothing resubmitted"
        return out
    for job in store.list_jobs():
        if _token_of(job.get("label")) == retry_token:
            out["job_id"] = str(job["job_id"])
            out["detail"] = "a retry job for this tranche already exists; adopted"
            return out
    ledger.append(
        "tranche",
        phase="retry_planned",
        manifest_id=manifest_id,
        tranche_index=index,
        submission_token=retry_token,
        parent_token=token,
        submitted_item_ids=[task.item_id for task in send],
    )
    job_id = council.submit(
        query_ref,
        send,
        label=_label_for(retry_token, manifest_id, index),
        profile=profile,
    )
    out["job_id"] = job_id
    ledger.append(
        "tranche",
        phase="retry_submitted",
        manifest_id=manifest_id,
        tranche_index=index,
        submission_token=retry_token,
        job_id=job_id,
        n_submitted=len(send),
    )
    if work:
        council.run(job_id)
    outcomes = _item_outcomes(store, job_id)
    out["failed_again"] = sorted(
        outcomes.get("error", []) + outcomes.get("cancelled", [])
    )
    return out


# ---------------------------------------------------------------------------
# reading the results back
# ---------------------------------------------------------------------------


def derived_verdict(
    final_ids: Iterable[str],
    set_a: Iterable[str],
    set_b: Iterable[str],
    deciding_prefers: Optional[str] = None,
) -> str:
    """``"A"``, ``"B"``, ``"both_acceptable"`` or ``"neither"``.

    Derived from the SETTLED TERM SET, not from a vote: the council's answer
    is the set of ids it chose, and "which side won" is whether that set is
    one of the two it was shown.

    ``both_acceptable`` is the one value equality cannot produce, so it comes
    only from the deciding voice -- meta's explicit ``prefers`` when meta
    decided, else the proposers' UNANIMOUS ``prefers``.  It overrides
    equality: a deciding voice that said both answers were acceptable has
    said something the final set cannot say, and silently reporting "A"
    because the set happened to match A's would lose it.
    """
    if str(deciding_prefers or "") == "both_acceptable":
        return "both_acceptable"
    final = frozenset(str(i) for i in final_ids)
    if final == frozenset(str(i) for i in set_a):
        return "A"
    if final == frozenset(str(i) for i in set_b):
        return "B"
    return "neither"


def verdict_side(derived: str, alcf_is_a: Any) -> str:
    """A/B mapped through the manifest's orientation to ``alcf``/``gomf``.

    ``both_acceptable`` and ``neither`` name no side and pass through
    unchanged.
    """
    if derived == "A":
        return "alcf" if bool(alcf_is_a) else "gomf"
    if derived == "B":
        return "gomf" if bool(alcf_is_a) else "alcf"
    return derived


@dataclass(frozen=True)
class ItemResult:
    """One adjudicated item: what was settled, and the research metadata.

    ``failure_type`` / ``correct_term_off_menu`` / ``rules_cited`` /
    ``confidence`` are the ITEM's, resolved by C7: meta's when meta decided,
    else the proposers' aggregate.  The proposer aggregate is reported beside
    them either way (``proposer_consensus``) so a reader can see what the
    bench said even on an item meta overruled, and the critic's audit is
    reported SEPARATELY -- it audits the panel and is not a vote.
    """

    item_id: str
    job_id: str
    status: str
    final_ids: Tuple[str, ...]
    decided_by: Optional[str]
    meta_skipped: bool
    set_a: Tuple[str, ...]
    set_b: Tuple[str, ...]
    alcf_is_a: Optional[bool]
    deciding_prefers: Optional[str]
    derived_verdict: str
    side: str
    failure_type: Optional[str]
    failure_type_source: str
    correct_term_off_menu: Optional[bool]
    rules_cited: Tuple[str, ...]
    confidence: Optional[str]
    why: Mapping[str, str] = field(default_factory=dict)
    off_menu_description: Mapping[str, str] = field(default_factory=dict)
    proposers: Tuple[Mapping[str, Any], ...] = ()
    proposer_consensus: Mapping[str, Any] = field(default_factory=dict)
    meta: Optional[Mapping[str, Any]] = None
    critic: Optional[Mapping[str, Any]] = None
    pool: Optional[str] = None
    swap: Optional[bool] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "job_id": self.job_id,
            "status": self.status,
            "final_ids": list(self.final_ids),
            "decided_by": self.decided_by,
            "meta_skipped": self.meta_skipped,
            "set_a": list(self.set_a),
            "set_b": list(self.set_b),
            "alcf_is_A": self.alcf_is_a,
            "deciding_prefers": self.deciding_prefers,
            "derived_verdict": self.derived_verdict,
            "side": self.side,
            "failure_type": self.failure_type,
            "failure_type_source": self.failure_type_source,
            "correct_term_off_menu": self.correct_term_off_menu,
            "rules_cited": list(self.rules_cited),
            "confidence": self.confidence,
            "why": dict(self.why),
            "off_menu_description": dict(self.off_menu_description),
            "proposers": [dict(p) for p in self.proposers],
            "proposer_consensus": dict(self.proposer_consensus),
            "meta": dict(self.meta) if self.meta is not None else None,
            "critic": dict(self.critic) if self.critic is not None else None,
            "pool": self.pool,
            "swap": self.swap,
        }


def _unique_mode(values: Sequence[Any]) -> Tuple[Any, bool]:
    """``(the one most common value, True)``, or ``(None, False)`` on a tie.

    A tie is a tie whether it is three ways or two: in both cases no value is
    what the bench said, and C7 asks for ``mixed`` rather than for whichever
    one sorted first.
    """
    present = [v for v in values if v is not None]
    if not present:
        return None, False
    counts = Counter(present).most_common()
    if len(counts) > 1 and counts[0][1] == counts[1][1]:
        return None, False
    return counts[0][0], True


def _lowest_confidence(values: Sequence[Any]) -> Optional[str]:
    known = [str(v) for v in values if str(v or "") in CONFIDENCE_ORDER]
    if not known:
        return None
    return min(known, key=CONFIDENCE_ORDER.index)


def _call_resolver(store: Any, by_call_id: Mapping[str, Mapping[str, Any]]) -> Any:
    """``call_id -> row``, this job's rows first and then the whole store.

    A reuse link CROSSES JOBS and users -- that is what reuse is for
    (``SqliteStore.find_reusable`` says so) -- so a resolver limited to the
    job in hand would report a perfectly good reused answer as missing
    metadata.  The store contract publishes no by-id call read, so the
    fallback is a one-row select, the same ``.engine`` read
    :func:`usage_report` already needs.
    """

    def resolve(call_id: str) -> Mapping[str, Any]:
        row = by_call_id.get(str(call_id))
        if row is not None:
            return row
        engine = getattr(store, "engine", None)
        if engine is None:  # pragma: no cover - SqliteStore always has one
            return {}
        import sqlalchemy as sa

        from ..store import schema as sch

        with engine.connect() as conn:
            found = conn.execute(
                sa.select(sch.calls).where(sch.calls.c.call_id == str(call_id))
            ).first()
        return dict(found._mapping) if found is not None else {}

    return resolve


def _validated_reply(
    call: Mapping[str, Any],
    schema: Mapping[str, Any],
    resolve: Any,
) -> Dict[str, Any]:
    """The schema-valid ``reply_parsed`` of a settled ok call.

    Follows ``reused_from`` when this row carries no parsed reply of its own,
    which is the only shape in which a reused answer can be missing one.
    ``attempts.reply_text`` is deliberately NOT consulted: it is written only
    for an attempt that produced no usable answer, so reading a label out of
    it would be reading a FAILED attempt's prose.

    Raises:
        Refused: there is no validated metadata to read.
    """
    parsed = call.get("reply_parsed")
    source = call
    hops = 0
    while not isinstance(parsed, Mapping) and source.get("reused_from") and hops < 8:
        source = resolve(str(source["reused_from"])) or {}
        parsed = source.get("reply_parsed")
        hops += 1
    if not isinstance(parsed, Mapping):
        raise Refused(
            f"call {call['call_id']} ({call.get('role')}/{call.get('seat_name')}) on "
            f"item {call.get('item_id')!r} settled ok but carries no parsed reply, "
            "and no reuse link supplies one; adjudication metadata must come from a "
            "schema-valid reply, never from raw prose",
            [str(call["call_id"])],
        )
    problem = _schema_problem(parsed, schema)
    if problem:
        raise Refused(
            f"call {call['call_id']} ({call.get('role')}/{call.get('seat_name')}) on "
            f"item {call.get('item_id')!r} holds a reply that does not satisfy the "
            f"{call.get('role')} answer schema: {problem}",
            [str(call["call_id"])],
        )
    return dict(parsed)


def _schema_problem(parsed: Mapping[str, Any], schema: Mapping[str, Any]) -> Optional[str]:
    """A complaint about *parsed*, or ``None``.  ``jsonschema`` when available.

    The same fallback shape the engine uses (a required-key check) for an
    install without ``jsonschema``: a weak check beats no check, because the
    thing being guarded against is reading a field that is not there.
    """
    if not schema:
        return None
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is usually installed
        for key in schema.get("required") or ():
            if key not in parsed:
                return f"the reply is missing the required key {key!r}"
        return None
    try:
        jsonschema.validate(instance=dict(parsed), schema=dict(schema))
    except jsonschema.ValidationError as exc:
        path = "/".join(str(part) for part in exc.absolute_path)
        return f"{exc.message}{f' at {path}' if path else ''}"
    return None


def read_results(
    store_url: Any,
    job_ids: Sequence[str],
    manifest: Any,
) -> List[ItemResult]:
    """Every item of *job_ids*, with its verdict and its research metadata.

    Args:
        store_url: the store, or an already-open one.
        job_ids: the adjudication jobs to read.
        manifest: the manifest the items came from -- the only thing that
            knows which of A and B was ALCF, which is how a derived verdict
            becomes a statement about a pipeline.

    Raises:
        Refused: a job is not an adjudication job; an item is not in the
            manifest (its verdict could not be attributed to a side); or an
            ``ok`` item's settled ``ok`` calls carry no schema-valid
            metadata.
    """
    _manifest_id, entries, _order = _manifest_parts(manifest)
    by_entry_id = {str(entry["id"]): entry for entry in entries}
    out: List[ItemResult] = []
    with _opened(store_url) as store:
        from ..queries import QueryRegistry

        registry = QueryRegistry(store)
        schemas: Dict[Tuple[str, str], Dict[str, Any]] = {}
        unknown: List[str] = []
        for job_id in job_ids:
            job = store.get_job(job_id)
            if job is None:
                raise Refused("no such job", [str(job_id)])
            key = (str(job["query_name"]), str(job["query_version"]))
            if key != (QUERY_NAME, QUERY_VERSION):
                raise Refused(
                    f"job {job_id} is {key[0]}@{key[1]}, not {QUERY_REF}", [str(job_id)]
                )
            if key not in schemas:
                spec = registry.get(*key)
                schemas[key] = {
                    role: dict(spec.answer_schema(role))
                    for role in ("proposer", "critic", "meta")
                }
            schema = schemas[key]
            calls = [dict(row) for row in store.get_calls(job_id)]
            resolve = _call_resolver(
                store, {str(row["call_id"]): row for row in calls}
            )
            by_item: Dict[str, List[Dict[str, Any]]] = {}
            for row in calls:
                by_item.setdefault(str(row["item_id"]), []).append(row)
            for item in store.get_items(job_id):
                item_id = str(item["item_id"])
                entry = by_entry_id.get(item_id)
                if entry is None:
                    unknown.append(item_id)
                    continue
                out.append(
                    _item_result(
                        item=item,
                        job_id=str(job_id),
                        entry=entry,
                        calls=by_item.get(item_id, []),
                        resolve=resolve,
                        schema=schema,
                    )
                )
        if unknown:
            raise Refused(
                f"{len(unknown)} item(s) of these jobs are not in the manifest, so "
                "their verdicts cannot be attributed to a side",
                sorted(set(unknown)),
            )
    return out


def _item_result(
    *,
    item: Mapping[str, Any],
    job_id: str,
    entry: Mapping[str, Any],
    calls: Sequence[Mapping[str, Any]],
    resolve: Any,
    schema: Mapping[str, Mapping[str, Any]],
) -> ItemResult:
    """Assemble one :class:`ItemResult` from an item row and its calls."""
    verdict = dict(item.get("verdict") or {})
    evidence = dict(item.get("evidence") or {})
    set_a = parse_answer(evidence.get("answer_a"))
    set_b = parse_answer(evidence.get("answer_b"))
    status = str(item.get("status") or "")
    final_ids = tuple(str(i) for i in verdict.get("selected_ids") or ())
    decided_by = verdict.get("decided_by")
    alcf_is_a = entry.get("alcf_is_A")

    live_ok = [
        row
        for row in calls
        if str(row.get("status")) == "ok" and bool(row.get("live", True))
    ]
    proposer_rows = [row for row in live_ok if str(row.get("role")) == "proposer"]
    critic_rows = [row for row in live_ok if str(row.get("role")) == "critic"]
    meta_rows = [row for row in live_ok if str(row.get("role")) == "meta"]

    base: Dict[str, Any] = {
        "item_id": str(item["item_id"]),
        "job_id": job_id,
        "status": status,
        "final_ids": final_ids,
        "decided_by": decided_by,
        "meta_skipped": bool(verdict.get("meta_skipped")),
        "set_a": set_a,
        "set_b": set_b,
        "alcf_is_a": None if alcf_is_a is None else bool(alcf_is_a),
        "pool": entry.get("pool"),
        "swap": None if entry.get("swap") is None else bool(entry.get("swap")),
    }
    if status != "ok":
        # An item that never settled ok has no metadata to be missing: it is
        # reported as what it is, so a reader counting adjudications does not
        # have to infer the denominator.
        return ItemResult(
            deciding_prefers=None,
            derived_verdict="neither",
            side="neither",
            failure_type=None,
            failure_type_source="unsettled",
            correct_term_off_menu=None,
            rules_cited=(),
            confidence=None,
            **base,
        )

    proposers: List[Dict[str, Any]] = []
    why: Dict[str, str] = {}
    off_menu: Dict[str, str] = {}
    for row in sorted(proposer_rows, key=lambda r: str(r.get("seat_name"))):
        parsed = _validated_reply(row, schema["proposer"], resolve)
        seat = str(row.get("seat_name"))
        proposers.append({"seat": seat, **parsed})
        why[seat] = str(parsed.get("why") or "")
        off_menu[seat] = str(parsed.get("off_menu_description") or "")

    meta: Optional[Dict[str, Any]] = None
    if meta_rows:
        row = meta_rows[0]
        parsed = _validated_reply(row, schema["meta"], resolve)
        seat = str(row.get("seat_name"))
        meta = {"seat": seat, **parsed}
        why[seat] = str(parsed.get("reasoning") or "")
        off_menu[seat] = str(parsed.get("off_menu_description") or "")

    critic: Optional[Dict[str, Any]] = None
    if critic_rows:
        row = critic_rows[0]
        parsed = _validated_reply(row, schema["critic"], resolve)
        critic = {"seat": str(row.get("seat_name")), **parsed}

    if not proposers and meta is None:
        raise Refused(
            f"item {item['item_id']!r} of job {job_id} settled ok with no ok "
            "proposer or meta call, so there is no validated metadata to read",
            [str(item["item_id"])],
        )

    failure_mode, failure_decided = _unique_mode(
        [p.get("failure_type") for p in proposers]
    )
    off_menu_mode, off_menu_decided = _unique_mode(
        [p.get("correct_term_off_menu") for p in proposers]
    )
    rules = sorted({str(r) for p in proposers for r in p.get("rules_cited") or ()})
    consensus: Dict[str, Any] = {
        "seats": len(proposers),
        "failure_type": failure_mode if failure_decided else "mixed",
        "failure_type_decided": failure_decided,
        "correct_term_off_menu": off_menu_mode if off_menu_decided else None,
        "rules_cited": rules,
        "confidence": _lowest_confidence([p.get("confidence") for p in proposers]),
        "prefers": _unanimous([p.get("prefers") for p in proposers]),
    }

    meta_decided = meta is not None and str(decided_by or "") == "meta"
    if meta_decided:
        assert meta is not None  # for the type checker; guarded by meta_decided
        failure_type = meta.get("failure_type")
        source = "meta"
        off_menu_flag: Optional[bool] = (
            None
            if meta.get("correct_term_off_menu") is None
            else bool(meta["correct_term_off_menu"])
        )
        item_rules = tuple(sorted({str(r) for r in meta.get("rules_cited") or ()}))
        confidence = _lowest_confidence([meta.get("final_confidence")])
        prefers = meta.get("prefers")
    else:
        failure_type = consensus["failure_type"]
        source = "proposer_majority" if failure_decided else "proposer_mixed"
        off_menu_flag = consensus["correct_term_off_menu"]
        item_rules = tuple(rules)
        confidence = consensus["confidence"]
        prefers = consensus["prefers"]

    derived = derived_verdict(final_ids, set_a, set_b, prefers)
    return ItemResult(
        deciding_prefers=None if prefers is None else str(prefers),
        derived_verdict=derived,
        side=verdict_side(derived, alcf_is_a),
        failure_type=None if failure_type is None else str(failure_type),
        failure_type_source=source,
        correct_term_off_menu=off_menu_flag,
        rules_cited=item_rules,
        confidence=confidence,
        why=why,
        off_menu_description=off_menu,
        proposers=tuple(proposers),
        proposer_consensus=consensus,
        meta=meta,
        critic=critic,
        **base,
    )


def _unanimous(values: Sequence[Any]) -> Optional[str]:
    """The one value every voice gave, or ``None`` if they did not agree.

    ``both_acceptable`` from the bench only overrides equality when the bench
    was UNANIMOUS about it (C7), so "they all said the same thing" is the
    question, not "most of them did".
    """
    present = {str(v) for v in values if v is not None}
    if len(present) == 1:
        return present.pop()
    return None
