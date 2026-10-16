"""The council's two headless-CLI backends: Claude Code and Codex.

Both run a local CLI once per invocation, as a child process with an
allowlisted environment and a throwaway working directory, and both are
billed against an interactive **subscription** rather than an API key.  The
machinery that makes that true -- the environment allowlist, the offline
billing preflight, the process-group kill on timeout -- lives in
:mod:`.cli_common`, which also records the CLI versions, config paths and
config key names this module was verified against.

The two differ in exactly how a schema goes in and an answer comes out:

* **Claude** takes the JSON Schema inline (``--json-schema <json>``) and
  answers on **stdout**, as a JSON result envelope that carries the answer
  plus usage and model metadata.  When it enforced the schema the envelope
  has a ``structured_output`` object, which is the answer already parsed --
  so ``schema_enforced`` is a FACT there, not a hope.
* **Codex** takes the schema as a **file** (``--output-schema <file>``) and
  writes its final message to another file (``-o <file>``), so the answer is
  read off disk after the child exits and there is no envelope, no usage and
  no resolved-model metadata to read.

Neither accepts a temperature or an output-token ceiling, which is why
``effective_params`` is just the model name for both.

Prompt text is never logged at INFO or above, and never appears in ``argv``:
it goes in on stdin, so it cannot leak through ``ps`` to another user on a
shared host.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..types import (
    InvalidOutput,
    LLMRequest,
    LLMResult,
)
from .cli_common import (
    OUTPUT_EXCERPT_CHARS,
    CliBackend,
    classify_failure,
    run_cli,
    strip_fences,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CODEX_OUTPUT_NAME",
    "CODEX_SCHEMA_NAME",
    "ClaudeCliBackend",
    "CodexCliBackend",
]

#: Names of the two files Codex is pointed at inside the per-call working
#: directory.  Plain constants rather than literals so the test can name the
#: same files it expects to see in ``argv``.
CODEX_SCHEMA_NAME = "schema.json"
CODEX_OUTPUT_NAME = "last.txt"


def _int_or_none(value: Any) -> Optional[int]:
    """Coerce a reported token count to ``int``, or ``None`` if it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ClaudeCliBackend(CliBackend):
    """A council seat served by the headless ``claude`` CLI.

    The argv is fixed and deliberately minimal -- ``-p`` (print and exit),
    ``--output-format json``, the schema, the model, the system prompt,
    ``--no-session-persistence`` and ``--tools ""``.  Two flags are
    conspicuously absent and must stay absent:

    * **``--bare``** would route authentication to ``ANTHROPIC_API_KEY`` or
      ``apiKeyHelper`` and never read OAuth or the keychain (its own
      ``--help`` says so, quoted in :mod:`.cli_common`).  Passing it would
      move the call onto metered API billing, i.e. defeat the entire purpose
      of a CLI seat.
    * **``--dangerously-skip-permissions``** would bypass every permission
      check.  A council seat picks ids from a closed menu; it has no business
      touching the filesystem at all, which is what ``--tools ""`` says.
    """

    kind = "claude_cli"
    default_executable = "claude"

    def __init__(
        self,
        executable: Optional[str] = None,
        *,
        allow_unverified: bool = False,
        config: Optional[Mapping[str, Any]] = None,
        _home: Optional[Path] = None,
        _parent_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(
            executable,
            allow_unverified=allow_unverified,
            config=config,
            _home=_home,
            _parent_env=_parent_env,
        )

    def argv(self, req: LLMRequest) -> List[str]:
        """The exact command line for *req*.

        Public because it is the security-relevant surface of this backend:
        the test asserts it flag by flag, and a reviewer should be able to
        read it without following it through a subprocess call.  The USER
        prompt is NOT here -- it goes in on stdin.
        """
        return [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(req.json_schema or {}),
            "--model",
            req.model,
            "--system-prompt",
            req.system,
            "--no-session-persistence",
            "--tools",
            "",
        ]

    async def _run(self, req: LLMRequest, started: float) -> LLMResult:
        """Run one ``claude -p`` invocation and read its result envelope."""
        # DEBUG only: the prompt is the user's data, and a council batch at
        # INFO would otherwise dump every item's evidence into the log.
        logger.debug(
            "council %s backend %s: asking %s (%d chars of prompt)",
            self.kind,
            self.name,
            req.model,
            len(req.user or ""),
        )
        code, out, err, _elapsed = await run_cli(
            self.argv(req),
            req.user,
            self._env(),
            self._timeout(req),
        )

        if code != 0:
            # stderr first: that is where both CLIs put the reason.
            return self._failed(
                classify_failure(self.executable, code, err or out).to_error(),
                started,
                text=out or None,
            )

        try:
            envelope = json.loads(out)
        except ValueError:
            return self._failed(
                InvalidOutput(
                    f"{self.name}: --output-format json did not produce JSON: "
                    f"{(out or '')[:OUTPUT_EXCERPT_CHARS]!r}"
                ).to_error(),
                started,
                text=out or None,
            )
        if not isinstance(envelope, Mapping):
            return self._failed(
                InvalidOutput(
                    f"{self.name}: result envelope was {type(envelope).__name__}, "
                    "not an object"
                ).to_error(),
                started,
                text=out or None,
            )

        usage = self._usage(envelope)
        resolved = self._resolved_model(envelope) or req.model

        if envelope.get("is_error"):
            # A zero exit with is_error set is how the CLI reports a refusal
            # it handled itself, so the same patterns apply as to a crash --
            # a usage limit is a usage limit whichever way it is delivered.
            text = self._envelope_text(envelope)
            return self._failed(
                classify_failure(self.executable, None, text).to_error(),
                started,
                text=text or out or None,
            )

        structured = envelope.get("structured_output")
        if isinstance(structured, Mapping):
            # The CLI validated the answer against the schema we handed it, so
            # this is a measured fact rather than an assumption.
            return LLMResult(
                ok=True,
                text=self._envelope_text(envelope) or json.dumps(dict(structured)),
                parsed=dict(structured),
                resolved_model=resolved,
                usage=usage,
                elapsed_s=time.monotonic() - started,
                schema_enforced=True,
            )

        # No structured output: fall back to parsing the free-text result.
        # The schema was requested but demonstrably not enforced on the way
        # out, so say ``False`` -- the engine's own validation is then the
        # only thing standing between a malformed answer and a ballot.
        result_text = envelope.get("result")
        if not isinstance(result_text, str) or not result_text.strip():
            return self._failed(
                InvalidOutput(
                    f"{self.name}: envelope had neither 'structured_output' nor a "
                    f"non-empty 'result' string (keys: "
                    f"{sorted(str(k) for k in envelope)})"
                ).to_error(),
                started,
                text=out or None,
            )

        parsed = self._parse_json_object(result_text)
        if parsed is None:
            return self._failed(
                InvalidOutput(
                    f"{self.name}: 'result' was not a JSON object: "
                    f"{result_text[:OUTPUT_EXCERPT_CHARS]!r}"
                ).to_error(),
                started,
                text=result_text,
            )

        return LLMResult(
            ok=True,
            text=result_text,
            parsed=parsed,
            resolved_model=resolved,
            usage=usage,
            elapsed_s=time.monotonic() - started,
            schema_enforced=False,
        )

    # -- envelope reading -------------------------------------------------

    @staticmethod
    def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
        """Parse *text* as a JSON object, tolerating code fences."""
        try:
            parsed = json.loads(strip_fences(text))
        except (ValueError, TypeError):
            return None
        return dict(parsed) if isinstance(parsed, Mapping) else None

    @staticmethod
    def _envelope_text(envelope: Mapping[str, Any]) -> Optional[str]:
        """The envelope's human-readable text, if it has any."""
        for key in ("result", "error", "message"):
            value = envelope.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _usage(envelope: Mapping[str, Any]) -> Optional[Dict[str, Optional[int]]]:
        """Token counts from the envelope's ``usage`` block, or ``None``.

        ``None`` when the envelope reported no usage at all -- that is the
        distinction ``CouncilWorker._enforce_budget`` needs to tell a free
        call from an unmeasurable one, and inventing zeros here would make a
        budgeted endpoint look free for ever.
        """
        raw = envelope.get("usage")
        if not isinstance(raw, Mapping):
            return None
        return {
            "prompt_tokens": _int_or_none(raw.get("input_tokens")),
            "completion_tokens": _int_or_none(raw.get("output_tokens")),
        }

    @staticmethod
    def _resolved_model(envelope: Mapping[str, Any]) -> Optional[str]:
        """The model the CLI actually used, from the ``modelUsage`` keys.

        The envelope reports usage per model as an object keyed by model id,
        so its keys are the only place the resolved name appears.  With more
        than one key (a fallback kicked in mid-session) they are all reported,
        sorted, because picking one would hide that it happened.
        """
        model_usage = envelope.get("modelUsage")
        if not isinstance(model_usage, Mapping) or not model_usage:
            return None
        names = sorted(str(k) for k in model_usage)
        return names[0] if len(names) == 1 else ", ".join(names)


class CodexCliBackend(CliBackend):
    """A council seat served by the headless ``codex exec`` CLI.

    Three of the flags carry the whole safety story, and all three were read
    off the installed ``codex exec --help`` (quoted in :mod:`.cli_common`):

    * **``--ignore-user-config``** -- "Do not load ``$CODEX_HOME/config.toml``;
      auth still uses ``CODEX_HOME``".  Without it, a provider configured in
      ``config.toml`` (an ARGO route, say) would quietly serve the call
      instead of the subscription, and the council would be billing somewhere
      it did not intend while reporting the model it asked for.  With it,
      config is ignored and auth still works.
    * **``--sandbox read-only``** -- a seat that answers a closed-menu
      question has no reason to write anything.
    * **``--ephemeral``** -- "Run without persisting session files to disk",
      so a batch of items does not accumulate sessions.

    ``--skip-git-repo-check`` is required because :func:`run_cli` runs the
    child in a bare temporary directory, which is not a git repository.
    """

    kind = "codex_cli"
    default_executable = "codex"

    def __init__(
        self,
        executable: Optional[str] = None,
        *,
        allow_unverified: bool = False,
        config: Optional[Mapping[str, Any]] = None,
        _home: Optional[Path] = None,
        _parent_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(
            executable,
            allow_unverified=allow_unverified,
            config=config,
            _home=_home,
            _parent_env=_parent_env,
        )

    def argv(self, req: LLMRequest, cwd: Path) -> List[str]:
        """The exact command line for *req*, with files inside *cwd*.

        Public for the same reason as :meth:`ClaudeCliBackend.argv`: this is
        the surface a reviewer and the test suite both read.  The trailing
        ``"-"`` is the installed CLI's documented spelling of "read the
        prompt from stdin", so neither the system nor the user prompt appears
        here.
        """
        return [
            self.executable,
            "exec",
            "--model",
            req.model,
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

    async def _run(self, req: LLMRequest, started: float) -> LLMResult:
        """Run one ``codex exec`` invocation and read its output file."""
        logger.debug(
            "council %s backend %s: asking %s (%d chars of prompt)",
            self.kind,
            self.name,
            req.model,
            len(req.user or ""),
        )

        # Codex names two paths that do not exist until run_cli has made the
        # working directory, so both the schema file and the argv are built
        # inside these hooks rather than up front.
        captured: Dict[str, Optional[str]] = {"last": None}

        def prepare(cwd: Path) -> Sequence[str]:
            (cwd / CODEX_SCHEMA_NAME).write_text(
                json.dumps(req.json_schema or {}), encoding="utf-8"
            )
            return self.argv(req, cwd)

        def collect(cwd: Path) -> None:
            # Read even when the child failed: a partial answer file is
            # evidence, and this is the last moment before the directory goes.
            path = cwd / CODEX_OUTPUT_NAME
            try:
                captured["last"] = path.read_text(encoding="utf-8")
            except OSError:
                captured["last"] = None

        code, out, err, _elapsed = await run_cli(
            None,
            f"{req.system}\n\n{req.user}",
            self._env(),
            self._timeout(req),
            prepare=prepare,
            collect=collect,
        )

        last = captured["last"]

        if code != 0:
            return self._failed(
                classify_failure(self.executable, code, err or out).to_error(),
                started,
                text=last or out or None,
            )

        if not last or not last.strip():
            return self._failed(
                InvalidOutput(
                    f"{self.name}: wrote no final message to {CODEX_OUTPUT_NAME} "
                    f"(exit 0, stderr: {(err or '')[:OUTPUT_EXCERPT_CHARS]!r})"
                ).to_error(),
                started,
                text=out or None,
            )

        parsed = ClaudeCliBackend._parse_json_object(last)
        if parsed is None:
            return self._failed(
                InvalidOutput(
                    f"{self.name}: final message was not a JSON object: "
                    f"{last[:OUTPUT_EXCERPT_CHARS]!r}"
                ).to_error(),
                started,
                text=last,
            )

        return LLMResult(
            ok=True,
            text=last,
            parsed=parsed,
            resolved_model=req.model,
            # Codex reports no token counts through ``codex exec``, so usage
            # is genuinely unknown rather than zero.  An endpoint with a
            # token budget will halt on this as ``budget_usage_unknown``,
            # which is the correct outcome: an unmeasurable budget is an
            # unenforceable one.
            usage=None,
            elapsed_s=time.monotonic() - started,
            # The schema went to the CLI as ``--output-schema``, which is
            # what constrains the final message.  Unlike Claude's
            # ``structured_output`` there is no field in the reply proving it
            # was applied, so this records what was ASKED; the engine
            # validates the answer either way.
            schema_enforced=bool(req.json_schema),
        )
