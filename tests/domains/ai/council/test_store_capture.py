"""The capture columns of KBUtilLib#92 items 1 and 2, end to end.

Four nullable, additive store columns and the code that writes them:

* ``attempts.reply_text`` -- what a model sent on an attempt that produced no
  usable answer, which a retry used to erase (item 1).
* ``calls.effective_params`` and ``calls.reuse_namespace`` -- two inputs of
  the row's own ``prompt_hash``, recorded as provenance (item 2).
* ``jobs.code_version`` -- which build of this package SUBMITTED the job.

PROVENANCE, NOT PROMPT VERIFICATION.  Nothing here asserts, and nothing in
the product claims, that a prompt can be re-rendered and checked against its
hash from store data: prompt text is deliberately not stored, and the
renderer is code in this repo that no row pins.  What these columns buy is
the ability to say what a call was actually made with and what a failing
model actually said.

The batched side of the anchor rule lives in ``test_batched_invocation.py``,
beside the rest of the batching seam.

No test here makes a network call, spends a token, needs a CLI login or needs
a GPU: every seat is a ``fake`` endpoint and every answer comes from a
script.  The two git tests drive a throwaway repository under ``tmp_path``
and skip when git is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest
import sqlalchemy as sa

from kbutillib.domains.ai.council import code_version as code_version_module
from kbutillib.domains.ai.council.code_version import code_version
from kbutillib.domains.ai.council.engine.worker import (
    CouncilWorker,
    renderer_id_version,
)
from kbutillib.domains.ai.council.store import schema as sch
from kbutillib.domains.ai.council.types import CouncilError, prompt_hash

from .engine_support import (
    InstrumentedBackend,
    critic_answer,
    endpoint_config,
    make_query,
    make_roster,
    make_task,
    meta_answer,
    proposer_answer,
    run,
    scripted,
)

WORKER_DEFAULTS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.02, 0.05),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.3,
    "max_idle_s": 15.0,
}


def worker_for(store: Any, backends: Mapping[str, Any], **kwargs: Any) -> CouncilWorker:
    """A worker over *backends*, with test-fast timings (mirrors test_engine.py)."""
    settings = dict(WORKER_DEFAULTS)
    settings.update(kwargs)
    config = settings.pop("config", None)
    user = settings.pop("user", "alice")
    worker_id = settings.pop("worker_id", "w-1")
    if config is None:
        config = endpoint_config(**{name: {} for name in backends})
    return CouncilWorker(store, config, backends, user, worker_id, **settings)


def agreeing_script(*ids: str) -> Dict[Any, Any]:
    """Every proposer picks *ids*; the critic approves."""
    return {
        "proposer": proposer_answer(*ids),
        "critic": critic_answer(),
        "meta": meta_answer(*ids),
    }


def fake(script: Any, **kwargs: Any) -> InstrumentedBackend:
    return InstrumentedBackend(scripted(script), **kwargs)


class FailFirstWithBody(InstrumentedBackend):
    """A backend whose FIRST call per seat fails with a BODY attached.

    ``FakeBackend``'s own error path carries no text -- a script that raises
    is a pure transport failure in its view -- but a gateway that returns 502
    with an HTML page, or a model that times out after streaming half an
    answer, hands back a failed result that DOES have a body.  That is the
    case ``attempts.reply_text`` exists for and the only way to drive it here
    is to build such a result.

    The first call's answer is run and discarded rather than skipped, so the
    request is still recorded in ``calls`` and the script's own counters
    advance exactly as a real retry sequence would leave them.
    """

    def __init__(self, script: Any, body: str, **kwargs: Any) -> None:
        super().__init__(script, **kwargs)
        self._body = body
        self._failed: set = set()

    async def complete(self, req: Any) -> Any:
        result = await super().complete(req)
        key = (req.role, req.seat.seat_name)
        if key in self._failed:
            return result
        self._failed.add(key)
        return result.model_copy(
            update={
                "ok": False,
                "text": self._body,
                "parsed": None,
                "error": CouncilError(
                    kind="transport",
                    message="the gateway answered 502 with a body",
                    retryable=True,
                ),
            }
        )


def attempts_of(store: Any, call_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Attempt rows, optionally for one call, ordered by attempt number."""
    stmt = sa.select(sch.attempts)
    if call_id is not None:
        stmt = stmt.where(sch.attempts.c.call_id == call_id)
    with store.engine.begin() as conn:
        rows = [dict(r._mapping) for r in conn.execute(stmt)]
    return sorted(rows, key=lambda r: r["attempt_no"])


# ---------------------------------------------------------------------------
# attempts.reply_text, single calls
# ---------------------------------------------------------------------------


class TestFailedAttemptKeepsItsReply:
    def test_a_retried_call_keeps_the_failed_reply_and_the_ok_attempt_is_null(
        self, store: Any
    ) -> None:
        """The exact loss issue #92 item 1 measured.

        Attempt 1 fails with a body and is RELEASED, so the call row keeps no
        reply of its own; attempt 2 answers and overwrites nothing.  Before
        this column the body of attempt 1 was gone the moment attempt 2
        succeeded -- and 105 of 113 retried items on the published run came
        back fine, which means the defect was in exactly that lost text.

        A singleton ``invalid_output`` is NOT what drives this case: it is not
        in ``RETRYABLE_KINDS``, so it fails its call terminally and there is
        never a second attempt on the same row (see the next test for the
        one-attempt shape).  A transport failure WITH a body is the singleton
        path that produces both attempts.
        """
        backend = FailFirstWithBody(
            scripted(agreeing_script("A:1")), "<html>502 Bad Gateway</html>"
        )
        worker = worker_for(store, {"fake1": backend})
        roster = make_roster(2)
        job_id = worker.submit(make_query(), [make_task()], roster)
        run(worker.work(job_id=job_id, until_idle=True))

        proposer_calls = [c for c in store.get_calls(job_id) if c["role"] == "proposer"]
        assert len(proposer_calls) == 2
        for call in proposer_calls:
            rows = attempts_of(store, call["call_id"])
            assert [r["attempt_no"] for r in rows] == [1, 2]
            assert rows[0]["outcome_kind"] == "transport"
            assert rows[0]["reply_text"] == "<html>502 Bad Gateway</html>"
            # The attempt that SUCCEEDED says nothing: its reply is on the
            # call row, and copying it here would store the same text twice.
            assert rows[1]["outcome_kind"] == "ok"
            assert rows[1]["reply_text"] is None

    def test_an_invalid_output_attempt_keeps_the_reply_the_model_sent(
        self, store: Any
    ) -> None:
        """The raw reply, not the repaired or parsed shape.

        ``invalid_output`` fails the call terminally, so this is the
        one-attempt shape -- and the attempt row holds precisely what the
        model sent, which is the only record of the defect.
        """
        invalid = {"picked": "not-a-list"}
        backend = fake({"proposer": invalid, "critic": critic_answer()})
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(2))
        run(worker.work(job_id=job_id, until_idle=True))

        calls = [c for c in store.get_calls(job_id) if c["role"] == "proposer"]
        assert calls
        for call in calls:
            (row,) = attempts_of(store, call["call_id"])
            assert row["outcome_kind"] == "invalid_output"
            assert row["reply_text"] == json.dumps(invalid, sort_keys=True)

    def test_a_hundred_thousand_character_reply_is_capped(self, store: Any) -> None:
        """A model that answers with 100,000 characters stores 65,536."""
        invalid = {"picked": "x" * 100_000}
        backend = fake({"proposer": invalid, "critic": critic_answer()})
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(2))
        run(worker.work(job_id=job_id, until_idle=True))

        call = [c for c in store.get_calls(job_id) if c["role"] == "proposer"][0]
        (row,) = attempts_of(store, call["call_id"])
        sent = json.dumps(invalid, sort_keys=True)
        assert len(sent) > sch.ATTEMPT_REPLY_TEXT_MAX_CHARS
        assert len(row["reply_text"]) == sch.ATTEMPT_REPLY_TEXT_MAX_CHARS == 65_536
        assert row["reply_text"] == sent[:65_536]

    def test_a_transport_failure_with_no_body_leaves_the_column_null(
        self, store: Any
    ) -> None:
        """"Nothing to say" stays NULL rather than becoming an empty string.

        ``FakeBackend``'s error path attaches no text at all, which is the
        honest shape of a connection that never answered -- 45 of the
        published run's 82,270 calls were exactly this.
        """

        def dies(req: Any) -> Any:
            raise RuntimeError("the connection went away")

        backend = InstrumentedBackend(dies)
        worker = worker_for(store, {"fake1": backend}, max_attempts=1)
        job_id = worker.submit(make_query(), [make_task()], make_roster(2))
        run(worker.work(job_id=job_id, until_idle=True))

        rows = attempts_of(store)
        assert rows, "the failed attempt must still be recorded"
        assert all(r["reply_text"] is None for r in rows)

    def test_a_reused_answer_records_no_reply_text(self, store: Any) -> None:
        """A reused attempt copies nobody's reply into the audit trail.

        ``outcome_kind='reused'`` already carries a full copy of the source
        call's reply on its OWN call row; writing it here as well would make
        one answer look like two pieces of evidence.
        """
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        query, roster = make_query(), make_roster(2)
        first = worker.submit(query, [make_task()], roster)
        run(worker.work(job_id=first, until_idle=True))
        second = worker.submit(query, [make_task()], roster)
        run(worker.work(job_id=second, until_idle=True))

        reused = [r for r in attempts_of(store) if r["outcome_kind"] == "reused"]
        assert reused, "the second job must have reused the first job's answer"
        assert all(r["reply_text"] is None for r in reused)


# ---------------------------------------------------------------------------
# calls.effective_params and calls.reuse_namespace
# ---------------------------------------------------------------------------


class TestCallRowProvenance:
    def test_a_new_call_carries_what_its_prompt_hash_was_built_from(
        self, store: Any
    ) -> None:
        """The row records the SAME two values that went into the hash.

        Checked by rebuilding the hash from the stored columns rather than by
        comparing them to a second evaluation of the same helpers: the claim
        worth pinning is that these two columns describe the call that was
        actually made, and a re-evaluation could agree with the row while
        both disagreed with the hash.

        This is NOT prompt verification: the prompts handed to ``prompt_hash``
        below come from rendering the query type HERE, in the test, with the
        task in hand.  The store holds no prompt text and this test does not
        pretend otherwise.
        """
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        query = make_query()
        roster = make_roster(2)
        task = make_task()
        job_id = worker.submit(query, [task], roster)

        rows = [c for c in store.get_calls(job_id) if c["role"] == "proposer"]
        seat = roster.proposers[0]
        (row,) = [r for r in rows if r["seat_name"] == seat.seat_name]
        system, user = query.render("proposer", task, {})
        rebuilt = prompt_hash(
            seat,
            row["reuse_namespace"],
            row["effective_params"],
            query.answer_schema("proposer"),
            str(query.name),
            str(query.version),
            renderer_id_version(query),
            "proposer",
            system,
            user,
        )
        assert rebuilt == row["prompt_hash"]
        # And they are the live helpers' answers, so a reader of the row sees
        # what this worker would send today.
        assert row["reuse_namespace"] == worker._reuse_namespace(seat)
        assert row["effective_params"] == worker._effective_params(seat)
        # Neither is secret: token ceilings, a temperature, a namespace label.
        assert "api_key" not in json.dumps(row["effective_params"])

    def test_a_critic_call_added_later_carries_them_too(self, store: Any) -> None:
        """Critic and meta rows are built by the same ``_call_row``, after the
        proposers settle -- so the columns are a property of every call, not
        only of the ones a submit creates."""
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(2))
        run(worker.work(job_id=job_id, until_idle=True))

        rows = [c for c in store.get_calls(job_id) if c["role"] != "proposer"]
        assert rows, "the critic always audits"
        for row in rows:
            assert row["reuse_namespace"]
            assert row["effective_params"] is not None


# ---------------------------------------------------------------------------
# jobs.code_version
# ---------------------------------------------------------------------------


class TestJobCodeVersion:
    def test_a_submitted_job_carries_the_three_keys(self, store: Any) -> None:
        backend = fake(agreeing_script("A:1"))
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(make_query(), [make_task()], make_roster(2))

        recorded = store.get_job(job_id)["code_version"]
        assert set(recorded) == {"package_version", "git_commit", "git_dirty"}
        # Survives the JSON round trip as the same values the module reports.
        assert recorded == json.loads(json.dumps(code_version()))

    def test_it_is_resolved_once_per_process(self) -> None:
        """Three git subprocesses per submitted job would be paid for nothing
        -- the build does not change while the process runs."""
        assert code_version() == code_version()
        assert code_version() is not code_version(), (
            "each caller must get its own dict, so storing or mutating one "
            "cannot disturb the next caller's answer"
        )


class TestCodeVersionGitFields:
    """The ownership gate: git fields only if this checkout owns the package."""

    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-C",
                str(cwd),
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        if shutil.which("git") is None:  # pragma: no cover - git is present here
            pytest.skip("git is not on PATH")
        self._git(tmp_path, "init", "-q")
        package = tmp_path / "somepackage"
        package.mkdir()
        (package / "__init__.py").write_text("# a package dropped in a checkout\n")
        return tmp_path

    def test_an_untracked_package_reports_no_git_fields(self, repo: Path) -> None:
        """A wheel in a venv inside somebody else's checkout.

        ``git -C <package dir> rev-parse HEAD`` would answer with THAT
        project's commit, which is provenance that is confidently wrong. Both
        fields must be null instead.
        """
        self._git(repo, "commit", "-q", "--allow-empty", "-m", "unrelated project")
        package = repo / "somepackage"
        assert code_version_module._git_fields(package) == (None, None)

    def test_a_tracked_package_reports_the_commit_and_the_dirty_flag(
        self, repo: Path
    ) -> None:
        """The positive control, without which the test above proves nothing:
        the nulls have to come from the ownership gate and not from a git
        invocation that never worked."""
        self._git(repo, "add", "somepackage/__init__.py")
        self._git(repo, "commit", "-q", "-m", "the package's own repository")
        package = repo / "somepackage"

        commit, dirty = code_version_module._git_fields(package)
        assert commit is not None and len(commit) == 40
        assert dirty is False

        (package / "__init__.py").write_text("# edited\n")
        commit_again, dirty_again = code_version_module._git_fields(package)
        assert commit_again == commit
        assert dirty_again is True

    def test_a_directory_in_no_repository_at_all_reports_no_git_fields(
        self, tmp_path: Path
    ) -> None:
        """A wheel in a venv under /opt, with no checkout anywhere above it.

        ``tmp_path`` is not inside a git repository on this fleet; if it ever
        is, the ownership gate still answers no, because nothing up there
        tracks this directory's ``__init__.py``.
        """
        package = tmp_path / "loose"
        package.mkdir()
        (package / "__init__.py").write_text("")
        assert code_version_module._git_fields(package) == (None, None)

    def test_a_failing_git_never_raises_into_a_submit(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """Submitting a job must not depend on a healthy git."""

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("git is not installed")

        monkeypatch.setattr(code_version_module.subprocess, "run", boom)
        assert code_version_module._git_fields(Path("/nowhere")) == (None, None)
