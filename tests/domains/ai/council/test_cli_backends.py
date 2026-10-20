"""The council's headless-CLI backends: Claude Code and Codex.

Every test here drives the REAL backend -- its own argv construction, its own
environment allowlist, its own subprocess handling, its own parsing -- against
a FAKE EXECUTABLE: a small Python script written into ``tmp_path`` that dumps
its ``argv``, ``cwd``, environment and stdin to a JSON file and then behaves
however the test needs (print an envelope, write an output file, exit
non-zero, or sleep past the deadline).  Nothing in the backend is
monkeypatched, so a passing test is evidence about the shipped code.

**No test runs the real ``claude`` or ``codex`` binary.**  No test makes a
network call, spends a token, needs a CLI login or needs a GPU.  The billing
preflight's refusal paths are exercised against fake home directories; the
real ``$HOME`` is never asserted against, because this machine's own settings
are clean and a test that depended on that would pass here and fail on the
reviewer's machine.

``TestTheDefaultPathInjectsNothing`` is the no-injection check: it passes
neither ``_parent_env`` nor ``_home``, so the code under test resolves
``os.environ`` and ``Path.home()`` itself.  That is what catches the
default-argument trap -- ``parent_env=os.environ`` in a signature is
evaluated once at import, and a test that always passes the environment
explicitly would never notice.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from kbutillib.domains.ai.council.backends.cli import (
    CODEX_OUTPUT_NAME,
    CODEX_SCHEMA_NAME,
    ClaudeCliBackend,
    CodexCliBackend,
)

# The web-search flag values, imported rather than re-spelled so a test
# cannot assert a spelling the shipped code does not use.  They come from the
# llm package because the council shim re-exports only the M1 surface.
from kbutillib.domains.ai.llm.apis.cli import (
    CLAUDE_WEB_SEARCH_ALLOWED_TOOLS,
    CLAUDE_WEB_SEARCH_TOOLS,
    CODEX_WEB_SEARCH_FLAG,
)
from kbutillib.domains.ai.llm.types import LLMRequest
from kbutillib.domains.ai.council.backends.cli_common import (
    CHILD_ENV_ALLOWLIST,
    DENIED_BILLING_VARS,
    QUOTA_PATTERNS,
    _child_env,
    billing_preflight,
    classify_failure,
    dropped_billing_vars,
    run_cli,
)
from kbutillib.domains.ai.council.protocols import Backend
from kbutillib.domains.ai.council.types import CompletionRequest, SeatSpec

# STDIN_FILENAME is not part of the re-export shim's public surface (it is an
# implementation detail of how the prompt reaches the child, not a billing or
# protocol concern), so it is imported directly from where it is defined.
from kbutillib.domains.ai.llm.apis.cli_common import STDIN_FILENAME

#: The five variables that must never reach a CLI child.  Four would move the
#: call onto metered API billing; ``CLAUDE_CODE_SESSION_ID`` is here to prove
#: the ``CLAUDE_CODE_*`` prefix rule, since this suite itself runs inside a
#: Claude Code session that sets variables of that shape.
FORBIDDEN_IN_CHILD = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CLAUDE_CODE_SESSION_ID",
)

SYSTEM = "You pick GO terms from a closed menu."
USER = 'Annotation string: "beta-galactosidase (EC 3.2.1.23)"'
MODEL = "claude-sonnet-5"

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"picked": {"type": "array"}},
    "required": ["picked"],
}

ANSWER: Dict[str, Any] = {
    "picked": [{"id": "GO:0004565", "confidence": "high", "why": "beta-gal"}],
    "overall_confidence": "high",
    "no_functional_content": False,
    "notes": None,
}

#: A DIFFERENT well-formed answer, used wherever a test needs to tell which of
#: two sources a result came from.  Asserting that ``structured_output`` wins
#: is only meaningful if the loser would have parsed too.
OTHER_ANSWER: Dict[str, Any] = {
    "picked": [{"id": "GO:0030246", "confidence": "low", "why": "carb binding"}],
    "overall_confidence": "low",
    "no_functional_content": False,
    "notes": "from the result string",
}


def run(coro: Any) -> Any:
    """Run one coroutine on a loop owned by this call.

    Deliberately NOT ``asyncio.run``: that leaves the thread with no current
    event loop, after which the deprecated ``asyncio.get_event_loop()``
    raises.  Other modules in this suite still call it and ``tests/domains``
    collects first, so ``asyncio.run`` here would break them from a distance.
    Same helper and same reason as ``engine_support.run`` and the HTTP
    backend tests.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def seat(**kw: Any) -> SeatSpec:
    """A CLI seat, with per-test overrides."""
    base: Dict[str, Any] = {
        "seat_name": "proposer_1",
        "endpoint": "claude-sub",
        "endpoint_kind": "claude_cli",
        "model": MODEL,
        "canonical_model": MODEL,
        "timeout_s": 30,
    }
    base.update(kw)
    return SeatSpec(**base)


def request(**kw: Any) -> CompletionRequest:
    """A proposer request against the fake menu."""
    base: Dict[str, Any] = {
        "seat": seat(**kw.pop("seat_kw", {})),
        "role": "proposer",
        "system": SYSTEM,
        "user": USER,
        "json_schema": SCHEMA,
        "trace_id": "t-1",
    }
    base.update(kw)
    return CompletionRequest(**base)


# ---------------------------------------------------------------------------
# The fake executable
# ---------------------------------------------------------------------------

_FAKE_SOURCE = '''#!{python}
"""A fake CLI.  Records how it was called, then behaves as configured."""
import json
import os
import sys
import time

CONFIG = json.loads({config!r})

# Read stdin FIRST (unless the test asked us not to): the parent hands the
# prompt in on stdin, and a fake that exited without draining it would make
# the parent look broken if stdin were still a pipe -- which is exactly what
# ``read_stdin=False`` tests: the prompt now arrives as a FILE, so a child
# that never reads it must not hang or break the parent either.
if CONFIG.get("read_stdin", True):
    stdin_text = sys.stdin.read()
else:
    stdin_text = None

record = {{
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "stdin": stdin_text,
    "env": dict(os.environ),
    "env_keys": sorted(os.environ),
    "pid": os.getpid(),
    "pgid": os.getpgid(0),
    "schema_file": None,
}}

# If a schema file was named on the command line, prove it exists and capture
# what the backend actually wrote into it.
if "--output-schema" in sys.argv:
    path = sys.argv[sys.argv.index("--output-schema") + 1]
    try:
        with open(path) as handle:
            record["schema_file"] = handle.read()
    except OSError as exc:
        record["schema_file"] = "ERROR: %s" % exc

# Written BEFORE any sleep, so a child that gets killed for timing out still
# leaves the evidence the test needs (its pgid, above all).
with open(CONFIG["record"], "w") as handle:
    json.dump(record, handle)

if CONFIG["sleep"]:
    time.sleep(CONFIG["sleep"])

if CONFIG["write_output"] is not None and "-o" in sys.argv:
    with open(sys.argv[sys.argv.index("-o") + 1], "w") as handle:
        handle.write(CONFIG["write_output"])

sys.stdout.write(CONFIG["stdout"])
sys.stderr.write(CONFIG["stderr"])
sys.exit(CONFIG["exit_code"])
'''


class Fake:
    """A fake executable on disk, plus access to what it recorded."""

    def __init__(self, path: Path, record: Path) -> None:
        self.path = path
        self.record_path = record

    def __str__(self) -> str:
        return str(self.path)

    @property
    def record(self) -> Dict[str, Any]:
        """What the child saw.  Fails loudly if it was never invoked."""
        assert self.record_path.is_file(), (
            f"{self.path.name} was never invoked (no record at {self.record_path})"
        )
        return json.loads(self.record_path.read_text())

    @property
    def invoked(self) -> bool:
        return self.record_path.is_file()


def make_fake(
    tmp_path: Path,
    name: str = "claude",
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    sleep: float = 0.0,
    write_output: Optional[str] = None,
    read_stdin: bool = True,
) -> Fake:
    """Write an executable fake CLI into *tmp_path* and return a handle.

    The script is a real file with a ``sys.executable`` shebang and the
    execute bit set, so the backend's own ``create_subprocess_exec`` runs it
    exactly as it would run the real binary -- no shell, no PATH lookup
    assumptions, and a genuine process group to kill.

    ``read_stdin=False`` writes a fake that never touches stdin at all -- the
    one way to prove a child that ignores its prompt cannot hang or break the
    parent now that the prompt arrives as a file rather than a pipe.
    """
    record = tmp_path / f"{name}-record.json"
    config = json.dumps(
        {
            "record": str(record),
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "sleep": sleep,
            "write_output": write_output,
            "read_stdin": read_stdin,
        }
    )
    path = tmp_path / name
    path.write_text(_FAKE_SOURCE.format(python=sys.executable, config=config))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return Fake(path, record)


def envelope(**kw: Any) -> str:
    """A Claude ``--output-format json`` result envelope."""
    base: Dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(ANSWER),
        "usage": {"input_tokens": 120, "output_tokens": 45},
        "modelUsage": {MODEL: {"inputTokens": 120, "outputTokens": 45}},
    }
    base.update(kw)
    return json.dumps(base)


def clean_home(tmp_path: Path, name: str = "home") -> Path:
    """A home directory whose Claude and Codex configs both bill by subscription.

    Used by the default-path tests so they assert the allowlist rather than
    the reviewer's own machine: with a clean fake home, both preflights are
    ``ready`` for a reason the test created.
    """
    home = tmp_path / name
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": []}})
    )
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "fake",
                    "access_token": "fake",
                    "refresh_token": "fake",
                    "account_id": "fake",
                },
                "last_refresh": "2026-10-15T00:00:00Z",
            }
        )
    )
    return home


@pytest.fixture
def ready_home(tmp_path: Path) -> Path:
    """A fake home directory both preflights call ``ready``."""
    return clean_home(tmp_path)


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


class TestClaudeArgv:
    """The Claude command line, flag by flag."""

    def test_the_exact_argv(self, tmp_path: Path, ready_home: Path) -> None:
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        assert fake.record["argv"] == [
            str(fake),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(SCHEMA),
            "--model",
            MODEL,
            "--system-prompt",
            SYSTEM,
            "--no-session-persistence",
            "--tools",
            "",
        ]

    def test_the_two_forbidden_flags_are_absent(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """``--bare`` would bypass subscription OAuth; the other bypasses permissions.

        ``--bare``'s own help says Anthropic auth becomes "strictly
        ANTHROPIC_API_KEY or apiKeyHelper" with OAuth never read -- i.e. it
        would move a subscription seat onto metered API billing.
        """
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(request()))

        argv = fake.record["argv"]
        assert "--bare" not in argv
        assert "--dangerously-skip-permissions" not in argv
        assert "--allow-dangerously-skip-permissions" not in argv

    def test_the_schema_travels_and_the_user_prompt_does_not(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(request()))

        argv = fake.record["argv"]
        assert "--json-schema" in argv
        assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
        # The prompt must not be visible in ``ps`` to another user.
        assert not any(USER in arg for arg in argv)
        assert USER not in " ".join(argv)

    def test_the_user_prompt_arrives_on_stdin(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(request()))

        assert fake.record["stdin"] == USER

    def test_tools_are_disabled_with_an_empty_string(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """``--tools ""`` is the documented spelling of "no tools at all"."""
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(request()))

        argv = fake.record["argv"]
        assert argv[argv.index("--tools") + 1] == ""


def _argv_realpaths(argv: list) -> list:
    """*argv* with every ABSOLUTE-PATH entry resolved through ``realpath``.

    On macOS the per-test temporary directory is reached by two spellings:
    ``/var/folders/...``, which is what ``tmp_path`` hands the backend, and
    ``/private/var/folders/...``, which is what the child process reports as
    its cwd -- ``/var`` is a symlink to ``/private/var``.  The argv the backend
    built and the argv this test expects then differ only in that spelling,
    and the comparison failed for a reason that has nothing to do with the
    command line.

    Resolving BOTH sides fixes it without touching backend behaviour: the
    backend still passes whatever path it was given.  Only absolute paths are
    resolved, so a flag or a model name is compared verbatim and a wrong one
    still reads as itself in the failure output.  No-ops on Linux, where the
    two spellings do not arise.
    """
    return [
        os.path.realpath(entry) if os.path.isabs(entry) else entry for entry in argv
    ]


class TestCodexArgv:
    """The Codex command line, flag by flag."""

    def _run_ok(self, tmp_path: Path, home: Path) -> Fake:
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(str(fake), _home=home, _parent_env={})
        result = run(
            backend.complete(request(seat_kw={"endpoint_kind": "codex_cli"}))
        )
        assert result.ok is True, result.error
        return fake

    def test_the_exact_argv(self, tmp_path: Path, ready_home: Path) -> None:
        fake = self._run_ok(tmp_path, ready_home)
        record = fake.record
        cwd = record["cwd"]

        assert _argv_realpaths(record["argv"]) == _argv_realpaths(
            [
                str(fake),
                "exec",
                "--model",
                MODEL,
                "--output-schema",
                str(Path(cwd) / CODEX_SCHEMA_NAME),
                "-o",
                str(Path(cwd) / CODEX_OUTPUT_NAME),
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-",
            ]
        )

    def test_the_forbidden_flags_are_absent(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        argv = self._run_ok(tmp_path, ready_home).record["argv"]
        assert "--bare" not in argv
        assert "--dangerously-skip-permissions" not in argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv

    def test_ignore_user_config_keeps_a_configured_provider_out(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """A provider in ``config.toml`` (e.g. an ARGO route) must not serve this.

        Its help says config is not loaded but "auth still uses CODEX_HOME",
        which is exactly what a subscription-billed seat needs.
        """
        argv = self._run_ok(tmp_path, ready_home).record["argv"]
        assert "--ignore-user-config" in argv
        assert "--sandbox" in argv
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--ephemeral" in argv

    def test_the_schema_is_written_to_a_file_the_child_can_read(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Codex takes a schema PATH, not an inline string, so the file must exist."""
        record = self._run_ok(tmp_path, ready_home).record
        assert json.loads(record["schema_file"]) == SCHEMA

    def test_the_prompt_arrives_on_stdin_and_not_in_argv(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        record = self._run_ok(tmp_path, ready_home).record
        assert record["stdin"] == f"{SYSTEM}\n\n{USER}"
        assert record["argv"][-1] == "-", "the trailing '-' means 'prompt on stdin'"
        assert not any(USER in arg for arg in record["argv"])
        assert not any(SYSTEM in arg for arg in record["argv"])


# ---------------------------------------------------------------------------
# The working directory
# ---------------------------------------------------------------------------


class TestTheWorkingDirectory:
    """A fresh, empty directory per call, gone afterwards."""

    @pytest.mark.parametrize("which", ["claude", "codex"])
    def test_the_cwd_is_fresh_and_is_removed_afterwards(
        self, tmp_path: Path, ready_home: Path, which: str
    ) -> None:
        here = os.getcwd()
        if which == "claude":
            fake = make_fake(tmp_path, "claude", stdout=envelope())
            backend: Any = ClaudeCliBackend(
                str(fake), _home=ready_home, _parent_env={}
            )
        else:
            fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
            backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))
        assert result.ok is True, result.error

        child_cwd = Path(fake.record["cwd"])
        assert child_cwd != Path(here)
        assert tmp_path not in child_cwd.parents, "not inside the test's own tree"
        assert not child_cwd.exists(), "the per-call directory must be removed"
        # And the test's own cwd is untouched.
        assert os.getcwd() == here

    def test_two_calls_get_two_different_directories(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(request()))
        first = fake.record["cwd"]
        run(backend.complete(request()))
        second = fake.record["cwd"]

        assert first != second
        assert not Path(first).exists()
        assert not Path(second).exists()


class TestConfiguredTempRoot:
    """A ``temp_root`` in the endpoint config must reach ``run_cli`` -- for
    BOTH backends -- without touching argv in any way.

    ``CouncilWorker.endpoint_mapping`` threads a configured ``cli_temp_root``
    through to each backend's own ``config`` mapping under the key
    ``temp_root`` (``engine/worker.py``).  Before this suite, ``_run`` on
    neither backend read that key back out, so the value could never reach
    ``run_cli`` and every child still built its per-call directory under the
    platform default -- exactly the gap this task closes.  ``TestStdinViaFile``
    already proves ``run_cli`` itself honours ``temp_root``; these tests prove
    each backend's ``_run`` actually passes it through, and that doing so
    leaves the argv it builds byte-for-byte unchanged.
    """

    def test_claude_backend_threads_a_configured_temp_root_to_the_cwd(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        root = tmp_path / "configured-temp-root"
        root.mkdir()
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(
            str(fake),
            _home=ready_home,
            _parent_env={},
            config={"temp_root": str(root)},
        )

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        child_cwd = Path(fake.record["cwd"])
        assert root in child_cwd.parents
        # The one thing this task must not touch: the exact argv from
        # ``TestClaudeArgv.test_the_exact_argv``, repeated here so a
        # ``temp_root`` regression on argv is caught right next to the
        # change that carries the risk.
        assert fake.record["argv"] == [
            str(fake),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(SCHEMA),
            "--model",
            MODEL,
            "--system-prompt",
            SYSTEM,
            "--no-session-persistence",
            "--tools",
            "",
        ]

    def test_claude_backend_without_a_temp_root_reproduces_base_behaviour(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        not_the_root = tmp_path / "not-configured"
        not_the_root.mkdir()
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        child_cwd = Path(fake.record["cwd"])
        assert not_the_root not in child_cwd.parents
        assert tmp_path not in child_cwd.parents, "still outside the test's own tree"
        assert fake.record["argv"] == [
            str(fake),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(SCHEMA),
            "--model",
            MODEL,
            "--system-prompt",
            SYSTEM,
            "--no-session-persistence",
            "--tools",
            "",
        ]

    def test_codex_backend_threads_a_configured_temp_root_to_the_cwd(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        root = tmp_path / "configured-temp-root-codex"
        root.mkdir()
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(
            str(fake),
            _home=ready_home,
            _parent_env={},
            config={"temp_root": str(root)},
        )

        result = run(
            backend.complete(request(seat_kw={"endpoint_kind": "codex_cli"}))
        )

        assert result.ok is True, result.error
        record = fake.record
        cwd = Path(record["cwd"])
        assert root in cwd.parents
        # Codex's argv embeds the cwd itself (the schema/output file paths),
        # so it is compared against paths built from the CHILD'S OWN reported
        # cwd, exactly as ``TestCodexArgv.test_the_exact_argv`` does -- a
        # ``temp_root`` regression on the surrounding flags would still fail
        # this even though the embedded paths move with the directory.
        assert _argv_realpaths(record["argv"]) == _argv_realpaths(
            [
                str(fake),
                "exec",
                "--model",
                MODEL,
                "--output-schema",
                str(cwd / CODEX_SCHEMA_NAME),
                "-o",
                str(cwd / CODEX_OUTPUT_NAME),
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-",
            ]
        )

    def test_codex_backend_without_a_temp_root_reproduces_base_behaviour(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        not_the_root = tmp_path / "not-configured-codex"
        not_the_root.mkdir()
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(
            backend.complete(request(seat_kw={"endpoint_kind": "codex_cli"}))
        )

        assert result.ok is True, result.error
        record = fake.record
        cwd = Path(record["cwd"])
        assert not_the_root not in cwd.parents
        assert tmp_path not in cwd.parents, "still outside the test's own tree"
        assert _argv_realpaths(record["argv"]) == _argv_realpaths(
            [
                str(fake),
                "exec",
                "--model",
                MODEL,
                "--output-schema",
                str(cwd / CODEX_SCHEMA_NAME),
                "-o",
                str(cwd / CODEX_OUTPUT_NAME),
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-",
            ]
        )


# ---------------------------------------------------------------------------
# The prompt travels as a FILE, not a pipe
# ---------------------------------------------------------------------------


class TestStdinViaFile:
    """``run_cli`` hands the child its prompt as a file, not a pipe.

    ``proc.communicate(stdin_text.encode())`` used to pump the child's stdin
    from this coroutine, on the same event loop that also drives the
    council's synchronous store I/O; when that loop stalled, the child gave
    up waiting for its prompt and exited with "Input must be provided
    through stdin" -- measured destroying 23 of 24 Claude attempts on one
    19,276-item production run.  These tests prove the fix directly against
    ``run_cli``, independent of either backend's argv construction.
    """

    def test_the_child_receives_the_complete_prompt(self, tmp_path: Path) -> None:
        prompt = "\n".join(f"item {i}: some evidence string" for i in range(500))
        fake = make_fake(tmp_path, "echoer")

        code, _out, _err, _elapsed = run(
            run_cli(
                [str(fake)],
                prompt,
                {"PATH": os.environ.get("PATH", "")},
                30.0,
            )
        )

        assert code == 0
        assert fake.record["stdin"] == prompt

    def test_the_prompt_never_appears_in_argv(self, tmp_path: Path) -> None:
        prompt = "a secret-shaped prompt: ANTHROPIC_API_KEY=sk-should-not-leak"
        fake = make_fake(tmp_path, "echoer")

        run(
            run_cli(
                [str(fake)],
                prompt,
                {"PATH": os.environ.get("PATH", "")},
                30.0,
            )
        )

        argv = fake.record["argv"]
        assert prompt not in argv
        assert prompt not in " ".join(argv)

    def test_the_prompt_file_is_gone_after_run_cli_returns(
        self, tmp_path: Path
    ) -> None:
        fake = make_fake(tmp_path, "echoer")
        seen: Dict[str, Any] = {}

        def collect(cwd: Path) -> None:
            path = cwd / STDIN_FILENAME
            # While the child is (or was) running, the prompt really was on
            # disk inside the per-call directory -- this is the one moment
            # that is still observable.
            assert path.is_file(), "the prompt must live on disk during the call"
            assert path.read_text(encoding="utf-8") == "the exact prompt text"
            seen["path"] = path

        run(
            run_cli(
                [str(fake)],
                "the exact prompt text",
                {"PATH": os.environ.get("PATH", "")},
                30.0,
                collect=collect,
            )
        )

        assert not seen["path"].exists(), "the prompt must not outlive the call"
        assert not seen["path"].parent.exists(), "its directory goes with it"

    def test_temp_root_places_the_working_directory_under_it(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "custom-temp-root"
        root.mkdir()
        fake = make_fake(tmp_path, "echoer")

        run(
            run_cli(
                [str(fake)],
                "hi",
                {"PATH": os.environ.get("PATH", "")},
                30.0,
                temp_root=root,
            )
        )

        child_cwd = Path(fake.record["cwd"])
        assert root in child_cwd.parents

    def test_temp_root_none_preserves_todays_default_location(
        self, tmp_path: Path
    ) -> None:
        not_the_root = tmp_path / "not-the-root"
        not_the_root.mkdir()
        fake = make_fake(tmp_path, "echoer")

        run(
            run_cli(
                [str(fake)],
                "hi",
                {"PATH": os.environ.get("PATH", "")},
                30.0,
            )
        )

        child_cwd = Path(fake.record["cwd"])
        assert not_the_root not in child_cwd.parents
        assert tmp_path not in child_cwd.parents, "still outside the test's own tree"

    def test_a_child_that_never_reads_stdin_does_not_hang_or_raise(
        self, tmp_path: Path
    ) -> None:
        """The regression that matters most: the pipe version stalled right here."""
        fake = make_fake(tmp_path, "silent", stdout="ok\n", read_stdin=False)

        started = time.monotonic()
        code, out, _err, _elapsed = run(
            run_cli(
                [str(fake)],
                "a prompt that is never read",
                {"PATH": os.environ.get("PATH", "")},
                10.0,
            )
        )
        elapsed = time.monotonic() - started

        assert code == 0
        assert out == "ok\n"
        assert elapsed < 5.0, "it should not have waited at all, let alone hung"

    def test_the_timeout_path_still_kills_the_process_group(
        self, tmp_path: Path
    ) -> None:
        """Unchanged behaviour: file-backed stdin must not weaken the drain contract."""
        from kbutillib.domains.ai.council.types import CallTimeout

        fake = make_fake(tmp_path, "sleeper", sleep=30.0)

        with pytest.raises(CallTimeout):
            run(
                run_cli(
                    [str(fake)],
                    "hi",
                    {"PATH": os.environ.get("PATH", "")},
                    1.0,
                )
            )

        pgid = fake.record["pgid"]
        for _ in range(50):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.1)
        else:  # pragma: no cover - only on a real leak
            pytest.fail(f"process group {pgid} survived the timeout")


# ---------------------------------------------------------------------------
# The environment allowlist
# ---------------------------------------------------------------------------


class TestTheEnvironmentAllowlist:
    """Nothing reaches the child that was not asked for by name."""

    @pytest.mark.parametrize("which", ["claude", "codex"])
    def test_no_billing_variable_reaches_the_child(
        self, tmp_path: Path, ready_home: Path, which: str
    ) -> None:
        parent = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(ready_home),
            "ANTHROPIC_API_KEY": "sk-ant-should-never-travel",
            "ANTHROPIC_AUTH_TOKEN": "should-never-travel",
            "ANTHROPIC_BASE_URL": "https://not-anthropic.example",
            "OPENAI_API_KEY": "sk-oai-should-never-travel",
            "OPENAI_BASE_URL": "https://not-openai.example",
            "CODEX_API_KEY": "should-never-travel",
            "CLAUDE_CODE_SESSION_ID": "should-never-travel",
            "CLAUDE_CODE_ENTRYPOINT": "should-never-travel",
        }
        if which == "claude":
            fake = make_fake(tmp_path, "claude", stdout=envelope())
            backend: Any = ClaudeCliBackend(
                str(fake), _home=ready_home, _parent_env=parent
            )
        else:
            fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
            backend = CodexCliBackend(
                str(fake), _home=ready_home, _parent_env=parent
            )

        result = run(backend.complete(request()))
        assert result.ok is True, result.error

        child = fake.record["env"]
        for name in FORBIDDEN_IN_CHILD:
            assert name not in child, f"{name} reached the CLI child"
        for name in DENIED_BILLING_VARS:
            assert name not in child
        assert "CLAUDE_CODE_ENTRYPOINT" not in child
        # ... and no value of theirs arrived under some other name.
        assert not any("should-never-travel" in v for v in child.values())
        assert not any("sk-ant" in v or "sk-oai" in v for v in child.values())

    def test_the_child_env_is_exactly_the_allowlist(self) -> None:
        """Built from an empty dict, so an unlisted name cannot appear."""
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/home/someone",
            "LANG": "en_US.UTF-8",
            "ANTHROPIC_API_KEY": "sk-x",
            "SOMETHING_ELSE": "x",
        }
        assert _child_env("claude_cli", parent) == {
            "PATH": "/usr/bin",
            "HOME": "/home/someone",
            "LANG": "en_US.UTF-8",
        }

    def test_the_claude_oauth_token_and_config_dir_do_travel(self) -> None:
        """A subscription credential is the point, so it is the one exception."""
        parent = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-value",
            "CLAUDE_CONFIG_DIR": "/elsewhere/.claude",
            "CLAUDE_CODE_SESSION_ID": "nope",
        }
        env = _child_env("claude_cli", parent)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-value"
        assert env["CLAUDE_CONFIG_DIR"] == "/elsewhere/.claude"
        assert "CLAUDE_CODE_SESSION_ID" not in env

    def test_codex_home_travels_but_claude_variables_do_not(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "CODEX_HOME": "/elsewhere/.codex",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-value",
            "CODEX_API_KEY": "nope",
        }
        env = _child_env("codex_cli", parent)
        assert env["CODEX_HOME"] == "/elsewhere/.codex"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "CODEX_API_KEY" not in env

    def test_an_absent_allowlisted_name_is_simply_absent(self) -> None:
        """No empty-string placeholders: ``HOME=""`` would break the CLI worse."""
        env = _child_env("claude_cli", {"PATH": "/usr/bin"})
        assert env == {"PATH": "/usr/bin"}
        for name in CHILD_ENV_ALLOWLIST:
            if name != "PATH":
                assert name not in env

    def test_an_unknown_kind_gets_the_bare_allowlist(self) -> None:
        parent = {"PATH": "/usr/bin", "CODEX_HOME": "/x", "CLAUDE_CONFIG_DIR": "/y"}
        assert _child_env("something_else", parent) == {"PATH": "/usr/bin"}


class TestDroppedBillingVars:
    """Report which denied variables were present -- by NAME, never by value."""

    def test_names_only_and_sorted(self) -> None:
        parent = {
            "ANTHROPIC_API_KEY": "sk-secret-value",
            "OPENAI_API_KEY": "sk-another-secret",
            "CLAUDE_CODE_SESSION_ID": "sid",
            "PATH": "/usr/bin",
        }
        names = dropped_billing_vars(parent)

        assert names == [
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_SESSION_ID",
            "OPENAI_API_KEY",
        ]
        assert names == sorted(names)
        joined = " ".join(names)
        assert "sk-secret-value" not in joined
        assert "sk-another-secret" not in joined

    def test_the_oauth_token_is_not_reported_as_dropped(self) -> None:
        """It is passed through, so reporting it as dropped would be a lie."""
        parent = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth", "CLAUDE_CONFIG_DIR": "/x"}
        assert dropped_billing_vars(parent) == []

    def test_an_empty_environment_drops_nothing(self) -> None:
        assert dropped_billing_vars({}) == []

    def test_every_claude_code_variable_is_caught_by_prefix(self) -> None:
        parent = {"CLAUDE_CODE_FUTURE_CREDENTIAL": "x", "CLAUDE_CODE_OAUTH_TOKEN": "y"}
        assert dropped_billing_vars(parent) == ["CLAUDE_CODE_FUTURE_CREDENTIAL"]


# ---------------------------------------------------------------------------
# The billing preflight
# ---------------------------------------------------------------------------


def write_claude_settings(home: Path, payload: Any, name: str = "settings.json") -> Path:
    """Write a Claude settings file (or raw text, for the malformed case)."""
    directory = home / ".claude"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def write_codex_auth(home: Path, payload: Any) -> Path:
    """Write a Codex ``auth.json`` (or raw text, for the malformed case)."""
    directory = home / ".codex"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "auth.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


class TestClaudeBillingPreflight:
    """Offline: config files only, no process, no secret value."""

    def test_an_anthropic_base_url_in_an_env_block_is_refused(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        write_claude_settings(home, {"env": {"ANTHROPIC_BASE_URL": "https://x.example"}})

        report = billing_preflight("claude_cli", parent_env={}, home=home)

        assert report["state"] == "refused"
        assert any("ANTHROPIC_BASE_URL" in r for r in report["reasons"])
        assert str(home / ".claude" / "settings.json") in report["checked"]

    @pytest.mark.parametrize(
        "key", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
    )
    def test_each_api_billing_key_is_refused(self, tmp_path: Path, key: str) -> None:
        home = tmp_path / "home"
        write_claude_settings(home, {"env": {key: "value"}})

        report = billing_preflight("claude_cli", parent_env={}, home=home)

        assert report["state"] == "refused"
        assert any(key in r for r in report["reasons"])
        # The VALUE must not be echoed back into a reason.
        assert not any("value" == r for r in report["reasons"])

    def test_an_api_key_helper_is_refused(self, tmp_path: Path) -> None:
        """``apiKeyHelper`` fetches an API key, so the call would not be subscription."""
        home = tmp_path / "home"
        write_claude_settings(home, {"apiKeyHelper": "/usr/local/bin/get-key"})

        report = billing_preflight("claude_cli", parent_env={}, home=home)

        assert report["state"] == "refused"
        assert any("apiKeyHelper" in r for r in report["reasons"])

    def test_clean_settings_are_ready(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_claude_settings(home, {"permissions": {"allow": ["Bash"]}, "hooks": {}})

        report = billing_preflight("claude_cli", parent_env={}, home=home)

        assert report["state"] == "ready"
        assert report["reasons"]

    def test_a_settings_local_file_is_read_too(self, tmp_path: Path) -> None:
        """The local override is where a per-machine API key would actually be put."""
        home = tmp_path / "home"
        write_claude_settings(home, {"permissions": {}})
        write_claude_settings(
            home, {"env": {"ANTHROPIC_API_KEY": "sk-x"}}, name="settings.local.json"
        )

        report = billing_preflight("claude_cli", parent_env={}, home=home)

        assert report["state"] == "refused"
        assert any("settings.local.json" in r for r in report["reasons"])

    def test_the_config_dir_override_is_read_as_well(self, tmp_path: Path) -> None:
        """A relocated config dir is where the real settings then live."""
        home = clean_home(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-x"}})
        )

        report = billing_preflight(
            "claude_cli", parent_env={"CLAUDE_CONFIG_DIR": str(elsewhere)}, home=home
        )

        assert report["state"] == "refused"
        assert str(elsewhere / "settings.json") in report["checked"]

    def test_malformed_settings_are_unknown_not_ready(self, tmp_path: Path) -> None:
        """"I could not tell" must never be reported as "I checked and it is fine"."""
        home = tmp_path / "home"
        write_claude_settings(home, "{this is not json")

        report = billing_preflight("claude_cli", parent_env={}, home=home)

        assert report["state"] == "unknown"

    def test_a_refusal_wins_over_an_unreadable_sibling(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_claude_settings(home, "{broken")
        write_claude_settings(
            home, {"apiKeyHelper": "x"}, name="settings.local.json"
        )

        report = billing_preflight("claude_cli", parent_env={}, home=home)

        assert report["state"] == "refused"

    def test_an_empty_env_block_is_ready(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_claude_settings(home, {"env": {}})
        assert billing_preflight("claude_cli", parent_env={}, home=home)["state"] == "ready"


class TestCodexBillingPreflight:
    """The key names here were read off this machine's real ``auth.json``."""

    def test_an_api_key_only_auth_file_is_refused(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_codex_auth(home, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"})

        report = billing_preflight("codex_cli", parent_env={}, home=home)

        assert report["state"] == "refused"
        assert any("OPENAI_API_KEY" in r for r in report["reasons"])
        assert "sk-x" not in " ".join(report["reasons"])

    def test_a_chatgpt_token_block_is_ready(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_codex_auth(
            home,
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "x",
                    "access_token": "y",
                    "refresh_token": "z",
                    "account_id": "a",
                },
                "last_refresh": "2026-10-15T00:00:00Z",
            },
        )

        report = billing_preflight("codex_cli", parent_env={}, home=home)

        assert report["state"] == "ready"

    def test_expired_tokens_are_still_ready_because_this_is_a_billing_check(
        self, tmp_path: Path
    ) -> None:
        """Liveness is the call's discovery to make; billing mode has not changed."""
        home = tmp_path / "home"
        write_codex_auth(
            home,
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "long-expired"},
                "last_refresh": "2024-01-01T00:00:00Z",
            },
        )

        assert billing_preflight("codex_cli", parent_env={}, home=home)["state"] == "ready"

    def test_tokens_win_when_an_api_key_is_also_present(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_codex_auth(
            home, {"OPENAI_API_KEY": "sk-x", "tokens": {"access_token": "y"}}
        )

        report = billing_preflight("codex_cli", parent_env={}, home=home)

        assert report["state"] == "ready"
        assert any("not used while" in r for r in report["reasons"])

    def test_malformed_auth_is_unknown(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_codex_auth(home, "not json at all {{{")

        assert billing_preflight("codex_cli", parent_env={}, home=home)["state"] == "unknown"

    def test_a_missing_auth_file_is_unknown(self, tmp_path: Path) -> None:
        home = tmp_path / "empty"
        home.mkdir()

        report = billing_preflight("codex_cli", parent_env={}, home=home)

        assert report["state"] == "unknown"
        assert str(home / ".codex" / "auth.json") in report["checked"]

    def test_neither_tokens_nor_key_is_unknown(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_codex_auth(home, {"auth_mode": "chatgpt", "tokens": {}})

        assert billing_preflight("codex_cli", parent_env={}, home=home)["state"] == "unknown"

    def test_codex_home_overrides_the_default_location(self, tmp_path: Path) -> None:
        home = clean_home(tmp_path)
        elsewhere = tmp_path / "other-codex"
        elsewhere.mkdir()
        (elsewhere / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-x"}))

        report = billing_preflight(
            "codex_cli", parent_env={"CODEX_HOME": str(elsewhere)}, home=home
        )

        assert report["state"] == "refused"
        assert report["checked"] == [str(elsewhere / "auth.json")]


class TestTheBillingGate:
    """What the backends DO with a preflight answer, before the first call."""

    def test_a_refused_preflight_halts_the_endpoint_without_running_anything(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        write_claude_settings(home, {"env": {"ANTHROPIC_BASE_URL": "https://x"}})
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "billing_guard"
        assert result.error.halts_endpoint is True
        assert not fake.invoked, "the CLI must not run at all when billing is refused"

    def test_an_unknown_preflight_refuses_the_first_call(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_codex_auth(home, "malformed {")
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(str(fake), _home=home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "billing_guard"
        assert result.error.halts_endpoint is True
        assert not fake.invoked

    def test_allow_unverified_lets_an_unknown_through(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        write_codex_auth(home, "malformed {")
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(
            str(fake), allow_unverified=True, _home=home, _parent_env={}
        )

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        assert result.parsed == ANSWER
        assert fake.invoked

    def test_allow_unverified_does_NOT_let_a_refusal_through(
        self, tmp_path: Path
    ) -> None:
        """"I know this bills wrongly" is a different claim from "I could not tell"."""
        home = tmp_path / "home"
        write_codex_auth(home, {"OPENAI_API_KEY": "sk-x"})
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(
            str(fake), allow_unverified=True, _home=home, _parent_env={}
        )

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "billing_guard"
        assert not fake.invoked

    def test_the_preflight_method_is_what_the_worker_calls(
        self, tmp_path: Path
    ) -> None:
        """``CouncilWorker._preflight`` reads ``state`` and a ``detail``/``message``."""
        home = tmp_path / "home"
        write_claude_settings(home, {"apiKeyHelper": "x"})
        backend = ClaudeCliBackend("claude", _home=home, _parent_env={})

        report = run(backend.preflight())

        assert report["state"] == "refused"
        assert report["detail"], "the worker reports this; empty means 'no reason given'"
        assert "apiKeyHelper" in report["detail"]

    def test_the_gate_is_cached_until_a_config_file_it_read_changes(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Cached per backend, invalidated by the FILES it was read from.

        M1 ran this gate exactly once per backend instance, on the reasoning
        that a billing fact cannot change mid-batch.  Review round 3
        (2026-10-15) retracted that: a user who runs ``claude login`` -- or
        edits ``settings.json`` -- while a long batch is running HAS changed
        how the next call bills, and a verdict cached for the life of the
        process would keep spending against the old answer.  So the gate now
        keeps the mtime/size of every path the preflight reported inspecting
        and re-runs as soon as one of them moves.

        Both halves matter and both are asserted here: an unchanged
        filesystem must NOT re-read anything (that was the point of caching),
        and a changed one must.
        """
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        preflights: list = []
        inner = backend.preflight

        async def counting_preflight() -> Dict[str, Any]:
            preflights.append(1)
            return await inner()

        backend.preflight = counting_preflight  # type: ignore[method-assign]

        assert run(backend.complete(request())).ok is True
        assert len(preflights) == 1

        # Nothing on disk moved: the second call must not stat-and-read again.
        assert run(backend.complete(request())).ok is True
        assert len(preflights) == 1

        # Make the settings hostile AFTER the gate has cleared.  The file it
        # read has changed, so the verdict it cached is no longer current.
        write_claude_settings(ready_home, {"apiKeyHelper": "x"})
        refused = run(backend.complete(request()))
        assert refused.ok is False
        assert refused.error is not None
        assert refused.error.kind == "billing_guard"
        assert len(preflights) == 2


# ---------------------------------------------------------------------------
# Parsing the Claude envelope
# ---------------------------------------------------------------------------


class TestClaudeEnvelopeParsing:
    def test_structured_output_is_preferred_over_result(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Both parse, so this really does prove which one won."""
        fake = make_fake(
            tmp_path,
            "claude",
            stdout=envelope(
                structured_output=ANSWER, result=json.dumps(OTHER_ANSWER)
            ),
        )
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        assert result.parsed == ANSWER
        assert result.parsed != OTHER_ANSWER
        assert result.schema_enforced is True

    def test_the_result_string_is_the_fallback_and_is_not_schema_enforced(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "claude", stdout=envelope(result=json.dumps(ANSWER)))
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        assert result.parsed == ANSWER
        assert result.schema_enforced is False

    def test_a_fenced_result_string_is_still_parsed(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """A model that decorated its JSON answered correctly."""
        fenced = f"```json\n{json.dumps(ANSWER)}\n```"
        fake = make_fake(tmp_path, "claude", stdout=envelope(result=fenced))
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        assert result.parsed == ANSWER
        assert result.schema_enforced is False

    def test_usage_is_read_from_the_envelope(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(
            tmp_path,
            "claude",
            stdout=envelope(usage={"input_tokens": 1234, "output_tokens": 56}),
        )
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.usage == {"prompt_tokens": 1234, "completion_tokens": 56}

    def test_no_usage_block_means_None_not_zero(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Zeros would make a budgeted endpoint look free for ever."""
        payload = json.loads(envelope())
        payload.pop("usage")
        fake = make_fake(tmp_path, "claude", stdout=json.dumps(payload))
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        assert result.usage is None

    def test_the_resolved_model_comes_from_model_usage_keys(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(
            tmp_path,
            "claude",
            stdout=envelope(modelUsage={"claude-opus-5": {"inputTokens": 1}}),
        )
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.resolved_model == "claude-opus-5"

    def test_without_model_usage_the_seat_model_is_reported(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        payload = json.loads(envelope())
        payload.pop("modelUsage")
        fake = make_fake(tmp_path, "claude", stdout=json.dumps(payload))
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        assert run(backend.complete(request())).resolved_model == MODEL

    def test_non_json_stdout_is_invalid_output(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "claude", stdout="Welcome to Claude Code!\n")
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"
        assert result.error.retryable is False

    def test_an_unparseable_result_string_is_invalid_output(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(
            tmp_path, "claude", stdout=envelope(result="I could not decide, sorry.")
        )
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"

    def test_an_envelope_with_neither_field_is_invalid_output(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "claude", stdout=json.dumps({"type": "result"}))
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"


class TestCodexOutputFile:
    def test_the_answer_is_read_from_the_output_file(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        assert result.parsed == ANSWER
        assert result.usage is None, "codex exec reports no token counts"
        assert result.resolved_model == MODEL

    def test_a_fenced_answer_is_still_parsed(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(
            tmp_path, "codex", write_output=f"```json\n{json.dumps(ANSWER)}\n```"
        )
        backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        assert run(backend.complete(request())).parsed == ANSWER

    def test_a_missing_output_file_is_invalid_output(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Exit 0 with nothing written is not an answer."""
        fake = make_fake(tmp_path, "codex", write_output=None)
        backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"
        assert CODEX_OUTPUT_NAME in result.error.message

    def test_an_empty_output_file_is_invalid_output(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "codex", write_output="   \n")
        backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"

    def test_non_json_output_is_invalid_output(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "codex", write_output="I think it is beta-gal.")
        backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "invalid_output"


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------


class TestFailureMapping:
    @pytest.mark.parametrize("which", ["claude", "codex"])
    def test_a_usage_limit_on_a_non_zero_exit_is_quota_exhausted(
        self, tmp_path: Path, ready_home: Path, which: str
    ) -> None:
        message = "Claude AI usage limit reached|1760000000"
        if which == "claude":
            fake = make_fake(tmp_path, "claude", stderr=message, exit_code=1)
            backend: Any = ClaudeCliBackend(
                str(fake), _home=ready_home, _parent_env={}
            )
        else:
            fake = make_fake(tmp_path, "codex", stderr=message, exit_code=1)
            backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "quota_exhausted"
        assert result.error.halts_endpoint is True
        assert result.error.retryable is False

    @pytest.mark.parametrize(
        "message",
        [
            "usage limit reached",
            "Rate limit exceeded, try later",
            "your limit reached for today",
            "monthly quota exhausted",
            "HTTP 429 from upstream",
            "Too Many Requests",
        ],
    )
    def test_every_quota_pattern_maps_to_quota_exhausted(
        self, tmp_path: Path, ready_home: Path, message: str
    ) -> None:
        fake = make_fake(tmp_path, "claude", stderr=message, exit_code=1)
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.error is not None
        assert result.error.kind == "quota_exhausted", message

    def test_every_declared_pattern_is_covered_by_the_table_above(self) -> None:
        """The parametrised list IS the module constant, not a subset of it."""
        covered = [
            "usage limit reached",
            "Rate limit exceeded, try later",
            "your limit reached for today",
            "monthly quota exhausted",
            "HTTP 429 from upstream",
            "Too Many Requests",
        ]
        for pattern in QUOTA_PATTERNS:
            assert any(pattern.search(m) for m in covered), (
                f"{pattern.pattern} has no example in the parametrised table"
            )

    @pytest.mark.parametrize(
        "message",
        ["You are not logged in", "authentication failed", "Please run /login"],
    )
    def test_authentication_failures_halt_the_endpoint_as_auth(
        self, tmp_path: Path, ready_home: Path, message: str
    ) -> None:
        fake = make_fake(tmp_path, "claude", stderr=message, exit_code=1)
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.error is not None
        assert result.error.kind == "auth", message
        assert result.error.halts_endpoint is True

    def test_quota_is_checked_before_auth(self) -> None:
        """"usage limit reached -- run /login to switch accounts" is a QUOTA refusal.

        Reading it as auth would send an operator to re-authenticate an
        account that is merely out of quota.
        """
        error = classify_failure(
            "claude", 1, "usage limit reached; run /login to switch accounts"
        )
        assert error.kind == "quota_exhausted"

    def test_another_non_zero_exit_is_a_retryable_transport_failure(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """A crashed node runtime is exactly what a second attempt fixes."""
        fake = make_fake(
            tmp_path, "claude", stderr="Segmentation fault (core dumped)", exit_code=139
        )
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "transport"
        assert result.error.retryable is True
        assert result.error.halts_endpoint is False

    def test_an_is_error_envelope_on_a_zero_exit_is_mapped_too(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """A usage limit is a usage limit whichever way the CLI delivers it."""
        fake = make_fake(
            tmp_path,
            "claude",
            stdout=envelope(is_error=True, result="Claude usage limit reached"),
        )
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "quota_exhausted"
        assert result.error.halts_endpoint is True

    def test_an_is_error_envelope_never_yields_a_parsed_answer(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Even with a parseable structured_output, is_error means no vote."""
        fake = make_fake(
            tmp_path,
            "claude",
            stdout=envelope(
                is_error=True, structured_output=ANSWER, result="something broke"
            ),
        )
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.parsed is None

    def test_a_missing_executable_is_a_typed_failure_not_a_raise(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """The protocol's hard rule: one bad seat cannot abort the item."""
        backend = ClaudeCliBackend(
            str(tmp_path / "does-not-exist"), _home=ready_home, _parent_env={}
        )

        result = run(backend.complete(request()))

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "transport"


class TestTimeout:
    """A deadline takes down the whole process group, not just the parent."""

    def test_a_slow_child_times_out_and_its_process_group_is_killed(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "claude", stdout=envelope(), sleep=30.0)
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        started = time.monotonic()
        result = run(backend.complete(request(seat_kw={"timeout_s": 1.0})))
        elapsed = time.monotonic() - started

        assert result.ok is False
        assert result.error is not None
        assert result.error.kind == "timeout"
        assert result.error.retryable is True
        assert elapsed < 20.0, "it waited for the child instead of killing it"

        # The fake wrote its record BEFORE sleeping, so we know its group id.
        pgid = fake.record["pgid"]
        for _ in range(50):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.1)
        else:  # pragma: no cover - only on a real leak
            pytest.fail(f"process group {pgid} survived the timeout")

    def test_the_temporary_directory_is_removed_even_on_timeout(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "codex", sleep=30.0)
        backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(request(seat_kw={"timeout_s": 1.0})))

        assert result.error is not None
        assert result.error.kind == "timeout"
        assert not Path(fake.record["cwd"]).exists()

    def test_run_cli_raises_call_timeout_directly(self, tmp_path: Path) -> None:
        """``run_cli`` is the one place a timeout is an exception by design."""
        from kbutillib.domains.ai.council.types import CallTimeout

        fake = make_fake(tmp_path, "sleeper", sleep=30.0)
        with pytest.raises(CallTimeout):
            run(run_cli([str(fake)], "", {"PATH": os.environ.get("PATH", "")}, 1.0))


# ---------------------------------------------------------------------------
# The no-injection check: the DEFAULT path, with nothing passed in
# ---------------------------------------------------------------------------


class TestTheDefaultPathInjectsNothing:
    """No ``_parent_env``, no ``_home`` -- the code resolves both itself.

    This is what catches the default-argument trap: ``parent_env=os.environ``
    or ``home=Path.home()`` written as a DEFAULT ARGUMENT is evaluated once at
    import, so a later ``monkeypatch.setenv`` would be invisible and every
    test that passed the environment explicitly would still pass.
    """

    @pytest.mark.parametrize("which", ["claude", "codex"])
    def test_no_billing_variable_reaches_the_child_on_the_default_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
    ) -> None:
        home = clean_home(tmp_path)
        # The REAL os.environ, not a dict handed to the backend.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-travel")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://not-anthropic.example")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-must-not-travel")
        monkeypatch.setenv("CODEX_API_KEY", "codex-must-not-travel")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-must-not-travel")

        if which == "claude":
            fake = make_fake(tmp_path, "claude", stdout=envelope())
            # NOTE: no _parent_env, no _home.
            backend: Any = ClaudeCliBackend(str(fake))
        else:
            fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
            backend = CodexCliBackend(str(fake))

        result = run(backend.complete(request()))

        assert result.ok is True, result.error
        child = fake.record["env"]
        for name in FORBIDDEN_IN_CHILD:
            assert name not in child, f"{name} reached the child on the default path"
        assert not any("must-not-travel" in value for value in child.values())
        # The allowlist did work -- this is not vacuously empty.
        assert child.get("HOME") == str(home)

    def test_billing_preflight_honours_a_reassigned_HOME_with_no_home_argument(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``billing_preflight("claude_cli")`` and nothing else."""
        home = tmp_path / "hostile-home"
        write_claude_settings(home, {"env": {"ANTHROPIC_BASE_URL": "https://x.example"}})
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

        report = billing_preflight("claude_cli")

        assert report["state"] == "refused"
        assert str(home / ".claude" / "settings.json") in report["checked"]

    def test_billing_preflight_honours_a_reassigned_HOME_for_codex_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "apikey-home"
        write_codex_auth(home, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"})
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CODEX_HOME", raising=False)

        assert billing_preflight("codex_cli")["state"] == "refused"

    def test_dropped_billing_vars_reads_the_live_environment_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        assert "ANTHROPIC_API_KEY" in dropped_billing_vars()

    def test_child_env_reads_the_live_environment_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.setenv("LANG", "en_GB.UTF-8")

        env = _child_env("claude_cli")

        assert "ANTHROPIC_API_KEY" not in env
        assert env["LANG"] == "en_GB.UTF-8"


# ---------------------------------------------------------------------------
# The rest of the protocol
# ---------------------------------------------------------------------------


class TestTheProtocolSurface:
    @pytest.mark.parametrize("cls", [ClaudeCliBackend, CodexCliBackend])
    def test_both_backends_satisfy_the_backend_protocol(self, cls: Any) -> None:
        assert isinstance(cls("exe"), Backend)

    @pytest.mark.parametrize("cls", [ClaudeCliBackend, CodexCliBackend])
    def test_effective_params_is_the_model_alone(self, cls: Any) -> None:
        """Neither CLI accepts a temperature or a token ceiling."""
        backend = cls("exe")
        params = backend.effective_params(
            seat(temperature=0.7, max_output_tokens=8000)
        )
        assert params == {"model": MODEL}

    @pytest.mark.parametrize("cls", [ClaudeCliBackend, CodexCliBackend])
    def test_models_is_empty_because_neither_lists_for_free(self, cls: Any) -> None:
        assert run(cls("exe").models()) == []

    @pytest.mark.parametrize("cls", [ClaudeCliBackend, CodexCliBackend])
    def test_aclose_is_safe_to_call(self, cls: Any) -> None:
        assert run(cls("exe").aclose()) is None

    @pytest.mark.parametrize("cls", [ClaudeCliBackend, CodexCliBackend])
    def test_the_kind_matches_the_seat_kinds_the_worker_routes_on(
        self, cls: Any
    ) -> None:
        assert cls("exe").kind in ("claude_cli", "codex_cli")

    def test_version_runs_version_and_nothing_else(self, tmp_path: Path) -> None:
        """``kbu council doctor`` reads this; it must send no prompt."""
        fake = make_fake(tmp_path, "claude", stdout="2.1.269 (Claude Code)\n")
        backend = ClaudeCliBackend(str(fake), _parent_env={})

        assert run(backend.version()) == "2.1.269 (Claude Code)"
        record = fake.record
        assert record["argv"] == [str(fake), "--version"]
        assert record["stdin"] == ""

    def test_version_is_None_when_the_executable_is_missing(
        self, tmp_path: Path
    ) -> None:
        backend = ClaudeCliBackend(str(tmp_path / "nope"), _parent_env={})
        assert run(backend.version()) is None

    def test_version_is_None_on_a_non_zero_exit(self, tmp_path: Path) -> None:
        fake = make_fake(tmp_path, "codex", stderr="boom", exit_code=1)
        backend = CodexCliBackend(str(fake), _parent_env={})
        assert run(backend.version()) is None

    def test_the_executable_defaults_to_the_cli_name(self) -> None:
        assert ClaudeCliBackend().executable == "claude"
        assert CodexCliBackend().executable == "codex"

    def test_an_endpoint_config_can_name_the_executable(self) -> None:
        backend = CodexCliBackend(config={"executable": "/opt/bin/codex", "name": "cx"})
        assert backend.executable == "/opt/bin/codex"
        assert backend.name == "cx"


# ---------------------------------------------------------------------------
# Web search: opt-in only, through the REAL execution path
# ---------------------------------------------------------------------------


def llm_request(**kw: Any) -> LLMRequest:
    """An :class:`LLMRequest` carrying the same prompts as :func:`request`.

    The web-search opt-in lives in ``params``, which is the llm layer's own
    vocabulary, so these tests go in through ``LLMRequest`` rather than
    through the council's ``CompletionRequest``.  ``complete()`` accepts both.
    """
    base: Dict[str, Any] = {
        "model": MODEL,
        "system": SYSTEM,
        "user": USER,
        "json_schema": SCHEMA,
        "timeout_s": 30,
    }
    base.update(kw)
    return LLMRequest(**base)


def _fill(golden: list, **subs: str) -> list:
    """*golden* with each whole-entry ``{name}`` placeholder replaced.

    Entry-by-entry rather than ``str.format``, because one of the golden
    entries is a JSON schema whose braces ``format`` would try to read as
    fields.  Every other entry is compared verbatim, which is the point of a
    golden list.
    """
    return [subs.get(entry[1:-1], entry) if entry in {
        "{" + name + "}" for name in subs
    } else entry for entry in golden]


#: The Claude command line as it stood at the base commit, before web search
#: existed.  A LITERAL list on purpose: deriving it from the code under test
#: would assert that ``argv`` equals itself.  ``{exe}`` is substituted.
CLAUDE_BASE_GOLDEN = [
    "{exe}",
    "-p",
    "--output-format",
    "json",
    "--json-schema",
    json.dumps(SCHEMA),
    "--model",
    MODEL,
    "--system-prompt",
    SYSTEM,
    "--no-session-persistence",
    "--tools",
    "",
]

#: The Codex command line as it stood at the base commit.  ``{exe}``,
#: ``{schema}`` and ``{output}`` are substituted.
CODEX_BASE_GOLDEN = [
    "{exe}",
    "exec",
    "--model",
    MODEL,
    "--output-schema",
    "{schema}",
    "-o",
    "{output}",
    "--ephemeral",
    "--ignore-user-config",
    "--sandbox",
    "read-only",
    "--skip-git-repo-check",
    "-",
]


class TestClaudeWebSearchArgv:
    """``params={"web_search": True}`` changes the ``--tools`` pair and nothing else.

    Every test drives the backend's REAL execution path: a fake executable on
    disk, invoked by the backend's own ``run_cli``/``create_subprocess_exec``,
    which writes the argv it was handed to a JSON file.  The assertions read
    that file.  Nothing is monkeypatched -- not ``argv``, not the runner --
    so a pass is evidence about the shipped command line.
    """

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param(None, id="absent"),
            pytest.param({}, id="empty"),
            pytest.param({"web_search": False}, id="false"),
            pytest.param({"web_search": None}, id="none"),
        ],
        ids=None,
    )
    def test_without_the_opt_in_the_argv_is_the_base_commit_s_byte_for_byte(
        self, tmp_path: Path, ready_home: Path, params: Optional[Dict[str, Any]]
    ) -> None:
        """The default path must not move: the council is a live consumer."""
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(
            backend.complete(
                llm_request() if params is None else llm_request(params=params)
            )
        )

        assert result.ok is True, result.error
        assert fake.record["argv"] == _fill(CLAUDE_BASE_GOLDEN, exe=str(fake))

    def test_the_opt_in_carries_the_documented_flags(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """``--tools WebSearch,WebFetch`` plus the variadic ``--allowedTools``.

        Both spellings are from the installed ``claude --help`` (2.1.269),
        quoted in ``llm/apis/cli_common.py``: ``--tools`` takes a
        comma-separated list, ``--allowedTools`` is variadic.
        """
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        result = run(backend.complete(llm_request(params={"web_search": True})))

        assert result.ok is True, result.error
        assert fake.record["argv"] == [
            str(fake),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(SCHEMA),
            "--model",
            MODEL,
            "--system-prompt",
            SYSTEM,
            "--no-session-persistence",
            "--tools",
            "WebSearch,WebFetch",
            "--allowedTools",
            "WebSearch",
            "WebFetch",
        ]

    def test_the_empty_tools_string_is_gone_rather_than_kept_alongside(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """``--tools ""`` and ``--tools WebSearch,WebFetch`` would contradict.

        The opt-in REPLACES the pair; it does not append a second one, which
        a CLI would resolve by last-wins or by erroring and either way would
        make the command line a guess.
        """
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(llm_request(params={"web_search": True})))

        argv = fake.record["argv"]
        assert argv.count("--tools") == 1
        assert argv[argv.index("--tools") + 1] == CLAUDE_WEB_SEARCH_TOOLS
        assert "" not in argv

    def test_the_allowed_tools_pair_is_last_so_nothing_is_swallowed(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """``--allowedTools`` is variadic and eats every following plain token."""
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(llm_request(params={"web_search": True})))

        argv = fake.record["argv"]
        tail = argv[argv.index("--allowedTools") + 1 :]
        assert tail == list(CLAUDE_WEB_SEARCH_ALLOWED_TOOLS)
        assert not any(entry.startswith("-") for entry in tail)

    def test_the_forbidden_flags_stay_absent_with_web_search_on(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Opting into two named tools must not widen anything else."""
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(llm_request(params={"web_search": True})))

        argv = fake.record["argv"]
        assert "--bare" not in argv
        assert "--dangerously-skip-permissions" not in argv
        assert "--allow-dangerously-skip-permissions" not in argv
        assert "--add-dir" not in argv

    def test_the_prompt_still_arrives_on_stdin_and_not_in_argv(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(backend.complete(llm_request(params={"web_search": True})))

        record = fake.record
        assert record["stdin"] == USER
        assert not any(USER in entry for entry in record["argv"])

    def test_a_council_seat_can_opt_in_through_its_extra_block(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Configuration route: ``SeatSpec.extra`` becomes ``LLMRequest.params``.

        This is how an endpoint turns search on without a caller writing
        code, so the two routes must produce the same command line.
        """
        fake = make_fake(tmp_path, "claude", stdout=envelope())
        backend = ClaudeCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(
            backend.complete(
                request(seat_kw={"extra": {"web_search": True}})
            )
        )

        argv = fake.record["argv"]
        assert argv[argv.index("--tools") + 1] == CLAUDE_WEB_SEARCH_TOOLS
        assert "--allowedTools" in argv


class TestCodexWebSearchArgv:
    """``--search`` goes BEFORE ``exec``, and the sandbox does not move.

    Same real execution path as the Claude tests: a fake ``codex`` on disk,
    the backend's own subprocess call, assertions against the argv file the
    child wrote.
    """

    def _run(
        self, tmp_path: Path, home: Path, **req_kw: Any
    ) -> Fake:
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(str(fake), _home=home, _parent_env={})
        result = run(backend.complete(llm_request(**req_kw)))
        assert result.ok is True, result.error
        return fake

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param(None, id="absent"),
            pytest.param({}, id="empty"),
            pytest.param({"web_search": False}, id="false"),
            pytest.param({"web_search": None}, id="none"),
        ],
    )
    def test_without_the_opt_in_the_argv_is_the_base_commit_s_byte_for_byte(
        self, tmp_path: Path, ready_home: Path, params: Optional[Dict[str, Any]]
    ) -> None:
        fake = self._run(
            tmp_path, ready_home, **({} if params is None else {"params": params})
        )
        record = fake.record
        cwd = Path(record["cwd"])

        assert _argv_realpaths(record["argv"]) == _argv_realpaths(
            _fill(
                CODEX_BASE_GOLDEN,
                exe=str(fake),
                schema=str(cwd / CODEX_SCHEMA_NAME),
                output=str(cwd / CODEX_OUTPUT_NAME),
            )
        )

    def test_the_opt_in_adds_search_before_the_exec_subcommand(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Measured on codex-cli 0.160.0: ``codex exec --search`` exits 2.

        ``--search`` is a TOP-LEVEL option -- it is in ``codex --help`` and
        not in ``codex exec --help`` -- so its position is the whole
        correctness question here, not a style choice.  The help lines and
        both probe exit codes are quoted in ``llm/apis/cli_common.py``.
        """
        fake = self._run(tmp_path, ready_home, params={"web_search": True})
        record = fake.record
        cwd = Path(record["cwd"])

        assert _argv_realpaths(record["argv"]) == _argv_realpaths(
            [
                str(fake),
                CODEX_WEB_SEARCH_FLAG,
                "exec",
                "--model",
                MODEL,
                "--output-schema",
                str(cwd / CODEX_SCHEMA_NAME),
                "-o",
                str(cwd / CODEX_OUTPUT_NAME),
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "-",
            ]
        )
        argv = record["argv"]
        assert argv.index(CODEX_WEB_SEARCH_FLAG) < argv.index("exec")

    def test_the_sandbox_stays_read_only_and_the_user_config_stays_ignored(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        """Search is the provider reading the web, not the agent writing to disk.

        Neither reason to be strict has gone away: a wider sandbox would let
        the child write, and loading ``config.toml`` could re-route the call
        to a metered provider.
        """
        argv = self._run(
            tmp_path, ready_home, params={"web_search": True}
        ).record["argv"]

        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in argv
        assert "--ephemeral" in argv
        assert "--skip-git-repo-check" in argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert "--dangerously-bypass-hook-trust" not in argv
        assert "--add-dir" not in argv

    def test_the_prompt_still_arrives_on_stdin_and_not_in_argv(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        record = self._run(
            tmp_path, ready_home, params={"web_search": True}
        ).record
        assert record["stdin"] == f"{SYSTEM}\n\n{USER}"
        assert record["argv"][-1] == "-"
        assert not any(USER in entry for entry in record["argv"])

    def test_a_council_seat_can_opt_in_through_its_extra_block(
        self, tmp_path: Path, ready_home: Path
    ) -> None:
        fake = make_fake(tmp_path, "codex", write_output=json.dumps(ANSWER))
        backend = CodexCliBackend(str(fake), _home=ready_home, _parent_env={})

        run(
            backend.complete(
                request(
                    seat_kw={
                        "endpoint_kind": "codex_cli",
                        "extra": {"web_search": True},
                    }
                )
            )
        )

        argv = fake.record["argv"]
        assert argv[1] == CODEX_WEB_SEARCH_FLAG


class TestCliBackendsDeclareWebSearchSupport:
    """Both CLI backends report the capability, as a free class attribute."""

    @pytest.mark.parametrize("cls", [ClaudeCliBackend, CodexCliBackend])
    def test_the_class_attribute_is_true(self, cls: Any) -> None:
        # On the CLASS, so a caller can ask before constructing anything and
        # certainly before spending a token.
        assert cls.supports_web_search is True
        assert cls("no-such-cli").supports_web_search is True

    def test_the_shared_base_defaults_to_false(self) -> None:
        """A subclass that has not thought about it inherits the safe answer."""
        from kbutillib.domains.ai.llm.apis.cli_common import CliBackend

        assert CliBackend.supports_web_search is False
