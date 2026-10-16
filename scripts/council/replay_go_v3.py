#!/usr/bin/env python3
"""Replay Jose Faria's recorded CONCORDIA V3 GO council through this worker.

ZERO TOKENS. No network, no ARGO, no CLI, no GPU. Every model answer comes
out of a recorded transcript on ``/scratch``; the only thing substituted for
a real council run is the BACKEND. The store is a real
:class:`~kbutillib.domains.ai.council.store.sqlite.SqliteStore` in a
temporary directory, the query type is the shipped ``go_mf@1`` seeded with
``QueryRegistry.seed_builtins()``, and the decision is made by the real
:class:`~kbutillib.domains.ai.council.engine.worker.CouncilWorker` calling
the real ``decide()``. Nothing is monkeypatched.

WHAT THIS PROVES
================
That the council engine, fed the SAME model replies Jose's V3 runner was
fed, reaches the SAME decision. It does not re-ask any model, so it proves
nothing about prompts or about model behaviour -- only about the rule.

The comparison is over SEMANTIC fields only: the selected ids as a SET, the
status, the decline reason, and whether meta was skipped. Id ORDER is
deliberately excluded, because the two implementations order a set they
agree on differently and by design: the V3 runner reports the meta-skip
path's ids in first-vote order (``go_council.py:383``) and the meta path's
in the order meta named them (``go_council.py:449-451``), while
``engine/decide.py`` reports both in MENU order.

THE ORACLE AND ITS PROVENANCE
=============================
``agent-io/prds/kbu-ai-council-v1/research/go-replay-manifest.json`` names
every file read here and pins its SHA-256; :func:`verify_manifest` asserts
those digests before anything else happens, so a moved or rewritten oracle
stops the run instead of quietly changing the answer.

The manifest also records WHICH generator produced the oracle, established
cryptographically rather than guessed -- see ``provenance`` there and the
report this script writes. Short version: the V3 kit's reply cache is keyed
by ``sha256(model, max_tokens, system, user)``, every one of its 433 rows is
reproduced by the V3 kit's own prompt constants, and the 09-18 live pilot's
proposer prompt reproduces none of them.

USAGE
=====
::

    python scripts/council/replay_go_v3.py            # all 100 rows, writes the report
    python scripts/council/replay_go_v3.py --limit 10 # a quick subset
    python scripts/council/replay_go_v3.py --emit-fixture tests/.../go_v3_replay_10.json

Run it from the repository root with the ``council`` extra installed
(``pip install -e '.[council]'``).

NORMALISATION. Eight recorded replies do not satisfy ``go_mf@1``'s answer
schema, which Jose's runner did not enforce: seven ``claudeopus45`` meta
replies omit ``no_functional_content`` and one ``gemini35flash`` proposer
reply carries ``"notes": null``. ``engine/normalise.py`` repairs exactly
those two defects before the schema gate and records every repair on the
call and the verdict, so the replay now scores them. The report lists each
repaired row, keeps the rule-only control column beside it, and still prints
the raw reply the schema refuses -- the transcript is unchanged, and so is
what the schema says about it.

EXIT CODE. ``0`` only when every scored row agrees.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]

MANIFEST_PATH = (
    REPO_ROOT / "agent-io/prds/kbu-ai-council-v1/research/go-replay-manifest.json"
)
REPORT_PATH = (
    REPO_ROOT / "agent-io/prds/kbu-ai-council-v1/research/go-replay-parity.md"
)
FIXTURE_PATH = (
    REPO_ROOT / "tests/domains/ai/council/fixtures/go_v3_replay_10.json"
)

#: The V3 roster, read off ``go_council.py:46-48`` (``COUNCIL``, ``CRITIC``,
#: ``META``) and confirmed against the recorded rows: every row's
#: ``proposers`` mapping has exactly these three keys, and the reply cache's
#: per-model row counts match (gpt54/gpt55 proposer 100 each, gemini35flash
#: proposer 99, claudesonnet5 critic 100, claudeopus45 meta 34).
RECORDED_PROPOSERS: Tuple[str, ...] = ("gemini35flash", "gpt54", "gpt55")
RECORDED_CRITIC = "claudesonnet5"
RECORDED_META = "claudeopus45"

#: Recorded roster name -> the seat name this council seats it under. The
#: proposer order is ``COUNCIL`` order, so ``proposer_1`` is gemini35flash.
SEAT_OF: Dict[str, str] = {
    RECORDED_PROPOSERS[0]: "proposer_1",
    RECORDED_PROPOSERS[1]: "proposer_2",
    RECORDED_PROPOSERS[2]: "proposer_3",
    RECORDED_CRITIC: "critic",
    RECORDED_META: "meta",
}

#: Keys the V3 RUNNER adds to the meta reply after the model answered
#: (``go_council.py:449-452``). They are not part of what the model said, and
#: ``go_mf``'s meta schema is ``additionalProperties: false``, so serving them
#: back as a model reply would fail a call that never failed live.
RUNNER_ADDED_META_KEYS = ("skipped",)

#: The endpoint alias every seat sits on. ``kind: replay`` is not one of
#: ``CouncilConfig``'s four real kinds, which is why the worker is handed the
#: mapping form of its endpoint facts.
ENDPOINT = "replay1"


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def _repo_commit() -> str:
    """The repository commit this run was made at, or ``"unknown"``.

    Recorded in the report because the parity figure is a property of the
    code as much as of the transcript: the same oracle through a different
    engine is a different measurement.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:  # pragma: no cover - git is present on every machine here
        return "unknown"
    return out.stdout.strip() or "unknown"


def sha256_file(path: Path) -> str:
    """SHA-256 of *path*, read in chunks (the results file is 23 MB)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> Dict[str, Any]:
    """Read the replay manifest."""
    return json.loads(path.read_text())


def verify_manifest(manifest: Mapping[str, Any]) -> List[str]:
    """Assert every digest the manifest pins.

    Returns the list of file keys that were checked. Raises ``SystemExit``
    rather than ``AssertionError`` on a mismatch: a changed oracle is an
    operator-visible condition, not a bug to traceback.
    """
    checked: List[str] = []
    for key, entry in dict(manifest["files"]).items():
        path = Path(entry["path"])
        if not path.exists():
            raise SystemExit(
                f"manifest file {key!r} is missing: {path}\n"
                "The oracle lives on /scratch on poplar (h100) and is not "
                "checked in. Run this script there, or run the offline "
                "fixture test instead (tests/domains/ai/council/test_go_replay.py)."
            )
        actual = sha256_file(path)
        if actual != entry["sha256"]:
            raise SystemExit(
                f"manifest file {key!r} has CHANGED.\n"
                f"  path:     {path}\n"
                f"  expected: {entry['sha256']}\n"
                f"  actual:   {actual}\n"
                "The parity report is void until the manifest is re-derived."
            )
        checked.append(key)
    return checked


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Every non-blank line of *path*, parsed."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Inputs -> CouncilTasks, recorded replies -> a ReplayBackend table
# ---------------------------------------------------------------------------


def task_from_input(record: Mapping[str, Any]) -> Any:
    """One ``CouncilTask`` from one input row.

    ``candidates`` IS the closed menu. ``go_mf``'s renderer reads ``rank``,
    ``id``, ``score``, ``label`` and ``description``, which are the kit's
    ``rank``, ``go_id``, ``score``, ``name`` and ``def``.
    """
    from kbutillib.domains.ai.council.types import CouncilTask, MenuEntry

    return CouncilTask(
        item_id=str(record["input_id"]),
        evidence={"text": record["text"], "source": record["source"]},
        menu=[
            MenuEntry(
                id=candidate["go_id"],
                label=candidate.get("name") or "",
                description=candidate.get("def"),
                rank=candidate.get("rank"),
                score=candidate.get("score"),
            )
            for candidate in record["candidates"]
        ],
    )


def replay_entries(result: Mapping[str, Any]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """The ``ReplayBackend`` table entries for one recorded row.

    Keyed ``(item_id, role, seat_name)``, which is what the worker puts in
    ``CompletionRequest.trace_id`` (``worker.py:941``).

    Two kinds of entry are deliberately WITHHELD, and withholding them is the
    honest choice in both cases:

    * **An errored proposer.** There is no reply to serve. The backend
      answers ``invalid_output`` for that seat, which is what a failed seat
      looks like, rather than a fabricated success.
    * **A meta reply on a meta-SKIPPED row.** The V3 runner synthesises that
      dict itself (``go_council.py:423-429``); no meta model was called. If
      this council were to reach for meta where V3 skipped, serving the
      synthetic dict would manufacture agreement. Withholding it turns that
      disagreement into a visible failure instead.
    """
    item_id = str(result["input_id"])
    table: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    for recorded_name, reply in dict(result.get("proposers") or {}).items():
        seat = SEAT_OF.get(recorded_name)
        if seat is None or "error" in reply:
            continue
        table[(item_id, "proposer", seat)] = dict(reply)

    critic = result.get("critic")
    if critic and "error" not in critic:
        table[(item_id, "critic", SEAT_OF[RECORDED_CRITIC])] = dict(critic)

    meta = result.get("meta") or {}
    if meta and not meta.get("skipped") and "error" not in meta:
        served = {k: v for k, v in meta.items() if k not in RUNNER_ADDED_META_KEYS}
        table[(item_id, "meta", SEAT_OF[RECORDED_META])] = served

    return table


def build_roster() -> Any:
    """The V3 roster, seated on ``replay`` endpoints.

    ``canonical_model`` carries the recorded model name, so the ``Roster``
    independence check is enforced against the council that actually sat.
    """
    from kbutillib.domains.ai.council.types import Roster, SeatSpec

    def seat(seat_name: str, model: str) -> SeatSpec:
        return SeatSpec(
            seat_name=seat_name,
            endpoint=ENDPOINT,
            endpoint_kind="replay",
            model=model,
            canonical_model=f"concordia-v3/{model}",
            max_output_tokens=4000,
            temperature=0,
            timeout_s=30,
        )

    return Roster(
        proposers=[
            seat(SEAT_OF[name], name) for name in RECORDED_PROPOSERS
        ],
        critic=seat(SEAT_OF[RECORDED_CRITIC], RECORDED_CRITIC),
        meta=seat(SEAT_OF[RECORDED_META], RECORDED_META),
    )


# ---------------------------------------------------------------------------
# The replay itself
# ---------------------------------------------------------------------------


def run_replay(
    tasks: Sequence[Any],
    table: Mapping[Tuple[str, str, str], Mapping[str, Any]],
    store_url: str,
) -> Dict[str, Any]:
    """Submit one job and work it until idle; return ``{item_id: verdict}``.

    Everything here is the shipped article: ``open_store`` builds a real
    SQLite store, ``QueryRegistry.seed_builtins()`` seeds the real
    ``go_mf@1``, and ``CouncilWorker`` is the real worker. The
    ``ReplayBackend`` is the only substitute, which is the design.
    """
    from kbutillib.domains.ai.council.backends.testing import ReplayBackend
    from kbutillib.domains.ai.council.engine.worker import CouncilWorker
    from kbutillib.domains.ai.council.queries import QueryRegistry
    from kbutillib.domains.ai.council.store import open_store
    from kbutillib.domains.ai.council.types import CouncilVerdict

    store = open_store(store_url)
    store.init_schema()
    registry = QueryRegistry(store)
    registry.seed_builtins()
    query = registry.get("go_mf")

    backend = ReplayBackend(table)
    config = {"endpoints": {ENDPOINT: {"kind": "replay", "max_concurrency": 8}}}
    worker = CouncilWorker(
        store,
        config,
        {ENDPOINT: backend},
        user="replay",
        worker_id="replay-worker",
        # The replay never waits on anything real, so the worker's retry and
        # idle timings are collapsed; left at their defaults a parked call
        # would sit for minutes against a backend that cannot change its mind.
        retry_backoff_s=(0.0,),
        retry_jitter=0.0,
        poll_interval_s=0.0,
        idle_grace_s=0.05,
        max_idle_s=30.0,
        heartbeat_interval_s=1e9,
    )

    job_id = worker.submit(query, list(tasks), build_roster(), label="go v3 replay")
    _run(worker.work(job_id=job_id, until_idle=True))

    verdicts: Dict[str, Any] = {}
    for row in store.get_items(job_id):
        if row.get("verdict"):
            verdicts[str(row["item_id"])] = CouncilVerdict.model_validate(row["verdict"])
    return verdicts


def _run(coro: Any) -> Any:
    """Run one coroutine on a loop this call owns.

    Deliberately not ``asyncio.run``: it clears the thread's event loop on
    the way out, which breaks later ``get_event_loop()`` callers in the same
    process. Same helper, same reason, as ``tests/.../engine_support.run``.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def recorded_decision(result: Mapping[str, Any]) -> Dict[str, Any]:
    """The V3 row's decision, in the four semantic fields."""
    meta = result.get("meta") or {}
    return {
        "status": result.get("status"),
        "selected_ids": sorted(set(meta.get("final_go_ids") or ())),
        "decline_reason": result.get("decline_reason"),
        "meta_skipped": bool(meta.get("skipped")),
    }


def observed_decision(verdict: Optional[Any]) -> Dict[str, Any]:
    """This council's decision, in the same four fields."""
    if verdict is None:
        return {
            "status": "<no verdict>",
            "selected_ids": [],
            "decline_reason": None,
            "meta_skipped": None,
        }
    return {
        "status": verdict.status,
        "selected_ids": sorted(set(verdict.selected_ids or ())),
        "decline_reason": verdict.decline_reason,
        "meta_skipped": bool(verdict.meta_skipped),
    }


def expected_difference_classes(
    record: Mapping[str, Any], result: Mapping[str, Any]
) -> List[str]:
    """Which permitted expected-difference predicates this row satisfies.

    These three are the ONLY excuses available; every other disagreement is
    a mismatch. Each is evaluated mechanically here, never assigned by hand.
    """
    classes: List[str] = []
    if result.get("status") == "error":
        classes.append("a_recorded_status_error")

    proposers = dict(result.get("proposers") or {})
    had_proposer_error = any("error" in reply for reply in proposers.values())
    if had_proposer_error and (result.get("critic") or result.get("meta")):
        classes.append("b_proposer_error_then_critic_or_meta")

    menu = {str(c["go_id"]) for c in record.get("candidates") or ()}
    for reply in proposers.values():
        if "error" in reply:
            continue
        ids = [str(p.get("go_id")) for p in reply.get("picked") or ()]
        if any(i not in menu for i in ids) or len(ids) != len(set(ids)):
            classes.append("c_off_menu_or_duplicate_pick")
            break
    return classes


def compare(
    inputs: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    verdicts: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """One comparison record per replayed row, in input-id order."""
    rows: List[Dict[str, Any]] = []
    for item_id in sorted(results, key=lambda i: int(i)):
        result = results[item_id]
        record = inputs[item_id]
        expected = recorded_decision(result)
        observed = observed_decision(verdicts.get(item_id))
        differing = [field for field in expected if expected[field] != observed[field]]
        rows.append(
            {
                "item_id": item_id,
                "recorded": expected,
                "observed": observed,
                "differing_fields": differing,
                "expected_difference_classes": expected_difference_classes(
                    record, result
                ),
                "agrees": not differing,
            }
        )
    return rows


def rule_only_decision(
    record: Mapping[str, Any], result: Mapping[str, Any], query: Any
) -> Dict[str, Any]:
    """What the pure decision RULE makes of one row's recorded replies.

    Calls the real :func:`~...engine.decide.decide` on
    :class:`SettledCall` rows built straight from the transcript. The ONLY
    thing this removes relative to :func:`run_replay` is the worker's
    answer-schema gate (``worker.py:966``), so the difference between the two
    numbers isolates that gate exactly.

    This is a DIAGNOSTIC. It never excuses a mismatch: the headline parity
    figure is always the one measured through the full worker.
    """
    from kbutillib.domains.ai.council.engine.decide import SettledCall, decide

    def settled(role: str, seat: str, reply: Mapping[str, Any]) -> Any:
        return SettledCall.from_call_row(
            {
                "role": role,
                "seat_name": seat,
                "status": "ok",
                "reply_parsed": dict(reply),
            },
            query,
        )

    proposers = dict(result.get("proposers") or {})
    proposer_calls = []
    for name in RECORDED_PROPOSERS:
        reply = proposers.get(name) or {}
        if "error" in reply:
            # A failed seat is a failed seat under the rule too; the gate is
            # not what made it fail, so it is reported as recorded.
            from kbutillib.domains.ai.council.types import CouncilError

            proposer_calls.append(
                SettledCall(
                    role="proposer",
                    seat_name=SEAT_OF[name],
                    status="error",
                    error=CouncilError(kind="invalid_output", message=reply["error"]),
                )
            )
            continue
        proposer_calls.append(settled("proposer", SEAT_OF[name], reply))

    critic = result.get("critic") or {}
    critic_call = settled("critic", SEAT_OF[RECORDED_CRITIC], critic) if critic else None

    meta = result.get("meta") or {}
    meta_call = None
    if meta and not meta.get("skipped"):
        served = {k: v for k, v in meta.items() if k not in RUNNER_ADDED_META_KEYS}
        meta_call = settled("meta", SEAT_OF[RECORDED_META], served)

    _what, verdict = decide(
        task_from_input(record), build_roster(), query, proposer_calls,
        critic_call, meta_call,
    )
    return observed_decision(verdict)


def diagnose(
    rows: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    store_url: str,
    also: Sequence[str] = (),
) -> Dict[str, Dict[str, Any]]:
    """Run :func:`rule_only_decision` over the rows that need explaining.

    Those are the rows that still MISMATCH, plus *also* -- the rows a repair
    carried (see :func:`normalised_rows`). The second group matters because
    the rule-only figure is the control on normalisation: the rule agreed on
    those rows before any repair existed, so a repair that changed a decision
    rather than merely admitting one shows up here as the rule-only column
    and the end-to-end column disagreeing.
    """
    from kbutillib.domains.ai.council.queries import QueryRegistry
    from kbutillib.domains.ai.council.store import open_store

    targets = [
        r["item_id"]
        for r in rows
        if not r["agrees"] and not r["expected_difference_classes"]
    ]
    targets.extend(item_id for item_id in also if item_id not in targets)
    targets.sort(key=int)
    if not targets:
        return {}

    store = open_store(store_url)
    store.init_schema()
    registry = QueryRegistry(store)
    registry.seed_builtins()
    query = registry.get("go_mf")

    out: Dict[str, Dict[str, Any]] = {}
    for item_id in targets:
        rule = rule_only_decision(inputs[item_id], results[item_id], query)
        out[item_id] = {
            "rule": rule,
            "agrees": rule == recorded_decision(results[item_id]),
        }
    return out


def schema_complaint(
    record: Mapping[str, Any], result: Mapping[str, Any], query: Any
) -> List[str]:
    """Which recorded replies of this row fail ``go_mf@1``'s answer schema."""
    from kbutillib.domains.ai.council.engine.worker import _validate_answer

    complaints: List[str] = []
    for name, reply in dict(result.get("proposers") or {}).items():
        if "error" in reply:
            continue
        bad = _validate_answer(reply, query.answer_schema("proposer"))
        if bad:
            complaints.append(f"proposer/{name}: {bad}")
    critic = result.get("critic") or {}
    if critic:
        bad = _validate_answer(critic, query.answer_schema("critic"))
        if bad:
            complaints.append(f"critic/{RECORDED_CRITIC}: {bad}")
    meta = result.get("meta") or {}
    if meta and not meta.get("skipped"):
        served = {k: v for k, v in meta.items() if k not in RUNNER_ADDED_META_KEYS}
        bad = _validate_answer(served, query.answer_schema("meta"))
        if bad:
            complaints.append(f"meta/{RECORDED_META}: {bad}")
    return complaints


def normalised_rows(verdicts: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``{item_id: {seat: [repair, ...]}}`` for every row a repair carried.

    Read straight off the verdicts the worker wrote, so the report says what
    the store says rather than what this script predicts.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for item_id in sorted(verdicts, key=int):
        verdict = verdicts[item_id]
        if getattr(verdict, "normalised", False):
            out[item_id] = {
                seat: [dict(entry) for entry in entries]
                for seat, entries in dict(verdict.repairs or {}).items()
            }
    return out


def failure_notes(verdict: Optional[Any]) -> List[str]:
    """The error messages a verdict carries, for the mismatch table."""
    if verdict is None:
        return ["no verdict was written"]
    return [
        f"{entry.get('stage')}/{entry.get('seat')}: "
        f"{entry.get('kind')}: {entry.get('message')}"
        for entry in (verdict.errors or ())
    ]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt(decision: Mapping[str, Any]) -> str:
    ids = ", ".join(decision["selected_ids"]) or "(none)"
    return (
        f"status={decision['status']} "
        f"decline={decision['decline_reason']} "
        f"meta_skipped={decision['meta_skipped']} "
        f"ids=[{ids}]"
    )


def render_report(
    rows: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    verdicts: Mapping[str, Any],
    diagnosis: Optional[Mapping[str, Mapping[str, Any]]] = None,
    complaints: Optional[Mapping[str, Sequence[str]]] = None,
    run_info: Optional[Mapping[str, str]] = None,
) -> str:
    """The parity report, as Markdown."""
    repaired = normalised_rows(verdicts)
    ok_rows = [r for r in rows if r["recorded"]["status"] == "ok"]
    excused = [r for r in rows if r["expected_difference_classes"]]
    excused_ids = {r["item_id"] for r in excused}
    scored = [r for r in ok_rows if r["item_id"] not in excused_ids]
    agreeing = [r for r in scored if r["agrees"]]
    mismatched = [r for r in scored if not r["agrees"]]

    prov = dict(manifest["provenance"])
    out: List[str] = []
    add = out.append

    add("# GO council V3 replay — parity report")
    add("")
    add(
        "Generated by `scripts/council/replay_go_v3.py`. Zero tokens: every "
        "model answer is a recorded one, served by `ReplayBackend` through "
        "the real `CouncilWorker`, the real `decide()`, a real `SqliteStore` "
        "and the shipped `go_mf@1`. No network call is made."
    )
    add("")

    # -- headline ---------------------------------------------------------
    add("## Headline")
    add("")
    add(f"- Recorded rows replayed: **{len(rows)}**")
    add(f"- Recorded `ok` rows: **{len(ok_rows)}**")
    add(
        f"- Rows excused by a permitted expected-difference predicate: "
        f"**{len(excused)}** ({', '.join(sorted(excused_ids, key=int)) or 'none'})"
    )
    add(f"- `ok` rows scored for parity: **{len(scored)}**")
    add(
        f"- **Agreement: {len(agreeing)}/{len(scored)}** "
        f"({100.0 * len(agreeing) / len(scored):.1f}%) on selected GO ids (as a "
        f"set), status, decline_reason and meta-skipped"
    )
    add(f"- Mismatches: **{len(mismatched)}**")
    add(
        f"- Rows whose agreement required an answer repair: "
        f"**{len(repaired)}** "
        f"({', '.join(sorted(repaired, key=int)) or 'none'})"
    )
    add("")
    add(
        "**The agreement figure is END TO END** -- measured through the "
        "shipped `CouncilWorker`, including its answer-schema gate, over ALL "
        "recorded `ok` rows outside the excused classes, not only the ten "
        "fixture rows."
    )
    add("")
    if run_info:
        add("Measured by this run:")
        add("")
        add(f"- command: `{run_info.get('command', '')}`")
        add(f"- host: `{run_info.get('host', '')}`")
        add(f"- finished (UTC): `{run_info.get('utc', '')}`")
        add(f"- interpreter: `{run_info.get('interpreter', '')}`")
        add("")

    # -- provenance -------------------------------------------------------
    add("## Provenance of the oracle")
    add("")
    add(f"**Generator: {prov['generator_verdict']}** — `{prov['generator_path']}`")
    add("")
    add(
        "Established cryptographically, not inferred. The V3 kit caches every "
        "reply under `sha256(model \\0 max_tokens \\0 system \\0 user \\0)` "
        "(`go_council.py:212`), so the prompts of the run that produced the "
        "oracle are recoverable by reconstruction and membership-testing "
        "against the `.cache.sqlite` sidecar."
    )
    add("")
    ev = dict(prov["evidence"])
    add("| reconstruction | cache keys found |")
    add("|---|---|")
    add(f"| V3 kit, proposer | {ev['v3_kit_proposer_keys']} |")
    add(f"| V3 kit, critic | {ev['v3_kit_critic_keys']} |")
    add(f"| V3 kit, meta | {ev['v3_kit_meta_keys']} |")
    add(f"| **V3 kit, total** | **{ev['v3_kit_total']}** |")
    add(f"| 09-18 live pilot, proposer | {ev['pilot_cross_check']} |")
    add("")
    add(f"The single proposer miss: {ev['the_single_miss']}")
    add("")
    add(
        f"**Consequence.** {prov['consequence']} The oracle was NOT produced "
        "by the 09-18 live pilot, whose proposer system prompt carries the "
        "candidate-provenance rules and whose candidate rendering differs."
    )
    add("")

    # -- expected differences ---------------------------------------------
    add("## Expected differences (listed, and NOT counted as failures)")
    add("")
    for key, entry in dict(manifest["expected_differences"]).items():
        if key == "note":
            continue
        ids = entry.get("input_ids") or []
        add(f"### `{key}` — {len(ids)} row(s): {ids or 'none'}")
        add("")
        add(f"Predicate: `{entry['predicate']}`")
        add("")
        if entry.get("note"):
            add(entry["note"])
            add("")

    if excused:
        add("How each excused row actually differed:")
        add("")
        add("| input id | classes | V3 recorded | this council |")
        add("|---|---|---|---|")
        for row in sorted(excused, key=lambda r: int(r["item_id"])):
            add(
                f"| {row['item_id']} | {', '.join(row['expected_difference_classes'])} "
                f"| {_fmt(row['recorded'])} | {_fmt(row['observed'])} |"
            )
        add("")
        add(
            "On the one error row the vote table also differs, exactly as the "
            "class (b) note predicts: V3 tallied the two proposers that "
            "answered and carried on to critic and meta, while this council "
            "stops at the failed proposer (`engine/decide.py` step 3) and "
            "writes an error verdict naming every seat. The recorded row and "
            "the replay agree that the item is an error; they disagree about "
            "how much work to do after that, which is the design difference."
        )
        add("")

    # -- mismatches -------------------------------------------------------
    add("## Mismatches")
    add("")
    if not mismatched:
        add("**None.** Every scored row agreed on all four semantic fields.")
        add("")
    else:
        add(
            f"**{len(mismatched)} row(s)** disagreed. Every one is listed, with "
            "its input id and both answers."
        )
        add("")
        add("| input id | fields | V3 recorded | this council | why |")
        add("|---|---|---|---|---|")
        for row in sorted(mismatched, key=lambda r: int(r["item_id"])):
            notes = "; ".join(failure_notes(verdicts.get(row["item_id"]))) or "\u2014"
            add(
                f"| {row['item_id']} | {', '.join(row['differing_fields'])} "
                f"| {_fmt(row['recorded'])} | {_fmt(row['observed'])} "
                f"| {notes} |"
            )
        add("")

    # -- normalisation ----------------------------------------------------
    add("## Normalisation: the eight replies the schema would refuse")
    add("")
    add(
        "`go_mf@1` declares an answer schema per role (`required`, plus "
        "`additionalProperties: false`) and the worker enforces it on every "
        "reply (`engine/worker.py` \u2192 `_validate_answer`). **Jose's V3 "
        "runner enforced no schema at all** \u2014 `go_council.py:162 "
        "parse_json` only requires that the text parse as JSON \u2014 so the "
        "transcript holds replies a `go_mf@1` run would have refused on "
        "arrival. Two defects, both in what the models returned live: "
        "`claudeopus45` omitted the required `no_functional_content` from "
        "seven meta replies, and `gemini35flash` returned `\"notes\": null` "
        "on one proposer reply where the schema says string."
    )
    add("")
    add(
        "`engine/normalise.py` repairs exactly those two defects BEFORE the "
        "gate and records every repair on the call row (`calls.normalised`) "
        "and on the verdict (`normalised`, plus a per-seat `repairs` list). "
        "The raw reply text is untouched. Nothing else is repaired: a reply "
        "missing a different required field is still `invalid_output`."
    )
    add("")

    if repaired:
        add(
            f"### The {len(repaired)} repaired rows, as the stored verdicts "
            "record them"
        )
        add("")
        add("| input id | seat | field | rule | original | repaired to |")
        add("|---|---|---|---|---|---|")
        for item_id in sorted(repaired, key=int):
            for seat in sorted(repaired[item_id]):
                for entry in repaired[item_id][seat]:
                    add(
                        f"| {item_id} | {seat} | `{entry.get('field')}` "
                        f"| `{entry.get('rule')}` "
                        f"| `{json.dumps(entry.get('original'))}` "
                        f"| `{json.dumps(entry.get('value'))}` |"
                    )
        add("")
        by_rule: Dict[str, List[str]] = {}
        for item_id, seats in repaired.items():
            for entries in seats.values():
                for entry in entries:
                    by_rule.setdefault(str(entry.get("rule")), []).append(item_id)
        add("| rule | rows |")
        add("|---|---|")
        for rule in sorted(by_rule):
            ids = sorted(set(by_rule[rule]), key=int)
            add(f"| `{rule}` | {len(ids)}: {', '.join(ids)} |")
        add("")
    else:
        add("No row needed a repair in this run.")
        add("")

    if complaints:
        add("### What the schema says about the RAW recorded reply")
        add("")
        add(
            "Measured by calling `_validate_answer` on the transcript "
            "untouched, so this is a statement about the recording and not "
            "about the repair."
        )
        add("")
        add("| input id | the recorded reply the schema refuses |")
        add("|---|---|")
        for item_id in sorted(complaints, key=int):
            for line in complaints[item_id]:
                add(f"| {item_id} | {line} |")
        add("")

    if diagnosis:
        agreeing_rule = [k for k, v in diagnosis.items() if v["agrees"]]
        add("### Rule only, with the gate removed \u2014 the control")
        add("")
        add(
            f"**The decision rule alone agrees on {len(agreeing_rule)}/"
            f"{len(diagnosis)} of these rows.** Measured, not asserted: "
            "`rule_only_decision()` calls the real `engine.decide()` on "
            "`SettledCall`s built from the same recorded replies, with the "
            "answer-schema gate as the only thing removed \u2014 and with no "
            "normalisation, since the rule reads a reply the gate never saw. "
            "It agreed on these rows BEFORE any repair existed, which is why "
            "it is kept now that it does: a repair that changed a DECISION "
            "rather than merely admitting one would show up as this column "
            "and the end-to-end column disagreeing."
        )
        add("")
        add("| input id | V3 recorded | rule only (gate removed) | end to end | agrees |")
        add("|---|---|---|---|---|")
        for item_id in sorted(diagnosis, key=int):
            row = next(r for r in rows if r["item_id"] == item_id)
            entry = diagnosis[item_id]
            add(
                f"| {item_id} | {_fmt(row['recorded'])} "
                f"| {_fmt(entry['rule'])} "
                f"| {_fmt(row['observed'])} "
                f"| {'yes' if entry['agrees'] else '**NO**'} |"
            )
        add("")
        add(
            "No change to `go_mf@1` follows from this. The schema is a port "
            "of what the V3 prompts specify, the worker is right to enforce "
            "it, and a live run requests structured output against that "
            "schema in the first place \u2014 the defect is in a transcript "
            "recorded without one."
        )
        add("")

    # -- the full table ---------------------------------------------------
    add("## Every replayed row")
    add("")
    add("| input id | agrees | repaired | V3 recorded | this council |")
    add("|---|---|---|---|---|")
    for row in rows:
        mark = "yes" if row["agrees"] else (
            "excused" if row["expected_difference_classes"] else "**NO**"
        )
        rules = sorted(
            {
                str(entry.get("rule"))
                for entries in repaired.get(row["item_id"], {}).values()
                for entry in entries
            }
        )
        rule_cell = ", ".join("`" + rule + "`" for rule in rules) or "\u2014"
        add(
            f"| {row['item_id']} | {mark} | {rule_cell} "
            f"| {_fmt(row['recorded'])} | {_fmt(row['observed'])} |"
        )
    add("")

    add("## Files read")
    add("")
    add(
        "Every digest below was re-measured by this run before anything else "
        "happened (`verify_manifest`), against the SHA-256 the manifest pins."
    )
    add("")
    add("| key | path | sha256 |")
    add("|---|---|---|")
    for key, entry in dict(manifest["files"]).items():
        add(f"| {key} | `{entry['path']}` | `{entry['sha256']}` |")
    add("")

    if run_info:
        add("## How this run was made")
        add("")
        add(f"- command: `{run_info.get('command', '')}`")
        add(f"- host: `{run_info.get('host', '')}`")
        add(f"- finished (UTC): `{run_info.get('utc', '')}`")
        add(f"- interpreter: `{run_info.get('interpreter', '')}`")
        add(f"- repository commit: `{run_info.get('commit', '')}`")
        add(f"- rows replayed: `{run_info.get('rows', '')}`")
        add("")
        add(
            "Zero tokens were spent: every reply came out of the transcript "
            "through `ReplayBackend`, and no backend that can reach a network "
            "was constructed."
        )
        add("")
    return "\n".join(out)


def print_table(rows: Sequence[Mapping[str, Any]]) -> None:
    """The same comparison, on stdout."""
    print(f"{'input_id':>9}  {'agree':<8} {'V3 recorded':<58} this council")
    print("-" * 150)
    for row in rows:
        mark = "yes" if row["agrees"] else (
            "excused" if row["expected_difference_classes"] else "NO"
        )
        print(
            f"{row['item_id']:>9}  {mark:<8} {_fmt(row['recorded']):<58} "
            f"{_fmt(row['observed'])}"
        )


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


def select_fixture_ids(results: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """The fixed row-selection rule, applied mechanically.

    The first ten ``ok`` rows by input id, extended in id order until at
    least one meta-skipped and one meta-run row are included.
    """
    ok = [
        i
        for i in sorted(results, key=lambda i: int(i))
        if results[i].get("status") == "ok"
    ]

    def skipped(item_id: str) -> bool:
        return bool((results[item_id].get("meta") or {}).get("skipped"))

    chosen = ok[:10]
    index = 10
    while index < len(ok) and not (
        any(skipped(i) for i in chosen) and any(not skipped(i) for i in chosen)
    ):
        chosen = ok[: index + 1]
        index += 1
    return chosen


def build_fixture(
    inputs: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    """A small, self-contained JSON fixture for the offline test."""
    chosen = select_fixture_ids(results)
    return {
        "description": (
            "Ten rows of Jose Faria's CONCORDIA V3 GO council, inputs plus "
            "recorded per-role replies plus the recorded decision, so the "
            "replay parity check runs offline on any machine. Extracted by "
            "scripts/council/replay_go_v3.py --emit-fixture."
        ),
        "source": {
            key: {"path": entry["path"], "sha256": entry["sha256"]}
            for key, entry in dict(manifest["files"]).items()
            if key in ("input_and_menu", "per_role_replies")
        },
        "selection_rule": dict(manifest["row_selection"])["rule"],
        "input_ids": [int(i) for i in chosen],
        "rows": [
            {"input": inputs[i], "result": results[i]} for i in chosen
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _by_id(records: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(r["input_id"]): dict(r) for r in records}


def _complaints_for(
    diagnosis: Mapping[str, Mapping[str, Any]],
    inputs: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    store_url: str,
) -> Dict[str, List[str]]:
    """The schema complaints for each diagnosed row, for the report."""
    if not diagnosis:
        return {}
    from kbutillib.domains.ai.council.queries import QueryRegistry
    from kbutillib.domains.ai.council.store import open_store

    store = open_store(store_url)
    store.init_schema()
    registry = QueryRegistry(store)
    registry.seed_builtins()
    query = registry.get("go_mf")
    out: Dict[str, List[str]] = {}
    for item_id in diagnosis:
        found = schema_complaint(inputs[item_id], results[item_id], query)
        if found:
            out[item_id] = found
    return out


def replay_rows(
    rows: Sequence[Mapping[str, Any]], store_url: str
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Replay ``[{"input":…, "result":…}, …]``; return (comparison, verdicts).

    This is the function the offline fixture test drives, so the test and the
    full /scratch run exercise ONE implementation rather than two that agree
    by inspection.
    """
    inputs = _by_id(r["input"] for r in rows)
    results = _by_id(r["result"] for r in rows)

    tasks = [task_from_input(inputs[i]) for i in sorted(inputs, key=lambda i: int(i))]
    table: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for item_id in results:
        table.update(replay_entries(results[item_id]))

    verdicts = run_replay(tasks, table, store_url)
    return compare(inputs, results, verdicts), verdicts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit", type=int, default=0, help="replay only the first N rows by input id"
    )
    parser.add_argument(
        "--report", type=Path, default=REPORT_PATH, help="where to write the Markdown report"
    )
    parser.add_argument(
        "--emit-fixture",
        type=Path,
        nargs="?",
        const=FIXTURE_PATH,
        default=None,
        help="also write the 10-row offline fixture",
    )
    parser.add_argument(
        "--no-report", action="store_true", help="print the table but write nothing"
    )
    args = parser.parse_args(argv)

    manifest = load_manifest()
    print(f"verifying manifest digests: {', '.join(verify_manifest(manifest))}")

    inputs = _by_id(read_jsonl(Path(manifest["files"]["input_and_menu"]["path"])))
    results = _by_id(read_jsonl(Path(manifest["files"]["per_role_replies"]["path"])))
    missing = set(results) - set(inputs)
    if missing:
        raise SystemExit(f"result rows with no input row: {sorted(missing, key=int)}")

    if args.emit_fixture is not None:
        fixture = build_fixture(inputs, results, manifest)
        args.emit_fixture.parent.mkdir(parents=True, exist_ok=True)
        args.emit_fixture.write_text(json.dumps(fixture, indent=1, sort_keys=False) + "\n")
        print(
            f"wrote {args.emit_fixture} "
            f"({len(fixture['rows'])} rows: {fixture['input_ids']})"
        )

    ordered = sorted(results, key=lambda i: int(i))
    if args.limit:
        ordered = ordered[: args.limit]
    rows_in = [{"input": inputs[i], "result": results[i]} for i in ordered]

    with tempfile.TemporaryDirectory(prefix="go-v3-replay-") as tmp:
        comparison, verdicts = replay_rows(rows_in, f"sqlite:///{tmp}/replay.sqlite")
        repaired = normalised_rows(verdicts)
        diagnosis = diagnose(
            comparison,
            inputs,
            results,
            f"sqlite:///{tmp}/diagnose.sqlite",
            also=sorted(repaired, key=int),
        )
        complaints = _complaints_for(diagnosis, inputs, results, f"sqlite:///{tmp}/q.sqlite")

    print_table(comparison)

    scored = [
        r
        for r in comparison
        if r["recorded"]["status"] == "ok" and not r["expected_difference_classes"]
    ]
    agreeing = [r for r in scored if r["agrees"]]
    print()
    print(f"agreement: {len(agreeing)}/{len(scored)} scored ok rows (end to end)")
    if repaired:
        print(
            f"normalised: {len(repaired)} row(s) needed an answer repair: "
            f"{', '.join(sorted(repaired, key=int))}"
        )
        for item_id in sorted(repaired, key=int):
            for seat, entries in sorted(repaired[item_id].items()):
                for entry in entries:
                    print(
                        f"  {item_id:>9}  {seat:<12} {entry.get('field')} "
                        f"[{entry.get('rule')}] -> {entry.get('value')!r}"
                    )

    if diagnosis:
        rule_ok = sum(1 for v in diagnosis.values() if v["agrees"])
        print(
            f"control: the decision rule alone agrees on {rule_ok}/"
            f"{len(diagnosis)} of the rows that needed a repair or still "
            "mismatch (the answer-schema gate is the whole difference)"
        )

    # The command as a reader would re-run it: this script's repo-relative
    # path plus the arguments it was actually given.
    invocation = " ".join(["python", "scripts/council/replay_go_v3.py", *sys.argv[1:]])
    run_info = {
        "command": invocation,
        "host": socket.getfqdn(),
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interpreter": f"{platform.python_implementation()} {platform.python_version()}",
        "commit": _repo_commit(),
        "rows": str(len(rows_in)),
    }

    if not args.no_report:
        report = render_report(
            comparison, manifest, verdicts, diagnosis, complaints, run_info
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report)
        print(f"wrote {args.report}")

    return 0 if len(agreeing) == len(scored) else 1


if __name__ == "__main__":
    sys.exit(main())
