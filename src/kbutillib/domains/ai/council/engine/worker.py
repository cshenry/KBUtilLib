"""The worker: submit a job, claim its calls, advance its items.

``CouncilWorker`` is the only thing in the council that both spends money and
writes to the store, and it is deliberately thin: every decision it makes is
either :func:`~.decide.decide`'s (what does this item need next?) or the
store's (which call may I claim?).  What is left is the four things that need
a clock, a network and a budget:

**submit** turns a query type, a list of tasks and a roster into one job, its
items and its first round of proposer calls -- in one store transaction, so a
job whose items landed but whose calls did not cannot exist.  An item with an
EMPTY menu is settled right there, from ``decide()``, without a single call.

**work** is the queue loop: reclaim what dead workers were holding, claim as
many due calls as there are free concurrency slots, run them concurrently,
and stop when the scope is idle or everything left is blocked.

**execute** runs one claimed call.  Before spending anything it tries three
ways not to: the route fingerprint must match this worker's own
configuration, an identical question answered by ANY user is reused, and two
identical questions in flight at once collapse into one call.  After spending
it normalises the answer (:mod:`.normalise` -- two measured reply defects,
repaired and recorded), validates it, records the attempt, counts the tokens,
enforces the endpoint's budget, and routes a typed failure to the one of five
outcomes it belongs to (retry with backoff, halt the endpoint, wait for a
model, fail the call, or settle it).

**advance** asks ``decide()`` what the item needs and does exactly that: add
the critic call, add the meta call, settle the verdict, or nothing.

Two columns the store contract could not write for itself are written here
(see ``note_call_error`` and ``set_job_status`` in the store): a PENDING
call's last error, which is what ``job_status`` reports its
``waiting_for_model`` and ``configuration_mismatch`` blockers from, and a
job reaching ``running`` or ``done``.  Both happen inside the same
transaction as the release or the settle they belong to.

Nothing here imports SQLAlchemy, a provider class or a query type.  The store
and the renderer are the protocols in :mod:`..protocols`; the model call goes
through the ONE public wrapper, :mod:`kbutillib.domains.ai.llm` -- the worker
names a ``kind`` and an endpoint mapping and never learns a provider's
constructor.  That is what lets the whole of this module be tested against a
SQLite file and a scripted fake, and it is also what makes a plug-in api type
usable by a council without a line changing here.

The wrapper makes exactly ONE attempt per call.  Everything that decides
whether to try again -- retries and backoff, the endpoint budget, the halt,
the per-endpoint and per-account concurrency limits, single-flight collapsing
and cross-user answer reuse -- stays here, because only this module knows what
a second attempt would cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import socket
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from ...llm import acomplete as llm_acomplete
from ...llm import get_api as llm_get_api
from ...llm import preflight_api as llm_preflight_api
from ...llm.types import LLMRequest
from ..config import DEFAULT_MAX_CONCURRENCY
from ..store.errors import StaleClaim
from ..types import (
    CLI_KINDS,
    BillingGuardRefused,
    CompletionRequest,
    ConfigError,
    CouncilError,
    CouncilTask,
    CouncilVerdict,
    MenuEntry,
    Roster,
    RosterError,
    SeatSpec,
    canonical_sha256,
    data_hash,
    default_reuse_namespace,
    entity_hash,
    prompt_hash,
    route_fingerprint,
    seat_generation_params,
)
from .decide import SettledCall, decide, tally
from .normalise import normalise

logger = logging.getLogger(__name__)

__all__ = ["CouncilWorker", "aevaluate", "evaluate", "renderer_id_version"]

#: Item statuses that are final.  Spelled here rather than imported from
#: ``store.queries`` so this module keeps its "no SQLAlchemy" property; the
#: two lists mean the same thing and a test pins that they agree.
SETTLED_ITEM_STATUSES = ("ok", "error", "cancelled")

#: What a reused call spent.  Zeroes rather than ``None``: the call WAS
#: measured -- at nothing -- and ``None`` means "unmeasurable", which is what
#: trips the budget guard.
ZERO_USAGE: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}

#: Concurrency for an endpoint whose config says nothing and whose kind is not
#: one of the four real ones (a ``fake`` endpoint in a test, say).
FALLBACK_MAX_CONCURRENCY = 4

#: Failure kinds that halt the whole endpoint for this user rather than fail
#: the one call: every other call to the same place would fail identically.
HALTING_KINDS = ("quota_exhausted", "auth", "billing_guard")

#: Failure kinds worth trying the same call again for.
RETRYABLE_KINDS = ("transport", "timeout")

#: Not in :data:`..types.ERROR_KINDS` -- it is not a model failure at all, but
#: the worker noticing that its own configuration no longer matches the one
#: the call was created under.  ``store.queries.BLOCKER_BY_ERROR_KIND`` maps
#: it to the ``configuration_mismatch`` blocker.
CONFIGURATION_MISMATCH = "configuration_mismatch"

# ---------------------------------------------------------------------------
# Batched invocation (t8)
# ---------------------------------------------------------------------------
#
# A batch is a set of claimed call rows sharing (job_id, role, seat_name,
# query_name, query_version) -- see :meth:`CouncilWorker._group_claimed`.
# ``batch_size`` (a queue-level setting, threaded through the constructor the
# same way ``concurrency`` already is) opts a worker into sending several
# items per backend invocation; ``batch_size <= 1`` (the default) leaves
# :meth:`CouncilWorker._group_claimed` returning one-row groups, bit for bit
# identical to the pre-batching behaviour.

#: CLI/model combinations with a VERIFIED effective output cap and a
#: DOCUMENTED mechanism for enforcing it.  Checked by
#: :meth:`CouncilWorker._effective_output_cap`.
#:
#: Empty today, and not because nobody looked: ``ClaudeCliBackend.argv``
#: (``llm/apis/cli.py``) passes ``-p``, ``--output-format``, ``--json-schema``,
#: ``--model``, ``--system-prompt``, ``--no-session-persistence`` and
#: ``--tools ""``, and ``CodexCliBackend.argv`` passes ``exec``, ``--model``,
#: ``--output-schema``, ``-o``, ``--ephemeral``, ``--ignore-user-config``,
#: ``--sandbox``, ``--skip-git-repo-check`` and ``-``.  NEITHER forwards an
#: output-token argument, so there is no flag to point at and no cap to
#: verify for either CLI kind on this commit.  A seat whose
#: ``(endpoint_kind, model)`` has no entry here is forced to run as singles
#: (see :meth:`CouncilWorker._batch_n`) -- that is the CORRECT, SAFE outcome
#: for every CLI seat today, not a bug to work around.
#:
#: Add an entry only once a real mechanism is found AND independently
#: measured end to end against the live CLI (see the t8 work record for what
#: "measure first" means here) -- never to make a config option "look"
#: enabled.  A test may monkeypatch this table to exercise the ceiling
#: arithmetic without waiting on that verification; production code must
#: never write to it.
VERIFIED_OUTPUT_CAPS: Dict[Tuple[str, str], int] = {}

#: The wrapper shape every batched reply must satisfy, checked SEPARATELY
#: from (and before) each element's own per-role answer schema -- see
#: :meth:`CouncilWorker._invoke_batch`.  Deliberately loose (just "there is an
#: ``answers`` array"): the per-role schema is what actually constrains an
#: element, via :data:`..queries.spec.wrap_batch_answer_schema` on the way out
#: and :func:`_validate_answer` per element on the way back in.
BATCH_WRAPPER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"answers": {"type": "array"}},
    "required": ["answers"],
}

#: Mirrors ``..queries.spec.BATCH_ITEM_KEY_FIELD`` -- duplicated as a bare
#: string rather than imported, so this module keeps its "nothing here
#: imports a query type" property at module scope (see the module docstring
#: and :meth:`CouncilWorker._invoke_batch`, which imports the SCHEMA/PROMPT
#: helpers lazily, only once a batch is actually being sent).
BATCH_ITEM_KEY_FIELD = "item_key"


@dataclass
class _BatchMember:
    """One claimed call row, prepared and ready to ride in a batch invocation.

    Built by :meth:`CouncilWorker._prepare_member`, which has already run
    every per-row check a single call would run BEFORE spending anything
    (route fingerprint, reuse, the permission gate) -- a member exists only
    for a row this worker is actually about to ask a model.
    """

    call: Dict[str, Any]
    token: Optional[str]
    job: Mapping[str, Any]
    roster: Roster
    seat: SeatSpec
    query: Any
    role: str
    system: str
    user: str
    schema: Dict[str, Any]
    context: Mapping[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def renderer_id_version(query: Any) -> str:
    """Identity of HOW a query type renders prompts and reads answers.

    Part of the prompt hash, so that two installations only reuse each other's
    answers when they would have asked and read them the same way.  The query
    type's own ``(name, version)`` is in the hash as well, but it is not
    enough on its own: a spec names its renderer and adapter as
    ``"<name>@<version>"``, and an installation shipping a different version
    of either asks or scores a different question under the same spec name.
    """
    renderer = getattr(query, "renderer", None) or "template"
    adapter = getattr(query, "adapter", None) or "generic"
    id_field = getattr(query, "id_field", None) or "id"
    return f"{renderer}|{adapter}|{id_field}"


def _as_endpoint_table(backends: Optional[Any]) -> Dict[str, Any]:
    """Normalise a ``backends`` argument into ``{alias: instance or None}``.

    Three shapes go in (see :class:`CouncilWorker`) and one comes out, so
    everything downstream can keep using ``set(self.backends)`` for "which
    endpoints do I claim calls for" and ``self.backends.get(alias)`` for "did
    somebody hand me an api type for this one".

    A mapping value that is not an api type at all is rejected here rather
    than at the first call: handing the worker the wrong thing is a
    programming mistake, and discovering it mid-batch would cost a claimed
    call and a confusing typed failure.
    """
    if backends is None:
        return {}
    if isinstance(backends, Mapping):
        table: Dict[str, Any] = {}
        for alias, api in backends.items():
            if api is not None and not callable(getattr(api, "complete", None)):
                raise TypeError(
                    f"backends[{alias!r}] is a {type(api).__name__}, which has no "
                    "complete(); pass an api type instance, None, or just the "
                    "endpoint aliases"
                )
            table[str(alias)] = api
        return table
    if isinstance(backends, str):
        return {backends: None}
    return {str(alias): None for alias in backends}


class _AccountLimit:
    """One concurrency budget, shared by every endpoint on the same account.

    Two endpoint aliases pointing at one subscription are one queue at the
    provider, so they must share one semaphore -- sized by the SMALLEST
    ``max_concurrency`` of the group, because the tightest declared limit is
    the real one.

    ``assigned`` counts calls this worker has taken responsibility for
    (claimed and not yet finished), which is what the claim loop sizes its
    next batch from; the semaphore counts calls actually in flight at the
    backend.  They differ while a claimed call waits for a slot, and
    conflating them would have the loop claim the whole queue at once.
    """

    def __init__(self, key: str, capacity: int) -> None:
        self.key = key
        self.capacity = max(1, int(capacity))
        self.assigned = 0
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def semaphore(self) -> asyncio.Semaphore:
        """The semaphore for the RUNNING loop, built on first use.

        A worker object may be used by two successive ``work()`` calls on two
        different loops (the sync wrappers each own one), and an
        ``asyncio.Semaphore`` bound to a closed loop raises when awaited.
        """
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._semaphore = asyncio.Semaphore(self.capacity)
            self._loop = loop
        return self._semaphore


class CouncilWorker:
    """Submits council jobs and works their call rows through to verdicts.

    Args:
        store: anything implementing :class:`~..protocols.CouncilStore`.
        config: the council configuration.  Either a ``CouncilConfig`` (its
            ``endpoint(name)`` accessor is used) or a plain mapping of the
            same information, ``{"endpoints": {name: {...}}}`` -- the mapping
            form exists because ``CouncilConfig.endpoints`` cannot express a
            ``fake`` or ``replay`` endpoint, its ``kind`` being a Literal over
            the four real ones, and the engine's own tests are built on
            exactly those.  ``None`` means "no endpoint facts", which is
            usable but gives up budgets and shared-account concurrency.
        backends: the endpoints THIS worker serves.  It claims calls for no
            others.  Three accepted shapes:

            * an ITERABLE of endpoint aliases -- the normal M2 form.  Every
              call then goes through ``llm.acomplete``, which resolves the
              endpoint's ``kind`` to an api type.
            * a MAPPING ``{alias: None}`` -- the same thing, spelled as a
              mapping.
            * a MAPPING ``{alias: api type instance}`` -- an explicitly
              injected api type for that alias, used instead of resolving one.
              This is how the test suites drive a scripted fake, and how
              ``CouncilUtils`` hands over the clients it built once for a
              whole job rather than per call.
        complete: the callable every model call is made through.  Defaults to
            ``llm.acomplete`` itself when no api type instance was injected,
            and otherwise to this worker's adapter over the injected ones.
            Substituting it is how a test counts model calls without touching
            a council module.
        user: the PROVENANCE label written to ``jobs.submitted_by`` and to the
            workers row.  It scopes nothing: the store's queue is the store's,
            so this worker claims every due call in it whatever label the job
            carries (review round 3).
        worker_id: this worker's id in the ``workers`` table.  Generated from
            the hostname when absent.
        allow_unverified: permit seats whose model is not verified, and let a
            CLI backend whose billing preflight answers ``unknown`` through.
            Off by default: an unverified seat is one nobody has confirmed
            serves the model it claims to.
        worker_kind: ``foreground`` (the default, i.e. ``kbu council run``) or
            ``daemon``.  Recorded on the workers row.
        seats: optional partition -- when given (not ``None``), this worker
            claims calls for only these seats, passed straight through to
            ``store.claim_calls()``.  ``None`` (the default) claims across
            every seat, unchanged from a worker built before queue mode
            existed.
        roles: the same partition, for role (``proposer``/``critic``/
            ``meta``).  ``None`` claims every role.
        concurrency: the per-child budget in QUEUE mode -- a queue's own
            ``concurrency``, not the account's full ``max_concurrency``.
            Accepted as a plain parameter rather than read from ``config``,
            so the caller (which already knows which queue this worker is)
            decides the number once, instead of this class re-deriving it
            from a ``QueueConfig`` it would otherwise have to look up by
            name.  When given, it overrides the capacity :meth:`_build_limits`
            would otherwise compute for every account this worker serves --
            correct for queue mode, where a queue resolves to exactly one
            account.  ``None`` (the default) leaves ``_build_limits``'s
            account-capacity arithmetic exactly as it was before queue mode.
        batch_size: items per backend invocation, the same queue-level number
            as ``QueueConfig.batch_size`` (threaded through exactly like
            ``concurrency``, for the same reason: the caller already knows
            which queue this worker is).  ``1`` (the default) is BIT FOR BIT
            today's behaviour -- :meth:`_group_claimed` returns one-row groups
            and nothing about batching is engaged.  Above 1, a worker may
            send up to that many homogeneous claimed rows in one invocation,
            subject to the derived ceiling in :meth:`_batch_n`.
        max_output_tokens_per_item: the configured per-item output estimate
            used to derive the batch ceiling (``QueueConfig``'s field of the
            same name).  ``None`` (the default) falls back to this worker's
            own running mean of observed per-item output for a
            ``(seat, role, served_model)``; with neither available, batching
            is forced to singles rather than running on an unknown guess.
    """

    def __init__(
        self,
        store: Any,
        config: Any = None,
        backends: Optional[Any] = None,
        user: str = "",
        worker_id: Optional[str] = None,
        *,
        complete: Optional[Callable[..., Any]] = None,
        allow_unverified: bool = False,
        worker_kind: str = "foreground",
        heartbeat_interval_s: float = 30.0,
        retry_backoff_s: Sequence[float] = (2.0, 8.0),
        retry_jitter: float = 0.25,
        max_attempts: int = 3,
        model_wait_s: float = 60.0,
        mismatch_wait_s: float = 300.0,
        poll_interval_s: float = 0.02,
        idle_grace_s: float = 1.0,
        max_idle_s: float = 60.0,
        rng: Optional[random.Random] = None,
        seats: Optional[Iterable[str]] = None,
        roles: Optional[Iterable[str]] = None,
        concurrency: Optional[int] = None,
        batch_size: int = 1,
        max_output_tokens_per_item: Optional[int] = None,
    ) -> None:
        if not user:
            raise ValueError("a council worker must know which user it works for")
        self.store = store
        self.config = config
        self.backends: Dict[str, Any] = _as_endpoint_table(backends)
        #: This worker's partition (``None`` means "every seat"/"every
        #: role", the pre-queue-mode behaviour).  Carried as plain attributes
        #: -- NOT folded into ``_claim``'s signature, which ``daemon.py``
        #: subclasses and calls with exactly ``(limit, job_id, summary)``.
        self.seats: Optional[set] = set(seats) if seats is not None else None
        self.roles: Optional[set] = set(roles) if roles is not None else None
        self._queue_concurrency: Optional[int] = (
            int(concurrency) if concurrency is not None else None
        )
        #: Queue-level batching knobs; see the class docstring.  ``1`` means
        #: "not opted in" and must leave every code path this worker takes
        #: identical to the pre-batching one.
        self.batch_size: int = max(1, int(batch_size))
        self.max_output_tokens_per_item: Optional[int] = (
            int(max_output_tokens_per_item)
            if max_output_tokens_per_item is not None
            else None
        )
        #: Running mean of OBSERVED per-item completion tokens, keyed by
        #: ``(seat_name, role, served_model)`` as ``(total_tokens,
        #: total_members)`` -- see :meth:`_per_item_output` and
        #: :meth:`_record_output_sample`.  Process-local and cold on every new
        #: worker: nothing here is persisted, which is fine, because an empty
        #: entry forces singles (never an unbounded guess) until this worker
        #: itself has observed something.
        self._output_samples: Dict[Tuple[str, str, str], Tuple[int, int]] = {}
        #: ``{(seat_name, role, tag)}`` already logged this worker's life, so
        #: a reduced or refused batch ceiling is reported once per seat per
        #: run rather than once per claimed group.
        self._logged_batch_notices: set = set()
        #: The one callable every model call is made through.  ``llm.acomplete``
        #: itself when nothing was injected -- identity a test can assert on,
        #: which is what makes "the worker goes through the wrapper" checkable
        #: rather than merely claimed.
        self._acomplete: Callable[..., Any] = complete or (
            self._acomplete_injected
            if any(api is not None for api in self.backends.values())
            else llm_acomplete
        )
        self.user = user
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.allow_unverified = bool(allow_unverified)
        self.worker_kind = worker_kind
        self.heartbeat_interval_s = float(heartbeat_interval_s)
        self.retry_backoff_s = tuple(float(v) for v in retry_backoff_s) or (2.0,)
        self.retry_jitter = float(retry_jitter)
        self.max_attempts = max(1, int(max_attempts))
        self.model_wait_s = float(model_wait_s)
        self.mismatch_wait_s = float(mismatch_wait_s)
        self.poll_interval_s = float(poll_interval_s)
        self.idle_grace_s = float(idle_grace_s)
        self.max_idle_s = float(max_idle_s)
        self._rng = rng or random.Random()

        self._limits: Dict[str, _AccountLimit] = {}
        self._limit_by_endpoint: Dict[str, _AccountLimit] = {}
        self._build_limits()

        self._queries: Dict[Tuple[str, str], Any] = {}
        self._rosters: Dict[str, Roster] = {}
        #: ``{endpoint: preflight report}`` and ``{endpoint: seat state}``.
        #: Facts about THIS MACHINE, so they are cached for the worker's life;
        #: the PERMISSION to act on them is read from each call's own job and
        #: is never cached (see :meth:`_permission_gate`).
        self._billing_states: Dict[str, Dict[str, Any]] = {}
        self._endpoint_states: Dict[str, Dict[str, Any]] = {}
        self._single_flight: Dict[str, asyncio.Future] = {}
        self._registered = False

    # -- configuration ----------------------------------------------------

    def endpoint_config(self, endpoint: str) -> Dict[str, Any]:
        """The non-secret facts about *endpoint*, or ``{}`` if unknown.

        Reads a ``CouncilConfig`` through its own accessor and a mapping
        directly, so the engine never has to care which it was handed.  An
        unknown endpoint is not an error here: a shared store holds other
        users' jobs, and a worker that cannot describe an endpoint simply
        never claims calls for it.
        """
        config = self.config
        if config is None:
            return {}
        accessor = getattr(config, "endpoint", None)
        if callable(accessor):
            try:
                return dict(accessor(endpoint))
            except Exception:  # noqa: BLE001 - an unknown endpoint is not fatal
                return {}
        if isinstance(config, Mapping):
            section = config.get("endpoints")
            if not isinstance(section, Mapping):
                section = config
            value = section.get(endpoint)
            return dict(value) if isinstance(value, Mapping) else {}
        return {}

    def _build_limits(self) -> None:
        """Group this worker's endpoints into shared concurrency budgets.

        In QUEUE mode (:attr:`_queue_concurrency` not ``None``) the account
        arithmetic below still runs -- it is still how two endpoint aliases
        on one subscription end up sharing one semaphore -- but every
        resulting capacity is then OVERRIDDEN to the queue's own
        ``concurrency``.  A queue resolves to exactly one account (an
        enforced invariant once ``queues`` is configured), so this makes
        ``_free_slots()`` that account's child budget rather than the whole
        subscription's, which is the correction confront round 1 required:
        the account's full ``max_concurrency`` is what OTHER queues on the
        same account are allowed to use too, and this child must not claim
        more than its own share.
        """
        for endpoint in sorted(self.backends):
            cfg = self.endpoint_config(endpoint)
            account = str(cfg.get("account") or "") or f"endpoint:{endpoint}"
            declared = cfg.get("max_concurrency")
            capacity = int(
                declared
                if declared
                else DEFAULT_MAX_CONCURRENCY.get(
                    str(cfg.get("kind") or ""), FALLBACK_MAX_CONCURRENCY
                )
            )
            limit = self._limits.get(account)
            if limit is None:
                self._limits[account] = limit = _AccountLimit(account, capacity)
            else:
                limit.capacity = min(limit.capacity, max(1, capacity))
            self._limit_by_endpoint[endpoint] = limit
        if self._queue_concurrency is not None:
            for limit in self._limits.values():
                limit.capacity = max(1, self._queue_concurrency)

    def endpoint_mapping(self, endpoint: str) -> Dict[str, Any]:
        """The endpoint mapping the wrapper is given for *endpoint*.

        :meth:`endpoint_config` plus the two keys the wrapper needs that a
        bare mapping-form config may omit: ``kind`` (which api type to
        resolve) and ``name``.  ``name`` matters more than it looks: it is
        part of the wrapper's client cache key, so without it two aliases
        whose configs happened to be identical -- two ``fake`` endpoints in a
        test, two vLLM ports behind one URL -- would share ONE client, and the
        shared-account concurrency tests would silently be measuring one
        endpoint twice.

        Also threads ``cli_temp_root`` (a ``CouncilConfig``-level setting,
        not a per-endpoint one -- a CLI backend has no other way to see it)
        through as ``temp_root``, which is the key a CLI backend's own
        invocation of ``run_cli`` reads for where to create its per-call
        working directory.  ``None``/absent when unconfigured, which is
        unchanged behaviour.
        """
        cfg = dict(self.endpoint_config(endpoint))
        cfg.setdefault("name", endpoint)
        temp_root = getattr(self.config, "cli_temp_root", None)
        if temp_root and "temp_root" not in cfg:
            cfg["temp_root"] = temp_root
        return cfg

    def _api_for(self, seat: SeatSpec) -> Optional[Any]:
        """The api type instance serving *seat*, or ``None`` if there is none.

        An explicitly injected instance wins; otherwise the wrapper resolves
        the endpoint's ``kind``.  ``None`` means "this worker cannot describe
        that endpoint" -- an unregistered kind, or no config for it at all --
        which is not an error here: a worker that cannot reach an endpoint
        simply never claims calls for it, and the two callers below both have
        a defined answer for ``None``.
        """
        injected = self.backends.get(seat.endpoint)
        if injected is not None:
            return injected
        cfg = self.endpoint_mapping(seat.endpoint)
        kind = str(cfg.get("kind") or seat.endpoint_kind or "")
        cfg.setdefault("kind", kind)
        try:
            return llm_get_api(kind, cfg)
        except (KeyError, TypeError) as exc:
            logger.debug(
                "council worker %s: no api type for endpoint %r (kind %r): %s",
                self.worker_id,
                seat.endpoint,
                kind,
                exc,
            )
            return None

    async def _acomplete_injected(
        self, request: Any, *, api_type: str, endpoint: Mapping[str, Any]
    ) -> Any:
        """``llm.acomplete``'s signature, served from the injected api types.

        The default :attr:`_acomplete` when a caller handed this worker api
        type instances instead of bare aliases.  It exists for two reasons
        that are really one: a caller that has already built its clients for
        a whole job should not have them rebuilt per call, and the test suites
        drive scripted fakes that way.  Anything the table has no instance for
        falls through to ``llm.acomplete``, so a worker holding a mixture
        behaves as if it held neither.
        """
        api = self.backends.get(str(dict(endpoint).get("name") or ""))
        if api is None:
            return await llm_acomplete(request, api_type=api_type, endpoint=endpoint)
        return await api.complete(request)

    def _seat_request(self, seat: SeatSpec) -> LLMRequest:
        """A prompt-less :class:`LLMRequest` carrying *seat*'s call parameters.

        ``effective_params`` is a function of the generation parameters only,
        and it is needed at SUBMIT time for the route fingerprint -- which is
        computed per seat, before any role's prompts exist.  So the request
        handed to it carries the parameters and no prompts, which is exactly
        the information it reads.
        """
        return LLMRequest(
            model=seat.model,
            max_output_tokens=seat.max_output_tokens,
            temperature=seat.temperature,
            params=dict(seat.extra or {}),
            timeout_s=seat.timeout_s,
        )

    def _effective_params(self, seat: SeatSpec) -> Dict[str, Any]:
        """What the api type serving *seat* would actually send for it.

        Not the seat's REQUESTED parameters: ARGO forces ``temperature: 1`` on
        some models whatever was asked for, and a prompt hash keyed on the
        request would hand one cached answer to two materially different
        questions.  Falls back to the provider-independent parameters when no
        api type can be resolved, which keeps a worker built with no config at
        all (``CouncilWorker(store, None, [], user)``) usable.
        """
        api = self._api_for(seat)
        if api is not None:
            return dict(api.effective_params(self._seat_request(seat)))
        return seat_generation_params(seat)

    def _reuse_namespace(self, seat: SeatSpec) -> str:
        cfg = dict(self.endpoint_config(seat.endpoint))
        cfg.setdefault("kind", seat.endpoint_kind)
        return default_reuse_namespace(cfg, seat.model)

    def _route_fingerprint(self, seat: SeatSpec) -> str:
        cfg = dict(self.endpoint_config(seat.endpoint))
        cfg.setdefault("kind", seat.endpoint_kind)
        return route_fingerprint(seat, cfg, self._effective_params(seat))

    # -- submit -----------------------------------------------------------

    def submit(
        self,
        query: Any,
        tasks: Sequence[CouncilTask],
        roster: Roster,
        label: Optional[str] = None,
        priority: int = 100,
        profile_name: Optional[str] = None,
        options: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Submit a job.  The synchronous form of :meth:`asubmit`.

        Raises:
            RuntimeError: called from inside a running event loop -- await
                :meth:`asubmit` there instead of nesting loops.
        """
        return _run_coroutine(
            self.asubmit(query, tasks, roster, label, priority, profile_name, options)
        )

    async def asubmit(
        self,
        query: Any,
        tasks: Sequence[CouncilTask],
        roster: Roster,
        label: Optional[str] = None,
        priority: int = 100,
        profile_name: Optional[str] = None,
        options: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Create a job, its items and its first round of proposer calls.

        Everything that can be refused is refused HERE, before a row exists:
        a seating that is not independent, an unverified seat, a CLI whose
        billing guard says no, a repeated item whose content changed, a menu
        with a duplicate id.  A job in the queue is a job somebody is paying
        for, so the cheap refusals happen first.

        An item whose menu is EMPTY is settled as part of the same
        transaction, from ``decide()``, with zero calls -- and a job all of
        whose items settle that way is ``done`` on arrival rather than queued
        forever behind calls that will never exist.

        Returns:
            The job id.

        Raises:
            RosterError: the seating is invalid, or a seat is unverified.
            BillingGuardRefused: a CLI backend's billing preflight refused.
            ConfigError: ``config`` declares ``queues`` and this roster (the
                profile's own, or one a submission overrode) is not covered
                by the validated queue set -- refused before a job or any
                call exists, exactly like every other submit-time refusal.
            ValueError: a duplicate item id with different content, or a menu
                with a duplicate id.
        """
        self._check_roster_against_queues(roster)
        self._check_roster(roster)
        await self._preflight(roster)
        self._queries[(str(query.name), str(query.version))] = query

        now = _utcnow()
        item_rows: List[Dict[str, Any]] = []
        call_rows: List[Dict[str, Any]] = []
        fingerprints: Dict[str, str] = {}

        for task in tasks:
            menu_ids = [str(entry.id) for entry in (task.menu or [])]
            duplicates = sorted({mid for mid in menu_ids if menu_ids.count(mid) > 1})
            if duplicates:
                raise ValueError(
                    f"item {task.item_id!r} offers menu id(s) {duplicates} more than "
                    "once; a closed menu must be a set, or one candidate would be "
                    "counted twice in the vote table"
                )
            fingerprint = canonical_sha256(
                {
                    "evidence": dict(task.evidence or {}),
                    "menu": [entry.model_dump(mode="json") for entry in task.menu or []],
                }
            )
            if task.item_id in fingerprints:
                if fingerprints[task.item_id] != fingerprint:
                    raise ValueError(
                        f"item id {task.item_id!r} appears twice with DIFFERENT "
                        "content; an item id is how a verdict is attributed, so the "
                        "two answers could not be told apart"
                    )
                # An identical repeat is the same question asked twice.
                continue
            fingerprints[task.item_id] = fingerprint

            item_row = self._item_row(query, task, len(item_rows))
            if not menu_ids:
                what, verdict = decide(task, roster, query, [], now=now)
                item_row.update(
                    status=verdict.status,
                    verdict=verdict.model_dump(mode="json"),
                    decided_at=now,
                )
            else:
                for seat in roster.proposers:
                    call_rows.append(
                        self._call_row(query, "proposer", seat, task, item_row, {})
                    )
            item_rows.append(item_row)

        unsettled = [row for row in item_rows if row["status"] not in SETTLED_ITEM_STATUSES]
        job_row: Dict[str, Any] = {
            "label": label or "",
            "query_name": str(query.name),
            "query_version": str(query.version),
            "submitted_by": self.user,
            # Provenance, like submitted_by: WHERE the job was entered.  A
            # store is one machine's, so this is constant in practice -- it
            # matters when a store file is copied off that machine to be read
            # somewhere else, which is the only time the question is asked.
            "submitted_host": socket.gethostname(),
            "submitted_at": now,
            "priority": int(priority),
            "status": "queued" if unsettled else "done",
            "roster": self._roster_payload(roster),
            "roster_fingerprint": roster.fingerprint(),
            "profile_name": profile_name,
            "options": dict(options or {}),
            "n_items": len(item_rows),
        }
        return self.store.create_job(job_row, item_rows, call_rows)

    def _check_roster_against_queues(
        self, roster: Roster, *, job_id: Optional[str] = None
    ) -> None:
        """Refuse *roster* if ``config`` declares ``queues`` and it falls
        outside the validated set.

        ``CouncilConfig.check_roster_against_queues`` is a predicate only --
        by its own docstring, "nothing in this module calls it" -- so it is
        wired in from here and from :meth:`_claim`, which are the two
        natural call sites its own docstring names: once at submission,
        before a job or any call row exists, and again before claiming an
        EXISTING job's pending calls, because a per-job ``roster()``
        override (or the special ``"proposers"`` override key, which
        replaces the whole bench) is keyed by seat name and can route a seat
        to an account no queue was ever validated against -- something
        config-LOAD validation cannot see, since it only resolves each
        profile with no overrides.

        A no-op for a plain mapping config (no ``check_roster_against_queues``
        method) and for a ``CouncilConfig`` with no ``queues`` declared --
        queue mode is opt-in.
        """
        checker = getattr(self.config, "check_roster_against_queues", None)
        if checker is None:
            return
        checker(roster, job_id=job_id)

    def _check_roster(self, roster: Roster) -> None:
        """Re-run the roster invariants, then the verification rule.

        Re-constructing the model is not redundant: a caller can mutate a
        ``Roster`` after it was built (pydantic validates on construction, not
        on assignment), and a council whose proposers stopped being
        independent is the one error that cannot be seen in its results.
        """
        Roster(
            proposers=list(roster.proposers),
            critic=roster.critic,
            meta=roster.meta,
            allow_meta_overlap=roster.allow_meta_overlap,
        )
        unverified = [seat.seat_name for _role, seat in roster.seats() if not seat.verified]
        if unverified and not self.allow_unverified:
            raise RosterError(
                f"seat(s) {unverified} are marked verified: false -- nobody has "
                "confirmed that endpoint serves the model they name. Verify them, "
                "or pass allow_unverified=True to spend anyway."
            )

    async def _preflight(self, roster: Roster) -> None:
        """Run each CLI endpoint's billing preflight once, before any spend.

        A CLI call is billed against an interactive subscription, so "which
        account is this going to charge?" is a question that must be answered
        before a batch runs, not discovered in the middle of one.  A refusal
        always raises; an ``unknown`` raises unless *allow_unverified*, since
        an unverifiable billing mode is exactly the case the guard exists for.

        Asked through ``llm.preflight_api``, so an api type with no preflight
        of its own reports ``ready`` rather than ``unknown`` -- there is
        nothing about it that could refuse, and treating "nothing to check" as
        "could not check" would refuse every job on a fake endpoint.
        """
        seen: set = set()
        for _role, seat in roster.seats():
            if seat.endpoint_kind not in CLI_KINDS or seat.endpoint in seen:
                continue
            seen.add(seat.endpoint)
            api = self._api_for(seat)
            if api is None:
                # Nothing to ask: this worker cannot even build an api type
                # for that endpoint, so it will never claim a call for it.
                continue
            report = await llm_preflight_api(api)
            state = str(report.get("state") or "unknown")
            if state == "ready":
                continue
            detail = report.get("detail") or report.get("message") or ""
            if state == "refused":
                raise BillingGuardRefused(
                    f"endpoint {seat.endpoint!r} refused the billing preflight: "
                    f"{detail or 'no reason given'}"
                )
            if self.allow_unverified:
                continue
            raise BillingGuardRefused(
                f"endpoint {seat.endpoint!r} could not confirm how it bills "
                f"({state}: {detail or 'no detail'}); pass allow_unverified=True "
                "to spend against it anyway"
            )

    def _item_row(self, query: Any, task: CouncilTask, order: int) -> Dict[str, Any]:
        """One item row, with the two content hashes that make it findable."""
        evidence = dict(task.evidence or {})
        entity = evidence.get(query.entity_field, "")
        rest = {k: v for k, v in evidence.items() if k != query.entity_field}
        return {
            "item_id": task.item_id,
            "entity_hash": entity_hash(entity),
            "data_hash": data_hash(rest, list(task.menu or [])),
            "evidence": evidence,
            "menu": [entry.model_dump(mode="json") for entry in task.menu or []],
            "item_order": order,
            "status": "pending",
        }

    def _roster_payload(self, roster: Roster) -> Dict[str, Any]:
        """The roster as the job stores it: seats plus their routes.

        Seat specs carry no credential, and ``route_fingerprints`` is a hash
        of where each seat was reached rather than the place itself, so this
        is safe in a store every user of the installation can read.
        """
        return {
            "proposers": [seat.model_dump(mode="json") for seat in roster.proposers],
            "critic": roster.critic.model_dump(mode="json"),
            "meta": roster.meta.model_dump(mode="json"),
            "allow_meta_overlap": bool(roster.allow_meta_overlap),
            "route_fingerprints": {
                seat.seat_name: self._route_fingerprint(seat)
                for _role, seat in roster.seats()
            },
        }

    def _call_row(
        self,
        query: Any,
        role: str,
        seat: SeatSpec,
        task: CouncilTask,
        item_row: Mapping[str, Any],
        context: Mapping[str, Any],
        upstream_digest: str = "",
    ) -> Dict[str, Any]:
        """One pending call row, with its prompt hash and its route.

        The prompt is rendered to HASH it and then thrown away: prompt text is
        never stored (the evidence a job was built from is in the item row,
        once), and the worker re-renders it from the item and the settled
        upstream calls when it actually runs.
        """
        system, user = query.render(role, task, dict(context))
        schema = query.answer_schema(role)
        return {
            "item_id": task.item_id,
            "role": role,
            "seat_name": seat.seat_name,
            "endpoint": seat.endpoint,
            "endpoint_kind": seat.endpoint_kind,
            "served_model": seat.model,
            "canonical_model": seat.canonical_model,
            "route_fingerprint": self._route_fingerprint(seat),
            "entity_hash": item_row.get("entity_hash"),
            "data_hash": item_row.get("data_hash"),
            "prompt_hash": prompt_hash(
                seat,
                self._reuse_namespace(seat),
                self._effective_params(seat),
                schema,
                str(query.name),
                str(query.version),
                renderer_id_version(query),
                role,
                system,
                user,
            ),
            "upstream_digest": upstream_digest,
            "submitted_by": self.user,
        }

    # -- the queue loop ---------------------------------------------------

    async def work(
        self,
        stop_event: Optional[Any] = None,
        job_id: Optional[str] = None,
        until_idle: bool = False,
        heartbeat: Optional[Callable[[], Any]] = None,
    ) -> Dict[str, Any]:
        """Claim and run calls until there is nothing left to do.

        Args:
            stop_event: anything with ``is_set()``; checked every pass.
            job_id: restrict to one job.  Calls of OTHER jobs that the store
                hands out are released immediately and untouched -- the claim
                contract has no job filter, so the scoping is done here.
            until_idle: return once the scope has no pending or in-flight
                call left, or once everything that remains is blocked --
                where "blocked" means ``job_status`` reports a blocker and
                nothing becomes workable within ``idle_grace_s``.
            heartbeat: called every ``heartbeat_interval_s`` alongside the
                store heartbeat, for a caller with its own liveness file.

        Returns:
            ``{"claimed", "finished", "released", "blockers", "idle"}``.
        """
        self.register()
        summary: Dict[str, Any] = {
            "claimed": 0,
            "finished": 0,
            "released": 0,
            "blockers": [],
            "idle": False,
        }
        running: set = set()
        last_beat = 0.0
        idle_since: Optional[float] = None

        try:
            while True:
                last_beat = self._maybe_heartbeat(last_beat, heartbeat)
                if self._should_stop(stop_event):
                    break

                self.store.reclaim_stale()
                free = self._free_slots()
                rows = self._claim(free, job_id, summary) if free > 0 else []
                summary["claimed"] += len(rows)
                for group in self._group_claimed(rows):
                    running.add(asyncio.ensure_future(self._run_group(group)))

                if running:
                    idle_since = None
                    done, pending = await asyncio.wait(
                        running,
                        timeout=self.poll_interval_s,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    running = set(pending)
                    for task in done:
                        # A raise here is a bug in the worker, not a call
                        # failure (a backend never raises for those), so it is
                        # re-raised rather than swallowed: nobody is watching.
                        task.result()
                        summary["finished"] += 1
                    continue
                if rows:
                    continue

                if not until_idle:
                    if stop_event is None:
                        break
                    await asyncio.sleep(self.poll_interval_s)
                    continue

                outstanding, blockers, due_in = self._scope_state(job_id)
                if not outstanding:
                    summary["idle"] = True
                    summary["blockers"] = blockers
                    break
                if blockers and (due_in is None or due_in > self.idle_grace_s):
                    # Everything left is blocked: either nothing this worker
                    # could run at all (a halted endpoint, a call for an
                    # endpoint it does not serve), or nothing that becomes due
                    # within the grace window -- a model-unavailable call
                    # parked for a minute and a configuration mismatch parked
                    # for five are blockers to REPORT, not delays to sit
                    # through.  A short retry backoff stays inside the window
                    # and is waited for, because nothing reports it.
                    summary["blockers"] = blockers
                    break
                now = time.monotonic()
                idle_since = now if idle_since is None else idle_since
                if now - idle_since > self.max_idle_s:
                    summary["blockers"] = blockers + [
                        {
                            "kind": "stalled",
                            "endpoint": "",
                            "detail": (
                                f"{outstanding} call(s) outstanding but none became "
                                f"workable in {self.max_idle_s:g}s"
                            ),
                        }
                    ]
                    break
                wait = due_in if due_in is not None else 1.0
                await asyncio.sleep(max(self.poll_interval_s, min(wait, 0.25)))
        finally:
            if running:
                # Calls already claimed are already being paid for; abandoning
                # them would leave claims to time out for ten minutes.
                await asyncio.gather(*running, return_exceptions=True)
        return summary

    def register(self) -> str:
        """Put this worker in the ``workers`` table (once per worker object)."""
        if not self._registered:
            self.store.register_worker(
                {
                    "worker_id": self.worker_id,
                    "user": self.user,
                    "kind": self.worker_kind,
                    "pid": os.getpid(),
                    "endpoints": sorted(self.backends),
                }
            )
            self._registered = True
        return self.worker_id

    def _maybe_heartbeat(
        self, last_beat: float, heartbeat: Optional[Callable[[], Any]]
    ) -> float:
        """Stamp the workers row if the interval has elapsed; return when."""
        now = time.monotonic()
        if last_beat and now - last_beat < self.heartbeat_interval_s:
            return last_beat
        self.store.heartbeat_worker(self.worker_id, _utcnow())
        if heartbeat is not None:
            heartbeat()
        return now

    def _should_stop(self, stop_event: Optional[Any]) -> bool:
        if stop_event is not None and stop_event.is_set():
            return True
        return bool(self.store.stop_requested(self.worker_id))

    def _free_slots(self) -> int:
        """How many more calls this worker may take responsibility for.

        Summed across every account this worker serves -- used ONLY to gate
        "is it worth claiming at all" in :meth:`work`'s loop.  The claim
        itself is no longer sized from this sum: see :meth:`_claim`, which
        claims PER ACCOUNT so that one saturated account's rows can never
        occupy the budget a different, free account was owed.
        """
        return sum(max(0, limit.capacity - limit.assigned) for limit in self._limits.values())

    def _endpoints_by_account(self) -> Dict[str, set]:
        """``{account: {endpoint, ...}}`` -- the inverse of ``_limit_by_endpoint``."""
        table: Dict[str, set] = {}
        for endpoint, limit in self._limit_by_endpoint.items():
            table.setdefault(limit.key, set()).add(endpoint)
        return table

    def _claim(
        self, limit: int, job_id: Optional[str], summary: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Claim due calls, ACCOUNT BY ACCOUNT, keeping only those in scope.

        THE STARVATION FIX.  The old shape claimed ONCE, against the SUM of
        every account's free capacity: ``store.claim_calls(..., limit=sum)``.
        The claim query has no per-account quota of its own -- it returns the
        GLOBAL FIFO head -- so a saturated account's rows occupied the whole
        batch, and the unconditional ``assigned += 1`` below let that
        account's ``assigned`` run straight past its ``capacity``, which is
        what starved every other account behind it (measured: 92 calls
        claimed against ~20 slots, 66 of them one account's).

        The fix claims SEPARATELY per account, each call's ``endpoints``
        restricted to that account's own aliases and its ``limit`` to that
        account's own free capacity (never the cross-account sum).  A
        saturated account's rows can now never be returned against a
        different account's budget, because that budget was never asked for
        them -- the starvation is structurally impossible, not merely
        corrected after the fact.  ``assigned`` is incremented inside this
        same per-row loop (bounded by the per-account ``free`` the query was
        already given, and belt-and-braced again just before the increment),
        so it can never exceed ``capacity``.

        *limit* is accepted only for signature compatibility -- ``daemon.py``
        subclasses this worker and overrides ``_claim``, calling
        ``super()._claim(limit, job_id, summary)`` with exactly these three
        positional arguments, and changing that signature would break it
        silently.  It no longer sizes anything: each account's own
        ``capacity - assigned`` does.

        A row refused by :meth:`_roster_refusal` -- a per-job queue-roster
        mismatch discovered only now, at claim time -- is released exactly
        like an out-of-scope row: handed back unmarked, counted in
        ``summary['released']``, no attempt charged.

        FOR A BATCHED QUEUE (``batch_size > 1``), capacity is measured in
        INVOCATIONS, not rows (confront round 1, point i): up to
        ``free * batch_size`` candidate rows are claimed -- enough to
        assemble up to ``free`` homogeneous groups via the EXISTING
        :meth:`_group_claimed` seam, consumed here rather than re-derived --
        and ``assigned`` moves by ONE PER KEPT GROUP.  A group that would
        push ``assigned`` past ``capacity`` is surplus (over-claimed only to
        leave room for grouping) and is released WITHOUT an attempt charge,
        exactly like an out-of-scope row.  ``batch_size <= 1`` makes
        ``_group_claimed`` return one-row groups, which makes this bit for
        bit the unbatched per-row accounting above.
        """
        kept: List[Dict[str, Any]] = []
        checked_jobs: Dict[str, Optional[str]] = {}
        for account, endpoints in sorted(self._endpoints_by_account().items()):
            account_limit = self._limits[account]
            free = max(0, account_limit.capacity - account_limit.assigned)
            if free <= 0:
                continue
            row_limit = free * max(1, self.batch_size)
            rows = self.store.claim_calls(
                self.worker_id,
                endpoints,
                row_limit,
                _utcnow(),
                seats=self.seats,
                roles=self.roles,
            )
            survivors: List[Dict[str, Any]] = []
            for row in rows:
                if job_id is not None and row["job_id"] != job_id:
                    # Out of scope: hand it straight back, unmarked.  The
                    # claim contract has no job filter, so this is the only
                    # way to scope a run, and charging an attempt for it
                    # would be charging the call for this worker's scope.
                    self._release_quietly(row)
                    summary["released"] += 1
                    continue
                refusal = self._roster_refusal(row, checked_jobs)
                if refusal is not None:
                    logger.warning(
                        "council worker %s: refusing claimed call %s (job %s, "
                        "seat %s) -- %s",
                        self.worker_id,
                        row["call_id"],
                        row["job_id"],
                        row["seat_name"],
                        refusal,
                    )
                    self._release_quietly(row)
                    summary["released"] += 1
                    continue
                survivors.append(row)

            for group in self._group_claimed(survivors):
                if account_limit.assigned >= account_limit.capacity:
                    # Belt and braces for the unbatched case, and the REAL
                    # gate for the batched one: this account's invocation
                    # slots are spent, so every remaining group -- rows that
                    # were over-claimed only to leave room for grouping -- is
                    # surplus.  Release it whole, unmarked, no attempt
                    # charged: a group is not any one row's refusal.
                    for row in group:
                        self._release_quietly(row)
                        summary["released"] += 1
                    continue
                account_limit.assigned += 1
                kept.extend(group)
        for scoped_job in sorted({row["job_id"] for row in kept}):
            self.store.set_job_status(scoped_job, "running", only_if=("queued",))
        return kept

    def _roster_refusal(
        self, row: Mapping[str, Any], cache: Dict[str, Optional[str]]
    ) -> Optional[str]:
        """``None`` if *row*'s job's roster is still inside the validated
        queue set (see :meth:`_check_roster_against_queues`); otherwise the
        refusal message.

        Checked once per JOB per ``_claim`` call, not once per row: the
        predicate validates the whole roster, so a second claimed row of the
        same job would only repeat the same verdict.  A no-op (returns
        ``None``) for a plain mapping config, or a ``CouncilConfig`` with no
        ``queues`` declared.
        """
        checker = getattr(self.config, "check_roster_against_queues", None)
        if checker is None:
            return None
        job_id = row["job_id"]
        if job_id in cache:
            return cache[job_id]
        message: Optional[str] = None
        job = self.store.get_job(job_id)
        if job is not None:
            try:
                checker(self._roster_for(job), job_id=job_id)
            except ConfigError as exc:
                message = str(exc)
        cache[job_id] = message
        return message

    def _release_quietly(self, row: Mapping[str, Any]) -> None:
        try:
            self.store.release_call(
                row["call_id"], row["claim_token"], None, increment_attempt=False
            )
        except StaleClaim:  # pragma: no cover - another worker already has it
            pass

    def _group_claimed(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> List[List[Mapping[str, Any]]]:
        """Group one claim batch for the execution path.

        ``batch_size <= 1`` (not opted in, the default) returns ONE ROW PER
        GROUP -- behaviourally IDENTICAL to the pre-batching "one task per
        claimed row" loop, bit for bit: :meth:`_run_group` sends a
        single-row group straight through the unmodified :meth:`_run_one`
        path, never through any of the batched-invocation machinery below.

        Opted in (``batch_size > 1``), rows are grouped by the full
        homogeneity key ``(job_id, role, seat_name, query_name,
        query_version)`` -- homogeneity is free given a partitioned worker's
        single-seat claim, but it is ASSERTED here rather than assumed: two
        rows land in the same bucket only when every one of those five
        fields agrees, and each bucket is then split into one or more groups
        of size :meth:`_batch_n`, which may itself reduce to 1 (an unverified
        output cap, no per-item estimate, or a low derived ceiling all force
        singles -- see :meth:`_batch_n`).  A bucket whose job or seat cannot
        be resolved falls back to one-row groups for that bucket only.
        """
        if self.batch_size <= 1 or not rows:
            return [[row] for row in rows]

        order: List[tuple] = []
        buckets: Dict[tuple, List[Mapping[str, Any]]] = {}
        for row in rows:
            key = (
                str(row["job_id"]),
                str(row["role"]),
                str(row["seat_name"]),
                str(row.get("query_name") or ""),
                str(row.get("query_version") or ""),
            )
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(row)

        groups: List[List[Mapping[str, Any]]] = []
        for key in order:
            groups.extend(self._split_bucket(key, buckets[key]))
        return groups

    def _split_bucket(
        self, key: tuple, bucket: List[Mapping[str, Any]]
    ) -> List[List[Mapping[str, Any]]]:
        """Split one homogeneous bucket into one or more sized groups."""
        job_id, role, seat_name = key[0], key[1], key[2]
        job = self.store.get_job(job_id)
        if job is None:  # pragma: no cover - a claimed row's job always exists
            return [[row] for row in bucket]
        roster = self._roster_for(job)
        try:
            seat = self._seat_for(roster, role, seat_name)
        except ValueError:  # pragma: no cover - claim_calls already filtered this
            return [[row] for row in bucket]

        n = self._batch_n(seat, role, len(bucket))
        if n <= 1:
            return [[row] for row in bucket]
        groups: List[List[Mapping[str, Any]]] = []
        remaining = list(bucket)
        while remaining:
            groups.append(remaining[:n])
            remaining = remaining[n:]
        return groups

    def _batch_n(self, seat: SeatSpec, role: str, rows_in_hand: int) -> int:
        """The size of the next batch for *seat*/*role*: ``1`` means singles.

        ``n = min(batch_size, rows_in_hand, derived_ceiling)``, where
        ``derived_ceiling = floor(effective_output_cap * 0.75 /
        per_item_output)``.  Three ways this forces (or reduces to) ``1``,
        each logged once per ``(seat, role)`` per worker run (confront round
        1, points a/b):

        * no VERIFIED effective output cap for this seat's
          ``(endpoint_kind, model)`` (see :data:`VERIFIED_OUTPUT_CAPS`) --
          batching is not merely unproven here, it is UNBUILDABLE: no CLI
          argv forwards an output-token ceiling today, so there is nothing to
          size a batch against;
        * no per-item output estimate (:attr:`max_output_tokens_per_item` nor
          a running mean) -- batching must never run on an unknown;
        * ``derived_ceiling < 1`` -- refused outright rather than assembling
          an empty batch, which would leave work spinning forever.

        A ceiling that is merely SMALLER than the configured ``batch_size``
        (but still ``>= 1``) reduces the batch instead of refusing it, and
        logs both numbers once.
        """
        desired = min(self.batch_size, max(0, rows_in_hand))
        if desired <= 1:
            return desired

        cap = self._effective_output_cap(seat)
        if cap is None:
            self._log_once(
                seat.seat_name,
                role,
                "no_cap",
                f"seat {seat.seat_name!r} role {role!r}: no VERIFIED effective "
                f"output cap for endpoint_kind={seat.endpoint_kind!r} "
                f"model={seat.model!r}; batching forced to singles until one is "
                "measured and verified (see worker.VERIFIED_OUTPUT_CAPS)",
            )
            return 1

        per_item = self._per_item_output(seat, role)
        if per_item is None or per_item <= 0:
            self._log_once(
                seat.seat_name,
                role,
                "no_estimate",
                f"seat {seat.seat_name!r} role {role!r}: no per-item output "
                "estimate yet (max_output_tokens_per_item is unset and no "
                "observation has been recorded); batching forced to singles",
            )
            return 1

        derived_ceiling = int(math.floor((cap * 0.75) / per_item))
        if derived_ceiling < 1:
            self._log_once(
                seat.seat_name,
                role,
                "ceiling_refused",
                f"seat {seat.seat_name!r} role {role!r}: derived_ceiling="
                f"{derived_ceiling} (< 1) from effective_output_cap={cap} and "
                f"per_item_output={per_item:.1f}; refusing batching this run, "
                "running as singles rather than assembling an empty batch",
            )
            return 1
        if derived_ceiling < desired:
            self._log_once(
                seat.seat_name,
                role,
                "ceiling_reduced",
                f"seat {seat.seat_name!r} role {role!r}: derived_ceiling="
                f"{derived_ceiling} < batch_size={self.batch_size}; reducing "
                f"this batch to {derived_ceiling} (effective_output_cap={cap}, "
                f"per_item_output={per_item:.1f})",
            )
            desired = derived_ceiling
        return max(1, desired)

    def _effective_output_cap(self, seat: SeatSpec) -> Optional[int]:
        """The lesser of a VERIFIED output cap and ``seat.max_output_tokens``.

        ``None`` when no cap for this seat's ``(endpoint_kind, model)`` is
        verified (see :data:`VERIFIED_OUTPUT_CAPS`) -- which, on this commit,
        is every CLI combination, because neither CLI backend's argv forwards
        an output-token ceiling at all.  A seat declaring batch support (an
        endpoint kind config validation already requires) does NOT by itself
        establish a cap.
        """
        verified = VERIFIED_OUTPUT_CAPS.get((seat.endpoint_kind, seat.model))
        if verified is None:
            return None
        return min(int(verified), int(seat.max_output_tokens))

    def _per_item_output(self, seat: SeatSpec, role: str) -> Optional[float]:
        """The per-item output estimate for *seat*/*role*, or ``None``.

        The configured :attr:`max_output_tokens_per_item` wins when set;
        otherwise the running mean of OBSERVED per-item completion tokens
        for this ``(seat, role, served_model)``, which
        :meth:`_record_output_sample` maintains -- ``None`` with no eligible
        observation yet, which is "unknown", not zero.
        """
        if self.max_output_tokens_per_item is not None:
            return float(self.max_output_tokens_per_item)
        total_tokens, total_members = self._output_samples.get(
            (seat.seat_name, role, seat.model), (0, 0)
        )
        if total_members <= 0:
            return None
        return total_tokens / total_members

    def _record_output_sample(
        self, seat_name: str, role: str, served_model: str, members: int, completion_tokens: int
    ) -> None:
        """Feed one SUCCESSFUL, COMPLETE invocation into the running mean.

        ``members`` is the number of items REQUESTED (the invocation's own
        batch size), not the number actually answered -- a model that
        answered 18 of 20 still cost roughly a 20-item invocation's worth of
        output, and dividing by 18 would overestimate the per-item cost the
        next ceiling is sized from.  Callers must only call this for an
        invocation that was ``ok``, not truncated, and whose usage was
        actually measured: including zero-cost or partial samples would drag
        the mean down and *enlarge* the next batch -- a feedback loop pointed
        straight at the truncation this heuristic exists to avoid.  Enforced
        HERE, at the one choke point every caller goes through, rather than
        trusted to each call site: a ``completion_tokens`` of zero or less is
        dropped rather than recorded, zero being exactly the sentinel a
        reused answer reports (:data:`ZERO_USAGE`) and also what an
        unmeasured-but-coerced usage dict would read as.
        """
        if members <= 0 or completion_tokens <= 0:
            return
        key = (seat_name, role, served_model)
        total_tokens, total_members = self._output_samples.get(key, (0, 0))
        self._output_samples[key] = (
            total_tokens + max(0, int(completion_tokens)),
            total_members + members,
        )

    def _log_once(self, seat_name: str, role: str, tag: str, message: str) -> None:
        """Log *message* at most once per ``(seat, role, tag)`` per worker run."""
        key = (seat_name, role, tag)
        if key in self._logged_batch_notices:
            return
        self._logged_batch_notices.add(key)
        logger.warning("council worker %s: %s", self.worker_id, message)

    async def _run_group(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Run a claimed GROUP: one unit against ``_AccountLimit``.

        A one-row group (today's only shape when ``batch_size <= 1``) runs
        through the unmodified :meth:`_run_one`.  A real batch (``len(rows) >
        1``) runs through :meth:`_run_batch_or_split`, which may bisect on a
        whole-invocation failure (:meth:`_invoke_batch`) -- however many
        backend invocations that recursion costs, ``assigned`` is released
        exactly ONCE here, when the whole group is done, because a batch is
        ONE in-flight unit against the account's capacity, not ``len(rows)``.
        """
        if len(rows) <= 1:
            for row in rows:
                await self._run_one(row)
            return
        endpoint = str(rows[0]["endpoint"])
        limit = self._limit_by_endpoint.get(endpoint)
        try:
            await self._run_batch_or_split(list(rows))
        finally:
            if limit is not None:
                limit.assigned = max(0, limit.assigned - 1)

    async def _run_one(self, row: Mapping[str, Any]) -> None:
        limit = self._limit_by_endpoint[row["endpoint"]]
        try:
            await self.execute(row)
        finally:
            limit.assigned = max(0, limit.assigned - 1)

    async def _run_one_within_group(self, call: Mapping[str, Any]) -> None:
        """Run one row through the unmodified single-call path, INSIDE a
        group whose ``assigned`` unit is managed by the group's own caller.

        This is how bisection bottoms out (see :meth:`_run_batch_or_split`):
        once a split reaches one row, that row is isolated and fails or
        succeeds exactly as it does today -- including the existing
        ``max_attempts`` retry/exhaustion gate, which this deliberately does
        NOT re-implement.
        """
        await self.execute(dict(call))

    # -- batched invocation -------------------------------------------------

    async def _run_batch_or_split(self, rows: List[Mapping[str, Any]]) -> None:
        """Run *rows* as one batched invocation, bisecting on failure.

        *rows* are already CLAIMED and stay that way across every recursive
        step: a whole-invocation failure (transport, timeout, truncation, or
        a wrapper-schema-invalid reply) is retried by splitting *rows* in
        half and recursing on each half directly under the SAME claims,
        sequentially -- never released back to the queue mid-split, because a
        re-queued half could recombine with the poison item and loop
        forever.  Recursion bottoms out at one row, which runs through
        :meth:`_run_one_within_group` -- the ordinary single-call path,
        including its own ``max_attempts`` gate -- so a poison item is
        isolated in ``log2(n)`` rounds and costs only its OWN attempts, never
        its batch-mates'.
        """
        if len(rows) <= 1:
            for row in rows:
                await self._run_one_within_group(row)
            return

        members: List[_BatchMember] = []
        for row in rows:
            member = await self._prepare_member(dict(row))
            if member is not None:
                members.append(member)
        if not members:
            return
        if len(members) <= 1:
            for member in members:
                await self._run_one_within_group(member.call)
            return

        outcome = await self._invoke_batch(members)
        if outcome != "batch_failed":
            return
        sent = [member.call for member in members]
        left, right = _bisect_rows(sent)
        await self._run_batch_or_split(left)
        await self._run_batch_or_split(right)

    async def _prepare_member(self, call: Dict[str, Any]) -> Optional[_BatchMember]:
        """Run one row's pre-spend checks and return it ready to batch.

        The same three checks :meth:`execute` runs for a single call, in the
        same order, because batching changes only the transport: a route
        fingerprint that no longer matches this worker's config parks the
        row (never included); an already-answered identical question is
        settled from reuse (never included); a job whose permission gate
        refuses this call settles or parks it here too.  ``None`` means the
        row was fully handled by one of those three and must not be sent to
        a model.  Single-flight collapsing is intentionally NOT applied to
        batch members (see the t8 work record).
        """
        token = call.get("claim_token")
        job = self.store.get_job(call["job_id"])
        if job is None:  # pragma: no cover - a claimed row's job always exists
            return None
        roster = self._roster_for(job)
        role = str(call["role"])
        try:
            seat = self._seat_for(roster, role, str(call["seat_name"]))
        except ValueError:  # pragma: no cover - claim_calls already filtered this
            return None

        expected_route = self._route_fingerprint(seat)
        if call.get("route_fingerprint") and call["route_fingerprint"] != expected_route:
            self._park(
                call,
                token,
                CouncilError(
                    kind=CONFIGURATION_MISMATCH,
                    message=(
                        f"call was created for route {call['route_fingerprint'][:12]}... "
                        f"but this worker's config for endpoint {call['endpoint']!r} "
                        f"now routes seat {seat.seat_name!r} to {expected_route[:12]}..."
                    ),
                ),
                wait_s=self.mismatch_wait_s,
            )
            return None

        query = self._query_for(job)
        item = self._item_for(call["job_id"], call["item_id"])
        if item is None:  # pragma: no cover - a claimed row's item always exists
            return None
        task = self._task_for(item)

        hash_key = str(call.get("prompt_hash") or "")
        if hash_key:
            reusable = self.store.find_reusable(hash_key)
            if reusable is not None and reusable["call_id"] != call["call_id"]:
                self._settle_reused(
                    call, token, reusable, query=query, roster=roster, role=role
                )
                return None

        gate = await self._permission_gate(call, token, seat, job)
        if gate is not None:
            return None

        context = self._context_for(role, call, query, roster)
        system, user = query.render(role, task, context)
        schema = query.answer_schema(role)
        return _BatchMember(
            call=call,
            token=token,
            job=job,
            roster=roster,
            seat=seat,
            query=query,
            role=role,
            system=system,
            user=user,
            schema=schema,
            context=context,
        )

    async def _invoke_batch(self, members: List[_BatchMember]) -> str:
        """Send ONE batched invocation for *members* and settle or fail it.

        Returns ``"ok"`` once every member has been individually settled
        (banked, released for retry, or terminally failed -- see
        :meth:`_settle_batch_answers`), or ``"batch_failed"`` when the
        invocation itself did not individually judge any item (transport,
        timeout, truncation, or a wrapper-schema-invalid reply), which the
        caller retries by bisection.

        Usage is recorded exactly once per invocation, by ``invocation_id``
        -- never anchored on "the first call row", which may itself be
        missing, invalid or stale, and which a bisected child batch could
        share with its parent (confront round 1, point f).  This happens for
        a FAILED invocation too, whose spend the single-call retry path
        discards today.
        """
        first = members[0]
        endpoint = str(first.call["endpoint"])
        seat = first.seat
        mapping = self.endpoint_mapping(endpoint)
        api_type = str(mapping.get("kind") or seat.endpoint_kind or "")

        from ..queries.spec import (
            BATCH_SYSTEM_SUFFIX,
            render_batch_user,
            wrap_batch_answer_schema,
        )

        role_schema = first.schema
        wrapped_schema = wrap_batch_answer_schema(role_schema)
        system = f"{first.system}{BATCH_SYSTEM_SUFFIX}"
        user = render_batch_user(
            [(str(m.call["call_id"]), m.user) for m in members]
        )
        trace_id = f"batch|{first.role}|{seat.seat_name}|{len(members)}"

        request: Any = LLMRequest(
            model=seat.model,
            system=system,
            user=user,
            json_schema=wrapped_schema,
            max_output_tokens=seat.max_output_tokens,
            temperature=seat.temperature,
            params=dict(seat.extra or {}),
            timeout_s=seat.timeout_s,
            trace_id=trace_id,
        )
        if self.backends.get(endpoint) is not None:
            request = CompletionRequest(
                seat=seat,
                role=first.role,
                system=system,
                user=user,
                json_schema=wrapped_schema,
                trace_id=trace_id,
                deadline_s=seat.timeout_s,
            )

        queued_at = _utcnow()
        async with self._slot(endpoint):
            started = _utcnow()
            result = await self._acomplete(request, api_type=api_type, endpoint=mapping)
        finished = _utcnow()

        batch_error: Optional[CouncilError] = None
        parsed_wrapper: Any = None
        if not result.ok:
            batch_error = result.error or CouncilError(
                kind="transport", message="the backend reported failure with no error"
            )
        elif result.truncated:
            batch_error = CouncilError(
                kind="invalid_output",
                message=(
                    "the batched reply was truncated at the token ceiling "
                    f"({seat.max_output_tokens}); a cut-off batch cannot be "
                    "trusted for any of its items"
                ),
            )
        else:
            complaint = _validate_answer(result.parsed, BATCH_WRAPPER_SCHEMA)
            if complaint is not None:
                batch_error = CouncilError(
                    kind="invalid_output",
                    message=f"the batched reply did not satisfy the wrapper schema: {complaint}",
                )
            else:
                parsed_wrapper = result.parsed

        invocation_id = uuid.uuid4().hex
        member_call_ids = [str(m.call["call_id"]) for m in members]
        anchor = first
        anchor_attempt_no = int(anchor.call.get("attempt_count") or 0) + 1
        usage = dict(result.usage) if result.usage is not None else None

        if usage is not None:
            spent = _token_total(usage)
            if spent:
                self.store.add_tokens(endpoint, spent)
            self._enforce_budget(endpoint, usage)

        if batch_error is not None:
            self._record_failed_invocation(
                members,
                invocation_id,
                anchor,
                anchor_attempt_no,
                usage,
                batch_error,
                started,
                finished,
                queued_at,
            )
            return "batch_failed"

        if usage is not None:
            self._record_output_sample(
                seat.seat_name,
                first.role,
                seat.model,
                len(members),
                int(usage.get("completion_tokens") or 0),
            )

        self._settle_batch_answers(
            members,
            parsed_wrapper,
            role_schema,
            invocation_id=invocation_id,
            anchor=anchor,
            anchor_attempt_no=anchor_attempt_no,
            usage=usage,
            started=started,
            finished=finished,
            queued_at=queued_at,
        )
        return "ok"

    def _invocation_usage(
        self,
        invocation_id: str,
        member_call_ids: Sequence[str],
        anchor_call_id: str,
        anchor_attempt_no: int,
        usage: Optional[Mapping[str, Any]],
        *,
        is_anchor: bool,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """The audit-trail ``usage`` payload every member attempt carries.

        Every member references the SAME ``invocation_id``, ``batch_size``,
        ``member_call_ids`` and anchor -- so the invocation is reconstructible
        from any one of its members' attempt rows -- but only the anchor's
        own record carries the REAL measured token counts.  The others get
        the bookkeeping fields alone: duplicating the real numbers onto every
        member would invite a naive reader to sum them and multiply the
        invocation's true cost by ``len(members)``.
        """
        payload: Dict[str, Any] = {
            "invocation_id": invocation_id,
            "batch_size": len(member_call_ids),
            "member_call_ids": list(member_call_ids),
            "anchor_call_id": anchor_call_id,
            "anchor_attempt_no": anchor_attempt_no,
        }
        if is_anchor and usage is not None:
            payload["prompt_tokens"] = usage.get("prompt_tokens")
            payload["completion_tokens"] = usage.get("completion_tokens")
        if extra:
            payload.update(dict(extra))
        return payload

    def _record_failed_invocation(
        self,
        members: List[_BatchMember],
        invocation_id: str,
        anchor: _BatchMember,
        anchor_attempt_no: int,
        usage: Optional[Mapping[str, Any]],
        error: CouncilError,
        started: datetime,
        finished: datetime,
        queued_at: datetime,
    ) -> None:
        """Audit a whole-invocation failure WITHOUT charging any row.

        Filed under a negative, UNCHARGED ``attempts.attempt_no`` (the same
        convention ``daemon.py``'s stop-deadline ``interrupted`` recording
        uses, via ``store.next_uncharged_attempt_no``): ``attempt_count``
        stays untouched, because a batch-level failure is not any one item's
        fault, and the single-item ``max_attempts`` budget must not be spent
        until a row is invoked ALONE (confront round 1, point h).  Claims are
        left exactly as they were -- this never releases or completes a
        call -- so the caller can bisect and retry under the same tokens.
        """
        member_call_ids = [str(m.call["call_id"]) for m in members]
        anchor_call_id = str(anchor.call["call_id"])
        with self.store.transaction():
            for member in members:
                call_id = str(member.call["call_id"])
                payload = self._invocation_usage(
                    invocation_id,
                    member_call_ids,
                    anchor_call_id,
                    anchor_attempt_no,
                    usage,
                    is_anchor=call_id == anchor_call_id,
                    extra={"ancestor_failure": True},
                )
                attempt_no = self._uncharged_attempt_no(call_id)
                self._write_attempt(
                    call_id,
                    attempt_no,
                    started,
                    finished,
                    error.kind,
                    payload,
                    queued_at,
                )

    def _uncharged_attempt_no(self, call_id: str) -> int:
        """A free, negative ``attempts.attempt_no`` that charges nothing.

        Mirrors ``daemon.py``'s ``_uncharged_attempt_no``: a store missing
        ``next_uncharged_attempt_no`` (a test double, say) falls back to a
        fixed ``-1``, which is wrong only if TWO uncharged rows are filed for
        the same call in one process lifetime -- acceptable for a fallback
        that exists purely so a minimal store double does not crash.
        """
        nxt = getattr(self.store, "next_uncharged_attempt_no", None)
        if callable(nxt):
            return int(nxt(call_id))
        return -1  # pragma: no cover - SqliteStore always has this method

    def _write_attempt(
        self,
        call_id: str,
        attempt_no: int,
        started: datetime,
        finished: datetime,
        outcome_kind: str,
        usage: Mapping[str, Any],
        queued_at: Optional[datetime],
    ) -> None:
        """``store.record_attempt``, tolerating a store built before
        ``queued_at`` existed -- the same fallback :meth:`_record_attempt`
        already uses for the single-call path.
        """
        try:
            self.store.record_attempt(
                call_id,
                attempt_no,
                started,
                finished,
                outcome_kind,
                usage=dict(usage),
                worker_id=self.worker_id,
                queued_at=queued_at,
            )
        except TypeError:
            self.store.record_attempt(
                call_id,
                attempt_no,
                started,
                finished,
                outcome_kind,
                usage=dict(usage),
                worker_id=self.worker_id,
            )

    def _settle_batch_answers(
        self,
        members: List[_BatchMember],
        parsed_wrapper: Any,
        role_schema: Mapping[str, Any],
        *,
        invocation_id: str,
        anchor: _BatchMember,
        anchor_attempt_no: int,
        usage: Optional[Mapping[str, Any]],
        started: datetime,
        finished: datetime,
        queued_at: datetime,
    ) -> None:
        """Settle every member of a WRAPPER-VALID batched reply, 1:N.

        The wrapper was already validated as a unit by :meth:`_invoke_batch`
        (there IS an ``answers`` array); this resolves each element to its
        call row by ``item_key``, strips that key, and validates the
        REMAINING payload against the role's OWN answer schema -- so
        ``_validate_answer`` and :mod:`.normalise` are reused unchanged per
        element, and one malformed element never fails its batch-mates
        (confront round 1, point c).  An unknown ``item_key`` is dropped with
        a warning; a repeated one keeps the first and drops the rest.
        """
        known_keys = {str(m.call["call_id"]) for m in members}
        answers = (
            parsed_wrapper.get("answers")
            if isinstance(parsed_wrapper, Mapping)
            else None
        )
        by_key: Dict[str, Mapping[str, Any]] = {}
        for element in answers or ():
            if not isinstance(element, Mapping):
                continue
            key = element.get(BATCH_ITEM_KEY_FIELD)
            if not isinstance(key, str) or key not in known_keys:
                logger.warning(
                    "council worker %s: batched reply named unknown item_key %r; "
                    "dropped",
                    self.worker_id,
                    key,
                )
                continue
            if key in by_key:
                logger.warning(
                    "council worker %s: batched reply repeated item_key %r; first "
                    "wins",
                    self.worker_id,
                    key,
                )
                continue
            by_key[key] = element

        member_call_ids = [str(m.call["call_id"]) for m in members]
        anchor_call_id = str(anchor.call["call_id"])
        for member in members:
            call_id = str(member.call["call_id"])
            base_usage = self._invocation_usage(
                invocation_id,
                member_call_ids,
                anchor_call_id,
                anchor_attempt_no,
                usage,
                is_anchor=call_id == anchor_call_id,
            )
            element = by_key.get(call_id)
            if element is None:
                self._settle_batch_released(
                    member,
                    base_usage,
                    started,
                    finished,
                    queued_at,
                    outcome_kind="missing",
                    message=(
                        f"the batched reply did not include an answer for item "
                        f"{member.call['item_id']!r} (item_key {call_id!r})"
                    ),
                )
                continue
            payload = {k: v for k, v in element.items() if k != BATCH_ITEM_KEY_FIELD}
            parsed, repairs = normalise(member.role, payload, role_schema, member.context)
            complaint = _validate_answer(parsed, role_schema)
            if complaint is not None:
                self._settle_batch_released(
                    member,
                    base_usage,
                    started,
                    finished,
                    queued_at,
                    outcome_kind="invalid_output",
                    message=complaint,
                )
                continue
            self._settle_batch_ok(
                member, base_usage, parsed, repairs, started, finished, queued_at
            )

    def _settle_batch_ok(
        self,
        member: _BatchMember,
        base_usage: Mapping[str, Any],
        parsed: Any,
        repairs: Sequence[Mapping[str, Any]],
        started: datetime,
        finished: datetime,
        queued_at: datetime,
    ) -> None:
        """Bank one member's valid, known answer."""
        call, token = member.call, member.token
        attempt_no = int(call.get("attempt_count") or 0) + 1
        try:
            with self.store.transaction():
                self.store.complete_call(
                    call["call_id"],
                    token,
                    "ok",
                    reply_text=json.dumps(
                        {BATCH_ITEM_KEY_FIELD: call["call_id"], **parsed}, sort_keys=True
                    ),
                    reply_parsed=parsed,
                    fresh_usage=None,
                    error=None,
                    normalised=list(repairs or ()) or None,
                )
                self._write_attempt(
                    call["call_id"], attempt_no, started, finished, "ok", base_usage, queued_at
                )
                self.advance(call["job_id"], call["item_id"])
        except StaleClaim:  # pragma: no cover - another worker already has it
            return

    def _settle_batch_released(
        self,
        member: _BatchMember,
        base_usage: Mapping[str, Any],
        started: datetime,
        finished: datetime,
        queued_at: datetime,
        *,
        outcome_kind: str,
        message: str,
    ) -> None:
        """Release a MISSING or INVALID member for retry, or exhaust it.

        Both are treated identically to each other and symmetrically to the
        single-call retry path: released with ``attempt_count`` incremented
        while below ``max_attempts``, settled as ``error`` and its item
        advanced on exhaustion (confront round 1, points d and e) -- a
        finite budget, because an always-omitted or always-invalid item must
        not consume calls indefinitely.
        """
        call, token = member.call, member.token
        failures = int(call.get("attempt_count") or 0) + 1
        error = CouncilError(kind=outcome_kind, message=message)
        if failures < self.max_attempts:
            try:
                with self.store.transaction():
                    self.store.note_call_error(call["call_id"], error.model_dump())
                    self._write_attempt(
                        call["call_id"], failures, started, finished, outcome_kind,
                        base_usage, queued_at,
                    )
                    self.store.release_call(
                        call["call_id"],
                        token,
                        _utcnow() + timedelta(seconds=self._backoff(failures)),
                        increment_attempt=True,
                    )
            except StaleClaim:  # pragma: no cover - another worker already has it
                return
            return
        try:
            with self.store.transaction():
                self.store.complete_call(
                    call["call_id"], token, "error", reply_text=None, error=error.model_dump()
                )
                self._write_attempt(
                    call["call_id"], failures, started, finished, outcome_kind,
                    base_usage, queued_at,
                )
                self.advance(call["job_id"], call["item_id"])
        except StaleClaim:  # pragma: no cover - another worker already has it
            return

    def _scope_state(
        self, job_id: Optional[str]
    ) -> Tuple[int, List[Dict[str, Any]], Optional[float]]:
        """Is there anything left to do in scope, and can anything run?

        Returns ``(outstanding, blockers, due_in)`` where *outstanding* counts
        live pending and claimed calls, *blockers* is ``job_status``'s answer
        for every job that has any, and *due_in* is the seconds until the
        soonest call this worker could run becomes due -- ``None`` when there
        is no such call at all, which with blockers present is the signal to
        stop rather than spin.
        """
        now = _utcnow()
        outstanding = 0
        blockers: List[Dict[str, Any]] = []
        due_in: Optional[float] = None
        halted: Dict[str, bool] = {}

        for scoped_job in self._scope_jobs(job_id):
            job_outstanding = 0
            for call in self.store.get_calls(scoped_job):
                if not call.get("live", True) or call["status"] not in (
                    "pending",
                    "claimed",
                ):
                    continue
                job_outstanding += 1
                if call["status"] == "claimed":
                    continue
                endpoint = str(call["endpoint"])
                if endpoint not in self.backends:
                    continue
                if endpoint not in halted:
                    halted[endpoint] = bool(
                        self.store.get_endpoint_state(endpoint)["halt_kind"]
                    )
                if halted[endpoint]:
                    continue
                not_before = call.get("not_before")
                wait = 0.0 if not_before is None else (not_before - now).total_seconds()
                wait = max(0.0, wait)
                due_in = wait if due_in is None else min(due_in, wait)
            outstanding += job_outstanding
            if job_outstanding:
                blockers.extend(self.store.job_status(scoped_job)["blockers"])
        return outstanding, blockers, due_in

    def _scope_jobs(self, job_id: Optional[str]) -> List[str]:
        if job_id is not None:
            return [job_id]
        # Every job in the store, not this worker's label: the claim contract
        # has no label filter either, so scoping the idle check by one would
        # report "nothing left to do" while claimable calls sat in the queue.
        return [
            job["job_id"]
            for job in self.store.list_jobs()
            if job["status"] not in ("done", "cancelled")
        ]

    # -- running one call -------------------------------------------------

    async def execute(self, call: Mapping[str, Any]) -> str:
        """Run one CLAIMED call and settle, release or discard it.

        Returns the outcome: ``ok``, ``reused``, ``stale``, one of the
        ``CouncilError`` kinds, or ``configuration_mismatch``.  Never raises
        for a call failure -- a backend does not, and neither does this.
        """
        call = dict(call)
        token = call.get("claim_token")
        job = self.store.get_job(call["job_id"])
        roster = self._roster_for(job)
        role = str(call["role"])
        seat = self._seat_for(roster, role, str(call["seat_name"]))

        # 1. Is this call still OUR call?  A route fingerprint that no longer
        #    matches means the config changed under a queued call: running it
        #    would attribute an answer from one route to another.
        expected_route = self._route_fingerprint(seat)
        if call.get("route_fingerprint") and call["route_fingerprint"] != expected_route:
            return self._park(
                call,
                token,
                CouncilError(
                    kind=CONFIGURATION_MISMATCH,
                    message=(
                        f"call was created for route {call['route_fingerprint'][:12]}... "
                        f"but this worker's config for endpoint {call['endpoint']!r} "
                        f"now routes seat {seat.seat_name!r} to {expected_route[:12]}..."
                    ),
                ),
                wait_s=self.mismatch_wait_s,
            )

        if str(call["endpoint"]) not in self.backends:
            # pragma: no cover - claim_calls filters these out
            raise ValueError(
                f"this worker does not serve endpoint {call['endpoint']!r}; it "
                f"serves {sorted(self.backends)}"
            )

        # Only now is the query type needed.  A worker whose route no longer
        # matches parks the call above without it, which is what lets a
        # misconfigured machine hand work back rather than fail on a query
        # type it was never going to render.
        query = self._query_for(job)
        item = self._item_for(call["job_id"], call["item_id"])
        task = self._task_for(item)

        # 2. Has anybody, anywhere, already answered this exact question?
        hash_key = str(call.get("prompt_hash") or "")
        if hash_key:
            reusable = self.store.find_reusable(hash_key)
            if reusable is not None and reusable["call_id"] != call["call_id"]:
                return self._settle_reused(
                    call, token, reusable, query=query, roster=roster, role=role
                )

        # 3. Is an identical question in flight right now, in this worker?
        leader = False
        future = self._single_flight.get(hash_key) if hash_key else None
        if future is not None:
            source = await asyncio.shield(future)
            if source is not None:
                return self._settle_reused(
                    call, token, source, query=query, roster=roster, role=role
                )
            # The leader failed; ask for ourselves rather than inherit it.
        elif hash_key:
            future = asyncio.get_running_loop().create_future()
            self._single_flight[hash_key] = future
            leader = True

        try:
            outcome = await self._invoke(
                call, token, query, roster, seat, task, role, job
            )
        except BaseException:
            if leader:
                self._resolve_leader(hash_key, future, None)
            raise
        if leader:
            self._resolve_leader(
                hash_key,
                future,
                self._reusable_self(call) if outcome == "ok" else None,
            )
        return outcome

    def _resolve_leader(
        self, hash_key: str, future: Any, source: Optional[Mapping[str, Any]]
    ) -> None:
        """Hand the single-flight followers the leader's answer (or nothing)."""
        self._single_flight.pop(hash_key, None)
        if future is not None and not future.done():
            future.set_result(source)

    def _reusable_self(self, call: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Re-read the call we just completed, as a reuse source."""
        rows = self.store.get_calls(call["job_id"], call["item_id"])
        for row in rows:
            if row["call_id"] == call["call_id"] and row["status"] == "ok":
                return row
        return None

    async def _invoke(
        self,
        call: Mapping[str, Any],
        token: Optional[str],
        query: Any,
        roster: Roster,
        seat: SeatSpec,
        task: CouncilTask,
        role: str,
        job: Mapping[str, Any],
    ) -> str:
        """Render, call the model through the wrapper, and route the outcome.

        This is the ONLY place in the council that spends a token, and the
        only place that calls :attr:`_acomplete`.  The call is one attempt:
        everything that might lead to a second one -- the backoff, the halt,
        the budget, the model-unavailable park -- happens after it returns,
        from the taxonomy on the result.

        :meth:`_permission_gate` runs FIRST, before the prompts are even
        rendered: whether this call may be made at all is decided from its own
        job's options, and nothing about rendering changes that answer.
        """
        gate = await self._permission_gate(call, token, seat, job)
        if gate is not None:
            return gate
        context = self._context_for(role, call, query, roster)
        system, user = query.render(role, task, context)
        schema = query.answer_schema(role)
        trace_id = f"{call['item_id']}|{role}|{seat.seat_name}"
        endpoint = str(call["endpoint"])
        mapping = self.endpoint_mapping(endpoint)
        api_type = str(mapping.get("kind") or seat.endpoint_kind or "")

        request: Any = LLMRequest(
            model=seat.model,
            system=system,
            user=user,
            json_schema=dict(schema or {}),
            max_output_tokens=seat.max_output_tokens,
            temperature=seat.temperature,
            params=dict(seat.extra or {}),
            timeout_s=seat.timeout_s,
            trace_id=trace_id,
        )
        if self.backends.get(endpoint) is not None:
            # An explicitly injected api type is handed the council's own
            # CompletionRequest instead.  It carries the SEAT and the ROLE,
            # which an LLMRequest deliberately does not (the wrapper is
            # domain-neutral), and the api types a caller injects are the ones
            # that may read them -- every scripted fake in these suites keys
            # its answer on (role, seat_name).  The two records are
            # interchangeable to anything that only reads the request:
            # llm.types.as_llm_request converts one to the other.
            request = CompletionRequest(
                seat=seat,
                role=role,
                system=system,
                user=user,
                json_schema=schema,
                trace_id=trace_id,
                deadline_s=seat.timeout_s,
            )

        # SERVICE-TIME INSTRUMENTATION.  ``queued_at`` is stamped the instant
        # this call is ready to enter the endpoint's (possibly account-
        # shared) concurrency slot; ``started`` only AFTER the slot is
        # actually acquired.  Stamping ``started`` before the slot (as this
        # used to) conflates queue-wait with service time -- measured to
        # disagree by 65x between an honest uncontended reading and a
        # store median taken under real contention.  With the split:
        # service time is ``finished - started`` and queue wait is
        # ``started - queued_at``.
        queued_at = _utcnow()
        async with self._slot(endpoint):
            started = _utcnow()
            result = await self._acomplete(
                request, api_type=api_type, endpoint=mapping
            )
        finished = _utcnow()

        if result.ok:
            if result.truncated:
                return self._fail_call(
                    call,
                    token,
                    CouncilError(
                        kind="invalid_output",
                        message=(
                            "the reply was truncated at the token ceiling "
                            f"({seat.max_output_tokens}); a cut-off answer is not a vote"
                        ),
                    ),
                    started,
                    finished,
                    result,
                    queued_at=queued_at,
                )
            # Two measured reply defects are repaired BEFORE the gate (see
            # engine/normalise.py).  The context is the one already built for
            # the prompt, so the flag a meta reply omitted is derived from
            # exactly the vote table and critic audit that meta was shown.
            parsed, repairs = normalise(role, result.parsed, schema, context)
            complaint = _validate_answer(parsed, schema)
            if complaint is not None:
                return self._fail_call(
                    call,
                    token,
                    CouncilError(kind="invalid_output", message=complaint),
                    started,
                    finished,
                    result,
                    queued_at=queued_at,
                )
            return self._settle_ok(
                call, token, result, started, finished, parsed, repairs,
                queued_at=queued_at,
            )

        error = result.error or CouncilError(
            kind="transport", message="the backend reported failure with no error"
        )
        if error.kind in HALTING_KINDS:
            return self._halt_endpoint(call, token, error)
        if error.kind == "model_unavailable":
            return self._park(call, token, error, wait_s=self.model_wait_s)
        if error.kind in RETRYABLE_KINDS:
            failures = int(call.get("attempt_count") or 0) + 1
            if failures < self.max_attempts:
                return self._retry_later(
                    call, token, error, failures, started, finished, result,
                    queued_at=queued_at,
                )
        return self._fail_call(
            call, token, error, started, finished, result, queued_at=queued_at
        )

    # -- the per-call permission gate -------------------------------------

    @staticmethod
    def job_permissions(job: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """What the JOB was submitted with.  The only authority on its calls.

        Read from ``jobs.options`` every time, for every call, because the
        permission belongs to the job and to nothing else (review round 3):
        a flag on this worker, or on a cached client, would let a job
        submitted with ``--allow-unverified`` admit the unknown calls of a
        job submitted without it -- a worker serves whatever is due in the
        queue, and the two jobs can be in flight at the same moment.
        """
        options = dict((job or {}).get("options") or {})
        return {
            "allow_unverified": bool(options.get("allow_unverified")),
            "allow_unavailable": bool(options.get("allow_unavailable")),
            "unservable_endpoints": dict(options.get("unservable_endpoints") or {}),
        }

    async def _permission_gate(
        self,
        call: Mapping[str, Any],
        token: Optional[str],
        seat: SeatSpec,
        job: Mapping[str, Any],
    ) -> Optional[str]:
        """May this call be made?  Returns an outcome if it settled it, else None.

        Three rules, all decided from the call's OWN job (see
        :meth:`job_permissions`):

        * An endpoint the job was told was **unservable** and still is: the
          call WAITS.  ``allow_unavailable`` let the job be created; it never
          made the endpoint reachable, and spending against it would only
          produce a transport failure.  Re-checked rather than taken on trust
          from the options, so a job queued while a gateway was down finishes
          once it is back.
        * A **refused** billing preflight: refused, always, and the endpoint
          is halted.  ``allow_unverified`` cannot reach this case -- "I know
          this bills the wrong way" is not "I could not tell".
        * An **unknown** billing state: admitted only if THIS job carries
          ``allow_unverified``.  Refused WITHOUT halting the endpoint,
          because the refusal is a fact about one job's permission and
          halting would stop another job that does have it.
        """
        permissions = self.job_permissions(job)
        endpoint = str(call["endpoint"])

        if endpoint in permissions["unservable_endpoints"]:
            state = await self._endpoint_state(endpoint, seat)
            if state["state"] == "unavailable":
                return self._park(
                    call,
                    token,
                    CouncilError(
                        kind="model_unavailable",
                        message=(
                            f"endpoint {endpoint!r} is unavailable on this machine, "
                            f"as it was when this job was submitted with "
                            f"allow_unavailable: {state['detail'] or 'no detail'}"
                        ),
                        retryable=True,
                    ),
                    wait_s=self.model_wait_s,
                )

        report = await self._billing_state(endpoint, seat)
        state = str(report.get("state") or "unknown")
        if state == "ready":
            return None
        detail = str(report.get("detail") or "no detail")
        if state == "refused":
            return self._halt_endpoint(
                call,
                token,
                BillingGuardRefused(
                    f"endpoint {endpoint!r} refused the billing preflight: {detail}"
                ).to_error(),
            )
        if permissions["allow_unverified"]:
            return None
        now = _utcnow()
        return self._fail_call(
            call,
            token,
            CouncilError(
                kind="billing_guard",
                message=(
                    f"endpoint {endpoint!r} could not confirm how it bills "
                    f"({state}: {detail}), and job {call['job_id']} was not "
                    "submitted with allow_unverified. Re-submit it with "
                    "allow_unverified=True (or `--allow-unverified`) to spend "
                    "against it."
                ),
                # NOT halting: another job in this store may legitimately
                # carry the permission this one does not, and a halt is the
                # whole endpoint's.
                halts_endpoint=False,
            ),
            now,
            now,
        )

    async def _billing_state(self, endpoint: str, seat: SeatSpec) -> Dict[str, Any]:
        """The endpoint's offline preflight report.  Free, and cached.

        Cached per endpoint for this worker's life: it is a fact about this
        machine's configuration files, not about a job, and re-reading four
        files per call in a 20k-item batch would be pure syscall overhead.
        Caching it is safe because it is only ever used to decide whether an
        UNKNOWN state is admitted -- the CLI api types re-run their own gate
        whenever one of the files it read changes, so a ``refused`` that
        appears mid-batch is still caught by the call itself.

        An api type with no ``preflight`` reports ``ready`` (through
        ``llm.preflight_api``): there is nothing about it that could refuse.
        An endpoint this worker cannot build an api type for also reports
        ``ready`` -- it will never be claimed for, and inventing a refusal
        here would be a verdict on a call that cannot happen.
        """
        cached = self._billing_states.get(endpoint)
        if cached is not None:
            return cached
        api = self._api_for(seat)
        report = (
            {"state": "ready", "detail": "no api type to ask", "reasons": []}
            if api is None
            else dict(await llm_preflight_api(api))
        )
        self._billing_states[endpoint] = report
        return report

    async def _endpoint_state(self, endpoint: str, seat: SeatSpec) -> Dict[str, Any]:
        """Is *endpoint* servable from this machine, for *seat*'s model?

        The same free check ``kbu council doctor`` and ``submit`` run
        (:mod:`..servable`), so "unavailable" means one thing in the council
        and not three.  Cached per endpoint for the worker's life, which makes
        a recovered endpoint picked up by the NEXT ``run`` rather than
        mid-sweep -- the alternative is a ``/models`` GET per parked call.
        """
        cached = self._endpoint_states.get(endpoint)
        if cached is not None:
            return cached
        from ..servable import probe_endpoint, seat_state

        probe = await probe_endpoint(endpoint, seat.endpoint_kind, self._api_for(seat))
        state = seat_state("", seat, probe, allow_unverified=True)
        self._endpoint_states[endpoint] = state
        return state

    @asynccontextmanager
    async def _slot(self, endpoint: str) -> AsyncIterator[None]:
        """Hold the endpoint's (possibly account-shared) concurrency slot."""
        limit = self._limit_by_endpoint.get(endpoint)
        if limit is None:  # pragma: no cover - endpoints come from backends
            yield
            return
        async with limit.semaphore():
            yield

    # -- outcomes ---------------------------------------------------------

    def _settle_ok(
        self,
        call: Mapping[str, Any],
        token: Optional[str],
        result: Any,
        started: datetime,
        finished: datetime,
        parsed: Optional[Any] = None,
        repairs: Optional[Sequence[Mapping[str, Any]]] = None,
        *,
        queued_at: Optional[datetime] = None,
    ) -> str:
        """Record a good answer, its tokens, its budget and its consequence.

        *parsed* is the answer AFTER :func:`~.normalise.normalise` and
        *repairs* is what it changed.  The repaired answer is what gets
        stored, because it is the answer the council read; ``reply_text``
        keeps the model's untouched original, and *repairs* records every
        difference between the two.
        """
        usage = dict(result.usage) if result.usage is not None else None
        endpoint = str(call["endpoint"])
        if usage is not None:
            # A fresh SINGLETON observation for the batching running mean
            # (see :meth:`_per_item_output`) -- purely additive bookkeeping,
            # read by nothing when batch_size <= 1.  This is what lets a
            # batched queue bootstrap its own per-item estimate from ordinary
            # single-call traffic ("fresh singleton observations") rather
            # than being permanently stuck forcing singles for lack of one.
            # Never reached for a REUSED answer: that settles through
            # :meth:`_settle_reused`, not here.
            self._record_output_sample(
                str(call["seat_name"]),
                str(call["role"]),
                str(call.get("served_model") or ""),
                1,
                int(usage.get("completion_tokens") or 0),
            )
        try:
            with self.store.transaction():
                self.store.complete_call(
                    call["call_id"],
                    token,
                    "ok",
                    reply_text=result.text,
                    reply_parsed=parsed if parsed is not None else result.parsed,
                    fresh_usage=usage,
                    error=None,
                    normalised=list(repairs or ()) or None,
                )
                self._record_attempt(
                    call, started, finished, "ok", usage, queued_at=queued_at
                )
                spent = _token_total(usage)
                if spent:
                    self.store.add_tokens(endpoint, spent)
                self._enforce_budget(endpoint, usage)
                self.advance(call["job_id"], call["item_id"])
        except StaleClaim:
            return "stale"
        return "ok"

    def _settle_reused(
        self,
        call: Mapping[str, Any],
        token: Optional[str],
        source: Mapping[str, Any],
        *,
        query: Any,
        roster: Roster,
        role: str,
    ) -> str:
        """Settle a call from somebody else's identical, already-paid answer.

        ``normalise`` is re-applied to the reused reply, so a reused answer
        is repaired identically to a fresh one.  It is not merely copied,
        because a reuse source may predate normalisation -- a row written by
        an earlier build holds the raw reply, and inheriting it would give
        this call an answer its own schema would have refused.  Re-applying
        is safe either way: ``normalise`` is idempotent, so a source that has
        already been through it is repaired by nothing and reports nothing,
        and the source's OWN record is then carried across so that both calls
        say the same thing about the same answer.
        """
        started = _utcnow()
        parsed = source.get("reply_parsed")
        repairs: List[Mapping[str, Any]] = []
        if isinstance(parsed, Mapping):
            parsed, repairs = normalise(
                role,
                dict(parsed),
                query.answer_schema(role),
                self._context_for(role, call, query, roster),
            )
        if not repairs:
            repairs = [dict(entry) for entry in (source.get("normalised") or ())]
        try:
            with self.store.transaction():
                self.store.complete_call(
                    call["call_id"],
                    token,
                    "ok",
                    reply_text=source.get("reply_text"),
                    reply_parsed=parsed,
                    fresh_usage=dict(ZERO_USAGE),
                    error=None,
                    reused_from=source["call_id"],
                    normalised=list(repairs) or None,
                )
                self._record_attempt(
                    call, started, _utcnow(), "reused", dict(ZERO_USAGE)
                )
                self.advance(call["job_id"], call["item_id"])
        except StaleClaim:
            return "stale"
        return "reused"

    def _fail_call(
        self,
        call: Mapping[str, Any],
        token: Optional[str],
        error: CouncilError,
        started: datetime,
        finished: datetime,
        result: Any = None,
        *,
        queued_at: Optional[datetime] = None,
    ) -> str:
        """Settle a call as failed: the seat errors and the item will too."""
        try:
            with self.store.transaction():
                self.store.complete_call(
                    call["call_id"],
                    token,
                    "error",
                    reply_text=getattr(result, "text", None),
                    error=error.model_dump(),
                )
                self._record_attempt(
                    call, started, finished, error.kind, None, queued_at=queued_at
                )
                self.advance(call["job_id"], call["item_id"])
        except StaleClaim:
            return "stale"
        return error.kind

    def _retry_later(
        self,
        call: Mapping[str, Any],
        token: Optional[str],
        error: CouncilError,
        failures: int,
        started: datetime,
        finished: datetime,
        result: Any = None,
        *,
        queued_at: Optional[datetime] = None,
    ) -> str:
        """Put a transiently-failed call back with a jittered backoff."""
        delay = self._backoff(failures)
        try:
            with self.store.transaction():
                self.store.note_call_error(call["call_id"], error.model_dump())
                self._record_attempt(
                    call, started, finished, error.kind, None, queued_at=queued_at
                )
                self.store.release_call(
                    call["call_id"],
                    token,
                    _utcnow() + timedelta(seconds=delay),
                    increment_attempt=True,
                )
        except StaleClaim:
            return "stale"
        return error.kind

    def _halt_endpoint(
        self, call: Mapping[str, Any], token: Optional[str], error: CouncilError
    ) -> str:
        """Stop using this endpoint for this user, and give the call back.

        The call is NOT failed and NOT charged an attempt: nothing is wrong
        with it, the place it was going is shut.  ``reset_halt`` plus another
        ``work()`` finishes the job without re-submitting anything.
        """
        return self._park(call, token, error, wait_s=None, halt=True)

    def _park(
        self,
        call: Mapping[str, Any],
        token: Optional[str],
        error: CouncilError,
        wait_s: Optional[float],
        halt: bool = False,
    ) -> str:
        """Release a call unspent, with its reason recorded on the row.

        One function for the three "not the call's fault" outcomes -- a halted
        endpoint, a model that is not loaded yet, and a route that no longer
        matches this worker's config.  ``calls.error`` is written in the SAME
        transaction as the release, because that column is where
        ``job_status`` reads the ``waiting_for_model`` and
        ``configuration_mismatch`` blockers from and a release alone would
        leave the job looking idle for no stated reason.

        No attempt row is written: ``attempts`` is keyed
        ``(call_id, attempt_no)`` and ``attempt_no`` comes from the call's
        ``attempt_count``, which these releases deliberately do not increment
        -- so there is no number to file one under.  The reason is on the call
        itself, which is what the operator and ``job_status`` read.
        """
        not_before = None if wait_s is None else _utcnow() + timedelta(seconds=wait_s)
        try:
            with self.store.transaction():
                if halt:
                    self.store.set_halt(
                        str(call["endpoint"]), error.kind, error.message
                    )
                self.store.note_call_error(call["call_id"], error.model_dump())
                self.store.release_call(
                    call["call_id"], token, not_before, increment_attempt=False
                )
        except StaleClaim:
            return "stale"
        return error.kind

    def _enforce_budget(self, endpoint: str, usage: Optional[Mapping[str, Any]]) -> None:
        """Halt a budgeted endpoint that has spent its budget, or cannot say.

        A soft threshold, checked AFTER the call: the council cannot know what
        a call will cost before it makes it, so the budget stops the next call
        rather than this one -- and the halt message carries the overshoot so
        the operator can see by how much.
        """
        budget = self.endpoint_config(endpoint).get("max_total_tokens")
        if not budget:
            return
        budget = int(budget)
        if usage is None:
            self.store.set_halt(
                endpoint,
                "budget_usage_unknown",
                f"endpoint has a {budget}-token budget but reported no usage for a "
                "completed call; an unmeasurable budget is unenforceable",
            )
            return
        spent = int(self.store.get_endpoint_state(endpoint)["epoch_tokens"])
        if spent >= budget:
            self.store.set_halt(
                endpoint,
                "budget_exhausted",
                f"spent {spent} of a {budget}-token budget "
                f"(overshoot {spent - budget}); reset_budget to continue",
            )

    def _record_attempt(
        self,
        call: Mapping[str, Any],
        started: datetime,
        finished: datetime,
        outcome_kind: str,
        usage: Optional[Mapping[str, Any]],
        *,
        queued_at: Optional[datetime] = None,
    ) -> None:
        """Record one try, plus -- when this attempt actually waited for a
        slot -- WHEN it started waiting.

        ``queued_at`` is ``None`` for anything that never went through
        :meth:`_slot` at all (a reused or single-flight-followed answer: see
        :meth:`_settle_reused`, which never calls this with one), which is
        exactly "nothing to say" for the ``attempts.queued_at`` column.
        """
        try:
            self.store.record_attempt(
                call["call_id"],
                int(call.get("attempt_count") or 0) + 1,
                started,
                finished,
                outcome_kind,
                usage=dict(usage) if usage else None,
                worker_id=self.worker_id,
                queued_at=queued_at,
            )
        except TypeError:
            # A store built before `record_attempt` grew `queued_at` (or a
            # test double standing in for one): fall back rather than lose
            # the attempt record over an instrumentation-only column.
            self.store.record_attempt(
                call["call_id"],
                int(call.get("attempt_count") or 0) + 1,
                started,
                finished,
                outcome_kind,
                usage=dict(usage) if usage else None,
                worker_id=self.worker_id,
            )

    def _backoff(self, failures: int) -> float:
        """The nth retry's delay, with +-25% jitter.

        Jitter matters even for one worker: N proposer seats on one endpoint
        fail together when that endpoint wobbles, and an un-jittered backoff
        would send all N back at the same instant.
        """
        base = self.retry_backoff_s[min(failures, len(self.retry_backoff_s)) - 1]
        spread = base * self.retry_jitter
        return max(0.0, base + self._rng.uniform(-spread, spread))

    # -- advancing an item ------------------------------------------------

    def advance(self, job_id: str, item_id: str) -> str:
        """Ask ``decide()`` what this item needs next, and do that.

        Called after every settled call, inside that call's transaction, so
        the reply, its attempt, its tokens and its consequence land together
        or not at all.

        Returns what was done: ``waiting``, ``need_critic``, ``need_meta``,
        ``verdict``, ``cancelled`` or ``settled``.
        """
        with self.store.transaction():
            job = self.store.get_job(job_id)
            if job is None or job["status"] == "cancelled":
                # A late reply to a cancelled job is still stored (it is paid
                # for, and reusable), but it creates nothing and settles
                # nothing.
                return "cancelled"
            item = self._item_for(job_id, item_id)
            if item is None or item["status"] in SETTLED_ITEM_STATUSES:
                return "settled"

            query = self._query_for(job)
            roster = self._roster_for(job)
            task = self._task_for(item)
            calls = [
                row
                for row in self.store.get_calls(job_id, item_id)
                if row.get("live", True)
            ]

            by_seat = {
                row["seat_name"]: row for row in calls if row["role"] == "proposer"
            }
            proposer_rows = [by_seat.get(seat.seat_name) for seat in roster.proposers]
            proposers = [
                SettledCall.from_call_row(row, query)
                if row is not None and row["status"] in ("ok", "error")
                else None
                for row in proposer_rows
            ]

            critic_digest = _upstream_digest(proposer_rows)
            critic_row = _live_call(
                calls, "critic", roster.critic.seat_name, critic_digest
            )
            critic_call = (
                SettledCall.from_call_row(critic_row, query)
                if critic_row is not None and critic_row["status"] in ("ok", "error")
                else None
            )
            meta_digest = _upstream_digest(list(proposer_rows) + [critic_row])
            meta_row = _live_call(calls, "meta", roster.meta.seat_name, meta_digest)
            meta_call = (
                SettledCall.from_call_row(meta_row, query)
                if meta_row is not None and meta_row["status"] in ("ok", "error")
                else None
            )

            what, verdict = decide(
                task, roster, query, proposers, critic_call, meta_call
            )
            if what == "waiting":
                return "waiting"
            if what == "need_critic":
                if critic_row is None:
                    ballots, votes, _errors, _usage = tally(task, roster, proposers)
                    self.store.add_calls(
                        [
                            dict(
                                self._call_row(
                                    query,
                                    "critic",
                                    roster.critic,
                                    task,
                                    item,
                                    {"votes": votes, "ballots": ballots},
                                    upstream_digest=critic_digest,
                                ),
                                job_id=job_id,
                            )
                        ]
                    )
                return "need_critic"
            if what == "need_meta":
                if meta_row is None:
                    ballots, votes, _errors, _usage = tally(task, roster, proposers)
                    self.store.add_calls(
                        [
                            dict(
                                self._call_row(
                                    query,
                                    "meta",
                                    roster.meta,
                                    task,
                                    item,
                                    {
                                        "votes": votes,
                                        "ballots": ballots,
                                        "critic": critic_call.parsed
                                        if critic_call is not None
                                        else None,
                                    },
                                    upstream_digest=meta_digest,
                                ),
                                job_id=job_id,
                            )
                        ]
                    )
                return "need_meta"

            self.store.settle_item(job_id, item_id, verdict.status, verdict)
            self._finish_job_if_done(job_id)
            return "verdict"

    def _finish_job_if_done(self, job_id: str) -> None:
        """Mark a job ``done`` once its last item is settled.

        Nothing in the store contract sets this status; the worker owns it and
        sets it inside the transaction that settled the final item, so a
        reader never sees "every item decided" and "still running" at once.
        ``only_if`` keeps it off a cancelled job.
        """
        items = self.store.get_items(job_id)
        if items and all(row["status"] in SETTLED_ITEM_STATUSES for row in items):
            self.store.set_job_status(
                job_id, "done", only_if=("queued", "running", "paused")
            )

    def _context_for(
        self, role: str, call: Mapping[str, Any], query: Any, roster: Roster
    ) -> Dict[str, Any]:
        """The council state a critic or meta prompt is rendered against.

        Rebuilt from the item's SETTLED calls rather than stored with the
        call: the prompt is not persisted anywhere, and rebuilding it from the
        same ``tally()`` the verdict uses is what makes the vote table a model
        was shown the vote table that was counted.
        """
        if role == "proposer":
            return {}
        calls = [
            row
            for row in self.store.get_calls(call["job_id"], call["item_id"])
            if row.get("live", True)
        ]
        item = self._item_for(call["job_id"], call["item_id"])
        task = self._task_for(item)
        by_seat = {row["seat_name"]: row for row in calls if row["role"] == "proposer"}
        proposers: List[Optional[SettledCall]] = []
        for seat in roster.proposers:
            row = by_seat.get(seat.seat_name)
            proposers.append(
                SettledCall.from_call_row(row, query)
                if row is not None and row["status"] in ("ok", "error")
                else None
            )
        ballots, votes, _errors, _usage = tally(task, roster, proposers)
        context: Dict[str, Any] = {"votes": votes, "ballots": ballots}
        if role == "meta":
            critic_row = _live_call(
                calls, "critic", roster.critic.seat_name, "", match_digest=False
            )
            context["critic"] = (
                SettledCall.from_call_row(critic_row, query).parsed
                if critic_row is not None and critic_row["status"] == "ok"
                else None
            )
        return context

    # -- lookups ----------------------------------------------------------

    def _query_for(self, job: Mapping[str, Any]) -> Any:
        """The query type a job was submitted against.

        Cached per ``(name, version)``, and otherwise read back out of the
        store through the registry -- which re-validates it, so a daemon on a
        machine that does not ship the renderer a shared job names refuses the
        job instead of rendering a different prompt.
        """
        key = (str(job["query_name"]), str(job["query_version"]))
        cached = self._queries.get(key)
        if cached is not None:
            return cached
        from ..queries import QueryRegistry

        query = QueryRegistry(self.store).get(key[0], key[1])
        self._queries[key] = query
        return query

    def _roster_for(self, job: Mapping[str, Any]) -> Roster:
        """The seating a job was submitted with, rebuilt from the job row."""
        cached = self._rosters.get(job["job_id"])
        if cached is not None:
            return cached
        raw = dict(job.get("roster") or {})
        roster = Roster(
            proposers=[SeatSpec(**seat) for seat in raw.get("proposers") or []],
            critic=SeatSpec(**raw["critic"]),
            meta=SeatSpec(**raw["meta"]),
            allow_meta_overlap=bool(raw.get("allow_meta_overlap")),
        )
        self._rosters[job["job_id"]] = roster
        return roster

    @staticmethod
    def _seat_for(roster: Roster, role: str, seat_name: str) -> SeatSpec:
        for seat_role, seat in roster.seats():
            if seat_role == role and seat.seat_name == seat_name:
                return seat
        raise ValueError(f"job roster has no {role} seat named {seat_name!r}")

    def _item_for(self, job_id: str, item_id: str) -> Optional[Dict[str, Any]]:
        for row in self.store.get_items(job_id):
            if row["item_id"] == item_id:
                return row
        return None

    @staticmethod
    def _task_for(item: Mapping[str, Any]) -> CouncilTask:
        return CouncilTask(
            item_id=str(item["item_id"]),
            evidence=dict(item.get("evidence") or {}),
            menu=[MenuEntry(**entry) for entry in item.get("menu") or []],
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _upstream_digest(rows: Sequence[Optional[Mapping[str, Any]]]) -> str:
    """Identity of the answers a critic or meta call was created FROM.

    Stored on the downstream call so that a reply which arrives after its
    upstream changed -- a proposer reclaimed and re-answered, say -- can be
    told apart from one that is still current.  ``decide`` treats a
    non-matching call as missing, so the stale answer is ignored rather than
    counted against a vote table it never saw.
    """
    payload = [
        [
            str(row["call_id"]),
            row.get("reply_parsed"),
        ]
        for row in rows
        if row is not None
    ]
    return canonical_sha256(payload)


def _live_call(
    calls: Sequence[Mapping[str, Any]],
    role: str,
    seat_name: str,
    digest: str,
    match_digest: bool = True,
) -> Optional[Dict[str, Any]]:
    """The live call of *role*/*seat_name*, optionally matching *digest*."""
    for row in calls:
        if row["role"] != role or row["seat_name"] != seat_name:
            continue
        if match_digest and str(row.get("upstream_digest") or "") != digest:
            continue
        return dict(row)
    return None


def _token_total(usage: Optional[Mapping[str, Any]]) -> int:
    if not usage:
        return 0
    return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)


def _bisect_rows(
    rows: Sequence[Mapping[str, Any]]
) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """Split *rows* into two near-equal halves for bisection-on-failure.

    Ceiling split (``20 -> 10/10``, ``5 -> 3/2``, ``3 -> 2/1``, ``2 ->
    1/1``), which is the path the design's own worked example
    (``20->10->5->3->2->1``) walks.
    """
    mid = (len(rows) + 1) // 2
    return list(rows[:mid]), list(rows[mid:])


def _validate_answer(parsed: Any, schema: Mapping[str, Any]) -> Optional[str]:
    """Return a complaint about *parsed*, or ``None`` if it satisfies *schema*.

    Uses ``jsonschema`` when it is importable and falls back to a required-key
    and type check otherwise.  The fallback is deliberately weak and
    deliberately present: ``jsonschema`` is not a dependency of this package,
    and the alternative to a weak check is no check -- which would let a reply
    missing ``picked`` be counted as a vote for nothing.
    """
    if not isinstance(parsed, Mapping):
        return (
            f"the backend returned {type(parsed).__name__}, not a JSON object; "
            "there is no answer to count"
        )
    if not schema:
        return None
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is usually installed
        jsonschema = None  # type: ignore[assignment]
    if jsonschema is not None:
        try:
            jsonschema.validate(instance=dict(parsed), schema=dict(schema))
        except jsonschema.ValidationError as exc:
            path = "/".join(str(part) for part in exc.absolute_path)
            where = f" at {path}" if path else ""
            return f"the reply does not satisfy the answer schema{where}: {exc.message}"
        except jsonschema.SchemaError as exc:  # pragma: no cover - a bad spec
            return f"the query type's answer schema is not valid JSON Schema: {exc.message}"
        return None
    for key in schema.get("required") or ():
        if key not in parsed:
            return f"the reply is missing the required key {key!r}"
    properties = schema.get("properties") or {}
    for key, declared in properties.items():
        if key not in parsed:
            continue
        expected = declared.get("type") if isinstance(declared, Mapping) else None
        if expected == "array" and not isinstance(parsed[key], (list, tuple)):
            return f"{key!r} is {type(parsed[key]).__name__}, expected an array"
        if expected == "boolean" and not isinstance(parsed[key], bool):
            return f"{key!r} is {type(parsed[key]).__name__}, expected a boolean"
        if expected == "string" and not isinstance(parsed[key], str):
            return f"{key!r} is {type(parsed[key]).__name__}, expected a string"
    return None


def _run_coroutine(coro: Any) -> Any:
    """Run *coro* on a loop this call owns, refusing to nest.

    Deliberately not ``asyncio.run``: that sets the thread's current event
    loop to ``None`` on the way out, which breaks other code in this
    repository that still calls ``asyncio.get_event_loop()``.  Owning a loop
    locally leaves the process-global asyncio state exactly as it was found.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError(
            "this is the synchronous form and there is already an event loop "
            "running; await the async form (asubmit / aevaluate / work) instead"
        )
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# One-shot convenience
# ---------------------------------------------------------------------------


async def aevaluate(
    task: CouncilTask,
    query: Any,
    roster: Roster,
    store: Any,
    backends: Any,
    *,
    user: str = "local",
    config: Any = None,
    worker_id: Optional[str] = None,
    allow_unverified: bool = False,
    label: str = "evaluate",
    **worker_kwargs: Any,
) -> CouncilVerdict:
    """Decide ONE item in the foreground, and return its verdict.

    Exactly a one-item job worked ``until_idle``: the same queue, the same
    reuse, the same budgets.  It is a convenience, not a second code path --
    which is what makes "a single evaluation" and "a 20k-item batch" behave
    identically.

    Raises:
        RuntimeError: the item did not reach a verdict (every remaining call
            is blocked); the message carries ``job_status``'s blockers.
    """
    worker = CouncilWorker(
        store,
        config,
        backends,
        user,
        worker_id,
        allow_unverified=allow_unverified,
        **worker_kwargs,
    )
    job_id = await worker.asubmit(query, [task], roster, label=label)
    summary = await worker.work(job_id=job_id, until_idle=True)
    rows = store.results(job_id, include_pending=True)
    for row in rows:
        if row["item_id"] == task.item_id and row.get("verdict"):
            return CouncilVerdict.model_validate(row["verdict"])
    raise RuntimeError(
        f"item {task.item_id!r} of job {job_id} reached no verdict; "
        f"blockers: {summary['blockers']}"
    )


def evaluate(
    task: CouncilTask,
    query: Any,
    roster: Roster,
    store: Any,
    backends: Any,
    **kwargs: Any,
) -> CouncilVerdict:
    """Decide one item in the foreground.  The synchronous :func:`aevaluate`."""
    return _run_coroutine(aevaluate(task, query, roster, store, backends, **kwargs))
