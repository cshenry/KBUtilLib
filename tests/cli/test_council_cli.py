"""Tests for ``kbu council`` -- the council's command surface.

Nothing here makes a network call except to 127.0.0.1, spends a token, needs
a CLI login or needs a GPU.  Two groups, and the distinction matters:

* Most tests drive a **fake council**: ``CouncilUtils.build_backends`` is
  monkeypatched to one ``FakeBackend``, so a submit -> run -> status ->
  results round trip exercises the real CLI, the real facade, the real engine
  and a real SQLite store without a model.
* :class:`TestDoctorWithNothingPatched` drives the **real** backends with
  NOTHING in the council package patched at all: its HTTP endpoint is a
  127.0.0.1 port that was bound and then closed, and its Claude endpoint is a
  shell script that records its own argv.  That is the only way to prove the
  two claims the module's docstring makes about ``doctor`` -- that it reaches
  the endpoint it says it does, and that it never prompts the CLI.

The published envelope (from ``council.py``'s docstring)::

    {"schema_version": 1, "verb": ..., "generated_at": ..., "data": {...},
     "warnings": [...]}
"""

from __future__ import annotations

import json
import socket
import sqlite3
import stat
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.domains.ai.council.backends.testing import FakeBackend
from kbutillib.domains.ai.council.store.schema import SCHEMA_VERSION
from kbutillib.domains.ai.council_utils import CouncilUtils

#: The envelope keys every verb emits.  Pinned as a set so an additive field
#: is a deliberate edit here rather than a silent contract change.
ENVELOPE_KEYS = {"schema_version", "verb", "generated_at", "data", "warnings"}

MENU = [
    {"id": "GO:0003824", "label": "catalytic activity", "rank": 1, "score": 0.9},
    {"id": "GO:0016740", "label": "transferase activity", "rank": 2, "score": 0.4},
]


def _free_port() -> int:
    """Bind a loopback port, read it, and close it again.

    The port is therefore known-closed: a connection to it fails immediately
    with ECONNREFUSED rather than hanging, which is what makes the
    unreachable-endpoint test fast and offline.
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _write_config(tmp_path: Path, config: Dict[str, Any]) -> Path:
    path = tmp_path / "council.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _fake_council_config(tmp_path: Path) -> Dict[str, Any]:
    """A five-seat council on one OpenAI-compatible endpoint.

    The base URL is never contacted in the tests that use this: they patch
    ``build_backends``.  It still has to be a real endpoint kind, because
    ``CouncilConfig.kind`` is a Literal over the four shipped kinds.
    """
    return {
        "endpoints": {"local": {"kind": "openai", "base_url": "http://127.0.0.1:9/v1"}},
        "models": {
            "p1": {"endpoint": "local", "canonical": "test/p1"},
            "p2": {"endpoint": "local", "canonical": "test/p2"},
            "p3": {"endpoint": "local", "canonical": "test/p3"},
            "cr": {"endpoint": "local", "canonical": "test/cr"},
            "mt": {"endpoint": "local", "canonical": "test/mt"},
        },
        "profiles": {"test": {"proposers": ["p1", "p2", "p3"], "critic": "cr", "meta": "mt"}},
        "default_profile": "test",
        "store": f"sqlite:///{tmp_path}/council.sqlite",
    }


def _script(req: Any) -> Dict[str, Any]:
    """Unanimous ``go_mf`` answers: ``go_id`` / ``final_go_ids``, not ``id``."""
    if req.role == "proposer":
        return {
            "picked": [{"go_id": "GO:0003824", "confidence": "high", "why": "ok"}],
            "overall_confidence": "high",
            "no_functional_content": False,
            "notes": "",
        }
    if req.role == "critic":
        return {
            "verdict": "consistent",
            "reason": "matches",
            "overpicked": [],
            "missed": [],
            "contentless_input": False,
        }
    return {
        "final_go_ids": ["GO:0003824"],
        "final_confidence": "medium",
        "no_functional_content": False,
        "reasoning": "adjudicated",
    }


@pytest.fixture()
def runner() -> CliRunner:
    """The default runner: stderr folded into ``output``.

    Folded on purpose, so an error-message assertion can read
    ``result.output`` whether the message went to stdout or (as a
    ``ClickException`` does) to stderr.  The one test that has to prove the
    streams are actually SEPARATE uses :func:`_separated_runner`.
    """
    return CliRunner()


def _separated_runner() -> CliRunner:
    """A runner that keeps stdout and stderr apart.

    ``mix_stderr`` was removed in click 8.2, where the streams are always
    separate, so its absence is the newer behaviour rather than a problem.
    """
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:  # pragma: no cover - depends on the installed click
        return CliRunner()


@pytest.fixture()
def fake_council(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """``--config``/``--store`` argv for a council whose backends are fakes.

    Returns the leading argv every verb in these tests is invoked with, with
    the store already initialised (``db init``) -- except where a test
    deliberately wants an unstamped one.
    """
    config_path = _write_config(tmp_path, _fake_council_config(tmp_path))
    store = f"sqlite:///{tmp_path}/council.sqlite"
    backend = FakeBackend(_script)
    monkeypatch.setattr(
        CouncilUtils,
        "build_backends",
        lambda self, roster, allow_unverified=None: {"local": backend},
    )
    argv = ["council", "--config", str(config_path), "--store", store]
    CliRunner().invoke(main, argv + ["db", "init", "--json"], catch_exceptions=False)
    return argv


@pytest.fixture()
def items_file(tmp_path: Path) -> Path:
    """A two-line JSONL fixture of ``CouncilTask`` dicts."""
    path = tmp_path / "items.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "item_id": item_id,
                    "evidence": {"text": text, "source": "test"},
                    "menu": MENU,
                }
            )
            for item_id, text in (("item-1", "alpha synthase"), ("item-2", "beta transferase"))
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _envelope(result: Any) -> Dict[str, Any]:
    """The one JSON object the invocation emitted, with its keys asserted.

    The default runner folds stderr in, so the envelope is found as the one
    line that starts an object rather than by decoding the whole stream;
    ``test_json_stdout_is_one_clean_object`` is what pins that stdout itself
    carries nothing else.
    """
    assert result.exit_code in (0, 1, 2), result.output
    lines = [line for line in result.output.splitlines() if line.startswith("{")]
    assert len(lines) == 1, f"expected exactly one JSON object, got {result.output!r}"
    payload = json.loads(lines[0])
    assert set(payload) == ENVELOPE_KEYS
    assert payload["schema_version"] == 1
    return payload


# ---------------------------------------------------------------------------
# db + the no-implicit-schema rule
# ---------------------------------------------------------------------------


def _council_tables(path: Path) -> List[str]:
    """Council table names in the SQLite file at *path* (``[]`` if absent)."""
    if not path.exists():
        return []
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        connection.close()
    return sorted(row[0] for row in rows)


class TestSchemaGuard:
    """Nothing but ``db init``/``db migrate`` may create a schema."""

    def test_jobs_on_a_schemaless_store_says_run_db_init_and_creates_nothing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        config_path = _write_config(tmp_path, _fake_council_config(tmp_path))
        store_file = tmp_path / "empty.sqlite"
        argv = [
            "council",
            "--config",
            str(config_path),
            "--store",
            f"sqlite:///{store_file}",
        ]
        result = runner.invoke(main, argv + ["jobs", "--json"])
        assert result.exit_code != 0
        assert "kbu council db init" in result.output
        # The refusal did not quietly bring a council into existence.
        assert _council_tables(store_file) == []

        after_init = runner.invoke(main, argv + ["db", "init", "--json"])
        assert after_init.exit_code == 0
        tables = _council_tables(store_file)
        assert "jobs" in tables and "calls" in tables and "schema_version" in tables

    def test_db_check_reports_an_unstamped_store_without_raising(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        config_path = _write_config(tmp_path, _fake_council_config(tmp_path))
        argv = ["council", "--config", str(config_path), "--store", f"sqlite:///{tmp_path}/x.sqlite"]
        envelope = _envelope(runner.invoke(main, argv + ["db", "check", "--json"]))
        assert envelope["verb"] == "db check"
        assert envelope["data"]["ok"] is False
        assert envelope["data"]["schema_version"] == 0
        assert any("kbu council db init" in w for w in envelope["warnings"])

    def test_db_init_seeds_the_builtins(self, runner: CliRunner, fake_council: List[str]) -> None:
        envelope = _envelope(runner.invoke(main, fake_council + ["db", "init", "--json"]))
        assert envelope["verb"] == "db init"
        assert sorted(envelope["data"]["query_types"]) == ["go_mf@1", "reaction_pickup@1"]

    def test_db_migrate_stamps_a_fresh_store(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        config_path = _write_config(tmp_path, _fake_council_config(tmp_path))
        argv = ["council", "--config", str(config_path), "--store", f"sqlite:///{tmp_path}/m.sqlite"]
        envelope = _envelope(runner.invoke(main, argv + ["db", "migrate", "--json"]))
        assert envelope["data"]["schema_version"] == SCHEMA_VERSION
        assert envelope["data"]["migrated"] is True


# ---------------------------------------------------------------------------
# submit / run / jobs
# ---------------------------------------------------------------------------


class TestJobLifecycle:
    """submit -> run -> jobs status -> jobs results, through the CLI."""

    def test_submit_run_status_results(
        self, runner: CliRunner, fake_council: List[str], items_file: Path, tmp_path: Path
    ) -> None:
        submitted = _envelope(
            runner.invoke(
                main,
                fake_council
                + ["submit", "--query", "go_mf", "--input", str(items_file), "--label", "cli test", "--json"],
            )
        )
        assert submitted["verb"] == "submit"
        job_id = submitted["data"]["job_id"]
        assert job_id
        assert submitted["data"]["n_items"] == 2

        run = _envelope(runner.invoke(main, fake_council + ["run", "--job", job_id, "--json"]))
        assert run["verb"] == "run"
        assert run["data"]["status"] == "done"
        assert run["data"]["blockers"] == []

        status = _envelope(runner.invoke(main, fake_council + ["jobs", "status", job_id, "--json"]))
        assert status["verb"] == "jobs status"
        assert status["data"]["percent_complete"] == 100.0
        assert status["data"]["by_status"] == {"ok": 2}

        results = _envelope(runner.invoke(main, fake_council + ["jobs", "results", job_id, "--json"]))
        assert results["verb"] == "jobs results"
        assert results["data"]["n_rows"] == 2
        assert [row["item_id"] for row in results["data"]["results"]] == ["item-1", "item-2"]
        assert results["data"]["results"][0]["verdict"]["selected_ids"] == ["GO:0003824"]

        out = tmp_path / "out" / "results.jsonl"
        written = _envelope(
            runner.invoke(main, fake_council + ["jobs", "results", job_id, "--out", str(out), "--json"])
        )
        assert written["data"]["out"] == str(out)
        assert "results" not in written["data"]
        lines = [json.loads(line) for line in out.read_text().splitlines()]
        assert [line["item_id"] for line in lines] == ["item-1", "item-2"]

    def test_run_submits_and_works_in_one_step(
        self, runner: CliRunner, fake_council: List[str], items_file: Path
    ) -> None:
        envelope = _envelope(
            runner.invoke(
                main,
                fake_council + ["run", "--query", "go_mf", "--input", str(items_file), "--json"],
            )
        )
        assert envelope["data"]["status"] == "done"
        assert envelope["data"]["n_items"] == 2

    def test_run_exits_one_when_the_job_ends_blocked(
        self, runner: CliRunner, fake_council: List[str], items_file: Path
    ) -> None:
        """A halted endpoint blocks the job, and `run` says so with exit 1."""
        job_id = _envelope(
            runner.invoke(
                main,
                fake_council + ["submit", "--query", "go_mf", "--input", str(items_file), "--json"],
            )
        )["data"]["job_id"]

        util = CouncilUtils(
            council_config=fake_council[fake_council.index("--config") + 1],
            store_url=fake_council[fake_council.index("--store") + 1],
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        try:
            util.store().set_halt("local", "quota_exhausted", "out of quota")
        finally:
            util.close()

        result = runner.invoke(main, fake_council + ["run", "--job", job_id, "--json"])
        envelope = _envelope(result)
        assert result.exit_code == 1
        assert envelope["data"]["blockers"]
        assert any("blocked" in warning for warning in envelope["warnings"])

    def test_run_refuses_both_forms_at_once(
        self, runner: CliRunner, fake_council: List[str], items_file: Path
    ) -> None:
        result = runner.invoke(
            main,
            fake_council + ["run", "--job", "x", "--query", "go_mf", "--input", str(items_file)],
        )
        assert result.exit_code != 0
        assert "either --job ID, or --query with --input" in result.output

    def test_jobs_lists_cancels_deletes_and_reruns(
        self, runner: CliRunner, fake_council: List[str], items_file: Path
    ) -> None:
        job_id = _envelope(
            runner.invoke(
                main,
                fake_council + ["submit", "--query", "go_mf", "--input", str(items_file), "--json"],
            )
        )["data"]["job_id"]

        listed = _envelope(runner.invoke(main, fake_council + ["jobs", "--json"]))
        assert listed["verb"] == "jobs"
        assert [row["job_id"] for row in listed["data"]["jobs"]] == [job_id]

        rerun = _envelope(runner.invoke(main, fake_council + ["jobs", "rerun", job_id, "--json"]))
        assert rerun["verb"] == "jobs rerun"
        assert rerun["data"]["rerun_of"] == job_id
        assert rerun["data"]["job_id"] != job_id

        for identifier in (job_id, rerun["data"]["job_id"]):
            cancelled = _envelope(
                runner.invoke(main, fake_council + ["jobs", "cancel", identifier, "--json"])
            )
            assert cancelled["data"]["status"] == "cancelled"
            deleted = _envelope(
                runner.invoke(main, fake_council + ["jobs", "delete", identifier, "--json"])
            )
            assert deleted["verb"] == "jobs delete"

        assert _envelope(runner.invoke(main, fake_council + ["jobs", "--json"]))["data"]["n_jobs"] == 0
        with_deleted = _envelope(
            runner.invoke(main, fake_council + ["jobs", "--include-deleted", "--json"])
        )
        assert with_deleted["data"]["n_jobs"] == 2

    def test_submit_names_the_line_of_a_bad_jsonl_record(
        self, runner: CliRunner, fake_council: List[str], tmp_path: Path
    ) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text('{"item_id": "ok", "evidence": {}, "menu": []}\n["not an object"]\n')
        result = runner.invoke(main, fake_council + ["submit", "--query", "go_mf", "--input", str(path)])
        assert result.exit_code != 0
        assert "bad.jsonl:2" in result.output

    def test_results_can_be_written_as_parquet(
        self, runner: CliRunner, fake_council: List[str], items_file: Path, tmp_path: Path
    ) -> None:
        """A verdict is nested, so it travels as JSON text in a parquet column."""
        pd = pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")
        job_id = _envelope(
            runner.invoke(
                main,
                fake_council + ["run", "--query", "go_mf", "--input", str(items_file), "--json"],
            )
        )["data"]["job_id"]
        out = tmp_path / "results.parquet"
        envelope = _envelope(
            runner.invoke(
                main,
                fake_council
                + ["jobs", "results", job_id, "--out", str(out), "--format", "parquet", "--json"],
            )
        )
        assert envelope["data"]["format"] == "parquet"
        frame = pd.read_parquet(out)
        assert list(frame["item_id"]) == ["item-1", "item-2"]
        assert json.loads(frame["verdict"][0])["selected_ids"] == ["GO:0003824"]

    def test_json_stdout_is_one_clean_object(
        self, fake_council: List[str], tmp_path: Path
    ) -> None:
        """Stdout discipline: the envelope and nothing else; warnings go to stderr.

        Uses a runner that keeps the streams apart, which is the only way to
        see the separation the other tests deliberately fold together.
        """
        separated = _separated_runner()
        store = tmp_path / "unstamped.sqlite"
        argv = [
            "council",
            "--config",
            fake_council[fake_council.index("--config") + 1],
            "--store",
            f"sqlite:///{store}",
            "db",
            "check",
            "--json",
        ]
        result = separated.invoke(main, argv)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert set(payload) == ENVELOPE_KEYS
        assert payload["verb"] == "db check"
        assert "warning:" not in result.stdout
        assert "kbu council db init" in result.stderr

    def test_human_output_renders_the_same_dict_the_json_path_serialises(
        self,
        runner: CliRunner,
        fake_council: List[str],
        items_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Output-contract rule 1: one built envelope, two renderings."""
        import kbutillib.interfaces.cli.council as council_cli

        seen: List[Dict[str, Any]] = []
        monkeypatch.setattr(council_cli, "_render", seen.append)
        result = runner.invoke(
            main, fake_council + ["submit", "--query", "go_mf", "--input", str(items_file)]
        )
        assert result.exit_code == 0, result.output
        assert len(seen) == 1
        assert set(seen[0]) == ENVELOPE_KEYS
        assert seen[0]["verb"] == "submit"


# ---------------------------------------------------------------------------
# query / calls / endpoints / models
# ---------------------------------------------------------------------------


class TestRegistryAndState:
    """The verbs that read and nudge what is already in the store."""

    def test_query_list_shows_both_builtins(
        self, runner: CliRunner, fake_council: List[str]
    ) -> None:
        envelope = _envelope(runner.invoke(main, fake_council + ["query", "list", "--json"]))
        assert envelope["verb"] == "query list"
        pairs = {(row["name"], row["version"]) for row in envelope["data"]["query_types"]}
        assert ("go_mf", "1") in pairs
        assert ("reaction_pickup", "1") in pairs

    def test_query_show_and_retire(self, runner: CliRunner, fake_council: List[str]) -> None:
        shown = _envelope(runner.invoke(main, fake_council + ["query", "show", "go_mf@1", "--json"]))
        assert shown["data"]["spec"]["name"] == "go_mf"
        assert shown["data"]["spec"]["id_field"] == "go_id"

        retired = _envelope(
            runner.invoke(main, fake_council + ["query", "retire", "reaction_pickup@1", "--json"])
        )
        assert retired["data"]["result"] == "retired"
        remaining = _envelope(runner.invoke(main, fake_council + ["query", "list", "--json"]))
        assert [row["name"] for row in remaining["data"]["query_types"]] == ["go_mf"]

    def test_query_retire_demands_an_exact_version(
        self, runner: CliRunner, fake_council: List[str]
    ) -> None:
        result = runner.invoke(main, fake_council + ["query", "retire", "go_mf"])
        assert result.exit_code != 0
        assert "NAME@VERSION" in result.output

    def test_calls_search_finds_the_calls_of_a_worked_job(
        self, runner: CliRunner, fake_council: List[str], items_file: Path
    ) -> None:
        runner.invoke(
            main, fake_council + ["run", "--query", "go_mf", "--input", str(items_file), "--json"]
        )
        envelope = _envelope(
            runner.invoke(
                main,
                fake_council + ["calls", "search", "--query", "go_mf", "--entity", "alpha synthase", "--json"],
            )
        )
        assert envelope["verb"] == "calls search"
        assert envelope["data"]["n_calls"] >= 3
        assert {row["query_name"] for row in envelope["data"]["calls"]} == {"go_mf"}

    def test_calls_search_refuses_entity_and_entity_hash_together(
        self, runner: CliRunner, fake_council: List[str]
    ) -> None:
        result = runner.invoke(
            main,
            fake_council + ["calls", "search", "--entity", "x", "--entity-hash", "a" * 64],
        )
        assert result.exit_code != 0
        assert "not both" in result.output

    def test_endpoints_status_then_reset_halt(
        self, runner: CliRunner, fake_council: List[str], tmp_path: Path
    ) -> None:
        config_path = fake_council[fake_council.index("--config") + 1]
        util = CouncilUtils(
            council_config=config_path,
            store_url=fake_council[fake_council.index("--store") + 1],
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        try:
            util.store().add_tokens("local", 42)
            util.store().set_halt("local", "quota_exhausted", "out of quota")
        finally:
            util.close()

        halted = _envelope(runner.invoke(main, fake_council + ["endpoints", "status", "--json"]))
        assert halted["verb"] == "endpoints status"
        assert halted["data"]["endpoints"][0]["halted"] is True
        assert any("is halted" in w for w in halted["warnings"])

        cleared = _envelope(
            runner.invoke(main, fake_council + ["endpoints", "reset-halt", "local", "--json"])
        )
        assert cleared["verb"] == "endpoints reset-halt"
        assert not cleared["data"]["halt_kind"]
        # A halt reset never touches the counters.
        assert cleared["data"]["lifetime_tokens"] == 42

        after = _envelope(runner.invoke(main, fake_council + ["endpoints", "status", "--json"]))
        assert after["data"]["endpoints"][0]["halted"] is False
        assert after["warnings"] == []

    def test_endpoints_reset_budget_keeps_the_lifetime_total(
        self, runner: CliRunner, fake_council: List[str]
    ) -> None:
        util = CouncilUtils(
            council_config=fake_council[fake_council.index("--config") + 1],
            store_url=fake_council[fake_council.index("--store") + 1],
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        try:
            util.store().add_tokens("local", 7)
        finally:
            util.close()
        envelope = _envelope(
            runner.invoke(main, fake_council + ["endpoints", "reset-budget", "local", "--json"])
        )
        assert envelope["data"]["epoch_tokens"] == 0
        assert envelope["data"]["lifetime_tokens"] == 7

    def test_models_list_reports_the_configured_aliases(
        self, runner: CliRunner, fake_council: List[str]
    ) -> None:
        envelope = _envelope(runner.invoke(main, fake_council + ["models", "list", "--json"]))
        assert envelope["verb"] == "models list"
        assert envelope["data"]["endpoint"] is None
        assert [row["alias"] for row in envelope["data"]["models"]] == ["cr", "mt", "p1", "p2", "p3"]


# ---------------------------------------------------------------------------
# doctor, with the fakes
# ---------------------------------------------------------------------------


class TestDoctorEnvelope:
    """``doctor``'s envelope and exit codes, with injected backends."""

    def test_doctor_json_envelope_and_exit_two_for_unknown_seats(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fake_council_config(tmp_path)
        for alias in config["models"]:
            config["models"][alias]["verified"] = False
        config_path = _write_config(tmp_path, config)

        class _Backend:
            kind = "openai"
            base_url = "http://127.0.0.1:9/v1"

            async def complete(self, req: Any) -> Any:  # pragma: no cover
                raise AssertionError("doctor must never call complete()")

            async def models(self) -> List[Dict[str, Any]]:
                return [{"id": "p1"}]

            def effective_params(self, seat: Any) -> Dict[str, Any]:
                return {}

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr(
            CouncilUtils,
            "build_backends",
            lambda self, roster, allow_unverified=None: {"local": _Backend()},
        )
        result = runner.invoke(
            main,
            ["council", "--config", str(config_path), "doctor", "--json"],
        )
        envelope = _envelope(result)
        assert envelope["verb"] == "doctor"
        assert result.exit_code == 2
        assert envelope["data"]["summary"]["unknown"] == 5
        assert envelope["data"]["exit_code"] == 2
        assert envelope["data"]["user"]

    def test_doctor_exits_zero_when_every_seat_is_ready(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = _write_config(tmp_path, _fake_council_config(tmp_path))

        class _Backend:
            kind = "openai"
            base_url = "http://127.0.0.1:9/v1"

            async def complete(self, req: Any) -> Any:  # pragma: no cover
                raise AssertionError("doctor must never call complete()")

            async def models(self) -> List[Dict[str, Any]]:
                return [{"id": alias} for alias in ("p1", "p2", "p3", "cr", "mt")]

            def effective_params(self, seat: Any) -> Dict[str, Any]:
                return {}

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr(
            CouncilUtils,
            "build_backends",
            lambda self, roster, allow_unverified=None: {"local": _Backend()},
        )
        result = runner.invoke(
            main,
            [
                "council",
                "--config",
                str(config_path),
                "--store",
                f"sqlite:///{tmp_path}/council.sqlite",
                "doctor",
                "--json",
            ],
        )
        envelope = _envelope(result)
        assert envelope["data"]["summary"] == {"ready": 5, "unknown": 0, "unavailable": 0}
        # Exit 0 even though the store has no schema: a missing schema is a
        # warning, not an unusable seat, and `db init` fixes it.
        assert result.exit_code == 0
        assert any("kbu council db init" in w for w in envelope["warnings"])


# ---------------------------------------------------------------------------
# doctor, with NOTHING patched
# ---------------------------------------------------------------------------


class TestDoctorWithNothingPatched:
    """``doctor`` through the REAL backends, with nothing monkeypatched.

    Every other test in this module injects a backend, which proves the CLI
    and the facade but cannot prove that ``doctor`` reaches the endpoint it
    claims to, nor that it never prompts a CLI.  This one builds the backends
    for real: the HTTP endpoint is a 127.0.0.1 port that was bound and closed,
    and the Claude endpoint is a script that appends its argv to a file.
    """

    @staticmethod
    def _fake_cli(tmp_path: Path) -> tuple:
        """A fake ``claude`` that records its argv and prints a version."""
        log = tmp_path / "argv.log"
        script = tmp_path / "fake-claude"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" >> {log}\n'
            'echo "9.9.9 (Fake Claude)"\n',
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return script, log

    def test_real_backends_report_a_closed_port_and_only_run_version(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        script, log = self._fake_cli(tmp_path)
        port = _free_port()
        home = tmp_path / "home"
        home.mkdir()
        # A temp HOME so the billing preflight reads nothing of the real user's
        # Claude configuration -- and so the child the fake CLI runs under is
        # pointed at the same place.
        monkeypatch.setenv("HOME", str(home))
        config_path = _write_config(
            tmp_path,
            {
                "endpoints": {
                    "closed": {"kind": "openai", "base_url": f"http://127.0.0.1:{port}/v1"},
                    "fakeclaude": {"kind": "claude_cli", "executable": str(script)},
                },
                "models": {
                    "p1": {"endpoint": "closed", "canonical": "test/p1"},
                    "p2": {"endpoint": "closed", "canonical": "test/p2"},
                    "cr": {"endpoint": "fakeclaude", "served_name": "sonnet", "canonical": "test/cr"},
                    "mt": {"endpoint": "fakeclaude", "served_name": "opus", "canonical": "test/mt"},
                },
                "profiles": {"real": {"proposers": ["p1", "p2"], "critic": "cr", "meta": "mt"}},
                "default_profile": "real",
                "store": f"sqlite:///{tmp_path}/council.sqlite",
            },
        )

        result = runner.invoke(
            main, ["council", "--config", str(config_path), "doctor", "--json"]
        )
        envelope = _envelope(result)
        assert envelope["schema_version"] == 1
        assert envelope["verb"] == "doctor"
        assert result.exit_code == 1

        states = {seat["seat"]: seat for seat in envelope["data"]["seats"]}
        assert states["proposer_1"]["state"] == "unavailable"
        assert states["proposer_2"]["state"] == "unavailable"
        assert str(port) in states["proposer_1"]["detail"]
        assert envelope["data"]["summary"]["unavailable"] == 2

        # The CLI ran, and ran ONLY --version: never -p, which is the flag that
        # would have sent a prompt and spent a subscription call.
        argv = log.read_text().split()
        assert argv == ["--version"]
        assert "-p" not in argv
        assert states["critic"]["cli_version"] == "9.9.9 (Fake Claude)"
