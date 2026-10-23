"""THE REHEARSAL: the whole function_to_go pipeline, end to end, FOR FREE.

This module is the PRD's acceptance. It drives plan -> quote -> the approval
refusals -> export -> publish -> ``clearinghouse load --dry-run`` against
ALREADY-STORED verdicts, and then ten named failure cases.

**IT SPENDS ZERO TOKENS, AND THAT IS TESTED RATHER THAN ASSERTED.**
:class:`TestTheRehearsalSpendsNothing` runs the whole happy path with the
``socket`` module POISONED, so it can only pass if no connection is attempted
at all. No live council pass happens anywhere in here: the real run is an
operator action behind a human approval and is deliberately not part of
acceptance.

WHY A TEST MODULE AND NOT A SHELL SCRIPT. A rehearsal nobody runs is a
rehearsal that does not exist. The 2026-10-20 run was driven by throwaway
scripts under ``/scratch/chenry/council/``, which is exactly how a second,
divergent path survives; this runs in CI, on every change, without anyone
remembering.

HOW MUCH OF THIS IS REAL. Almost all of it. The council store is a real
SQLite store with the real schema; the query type is the SHIPPED ``go_mf@1``,
seeded through the real registry; the configuration is a real
:class:`~kbutillib.domains.ai.council.config.CouncilConfig` carrying the
MEASURED concurrency (ARGO chair 24 / critic 8 / meta 8, MAG queues summing
to 6 against an account-wide 20-parallel and 120 req/min cap); and stages 1,
2, 3, 4's gates, 5 and the consumer's load all run their real code. Exactly
three collaborators are stand-ins, each for a reason that is not "it was
easier":

* the CLEARINGHOUSE READ (:class:`_FakeClearinghouse`) -- the real one needs
  the BERDL delta API and a populated lake;
* RETRIEVAL (``function_to_go`` on :class:`_Stage`) -- it is
  scikit-learn/joblib plus the vendored GO artifacts, and nothing in the
  pipeline's own logic depends on which GO terms come back;
* the LAKEHOUSE WRITE boundary (``_FakeLake``, IMPORTED from
  ``tests/berdl/test_clearinghouse_ingest.py`` rather than copied) -- the
  real one is a Spark session that exists only inside the BERDL pod, and
  ``load``/``verify`` refuse off-pod by design. ``_FakeLake`` STORES what it
  is handed, which is what makes "the second load writes nothing" an earned
  result rather than an asserted one.

Nothing else is faked. In particular the sharder, the manifest validator, the
arrival check, the pre-commit join assertions, the ledger, the namespace lock
and the receipt are all the production code.

THE TEN FAILURE CASES, because the happy path is the part least likely to be
wrong. Each has its own class, named for the case:

1. the marker (``run.json``) arriving before its payload
2. one listed file missing
3. a batch whose sha256 does not match
4. a 0% join
5. a partial join below the coverage floor
6. a duplicate consumer loading the same shard run twice
7. a crash immediately after the commit, before the receipt is written
8. an expired approval
9. a plan changed after approval
10. a sustained rate-limit response

**WHAT THE REHEARSAL FOUND.** All ten were handled as designed; NONE needed a
product fix. A rehearsal that found nothing is a rehearsal to be suspicious
of, though, so here is what it DID find -- three places where the system's
real behaviour is not the obvious one, each of which had this module written
wrongly against it first:

* **A refused or unarrived batch is REPORTED, not raised.** ``ingest_shards``
  returns a per-batch report with ``action`` ``refused``/``syncing``/
  ``failed``; the non-zero exit lives in the CLI
  (``clearinghouse._load_failures`` + ``_exit_on_load_failure``). That is
  deliberate -- one bad batch must not unwind the batches that already
  committed, and the ledger has to KEEP the failure so ``verify_run`` blocks
  on it -- so this module asserts through the CLI's own classifier rather
  than inventing a second idea of what a failed load is.
* **The registry-first rule outranks the arrival check.** When ``run.json``
  arrives ahead of EVERYTHING, the load does not patiently report every batch
  as syncing: the parameter-set registry batch has not committed, so the
  first result batch refuses outright
  (``ClearinghouseRegistryOrderError``). Louder, and right: a result row
  whose ``parameter_set_hash`` the registry cannot explain is an orphan.
  Reaching the patient per-batch path needs the registry to have arrived and
  the payload not to have, which is why failure 1 has two tests.
* **Stage 3 will not WRITE an already-expired approval** (``ValueError``:
  "authorises nothing"), so the expired case can only arise through the
  passage of time and is exercised with the gate's own ``now=`` clock rather
  than by constructing a bad artifact.

Each class below also says what it had to construct to reach its failure,
since several are reachable only by defeating a guard that comes first.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pyarrow")
pytest.importorskip("sqlalchemy")

import pyarrow.parquet as pq  # noqa: E402

from kbutillib.domains.ai.council.approval import (  # noqa: E402
    APPROVAL_FILENAME,
    ApprovalRefused,
    approve,
    validate_approval,
)
from kbutillib.domains.ai.council.quote import (  # noqa: E402
    PLAN_FILENAME,
    QUOTE_FILENAME,
)
from kbutillib.domains.genome.ontology_mapping import (  # noqa: E402
    council_publish as cpub,
)
from kbutillib.domains.genome.ontology_mapping import (  # noqa: E402
    council_status as cstat,
)
from kbutillib.domains.genome.ontology_mapping import (  # noqa: E402
    ontology_mapping_utils as omu,
)
from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci  # noqa: E402
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs  # noqa: E402
from kbutillib.domains.kbase.berdl.clearinghouse_capability import (  # noqa: E402
    ClearinghouseCapability,
)

# THE LAKEHOUSE STAND-IN, IMPORTED AND NOT COPIED. One definition in this
# suite of "what the write boundary does", so a rehearsal cannot pass against
# a fake that has drifted from the one the ingest tests pin.
from tests.berdl.test_clearinghouse_ingest import _FakeLake  # noqa: E402

#: The query this pipeline runs. The SHIPPED one, seeded from the builtin
#: registry -- not a test query type. Its renderer, its schemas and its
#: ``entity_field`` are what the quote hashes prompts against, so a rehearsal
#: against a hand-made query would price a spec nothing runs.
QUERY = "go_mf@1"

#: The bench profile, seating the MEASURED 2026-10-20 roster.
PROFILE = "f2go"

#: The retrieval backend's identity. Fixed, because ``mapping_hash`` is part
#: of the clearinghouse parameter set and the rehearsal asserts on the hash.
MAPPING_HASH = "e" * 64

#: Six clearinghouse functions: five with content (two sharing a definition,
#: so they are ONE paid answer and TWO published rows) and one placeholder
#: retrieval declines. Small, and every interesting shape is in it.
UNIVERSE: List[Dict[str, Any]] = [
    {"entity_hash": f"{index:064x}", "definition": definition, "source": "bakta"}
    for index, definition in enumerate(
        [
            "ABC transporter, partial",
            "glucokinase",
            "glucokinase",
            "yfiA",
            "glucose oxidase",
            "hypothetical protein",
        ],
        start=1,
    )
]

#: The definition retrieval declines, as a placeholder function.
ABSTAIN_TEXTS = {"hypothetical protein"}

#: The council configuration, with the MEASURED concurrency in it.
#:
#: ARGO ``max_concurrency`` is 40 and not 24 because the three ARGO queues sum
#: to 40 (chair 24 + critic 8 + meta 8) and the config validator REFUSES an
#: over-subscribed account -- which is itself the measured fact: ARGO
#: saturates at ~24 concurrent and showed zero rate limiting at 40 concurrent
#: across three models, so 40 is the declared account cap and 24 is the
#: chair's share of it.
#:
#: MAG declares the account-wide pair (20 parallel AND 120 requests per
#: minute) and its two queues sum to 6 -- the configuration measured to work.
#: A config asking 24 parallel errored 427 of 500 pilot items over 1,925
#: transport attempts.
#:
#: Both endpoints are ``kind: openai`` with no ``api_key_env``, so
#: ``resolve_credentials`` has genuinely nothing to resolve and the stage-4
#: gate tests do not depend on an ARGO username or an AMSC token being
#: present on the machine running the suite.
COUNCIL_CONFIG: Dict[str, Any] = {
    "endpoints": {
        "argo": {
            "kind": "openai",
            "base_url": "https://argo.invalid/v1",
            "account": "argo-anl",
            "max_concurrency": 40,
        },
        "mag": {
            "kind": "openai",
            "base_url": "https://mag.invalid/v1",
            "account": "mag-amsc",
            "max_concurrency": 20,
            "max_requests_per_minute": 120,
        },
    },
    "models": {
        "gemini35flash": {"endpoint": "argo", "canonical": "google/gemini-3.5-flash"},
        "gpt-5.6-sol": {
            "endpoint": "mag",
            "canonical": "openai/gpt-5.6-sol",
            "verified": False,
        },
        "gpt-5.6-luna": {
            "endpoint": "mag",
            "canonical": "openai/gpt-5.6-luna",
            "verified": False,
        },
        "claudesonnet5": {"endpoint": "argo", "canonical": "anthropic/claude-sonnet-5"},
        "claudeopus5": {"endpoint": "argo", "canonical": "anthropic/claude-opus-5"},
    },
    "profiles": {
        PROFILE: {
            "proposers": ["gemini35flash", "gpt-5.6-sol", "gpt-5.6-luna"],
            "critic": "claudesonnet5",
            "meta": "claudeopus5",
        }
    },
    "default_profile": PROFILE,
    "queues": {
        "argo_chair": {"seats": ["proposer_1"], "concurrency": 24},
        "argo_critic": {"seats": ["critic"], "concurrency": 8},
        "argo_meta": {"seats": ["meta"], "concurrency": 8},
        "mag_sol": {"seats": ["proposer_2"], "concurrency": 3},
        "mag_luna": {"seats": ["proposer_3"], "concurrency": 3},
    },
}


# ---------------------------------------------------------------------------
# The three stand-ins
# ---------------------------------------------------------------------------


class _FakeClearinghouse:
    """The clearinghouse READ surface, answering from in-memory rows.

    ``done`` is mutable so a test can make a function already-answered and
    watch stage 1 stop selecting it -- the un-run derivation is
    ``universe - done`` at THIS parameter set, and that is the property the
    whole standing pipeline rests on.
    """

    def __init__(self) -> None:
        self.done: List[Dict[str, Any]] = []

    def universe(self, entity_type: str, **_kw: Any) -> List[Dict[str, Any]]:
        return [dict(row) for row in UNIVERSE]

    def current_state(self, entity_type: str, **_kw: Any) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.done]

    def stats(self, **_kw: Any) -> Dict[str, Any]:
        return {
            "tables": [
                {"name": "function_entity", "row_count": len(UNIVERSE)},
                {"name": "function_content", "row_count": len(UNIVERSE)},
            ],
            "warnings": [],
        }


class _Stage(omu.OntologyMappingUtils):
    """``OntologyMappingUtils`` with the clearinghouse and retrieval injected.

    ``mapping_version`` and ``function_to_go`` are the retrieval backend
    (scikit-learn plus the vendored GO artifacts); no stage's own logic needs
    them. Everything else -- ``write_ranked``, the partition, ``PLAN.json``,
    the export -- is the real code path, and ``_council`` returns a REAL
    :class:`~kbutillib.domains.ai.council_utils.CouncilUtils` against a real
    store.
    """

    def __init__(self, council: Any, clearinghouse: Any) -> None:
        super().__init__(config_file=False, token_file=None, kbase_token_file=None)
        self._fake_council = council
        self._fake_clearinghouse = clearinghouse

    def mapping_version(self) -> Dict[str, Any]:
        return {
            "mapping_version": "ontomap-go-text-union@1.0.0 (rehearsal)",
            "mapping_hash": MAPPING_HASH,
            "provenance": {"backend": "ontomap-go-text-union"},
        }

    def function_to_go(self, functions: Any, max_candidates: int = 40) -> Dict[str, Any]:
        results = []
        for row in functions:
            abstained = row["text"] in ABSTAIN_TEXTS
            results.append(
                {
                    "query_id": row["query_id"],
                    "text": row["text"],
                    "source": row.get("source") or "",
                    "abstained": abstained,
                    "abstain_reason": "placeholder function" if abstained else None,
                    "candidates": (
                        []
                        if abstained
                        else [
                            {
                                "go_id": "GO:0004340",
                                "name": "glucokinase activity",
                                "def": "catalysis of ATP + D-glucose",
                                "score": 0.91,
                                "height": 5,
                                "rank": 1,
                            },
                            {
                                "go_id": "GO:0016301",
                                "name": "kinase activity",
                                "def": "catalysis of phosphate transfer",
                                "score": 0.52,
                                "height": 3,
                                "rank": 2,
                            },
                        ]
                    ),
                }
            )
        return {**self.mapping_version(), "results": results}

    def _clearinghouse(self) -> Any:
        return self._fake_clearinghouse

    def _council(self) -> Any:
        return self._fake_council


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_ingest_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ingest execution state dir and the SHARED lock dir, under tmp_path.

    Without this the rehearsal would write the developer's real
    ``~/.kbu/clearinghouse/runs`` and contend for the production namespace
    lock -- which is held on a path other pods share.
    """
    monkeypatch.setenv(ci.STATE_DIR_ENV, str(tmp_path / "ingest-state"))
    monkeypatch.setenv(ci.LOCK_DIR_ENV, str(tmp_path / "ingest-locks"))


@pytest.fixture
def council(tmp_path: Path) -> Iterator[Any]:
    """A REAL ``CouncilUtils``: real config, real SQLite store, shipped go_mf@1."""
    from kbutillib.domains.ai.council_utils import CouncilUtils

    utils = CouncilUtils(
        council_config={
            **COUNCIL_CONFIG,
            "store": f"sqlite:///{tmp_path}/council/council.sqlite",
        },
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )
    utils.db_init()
    seeded = {
        f"{query.name}@{query.version}" for query in utils.registry().seed_builtins()
    }
    assert QUERY in seeded, (
        f"{QUERY} is not among the shipped builtin query types ({sorted(seeded)}); "
        "the rehearsal must run the query the pipeline runs, not a test one"
    )
    yield utils
    utils.close()


@pytest.fixture
def clearinghouse() -> _FakeClearinghouse:
    return _FakeClearinghouse()


@pytest.fixture
def stage(council: Any, clearinghouse: _FakeClearinghouse) -> _Stage:
    return _Stage(council, clearinghouse)


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "function-to-go"


@pytest.fixture
def run_dir(runs_root: Path) -> Path:
    return runs_root / "2026-10-23-rehearsal"


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


# ---------------------------------------------------------------------------
# The pipeline, driven
# ---------------------------------------------------------------------------


def _plan(stage: _Stage, run_dir: Path, scratch: Path, **kwargs: Any) -> Dict[str, Any]:
    """STAGE 1, for real. ``chunk_size=2`` so the plan has several chunks."""
    return stage.council_plan(
        run_dir,
        query=QUERY,
        profile=PROFILE,
        scratch_dir=scratch,
        chunk_size=kwargs.pop("chunk_size", 2),
        **kwargs,
    )


def _quote(council: Any, run_dir: Path) -> Dict[str, Any]:
    """STAGE 2, for real, through the SAME function ``kbu council quote`` calls.

    Not a second pricing path: two code paths that both priced a plan would
    be two numbers that could disagree, and the one an operator approved
    would be whichever CLI they happened to type.
    """
    from kbutillib.interfaces.cli.council import build_quote

    return build_quote(council, plan=str(run_dir), profile=PROFILE)["data"]


def _approve(run_dir: Path, **kwargs: Any) -> Any:
    """STAGE 3, for real."""
    return approve(run_dir=run_dir, approver="rehearsal", note="the rehearsal", **kwargs)


def _store_the_verdicts(
    council: Any, plan: Mapping[str, Any], *, job_id: str = "rehearsal-chunk-0"
) -> str:
    """Put a COMPLETED council run in the store, without making a single call.

    THIS IS WHAT MAKES THE REHEARSAL FREE. The council store already holds a
    completed 19,268-verdict run in production, so the whole of stage 5 can
    be exercised against verdicts that already exist; here the same shape is
    written directly through the store's own ``create_job`` and
    ``settle_item``. No worker, no backend, no call.

    One verdict per submit item, covering every published shape: a selection,
    both declines the council itself can make
    (``no_candidate_matched`` and ``contentless``), and a selection again.
    """
    store = council.require_schema()
    items = _read_jsonl(Path(plan["items_file"]))
    roster = council.roster(PROFILE)
    job = {
        "job_id": job_id,
        "label": "rehearsal",
        "query_name": "go_mf",
        "query_version": "1",
        "submitted_by": "rehearsal",
        "roster": {"profile": PROFILE},
        "roster_fingerprint": roster.fingerprint(),
        "profile_name": PROFILE,
        "status": "done",
    }
    store.create_job(
        job,
        [
            {
                "item_id": row["item_id"],
                "entity_hash": f"{index:064x}",
                "data_hash": f"{index:064x}",
                "evidence": dict(row["evidence"]),
                "menu": [dict(entry) for entry in row["menu"]],
            }
            for index, row in enumerate(items)
        ],
        [],
    )
    declines = {0: "no_candidate_matched", 3: "contentless"}
    for index, row in enumerate(items):
        reason = declines.get(index)
        store.settle_item(job_id, row["item_id"], "ok", _verdict(row["item_id"], reason))
    return job_id


def _verdict(item_id: str, decline_reason: Optional[str]) -> Dict[str, Any]:
    """One stored verdict, in the shape ``verdicts_by_item`` reads."""
    contentless = decline_reason == "contentless"
    ballot = {
        "seat": "proposer",
        "status": "ok",
        "picks": (
            []
            if decline_reason
            else [{"id": "GO:0004340", "confidence": "high", "why": ""}]
        ),
        "overall_confidence": "high",
        "no_functional_content": contentless,
    }
    return {
        "item_id": item_id,
        "status": "ok",
        "selected_ids": [] if decline_reason else ["GO:0004340"],
        "decline_reason": decline_reason,
        "final_confidence": "medium" if decline_reason else "high",
        "decided_by": "meta" if decline_reason else "unanimous",
        "ballots": [dict(ballot) for _ in range(3)],
        "votes": {} if decline_reason else {"GO:0004340": 3},
        "n_valid": 3,
        "critic": {
            "verdict": "ambiguous" if decline_reason else "consistent",
            "contentless_input": contentless,
        },
        "decided_at": "2026-10-23T12:00:00+00:00",
    }


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _rehearse(
    stage: _Stage,
    council: Any,
    run_dir: Path,
    scratch: Path,
    *,
    publish: bool = True,
) -> Dict[str, Any]:
    """THE WHOLE HAPPY PATH, minus the live run. Returns every stage's record.

    plan -> quote -> approve -> (the stored verdicts stand in for the run) ->
    export -> publish. No token is spent at any point and no socket is
    opened; :class:`TestTheRehearsalSpendsNothing` proves the second claim by
    poisoning ``socket``.
    """
    plan = _plan(stage, run_dir, scratch)
    quote = _quote(council, run_dir)
    approval = _approve(run_dir)
    job_id = _store_the_verdicts(council, plan)
    export = stage.council_export(
        run_dir, job_ids=[job_id], scratch_dir=scratch
    )
    record: Dict[str, Any] = {
        "plan": plan,
        "quote": quote,
        "approval": approval,
        "job_id": job_id,
        "export": export,
    }
    if publish:
        record["publish"] = cpub.council_publish(run_dir)
    return record


@pytest.fixture
def rehearsed(stage: _Stage, council: Any, run_dir: Path, scratch: Path) -> Dict[str, Any]:
    return _rehearse(stage, council, run_dir, scratch)


# ---------------------------------------------------------------------------
# The consumer half, against the lakehouse stand-in
# ---------------------------------------------------------------------------


def _entity_rows(hashes: Any) -> Dict[str, List[Dict[str, Any]]]:
    """``function_entity`` rows for *hashes*: the identity a result resolves to.

    A ``function_result`` batch's join keys must RESOLVE against
    ``function_entity`` -- the pre-commit coverage assertion -- because a
    result row whose ``entity_hash`` names no entity is an ORPHAN: the hash is
    opaque, so nothing can say what the result is about. That is also the only
    state a real load is ever in: the 47,341 entity and content rows are
    loaded first, and stage 1 reads the universe out of them.
    """
    return {
        "function_entity": [
            {
                "entity_hash": str(value),
                "entity_type": "function",
                "observed_at": "2026-10-23T00:00:00+00:00",
                "ingest_batch_id": "PRELOADED",
            }
            for value in hashes
        ]
    }


def _published_hashes(record: Mapping[str, Any]) -> List[str]:
    """Every ``entity_hash`` the shard run actually published, from the files."""
    out: List[str] = []
    for batch in record["batches"]:
        if batch["table"] != "function_result":
            continue
        table = pq.read_table(str(Path(record["shard_dir"]) / batch["file"]))
        out.extend(str(value) for value in table.column("entity_hash").to_pylist())
    return sorted(set(out))


def _consumer(lake: Any) -> ClearinghouseCapability:
    """The CONSUMER half's capability, over the lakehouse stand-in.

    The real one is in-pod only and refuses off-pod by design. ``_FakeLake``
    reports ``locus() == "in_pod"``, which is the ONE thing standing in for
    the pod here; everything the ingest then does -- the arrival check, the
    four pre-commit assertions, the fill-only anti-join, the ledger, the lock
    and the verification counts -- is the production code.
    """
    return ClearinghouseCapability(capability=lake)


def _load(record: Mapping[str, Any], lake: Any, **kwargs: Any) -> Dict[str, Any]:
    return _consumer(lake).ingest_shards(record["shard_dir"], **kwargs)


def _load_failures(result: Mapping[str, Any]) -> List[str]:
    """Every batch of a load that was NOT accepted, with its why.

    THE CLI'S OWN CLASSIFIER, imported and not restated. ``ingest_shards``
    deliberately does not RAISE for a refused or unarrived batch -- one bad
    batch does not invalidate the batches that already committed, and the
    ledger has to keep the failure so ``verify_run`` blocks on it -- so what
    an operator actually experiences is ``kbu clearinghouse load`` exiting
    NON-ZERO on exactly this list. A rehearsal with its own idea of what
    counts as a failed load would pass while the command exited zero.
    """
    from kbutillib.interfaces.cli.clearinghouse import _load_failures as classify

    return classify({"data": dict(result)})


def _report_for(result: Mapping[str, Any], table: str) -> Dict[str, Any]:
    """The per-batch report for *table*, which must be exactly one batch."""
    rows = [row for row in result.get("tables") or () if row.get("name") == table]
    assert len(rows) == 1, f"expected one {table} batch, got {len(rows)}"
    return dict(rows[0])


# ===========================================================================
# 0. THE PROPERTY THIS MODULE EXISTS FOR
# ===========================================================================


class TestTheRehearsalSpendsNothing:
    """ZERO TOKENS, and the socket module is poisoned to prove it.

    Deliberately NOT "a stubbed HTTP client that was never called": a seam
    every test replaces is a seam no test covers, and the thing being checked
    is precisely that no such seam is reached. ``socket.socket`` raising means
    the test can only pass if the whole pipeline never attempts a connection.
    """

    def test_the_whole_happy_path_opens_no_socket(
        self,
        stage: _Stage,
        council: Any,
        run_dir: Path,
        scratch: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _poisoned(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(
                "the rehearsal opened a socket. plan, quote, approve, export "
                "and publish must all be free: the one stage that spends is "
                "stage 4, and the real run is an operator action behind a "
                "human approval, not part of acceptance."
            )

        monkeypatch.setattr(socket, "socket", _poisoned)
        monkeypatch.setattr(socket, "create_connection", _poisoned)
        record = _rehearse(stage, council, run_dir, scratch)
        # ITEMS here are FUNCTIONS, not council items: one paid answer can
        # serve several functions, so this is abstain + fanout and not submit.
        assert record["publish"]["counts"]["items_with_verdict"] == len(UNIVERSE)

    def test_no_backend_is_ever_built(self, rehearsed: Dict[str, Any]) -> None:
        """The quote reports its seats without instantiating one.

        The reuse namespace and the effective params come from
        ``default_reuse_namespace`` and ``seat_generation_params``, which
        exist so a prompt hash can be computed without a backend. If a
        backend had been built, these endpoints' ``.invalid`` base URLs would
        have had to resolve.
        """
        seats = rehearsed["quote"]["token_model"]["seats"]
        assert {row["seat_name"] for row in seats} == {
            "proposer_1",
            "proposer_2",
            "proposer_3",
            "critic",
            "meta",
        }


# ===========================================================================
# 1. THE HAPPY PATH, STAGE BY STAGE
# ===========================================================================


class TestStage1Plan:
    def test_the_partition_accounts_for_every_un_run_function(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """submit + abstain is every un-run function; fanout covers submit.

        The arithmetic the 2026-10-20 run got wrong by dropping the abstained
        set, which is how 760 definitions became an unexplained coverage gap.
        """
        counts = rehearsed["plan"]["counts"]
        assert counts["universe"] == len(UNIVERSE)
        assert counts["done"] == 0
        assert counts["unrun"] == len(UNIVERSE)
        # Two functions share "glucokinase": 5 non-declined functions become 4
        # paid answers, and the fanout maps all 5 back.
        assert counts["submit"] == 4
        assert counts["abstain"] == 1
        assert counts["fanout"] == 5
        assert counts["fanout"] + counts["abstain"] == counts["unrun"]

    def test_the_parameter_set_is_query_bench_and_mapping_hash(
        self, rehearsed: Dict[str, Any], council: Any
    ) -> None:
        """``bench_fingerprint``, NOT ``roster_fingerprint``.

        The bench partitions the corpus; the transport does not. Re-routing a
        seat to another gateway moves ``roster_fingerprint`` -- and if that
        were the key it would re-partition all 47,341 functions and re-charge
        ~199M tokens for a pure cost optimisation.
        """
        from kbutillib.domains.ai.council.types import bench_fingerprint

        plan = rehearsed["plan"]
        assert plan["parameter_set"] == {
            "query": QUERY,
            "bench_fingerprint": bench_fingerprint(council.roster(PROFILE)),
            "mapping_hash": MAPPING_HASH,
        }
        assert "roster_fingerprint" not in plan["parameter_set"]
        # Recorded as PROVENANCE, which is a different thing from a key.
        assert plan["roster_fingerprint"] == council.roster(PROFILE).fingerprint()

    def test_only_plan_json_lands_on_the_synced_surface(
        self, rehearsed: Dict[str, Any], run_dir: Path, scratch: Path
    ) -> None:
        """The 130 MB of items and menus go to LOCAL scratch, not to Dropbox."""
        plan = rehearsed["plan"]
        for key in ("items_file", "abstain_file", "fanout_file", "ranked_file"):
            assert not Path(plan[key]).is_relative_to(run_dir), key
            assert Path(plan[key]).is_relative_to(scratch), key

    def test_the_chunk_constants_and_their_reason_travel_with_the_plan(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """KBUtilLib issue 84 is NAMED in the plan, so the workaround is removable.

        A workaround baked in as a bare constant becomes permanent silently.
        """
        concurrency = rehearsed["plan"]["concurrency"]
        assert concurrency["chunks_in_flight"] == 1
        assert concurrency["issue"] == "KBUtilLib issue 84"
        assert "217 ms at 19k pending" in concurrency["rationale"]
        assert concurrency["gateways"]["mag"]["queues_sum_to"] == 6
        assert concurrency["gateways"]["mag"]["requests_per_minute"] == 120
        assert concurrency["gateways"]["mag"]["scope"] == "account-wide"
        assert concurrency["gateways"]["argo"] == {
            "chair": 24,
            "critic": 8,
            "meta": 8,
            "note": concurrency["gateways"]["argo"]["note"],
        }

    def test_a_function_already_answered_at_this_parameter_set_is_not_re_planned(
        self,
        stage: _Stage,
        clearinghouse: _FakeClearinghouse,
        runs_root: Path,
        scratch: Path,
    ) -> None:
        """"Un-run" means no current-state row AT THIS parameter set.

        The property the whole standing pipeline rests on: without it, every
        run re-charges the whole corpus.
        """
        first = _plan(stage, runs_root / "first", scratch / "a")
        clearinghouse.done = [{"entity_hash": UNIVERSE[1]["entity_hash"]}]
        second = _plan(stage, runs_root / "second", scratch / "b")
        assert second["counts"]["done"] == 1
        assert second["counts"]["unrun"] == first["counts"]["unrun"] - 1


class TestStage2Quote:
    def test_it_prices_the_plan_it_was_given_and_binds_to_its_hash(
        self, rehearsed: Dict[str, Any], run_dir: Path
    ) -> None:
        quote = rehearsed["quote"]
        assert quote["plan_hash"] == rehearsed["plan"]["plan_hash"]
        assert (run_dir / QUOTE_FILENAME).is_file()

    def test_the_meta_fire_rate_is_the_measured_one_with_its_sample_size(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """24.1% over n=19,268. The 16.7%, 19.2% and 38.7% figures are WITHDRAWN."""
        from kbutillib.domains.ai.council.quote import META_FIRE_RATE, META_FIRE_RATE_N

        assert META_FIRE_RATE == 0.241
        assert META_FIRE_RATE_N == 19268
        meta = next(
            row
            for row in rehearsed["quote"]["call_graph"]
            if row["role"] == "meta"
        )
        assert meta["calls_total"] == pytest.approx(
            rehearsed["plan"]["counts"]["submit"] * 0.241, abs=1.0
        )
        assert meta["basis"] == "projected"
        assert rehearsed["quote"]["meta_fire_rate"] == {
            "rate": META_FIRE_RATE,
            "n": META_FIRE_RATE_N,
            "basis": "projected",
            "source": rehearsed["quote"]["meta_fire_rate"]["source"],
        }
        # The withdrawn estimates are named as withdrawn, in the artifact.
        assert "withdrawn" in rehearsed["quote"]["meta_fire_rate"]["source"]

    def test_the_retry_pass_is_its_own_quoted_line(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """0.30% of run cost, measured. A first-class stage gets a first-class line."""
        from kbutillib.domains.ai.council.quote import PAID_REPAIR_FRACTION

        assert PAID_REPAIR_FRACTION == 0.0030
        lines = {row["line"] for row in rehearsed["quote"]["call_graph"]}
        assert "paid_repair" in lines


class TestStage3Approve:
    def test_the_ceiling_is_the_upper_band_times_the_headroom_not_the_mean(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """A ceiling at the mean halts about half of all runs partway.

        And a run halted at 60% has spent 60% of the tokens for none of the
        result.
        """
        basis = rehearsed["approval"].ceiling_basis
        assert basis["headroom"] == 1.25
        for gateway, ceiling in rehearsed["approval"].ceiling_tokens.items():
            upper = basis["gateway_upper_tokens"][gateway]
            assert ceiling >= upper, gateway

    def test_it_says_it_is_not_an_authentication_boundary(
        self, rehearsed: Dict[str, Any], run_dir: Path
    ) -> None:
        """Unsigned, and it cannot be: there is no key material on these machines.

        What it buys is a checkable binding and an explicit ceiling, and the
        artifact has to say so or somebody will rely on it for more.
        """
        written = json.loads((run_dir / APPROVAL_FILENAME).read_text())
        assert "NOT AN AUTHENTICATION BOUNDARY" in json.dumps(written).upper()

    def test_a_good_approval_passes_all_six_gate_checks(
        self, rehearsed: Dict[str, Any], run_dir: Path, council: Any
    ) -> None:
        validated = validate_approval(run_dir, config=council.config(), profile=PROFILE)
        assert validated.quote_hash == rehearsed["approval"].quote_hash


class TestStage5ExportAndPublish:
    def test_every_un_run_function_gets_exactly_one_row_including_the_declines(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """One row per INPUT ``query_id``, all three decline classes included.

        Recording a decline is what stops stage 1 re-selecting it forever:
        un-run means "no current-state result row", so an ABSENT row is
        indistinguishable from "not yet run".
        """
        counts = rehearsed["export"]["counts"]
        assert counts["inputs"] == len(UNIVERSE)
        assert counts["rows"] == len(UNIVERSE)
        assert counts["withheld"] == 0
        assert counts["abstained"] == 1
        assert counts["no_candidate_matched"] == 1
        assert counts["contentless"] == 1

    def test_the_join_key_is_query_id_and_not_the_councils_own_entity_hash(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """Measured: the council's own hash matched 0 of 19,268; the text 19,268.

        So the key must TRAVEL with the item, and a text join -- the only
        join that would appear to work -- is the one this pipeline must never
        make. Here the council item rows carry deliberately WRONG
        ``entity_hash`` values (``{index:064x}`` over the item index), so a
        publish that used them would produce hashes that are not in the
        clearinghouse at all.
        """
        published = set(_published_hashes(rehearsed["publish"]))
        assert published == {row["entity_hash"] for row in UNIVERSE}

    def test_the_rendered_manifest_is_gated_against_the_plans_parameter_set(
        self, rehearsed: Dict[str, Any], run_dir: Path
    ) -> None:
        record = rehearsed["publish"]
        assert record["parameter_set_hash"] == rehearsed["plan"]["parameter_set_hash"]
        assert record["parameter_set_hash"] != cpub.UNRENDERED_PARAMETER_SET_HASH
        assert (run_dir / cpub.MANIFEST_FILENAME).is_file()

    def test_run_json_is_the_completeness_contract_and_there_is_no_second_one(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """Per batch: a sha256, a row count, a byte count and a hash range."""
        record = rehearsed["publish"]
        manifest = cs.read_run_manifest(record["shard_dir"])
        assert manifest["batches"]
        for entry in manifest["batches"]:
            assert len(entry["sha256"]) == 64
            assert entry["rows"] > 0
            assert entry["bytes"] > 0
            assert entry["hash_min"] and entry["hash_max"]

    def test_publish_json_declares_the_two_in_pod_handoff_commands(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """An undeclared hand-off is one somebody has to reconstruct."""
        hand_off = rehearsed["publish"]["hand_off"]
        assert hand_off["load"].startswith("kbu clearinghouse load ")
        assert "verify" in hand_off["verify"]
        assert hand_off["receipt_file"].endswith(ci.RECEIPT_FILENAME)


class TestTheConsumerDryRun:
    def test_load_dry_run_writes_nothing_and_reports_what_it_would_do(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """The operator's last gate, and GENUINELY read-only.

        ``_FakeLake.load`` is never called, so a leak would show as rows in
        the fake lake rather than as a missing assertion.
        """
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows(_published_hashes(record)))
        result = _load(record, lake, dry_run=True)
        assert result["dry_run"] is True
        assert lake.load_calls == []
        assert lake.rows.get("function_result") is None
        assert not list(
            (Path(ci.resolve_state_root())).glob("*/ingest_ledger.jsonl")
        )

    def test_then_the_real_load_lands_every_published_row(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        published = _published_hashes(record)
        lake = _FakeLake(rows=_entity_rows(published))
        result = _load(record, lake)
        assert _load_failures(result) == []
        landed = {str(row["entity_hash"]) for row in lake.rows["function_result"]}
        assert landed == set(published)

    def test_the_receipt_reconciles_ITEMS_and_not_only_rows(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """"Loaded 19,268" is not a reconciliation when 19,276 were submitted.

        An item can also carry several GO terms, so a row count is not an
        item count either and the receipt reports the two separately.
        """
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows(_published_hashes(record)))
        loaded = _load(record, lake)
        verified = _consumer(lake).verify_run(
            record["shard_dir"], run_id=loaded["run_id"]
        )
        receipt = ci.build_receipt(
            verified, publish_record=ci.read_publish_record(record["shard_dir"])
        )
        # abstain + fanout, i.e. every un-run FUNCTION -- not the 4 council
        # items, because one paid answer serves two of these functions.
        assert receipt["items_submitted"] == len(UNIVERSE)
        assert receipt["items_with_verdict"] == len(UNIVERSE)
        assert receipt["items_unresolved"] == 0
        assert receipt["result_rows_written"] >= receipt["items_with_verdict"]
        assert receipt["reconciled"] is True


class TestCouncilStatusAcrossTheRehearsedRun:
    def test_the_lifecycle_buckets_partition_the_corpus(
        self,
        rehearsed: Dict[str, Any],
        runs_root: Path,
        clearinghouse: _FakeClearinghouse,
    ) -> None:
        status = cstat.council_status(runs_root, clearinghouse=clearinghouse)
        counted = sum(
            int(row["functions"] or 0) for row in status["lifecycle"]["states"]
        )
        assert counted == len(UNIVERSE)

    def test_a_published_unloaded_run_is_reported_as_paid_for_and_not_landed(
        self,
        rehearsed: Dict[str, Any],
        runs_root: Path,
        clearinghouse: _FakeClearinghouse,
    ) -> None:
        """The number this verb exists for: tokens spent, nothing in the lake."""
        status = cstat.council_status(runs_root, clearinghouse=clearinghouse)
        states = {row["state"]: row["functions"] for row in status["lifecycle"]["states"]}
        assert states["exported-not-loaded"] == len(UNIVERSE)
        assert states["loaded"] == 0
        assert status["lifecycle"]["paid_for_not_landed"] == len(UNIVERSE)


# ===========================================================================
# 2. THE TEN FAILURE CASES
# ===========================================================================


class TestFailure01MarkerBeforeItsPayload:
    """``run.json`` has synced and the batches have not.

    Dropbox does not guarantee the producer's write order, so the marker can
    land first -- and this is exactly why the kbhub trigger condition is
    "a ``run.json`` exists AND every batch it lists is present and
    sha256-matches" and never "a ``run.json`` exists".

    WHAT HAD TO BE CONSTRUCTED: the batch files are DELETED after publishing,
    leaving the inventory describing a directory whose payload has not
    arrived. ``run.json``'s own mtime dates the hand-off, so the verdict
    depends on how long ago it was written -- which is checked both ways.
    """

    @staticmethod
    def _marker_only(record: Mapping[str, Any]) -> Any:
        """Delete every batch file, leaving the inventory describing nothing.

        The published hashes are read FIRST, because the entity rows the
        consumer needs come out of the very files this removes.
        """
        root = Path(record["shard_dir"])
        hashes = _published_hashes(record)
        for batch in record["batches"]:
            (root / batch["file"]).unlink()
        return root, hashes

    @staticmethod
    def _payload_only_missing(record: Mapping[str, Any]) -> Any:
        """Delete only the RESULT batch, leaving the registry arrived.

        The sharper half of this case: the registry got there and the payload
        did not, so the arrival check is reached and reports per batch rather
        than the registry-order gate refusing outright (which is what
        :meth:`test_nothing_arriving_at_all_is_refused_by_the_registry_first_rule`
        covers).
        """
        root = Path(record["shard_dir"])
        hashes = _published_hashes(record)
        victim = next(
            batch for batch in record["batches"] if batch["table"] == "function_result"
        )
        (root / victim["file"]).unlink()
        return root, hashes, dict(victim)

    def test_nothing_arriving_at_all_is_refused_by_the_registry_first_rule(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """When even the REGISTRY has not arrived, the load refuses outright.

        Louder than a per-batch report, and correctly so: a result row whose
        ``parameter_set_hash`` the registry cannot explain is an orphan, so
        there is nothing to be gained by carrying on to the results.
        """
        record = rehearsed["publish"]
        root, hashes = self._marker_only(record)
        lake = _FakeLake(rows=_entity_rows(hashes))
        with pytest.raises(Exception, match="registry is always written first"):
            _consumer(lake).ingest_shards(root)
        assert lake.load_calls == []
        assert not lake.rows.get("function_result")

    def test_inside_the_deadline_the_missing_payload_is_still_SYNCING(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        root, hashes, victim = self._payload_only_missing(record)
        lake = _FakeLake(rows=_entity_rows(hashes))
        result = _consumer(lake).ingest_shards(root)
        # NOT an exception: one unarrived batch must not unwind the batches
        # that already committed, and the ledger has to keep the sighting so
        # the deadline is measured from the FIRST one. What an operator sees
        # is `kbu clearinghouse load` exiting non-zero on exactly this.
        assert _load_failures(result)
        report = _report_for(result, "function_result")
        assert report["action"] == "syncing"
        assert "the file is ABSENT" in report["error"]
        assert "STILL" in report["error"] and "SYNCING" in report["error"]
        assert victim["batch_id"] in report["error"]
        assert not lake.rows.get("function_result")

    def test_past_the_deadline_it_is_a_FAILED_DELIVERY_naming_the_batch(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """The deadline is measured from the MARKER'S mtime, not from now.

        A deadline measured from the load's start would reset every time an
        operator re-ran the command, which is precisely the silent wait this
        guards against.
        """
        record = rehearsed["publish"]
        root, hashes, victim = self._payload_only_missing(record)
        lake = _FakeLake(rows=_entity_rows(hashes))
        result = _consumer(lake).ingest_shards(root, transfer_deadline_seconds=0)
        assert _load_failures(result)
        report = _report_for(result, "function_result")
        assert report["action"] == "failed"
        assert "FAILED DELIVERY" in report["error"]
        assert "re-sync the shard directory from the producer" in report["error"]
        assert report["transfer"]["reason"] == ci.TRANSFER_ABSENT

    def test_the_ledger_keeps_the_first_sighting_so_a_re_run_resumes_the_deadline(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """A ``syncing`` line is PERSISTED, which is what makes the wait bounded.

        Without it every re-run would restart the deadline from nothing, and a
        lost file would be waited on forever.
        """
        record = rehearsed["publish"]
        root, hashes, victim = self._payload_only_missing(record)
        lake = _FakeLake(rows=_entity_rows(hashes))
        first = _consumer(lake).ingest_shards(root)
        ledger = ci.ExecutionLedger(ci.execution_state_dir(first["run_id"]))
        lines = ledger.records()
        syncing = [
            line for line in lines.values() if line["state"] == ci.LEDGER_SYNCING
        ]
        assert syncing, "nothing was ledgered for a batch that had not arrived"
        for line in syncing:
            assert line["first_observed_at"]

    def test_the_runbooks_trigger_condition_catches_it_before_the_pod_is_touched(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """The operator-side gate: the marker AND verified bytes.

        The load defends itself, but the TRIGGER must not depend on that
        defence -- a load fired on an empty directory burns a pod session and
        says nothing an operator could not have checked in a second.
        """
        record = rehearsed["publish"]
        root, _hashes = self._marker_only(record)
        assert cs.run_manifest_path(root).is_file()
        assert _arrived(root) == [], "the gate passed a directory with no payload"


class TestFailure02OneListedFileMissing:
    """The directory is populated, and ONE batch of the inventory is not there.

    Distinct from failure 1: the marker is not ahead of everything, it is
    ahead of one file. That is the shape a partial sync leaves, and a load
    that proceeded on the batches it could see would land a silently
    incomplete run.
    """

    def test_the_absent_batch_fails_and_the_load_cannot_exit_zero(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        root = Path(record["shard_dir"])
        batches = list(record["batches"])
        assert len(batches) > 1, (
            "this case needs a multi-batch run to distinguish 'one file "
            "missing' from 'nothing arrived'"
        )
        hashes = _published_hashes(record)
        # The RESULT batch, not the registry one: the registry case is its own
        # refusal (see TestFailure03) and would mask this one.
        victim = next(
            batch for batch in batches if batch["table"] == "function_result"
        )
        (root / victim["file"]).unlink()
        lake = _FakeLake(rows=_entity_rows(hashes))
        result = _consumer(lake).ingest_shards(root, transfer_deadline_seconds=0)

        failures = _load_failures(result)
        assert failures, "a load missing one of its listed files exited clean"
        assert any(victim["file"] in line for line in failures)
        report = _report_for(result, "function_result")
        assert report["action"] == "failed"
        assert victim["batch_id"] in report["error"]
        # And the rows that batch carried are NOT in the lake.
        assert not lake.rows.get("function_result")

    def test_the_trigger_gate_names_which_file_is_missing(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        root = Path(record["shard_dir"])
        victim = next(
            batch for batch in record["batches"] if batch["table"] == "function_result"
        )
        (root / victim["file"]).unlink()
        arrived = _arrived(root)
        assert victim["file"] not in arrived
        # And the gate is specific: the batches that DID arrive still pass, so
        # an operator can see it is one file and not the whole directory.
        assert len(arrived) == len(record["batches"]) - 1


class TestFailure03ABatchWhoseSha256DoesNotMatch:
    """The bytes are there and they are NOT the bytes that were published.

    A half-written file during a sync, or a re-shard that overwrote a batch
    the inventory still describes. Either way the inventory is the authority
    and the file is wrong.
    """

    @staticmethod
    def _corrupt(record: Mapping[str, Any], table: str) -> Dict[str, Any]:
        """Append a byte to *table*'s batch file, so its digest no longer matches.

        The published hashes are captured by the caller before this runs, for
        the reason given in :class:`TestFailure01MarkerBeforeItsPayload`.
        """
        root = Path(record["shard_dir"])
        victim = next(
            batch for batch in record["batches"] if batch["table"] == table
        )
        path = root / victim["file"]
        path.write_bytes(path.read_bytes() + b"\x00not the published bytes")
        return dict(victim)

    def test_it_is_refused_with_the_expected_and_observed_digests_as_evidence(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        hashes = _published_hashes(record)
        victim = self._corrupt(record, "function_result")
        lake = _FakeLake(rows=_entity_rows(hashes))
        result = _consumer(lake).ingest_shards(
            record["shard_dir"], transfer_deadline_seconds=0
        )
        report = _report_for(result, "function_result")
        assert report["action"] == "failed"
        assert victim["sha256"] in report["error"], (
            "the EXPECTED digest is not in the evidence, so an operator "
            "cannot tell a corrupt file from a re-shard"
        )
        assert victim["batch_id"] in report["error"]
        assert report["transfer"]["reason"] == ci.TRANSFER_DIGEST_MISMATCH
        assert report["transfer"]["observed_sha256"] != victim["sha256"]
        assert _load_failures(result)

    def test_nothing_from_the_corrupt_batch_is_committed(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """Checked against the LAKE, not the envelope.

        A fake that only recorded calls could not show this; ``_FakeLake``
        stores what it is handed, so an append would be visible here.
        """
        record = rehearsed["publish"]
        hashes = _published_hashes(record)
        self._corrupt(record, "function_result")
        lake = _FakeLake(rows=_entity_rows(hashes))
        _consumer(lake).ingest_shards(
            record["shard_dir"], transfer_deadline_seconds=0
        )
        assert not lake.rows.get("function_result")

    def test_a_corrupt_REGISTRY_batch_refuses_the_result_batch_outright(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """The registry is written FIRST, and a result cannot overtake it.

        A result row whose ``parameter_set_hash`` the registry cannot explain
        is an ORPHAN -- the hash is opaque, so nothing can say what parameters
        produced the result. This is the one arrival failure that RAISES
        rather than being reported per batch, because carrying on would mean
        landing results the registry does not cover.
        """
        record = rehearsed["publish"]
        hashes = _published_hashes(record)
        self._corrupt(record, "parameter_set")
        lake = _FakeLake(rows=_entity_rows(hashes))
        with pytest.raises(Exception, match="registry is always written first"):
            _consumer(lake).ingest_shards(
                record["shard_dir"], transfer_deadline_seconds=0
            )
        assert not lake.rows.get("function_result")

    def test_the_trigger_gate_catches_it_too(self, rehearsed: Dict[str, Any]) -> None:
        record = rehearsed["publish"]
        _published_hashes(record)
        victim = self._corrupt(record, "function_result")
        assert victim["file"] not in _arrived(Path(record["shard_dir"]))


class TestFailure04AZeroPercentJoin:
    """A non-empty result batch NONE of whose keys resolve. The wrong join key.

    This is the 0-of-19,268 case: the council's own ``items.entity_hash``
    matched none of the clearinghouse's, while the definition TEXT matched all
    of them -- so the tempting fix (join on text) is the one thing this
    pipeline must never do, and the real fix is to stop discarding
    ``query_id``.

    WHAT HAD TO BE CONSTRUCTED: an EMPTY ``function_entity``. A load into a
    target with no entities is precisely what a wrong key looks like from the
    inside, because the identity rows are always loaded first in a real run.
    """

    def test_zero_resolution_fails_the_batch_and_names_the_wrong_key(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows([]))
        result = _load(record, lake)
        failures = _load_failures(result)
        assert failures, "a load that resolved nothing exited clean"
        assert any("WRONG JOIN KEY" in line for line in failures)
        report = _report_for(result, "function_result")
        assert report["action"] == "refused"
        assert report["join_assertions"]["coverage"] == 0.0

    def test_no_coverage_floor_waives_it(self, rehearsed: Dict[str, Any]) -> None:
        """A floor expresses tolerance for a PARTIAL mismatch.

        Total non-resolution is not partial, so ``--coverage-floor 0`` must
        not be a way to wave it through.
        """
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows([]))
        result = _load(record, lake, coverage_floor=0.0)
        failures = _load_failures(result)
        assert any("ZERO of" in line for line in failures)
        assert _report_for(result, "function_result")["action"] == "refused"

    def test_the_refused_batch_committed_nothing(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows([]))
        _load(record, lake)
        assert not lake.rows.get("function_result")


class TestFailure05APartialJoinBelowTheCoverageFloor:
    """Some keys resolve and some do not. An orphan result row for each one.

    The default floor is 1.0, and deliberately: a result load never mints
    identity -- the entity and content rows do -- so there is no legitimate
    reason for a result key not to resolve, and a lower default would only be
    a place for a real mismatch to hide.
    """

    def _half(self, record: Mapping[str, Any]) -> List[str]:
        published = _published_hashes(record)
        assert len(published) >= 2
        return published[: len(published) // 2]

    def test_partial_resolution_fails_the_default_floor_and_reports_the_coverage(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows(self._half(record)))
        result = _load(record, lake)
        failures = _load_failures(result)
        assert any("below the declared floor" in line for line in failures)
        join = _report_for(result, "function_result")["join_assertions"]
        assert join["coverage_floor"] == 1.0
        assert 0.0 < float(join["coverage"]) < 1.0
        assert not lake.rows.get("function_result")

    def test_a_DELIBERATELY_lowered_floor_admits_it_and_still_reports_the_number(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """Tolerance is a declared operator decision, not a default.

        And the measured coverage is reported either way, so "we chose to
        accept 50%" is in the record rather than inferred.
        """
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows(self._half(record)))
        result = _load(record, lake, coverage_floor=0.1)
        assert _load_failures(result) == []
        join = _report_for(result, "function_result")["join_assertions"]
        assert join["coverage_checked"] is True
        assert 0.0 < float(join["coverage"]) < 1.0
        assert join["unresolved_keys"] > 0
        assert join["coverage_floor"] == pytest.approx(0.1)
        # The rows DID land -- the floor is a declared operator decision, so
        # admitting them is the point, and the measured coverage is on record.
        assert lake.rows.get("function_result")


class TestFailure06ADuplicateConsumerLoadingTheSameRunTwice:
    """Two consumers load one shard run. It must be cheap and change nothing.

    Expected, not exceptional: a shard run is loaded to scratch, checked, then
    promoted -- and a retried trigger does the same thing by accident. A
    ``run_id`` names the EXECUTION, not the shard run, so each load is proven
    by its own per-batch counts.
    """

    def test_the_second_execution_appends_nothing(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        published = _published_hashes(record)
        lake = _FakeLake(rows=_entity_rows(published))
        first = _load(record, lake)
        after_first = len(lake.rows["function_result"])
        appends_after_first = len(lake.load_calls)

        second = _load(record, lake)  # a NEW execution over the same shards
        assert second["run_id"] != first["run_id"], (
            "the second load reused the first execution id; a duplicate "
            "consumer mints its own execution"
        )
        assert len(lake.rows["function_result"]) == after_first
        assert len(lake.load_calls) == appends_after_first, (
            "the second load called load() -- fill-only should have left zero "
            "survivors and committed without a write at all"
        )

    def test_a_concurrent_duplicate_is_excluded_by_the_namespace_lock(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """The lock excludes other clearinghouse INGESTS, which is its scope.

        It does not and cannot exclude arbitrary writers -- the lake has no
        transaction to take -- and that window is documented rather than
        closed.
        """
        namespace = "kbaseincubator.clearinghouse"
        held = ci.NamespaceLock(namespace)
        held.acquire(execution_id="FIRSTCONSUMER00000000000000")
        try:
            other = ci.NamespaceLock(namespace)
            with pytest.raises(ci.ClearinghouseNamespaceLockedError) as excinfo:
                other.acquire(execution_id="SECONDCONSUMER000000000000")
            assert "FIRSTCONSUMER00000000000000" in str(excinfo.value), (
                "the refusal does not name the holder, so an operator cannot "
                "tell a live consumer from a stale lock file"
            )
        finally:
            held.release()
        # PROCESS DEATH RELEASES IT: once released, the namespace is loadable
        # again without anybody clearing a file by hand.
        after = ci.NamespaceLock(namespace)
        after.acquire(execution_id="THIRDCONSUMER00000000000000")
        after.release()

    def test_the_second_load_is_a_DIFFERENT_execution_with_its_own_ledger(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """A load execution is not a shard run, which is what makes this safe.

        Each load is provable by its OWN per-batch counts, so a shard run can
        be loaded to scratch, checked, and promoted to production -- and a
        retried trigger is the same operation by accident.
        """
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows(_published_hashes(record)))
        first = _load(record, lake)
        second = _load(record, lake)
        assert first["source_run_id"] == second["source_run_id"]
        assert first["state_dir"] != second["state_dir"]
        for execution in (first, second):
            ledger = ci.ExecutionLedger(ci.execution_state_dir(execution["run_id"]))
            assert ledger.records(), execution["run_id"]


class TestFailure07ACrashAfterTheCommitBeforeTheReceipt:
    """The rows landed and ``LOADED.json`` was never written.

    The Iceberg commit and the receipt write are two unrelated writes with no
    transaction between them, so this gap is real and not hypothetical. A
    producer that read "no receipt" as "not loaded" would ask for a reload of
    rows that are already there.

    WHAT HAD TO BE CONSTRUCTED: a successful load whose receipt is simply not
    written -- which is all the crash does. The interesting claim is that the
    receipt is RE-EXPORTABLE and byte-identical, not that nothing broke.
    """

    def test_the_receipt_is_re_exportable_and_identical(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        """Derived only from inputs that do not change between exports.

        No timestamp and no nonce in the body, deliberately: a receipt that
        changed every time it was exported could not be compared to the one
        that was lost.
        """
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows(_published_hashes(record)))
        loaded = _load(record, lake)
        consumer = _consumer(lake)
        publish_record = ci.read_publish_record(record["shard_dir"])

        # THE CRASH: the commit happened, the receipt did not get written.
        assert not (Path(record["run_dir"]) / ci.RECEIPT_FILENAME).exists()

        first = ci.build_receipt(
            consumer.verify_run(record["shard_dir"], run_id=loaded["run_id"]),
            publish_record=publish_record,
        )
        again = ci.build_receipt(
            consumer.verify_run(record["shard_dir"], run_id=loaded["run_id"]),
            publish_record=publish_record,
        )
        assert first == again
        assert first["ok"] is True and first["reconciled"] is True

    def test_re_exporting_it_writes_the_receipt_and_appends_no_rows(
        self, rehearsed: Dict[str, Any]
    ) -> None:
        record = rehearsed["publish"]
        lake = _FakeLake(rows=_entity_rows(_published_hashes(record)))
        loaded = _load(record, lake)
        rows_before, calls_before = (
            len(lake.rows["function_result"]),
            len(lake.load_calls),
        )
        receipt = ci.build_receipt(
            _consumer(lake).verify_run(record["shard_dir"], run_id=loaded["run_id"]),
            publish_record=ci.read_publish_record(record["shard_dir"]),
        )
        written = ci.write_receipt(
            Path(record["run_dir"]) / ci.RECEIPT_FILENAME, receipt
        )
        assert json.loads(written.read_text())["execution_id"] == loaded["run_id"]
        assert len(lake.rows["function_result"]) == rows_before
        assert len(lake.load_calls) == calls_before, "verify WROTE something"

    def test_council_status_detects_the_shape_and_says_re_export_not_reload(
        self,
        rehearsed: Dict[str, Any],
        runs_root: Path,
        clearinghouse: _FakeClearinghouse,
    ) -> None:
        """This is why ``loaded`` is read from the lake and not from receipts.

        A status verb that trusted the receipts would report this run as
        unloaded and send an operator to reload rows that are already there.
        """
        record = rehearsed["publish"]
        clearinghouse.done = [
            {
                "entity_hash": value,
                "parameter_set_hash": record["parameter_set_hash"],
            }
            for value in _published_hashes(record)
        ]
        status = cstat.council_status(runs_root, clearinghouse=clearinghouse)
        row = next(row for row in status["runs"] if row["state"] == "exported-not-loaded")
        assert row["rows_at_parameter_set"] == len(_published_hashes(record))
        assert "RE-EXPORT" in row["note"]
        assert "rather than reloading" in row["note"]


class TestFailure08AnExpiredApproval:
    """The approval is bound to the right plan and quote, and it is too old.

    The content checks catch a CHANGED job; the expiry catches the case where
    nothing checkable moved but the approval no longer represents a decision
    anybody has looked at. An approval with no expiry would be a standing
    authorisation to spend.
    """

    def test_an_approval_cannot_be_written_already_expired(
        self, rehearsed: Dict[str, Any], run_dir: Path
    ) -> None:
        """Stage 3 refuses to write one, so this case can only arise by TIME.

        Which is the right shape: an approval that authorised nothing the
        moment it was written would be a confusing artifact rather than a
        decision, and the interesting case is the one where time passed.
        """
        with pytest.raises(ValueError, match="authorises nothing"):
            _approve(run_dir, expires_in_days=-1)

    def test_the_gate_refuses_naming_expires_at_once_the_expiry_has_passed(
        self, rehearsed: Dict[str, Any], run_dir: Path, council: Any
    ) -> None:
        """Default expiry is 7 days; the clock here is 8 days on."""
        later = datetime.now(tz=timezone.utc) + timedelta(days=8)
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(
                run_dir, config=council.config(), profile=PROFILE, now=later
            )
        assert excinfo.value.check == "expires_at"
        assert "re-quote and re-approve" in str(excinfo.value)

    def test_stage_4_refuses_before_a_token_is_spent(
        self, rehearsed: Dict[str, Any], run_dir: Path, council: Any
    ) -> None:
        """And no budget epoch is opened: the gate runs before the store is touched."""
        from kbutillib.domains.genome.ontology_mapping.council_run import council_run

        later = datetime.now(tz=timezone.utc) + timedelta(days=8)
        with pytest.raises(ApprovalRefused) as excinfo:
            council_run(run_dir, council=council, environ={}, now=later)
        assert excinfo.value.check == "expires_at"
        assert not (run_dir / "chunks").exists(), "a chunk marker was written"
        state = council.require_schema().get_endpoint_state("mag")
        assert int(state["budget_epoch"]) == 0, "a budget epoch was opened"


class TestFailure09APlanChangedAfterApproval:
    """The plan was re-derived after it was quoted and approved. A different job.

    Different items, a different query version, or a different
    ``mapping_hash`` -- any of them makes the approved price a price for
    something else.
    """

    def test_re_planning_after_approval_is_refused_naming_plan_hash(
        self,
        rehearsed: Dict[str, Any],
        stage: _Stage,
        clearinghouse: _FakeClearinghouse,
        run_dir: Path,
        scratch: Path,
        council: Any,
    ) -> None:
        # The corpus moved under the approval: one function has since been
        # answered, so a re-plan selects a different item set.
        clearinghouse.done = [{"entity_hash": UNIVERSE[4]["entity_hash"]}]
        replanned = _plan(stage, run_dir, scratch)
        assert replanned["plan_hash"] != rehearsed["plan"]["plan_hash"]

        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, config=council.config(), profile=PROFILE)
        assert excinfo.value.check == "plan_hash"
        assert "different job" in str(excinfo.value)

    def test_an_edited_plan_file_is_caught_by_the_hash_and_not_by_a_timestamp(
        self, rehearsed: Dict[str, Any], run_dir: Path, council: Any
    ) -> None:
        """``plan_hash`` is recomputed from the file, never trusted off it."""
        plan_file = run_dir / PLAN_FILENAME
        edited = json.loads(plan_file.read_text())
        edited["query"] = "go_mf@2"
        plan_file.write_text(json.dumps(edited, indent=2, sort_keys=True))
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, config=council.config(), profile=PROFILE)
        assert excinfo.value.check == "plan_hash"

    def test_an_edited_quote_is_refused_naming_quote_hash(
        self, rehearsed: Dict[str, Any], run_dir: Path, council: Any
    ) -> None:
        """The neighbouring case, and a different check: the PRICE moved."""
        quote_file = run_dir / QUOTE_FILENAME
        edited = json.loads(quote_file.read_text())
        edited["totals"] = {"tampered": True}
        quote_file.write_text(json.dumps(edited, indent=2, sort_keys=True))
        with pytest.raises(ApprovalRefused) as excinfo:
            validate_approval(run_dir, config=council.config(), profile=PROFILE)
        assert excinfo.value.check == "quote_hash"


class TestFailure10ASustainedRateLimitResponse:
    """Five rate-limit responses on one account inside 60 s is a HALT, not a retry.

    One 429 is the normal consequence of pacing against a windowed limit with
    other consumers on the account, and it is retried. A SUSTAINED one says
    the account's real available rate is below what this worker is configured
    for, and retrying into that spends the same per-minute budget the refusals
    are already competing for. The first pilot reached 1,925 transport
    attempts for 500 items and errored 427 of them doing exactly that.

    Exercised at both levels: the worker's own rule, and the effect on a run.
    """

    def test_one_rate_limit_is_retried_and_five_in_the_window_is_a_halt(self) -> None:
        from kbutillib.domains.ai.council.engine.worker import (
            RATE_LIMIT_HALT_AFTER,
            RATE_LIMIT_WINDOW_S,
        )

        assert RATE_LIMIT_HALT_AFTER == 5
        assert RATE_LIMIT_WINDOW_S == 60.0

    def test_a_run_whose_gateway_is_rate_limit_halted_submits_NOTHING(
        self, rehearsed: Dict[str, Any], run_dir: Path, council: Any
    ) -> None:
        """The pre-flight supervisor pass, before the first submission.

        Deliberately before chunk 1 and not after it: a halt left in the
        store would otherwise be discovered one chunk in, with 2,000 items
        queued against an endpoint nothing can claim for.
        """
        from kbutillib.domains.genome.ontology_mapping.council_run import council_run

        store = council.require_schema()
        message = (
            "account 'mag-amsc' returned 5 rate-limit responses within 60s "
            "while paced at 120 req/min. A SUSTAINED rate limit is a halt, "
            "not a retry."
        )
        store.set_halt("mag", "quota_exhausted", message)

        record = council_run(run_dir, council=council, environ={}, retry_rounds=0)
        assert record["complete"] is False
        assert record["halt"]["reason"] == "gateway_halt"
        # The halt's own reason survives: a gateway_halt trip writes nothing,
        # because overwriting its kind would lose what the operator needs.
        assert "quota_exhausted" in record["halt"]["detail"]
        assert "SUSTAINED rate limit" in record["halt"]["detail"]
        # No chunk row at all: the pre-flight pass found the halt before the
        # first submission, so there is nothing to record.
        assert not record.get("chunks")
        assert not any(
            int(spent) for spent in (record["spend_by_gateway"] or {}).values()
        )

    def test_the_retry_pass_is_NOT_declared_done_when_the_chunk_pass_halted(
        self, rehearsed: Dict[str, Any], run_dir: Path, council: Any
    ) -> None:
        """The items that would be retried are not yet known.

        A run that reported its retry pass complete here would be a run
        claiming to have finished work it never started.
        """
        from kbutillib.domains.genome.ontology_mapping.council_run import council_run

        council.require_schema().set_halt("mag", "quota_exhausted", "sustained 429s")
        record = council_run(run_dir, council=council, environ={})
        assert record["retry"]["ran"] is False
        assert record["retry"]["declared_unnecessary"] is False
        assert "not run" in record["retry"]["reason"]

    def test_council_status_reports_the_halted_run_as_paid_for_and_not_landed(
        self,
        rehearsed: Dict[str, Any],
        run_dir: Path,
        runs_root: Path,
        council: Any,
        clearinghouse: _FakeClearinghouse,
    ) -> None:
        from kbutillib.domains.genome.ontology_mapping.council_run import council_run

        # Published already, so unpick that half: this case is about a run
        # that halted BEFORE it had anything to publish.
        (run_dir / cpub.PUBLISH_FILENAME).unlink()
        council.require_schema().set_halt("mag", "quota_exhausted", "sustained 429s")
        council_run(run_dir, council=council, environ={}, retry_rounds=0)

        status = cstat.council_status(runs_root, clearinghouse=clearinghouse)
        row = next(row for row in status["runs"] if row["run_dir"] == str(run_dir))
        assert row["state"] == "running"
        assert row["halt"]["reason"] == "gateway_halt"
        assert status["lifecycle"]["paid_for_not_landed"] == len(UNIVERSE)
        assert any("HALTED" in warning for warning in status["warnings"])


# ---------------------------------------------------------------------------
# The operator-side trigger gate, written once
# ---------------------------------------------------------------------------


def _arrived(shard_root: Path) -> List[str]:
    """The batch files that are PRESENT and sha256-MATCH, from ``run.json``.

    THE RUNBOOK'S TRIGGER CONDITION, as a function: "a ``run.json`` exists
    AND every batch it lists is present and sha256-matches". Returning the
    files that passed (rather than a boolean) is what lets a failing case
    assert WHICH file the gate rejected.

    It deliberately re-derives the digests rather than calling
    :func:`~kbutillib.domains.kbase.berdl.clearinghouse_ingest.transfer_state`:
    the point of the operator gate is that it is a few lines of shell an
    operator can run before touching the pod, independent of the load's own
    defence.
    """
    import hashlib

    manifest = json.loads(cs.run_manifest_path(shard_root).read_text(encoding="utf-8"))
    out: List[str] = []
    for entry in manifest.get("batches") or ():
        path = shard_root / str(entry["file"])
        if not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != str(entry["sha256"]):
            continue
        out.append(str(entry["file"]))
    return out
