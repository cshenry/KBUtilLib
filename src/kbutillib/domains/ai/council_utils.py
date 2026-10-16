"""``CouncilUtils`` -- the council's one supported entry point from KBUtilLib.

Everything the council can do is reachable from here: load the configuration,
open the store, seat a roster, submit a job, work its queue in the foreground,
read its results, and report whether the seats are even reachable.  The
package underneath (:mod:`kbutillib.domains.ai.council`) is where the protocol
lives; this module is the facade the toolkit property, the ``kbu council`` CLI
and a notebook all go through, so there is exactly one place that knows how a
configuration becomes a roster and how a roster becomes backends.

Three properties of this module are load-bearing:

**It does NOT inherit ``ArgoUtils``.**  ``ArgoUtils.__init__`` builds an
``httpx`` client and may ping the gateway, which would make
``KBUtilLib().council`` cost a network round trip (and, on a bad day, a
token) just to read a config file.  The council reaches ARGO the same way it
reaches every other endpoint -- through ``llm.get_api("argo", <endpoint
mapping>)``, the same wrapper a plug-in kind arrives by -- so there is nothing
to inherit.

**Every asynchronous operation has both forms.**  ``submit``/``submit_async``,
``run``/``run_async``, ``evaluate``/``evaluate_async``, ``doctor``/
``doctor_async`` and ``models``/``models_async``.  The sync form is the one a
script or the CLI calls; the async form is what a notebook with a live event
loop should await.  The sync form still works in a notebook: see
:func:`_run_sync`, which hands the coroutine to a worker thread when the
calling thread already has a running loop.  Methods that touch only the store
or the config (``store``, ``user``, ``jobs``, ``results``, ...) are sync only
-- they never await anything, and an ``_async`` twin that wraps a blocking
SQLite read in a coroutine would be a lie about where the thread goes.

**Nothing here creates a database schema implicitly.**  :meth:`store` opens
the store and stops; :meth:`db_init` is the only thing that creates tables,
and :meth:`require_schema` is what every other verb calls first so that a
fresh machine is told to run ``kbu council db init`` rather than silently
accumulating half a council in a file nobody meant to create.
"""

from __future__ import annotations

import asyncio
import getpass
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from ...core.shared_env_utils import SharedEnvUtils
from . import llm

__all__ = ["CouncilUtils", "CouncilUtilsImpl"]

#: What :meth:`CouncilUtils.doctor` may report for one seat.  ``unknown`` is
#: deliberately distinct from ``unavailable``: "I could not tell" must never
#: be rendered as "I checked and it is broken", and the two drive different
#: exit codes out of ``kbu council doctor``.
SEAT_STATES = ("ready", "unknown", "unavailable")

#: The message every verb that needs tables fails with.  Spelled once because
#: it is a user-facing instruction the CLI test pins.
NO_SCHEMA_HINT = (
    "this council store has no schema yet; run `kbu council db init` to create "
    "it (nothing creates it implicitly, so a typo in a store path cannot "
    "quietly become a second, empty council)"
)


# ---------------------------------------------------------------------------
# Running a coroutine from synchronous code
# ---------------------------------------------------------------------------


def _run_sync(coro: Any) -> Any:
    """Run *coro* to completion from synchronous code, loop or no loop.

    Two cases, and the second is the one that matters:

    * **No running loop** -- a loop is created, used and closed here.
      Deliberately NOT ``asyncio.run``, which sets the thread's current event
      loop to ``None`` on the way out; other modules in this repository still
      call ``asyncio.get_event_loop()``, and ``CouncilWorker``'s own sync
      wrapper avoids it for exactly that reason.  Owning a loop locally
      leaves the process-global asyncio state as it was found.
    * **A loop is already running** (a Jupyter cell, an async web handler) --
      the coroutine goes to a worker thread with a loop of its own and this
      thread blocks on the join.  Nesting a loop inside a running loop is
      impossible, and refusing would make ``kbu.council.submit(...)`` unusable
      from the notebook that is the main reason the facade exists.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    box: Dict[str, Any] = {}

    def _target() -> None:
        inner = asyncio.new_event_loop()
        try:
            box["value"] = inner.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on this thread
            box["error"] = exc
        finally:
            inner.close()

    thread = threading.Thread(target=_target, name="kbu-council-sync", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _split_query_ref(ref: str) -> Tuple[str, Optional[str]]:
    """Split ``"go_mf@1"`` into ``("go_mf", "1")`` and ``"go_mf"`` into
    ``("go_mf", None)`` -- i.e. "the latest active version"."""
    text = str(ref or "").strip()
    if "@" in text:
        name, version = text.split("@", 1)
        return name.strip(), version.strip() or None
    return text, None


def _safe_store_url(url: str) -> str:
    """*url* with any password removed, for printing and for the store.

    A SQLite URL is a path and is returned unchanged; a PostgreSQL URL can
    carry ``user:password@host``, and ``kbu council doctor`` prints the store
    it opened.
    """
    text = str(url or "")
    if "@" not in text or "//" not in text:
        return text
    scheme, _, rest = text.partition("//")
    userinfo, _, hostpart = rest.partition("@")
    if ":" in userinfo:
        user, _, _password = userinfo.partition(":")
        userinfo = f"{user}:***"
    return f"{scheme}//{userinfo}@{hostpart}"


# ---------------------------------------------------------------------------
# CouncilUtils
# ---------------------------------------------------------------------------


class CouncilUtils(SharedEnvUtils):
    """The council, as one object.

    Args:
        council_config: What :func:`~.council.config.load_council_config`
            should read -- a path, an already-parsed mapping, or ``None`` for
            the packaged default overlaid with the user's ``ai_council``
            config section.
        store_url: Override the store URL the configuration names.  This is
            how ``kbu council --store ...`` and the test suite point at a
            throwaway SQLite file without writing a config file.
        allow_unverified: Permit seats whose model nobody has verified, and
            let a CLI endpoint whose billing preflight answers ``unknown``
            through.  Off by default.
        **kwargs: Passed to :class:`~kbutillib.core.shared_env_utils.SharedEnvUtils`.
    """

    def __init__(
        self,
        council_config: Optional[Union[str, Path, Mapping[str, Any]]] = None,
        store_url: Optional[str] = None,
        allow_unverified: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.council_config_source = council_config
        self.store_url_override = str(store_url) if store_url else None
        self.allow_unverified = bool(allow_unverified)
        self._config: Any = None
        self._store: Any = None
        self._user: Optional[str] = None

    # -- configuration ----------------------------------------------------

    def config(self, reload: bool = False) -> Any:
        """The validated :class:`~.council.config.CouncilConfig`.

        Cached: a configuration is read once per object, because a roster
        resolved from one config and a store opened from another is the kind
        of inconsistency that only shows up in the results.
        """
        if self._config is None or reload:
            from .council.config import load_council_config

            self._config = load_council_config(self.council_config_source)
        return self._config

    def config_snapshot(self) -> Dict[str, Any]:
        """The configuration with every credential removed."""
        return self.config().snapshot()

    def store_url(self) -> str:
        """The store URL this object will open (the override, or the config's)."""
        return self.store_url_override or self.config().store

    # -- store ------------------------------------------------------------

    def store(self, **kwargs: Any) -> Any:
        """The council store, opened once and cached.

        Does NOT create the schema.  ``kwargs`` go to the implementation --
        ``check_version=False`` in particular, which is how ``db migrate``
        opens a database whose version this code would otherwise refuse.
        """
        if self._store is None or kwargs:
            from .council.store import open_store

            store = open_store(self.store_url(), **kwargs)
            if kwargs:
                # A one-off open (migrate) is NOT cached: its options are not
                # the ones every other verb wants.
                return store
            self._store = store
        return self._store

    def close(self) -> None:
        """Release the store's connection pool, if one was opened."""
        store = self._store
        self._store = None
        closer = getattr(store, "close", None)
        if callable(closer):
            closer()

    def schema_version(self) -> int:
        """The store's schema version, or ``0`` when it has no schema."""
        return int(self.store().schema_version())

    def require_schema(self) -> Any:
        """Return the store, refusing one that has no schema.

        Every verb except ``db *`` and ``doctor`` calls this first.  The
        refusal names the command that fixes it, because the usual cause is a
        fresh machine or a mistyped ``--store``.

        Raises:
            SchemaVersionMismatch: the store carries no council schema.
        """
        from .council.store.errors import SchemaVersionMismatch

        store = self.store()
        if int(store.schema_version()) == 0:
            raise SchemaVersionMismatch(f"{_safe_store_url(self.store_url())}: {NO_SCHEMA_HINT}")
        return store

    def db_init(self) -> Dict[str, Any]:
        """Create the schema and seed the builtin query types.  Idempotent."""
        from .council.queries import QueryRegistry

        store = self.store()
        store.init_schema()
        seeded = QueryRegistry(store).seed_builtins()
        return {
            "store": _safe_store_url(self.store_url()),
            "schema_version": int(store.schema_version()),
            "query_types": [f"{spec.name}@{spec.version}" for spec in seeded],
        }

    def db_migrate(self) -> Dict[str, Any]:
        """Bring the store up to the current schema version.

        Opened with ``check_version=False``, because the databases a migration
        exists for are precisely the ones a version check refuses to open.
        """
        store = self.store(check_version=False)
        try:
            before = int(store.schema_version())
            # An ALREADY-STAMPED store may still be missing a column added to
            # the same schema version later (see schema.ADDITIVE_COLUMNS).
            # Asked for here, BEFORE migrate() does it silently, so the report
            # can name what it added rather than saying "migrated: false"
            # about a file it just altered.
            adder = getattr(store, "add_missing_columns", None)
            added = list(adder()) if before and callable(adder) else []
            version = int(store.migrate())
        finally:
            closer = getattr(store, "close", None)
            if callable(closer) and store is not self._store:
                closer()
        return {
            "store": _safe_store_url(self.store_url()),
            "schema_version_before": before,
            "schema_version": version,
            "columns_added": added,
            "migrated": before != version or bool(added),
        }

    def db_check(self) -> Dict[str, Any]:
        """Report the store's reachability and schema version.

        Reports rather than raises: an unreachable store or an unstamped file
        is the ANSWER this verb exists to give, so it comes back in the
        payload with ``ok`` false instead of as a traceback.
        """
        from .council.store.schema import SCHEMA_VERSION

        data: Dict[str, Any] = {
            "store": _safe_store_url(self.store_url()),
            "reachable": False,
            "schema_version": 0,
            "expected_schema_version": int(SCHEMA_VERSION),
            "ok": False,
            "error": None,
        }
        try:
            version = int(self.store().schema_version())
        except Exception as exc:  # noqa: BLE001 - `db check` reports, never raises
            data["error"] = f"{type(exc).__name__}: {exc}"
            return data
        data["reachable"] = True
        data["schema_version"] = version
        data["ok"] = version == int(SCHEMA_VERSION)
        if version == 0:
            data["error"] = NO_SCHEMA_HINT
        return data

    # -- identity ---------------------------------------------------------

    def user(self) -> str:
        """Who is submitting: the KBase username, else the local account.

        M1 identity rule.  A KBase token is only consulted when one is
        actually configured, so a machine with no token never makes a network
        call to find out its own name; an unresolvable token falls back the
        same way a missing one does, because a council job must not fail over
        the spelling of its owner.  A later milestone replaces this with
        ``council.identity``.
        """
        if self._user:
            return self._user
        name = ""
        try:
            token = self.get_token("kbase")
        except Exception:  # noqa: BLE001 - identity must never be fatal
            token = None
        if token:
            try:
                from ...installed_clients.authclient import KBaseAuth

                name = str(KBaseAuth().get_user(token) or "").strip()
            except Exception as exc:  # noqa: BLE001 - fall back, but say so
                self.log_debug(f"council: could not resolve the KBase token to a user: {exc}")
                name = ""
        if not name:
            try:
                name = getpass.getuser()
            except Exception:  # noqa: BLE001 - no controlling terminal, no pwd entry
                name = "unknown"
        self._user = name
        return name

    # -- seating and backends ---------------------------------------------

    def roster(self, profile: Optional[str] = None, **seat_overrides: Any) -> Any:
        """Resolve a profile (plus seat overrides) into a validated roster.

        Overrides are keyed by SEAT NAME -- ``critic="opus-cli"``,
        ``proposer_2="gpt55"`` -- with ``proposers=[...]`` replacing the bench.
        """
        return self.config().roster(profile, overrides=seat_overrides or None)

    def build_backends(self, roster: Any, allow_unverified: Optional[bool] = None) -> Dict[str, Any]:
        """One api type instance per DISTINCT endpoint the roster seats.

        Keyed by endpoint alias, which is what ``CouncilWorker`` claims calls
        for: a worker serves exactly the endpoints it was handed something
        for, and no others.

        There is no dispatch from ``kind`` to a provider class here any more
        (``_backend_for``, M1): :func:`llm.get_api` owns it, which is what
        makes a plug-in kind reachable through this facade without a line
        changing in the council.  Built once per verb rather than per call
        because an ``httpx.AsyncClient`` is worth reusing across a 20k-item
        batch.
        """
        unverified = self.allow_unverified if allow_unverified is None else bool(allow_unverified)
        config = self.config()
        backends: Dict[str, Any] = {}
        for _role, seat in roster.seats():
            if seat.endpoint in backends:
                continue
            mapping = self._endpoint_mapping(seat.endpoint, config, unverified)
            backends[seat.endpoint] = self._get_api(seat.endpoint, mapping)
        return backends

    def _endpoint_mapping(
        self, endpoint: str, config: Any, unverified: bool
    ) -> Dict[str, Any]:
        """The endpoint mapping :func:`llm.get_api` is given for *endpoint*.

        The endpoint's own configuration, plus ``name`` (part of the wrapper's
        client cache key, so two aliases are two clients) and
        ``allow_unverified`` -- which the CLI api types read, and which belongs
        in the key: "spend against an endpoint whose billing mode could not be
        confirmed" is a different client from one that would refuse.
        """
        cfg = dict(config.endpoint(endpoint))
        cfg.setdefault("name", endpoint)
        cfg["allow_unverified"] = bool(unverified)
        return cfg

    def _get_api(self, endpoint: str, mapping: Mapping[str, Any]) -> Any:
        """Resolve one endpoint through the wrapper, reporting a bad kind well.

        ``llm.get_api`` raises ``KeyError`` for an unregistered kind, which is
        correct for a programming error but reads badly for what this almost
        always is -- a ``kind:`` typo in a YAML file.  ``ConfigError`` is what
        the CLI already knows how to print.
        """
        from .council.config import ConfigError

        kind = str(dict(mapping).get("kind") or "")
        try:
            return llm.get_api(kind, mapping)
        except KeyError as exc:
            raise ConfigError(
                f"endpoint {endpoint!r} is kind {kind!r}, which this installation "
                f"has no api type for; registered kinds are {llm.api_types()}"
            ) from exc

    async def _aclose_backends(self, backends: Mapping[str, Any]) -> None:
        """Release every api type's client/process resources.

        Closes what it was handed -- which is what a test that monkeypatched
        :meth:`build_backends` hands over, and which is not in the wrapper's
        cache -- and then clears the wrapper's cache, so a client this verb
        closed can never be handed to the next one.  A double close on an
        instance that is in both sets is harmless and swallowed; leaving a
        closed client cached would not be.
        """
        for backend in backends.values():
            closer = getattr(backend, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - a close never fails a verb
                self.log_debug(f"council: api type close failed: {exc}")
        await llm.aclose_all()

    def _worker(
        self,
        store: Any,
        backends: Mapping[str, Any],
        allow_unverified: bool,
        **kwargs: Any,
    ) -> Any:
        from .council.engine.worker import CouncilWorker

        return CouncilWorker(
            store,
            self.config(),
            backends,
            self.user(),
            allow_unverified=allow_unverified,
            **kwargs,
        )

    # -- query types ------------------------------------------------------

    def registry(self) -> Any:
        """A :class:`~.council.queries.QueryRegistry` over this store."""
        from .council.queries import QueryRegistry

        return QueryRegistry(self.require_schema())

    def queries(self, include_retired: bool = False) -> List[Dict[str, Any]]:
        """The saved query-type rows, active ones only by default."""
        return self.registry().list(include_retired=include_retired)

    def query(self, ref: str) -> Any:
        """One saved query type, by ``name`` or ``name@version``."""
        name, version = _split_query_ref(ref)
        return self.registry().get(name, version)

    def register_query(self, path: Union[str, Path, Mapping[str, Any]]) -> Any:
        """Validate a query-type spec and save it.  Immutable once saved."""
        return self.registry().register(path, created_by=self.user())

    def retire_query(self, name: str, version: str) -> bool:
        """Hide a query type from lookups without removing it."""
        return self.registry().retire(name, version)

    def _resolve_query(self, query: Any) -> Any:
        """A query type from a spec object or a ``name[@version]`` string."""
        if hasattr(query, "render") and hasattr(query, "name"):
            return query
        return self.query(str(query))

    @staticmethod
    def _as_tasks(items: Any) -> List[Any]:
        """Coerce dicts / ``CouncilTask``s / one of either into a task list."""
        from .council.types import CouncilTask

        if isinstance(items, (CouncilTask, Mapping)):
            items = [items]
        tasks: List[Any] = []
        for item in items or ():
            tasks.append(item if isinstance(item, CouncilTask) else CouncilTask.model_validate(item))
        return tasks

    # -- submit / run / evaluate ------------------------------------------

    async def submit_async(
        self,
        query: Any,
        items: Any,
        label: Optional[str] = None,
        priority: int = 100,
        profile: Optional[str] = None,
        allow_unverified: bool = False,
        **seat_overrides: Any,
    ) -> str:
        """Create a job, its items and its first round of proposer calls.

        Returns:
            The job id.  Nothing has been spent yet: the calls are pending
            rows, and ``run``/the daemon is what works them.
        """
        unverified = self.allow_unverified or bool(allow_unverified)
        store = self.require_schema()
        spec = self._resolve_query(query)
        roster = self.roster(profile, **seat_overrides)
        backends = self.build_backends(roster, allow_unverified=unverified)
        try:
            worker = self._worker(store, backends, unverified)
            return await worker.asubmit(
                spec,
                self._as_tasks(items),
                roster,
                label=label,
                priority=priority,
                profile_name=profile or self.config().default_profile,
            )
        finally:
            await self._aclose_backends(backends)

    def submit(
        self,
        query: Any,
        items: Any,
        label: Optional[str] = None,
        priority: int = 100,
        profile: Optional[str] = None,
        allow_unverified: bool = False,
        **seat_overrides: Any,
    ) -> str:
        """Submit a job.  The synchronous :meth:`submit_async`."""
        return _run_sync(
            self.submit_async(
                query,
                items,
                label=label,
                priority=priority,
                profile=profile,
                allow_unverified=allow_unverified,
                **seat_overrides,
            )
        )

    async def run_async(self, job_id: str, **worker_kwargs: Any) -> Dict[str, Any]:
        """Work one job's calls in the foreground until the job is idle.

        The roster comes from the JOB, not from the current configuration: a
        job is worked with the seating it was submitted under, or its verdicts
        would be attributed to a council that never voted.

        Returns:
            ``job_status(job_id)`` with the worker's own summary added under
            ``"worker"``.  A job that ended with a non-empty ``blockers`` list
            did not finish, which is what ``kbu council run`` exits 1 on.
        """
        from .council.types import Roster, SeatSpec

        store = self.require_schema()
        job = store.get_job(job_id)
        if job is None:
            raise ValueError(f"no such job: {job_id}")
        raw = dict(job.get("roster") or {})
        if not raw:
            raise ValueError(f"job {job_id} stored no roster; it cannot be worked")
        roster = Roster(
            proposers=[SeatSpec(**seat) for seat in raw.get("proposers") or []],
            critic=SeatSpec(**raw["critic"]),
            meta=SeatSpec(**raw["meta"]),
            allow_meta_overlap=bool(raw.get("allow_meta_overlap")),
        )
        unverified = self.allow_unverified or any(
            not seat.verified for _role, seat in roster.seats()
        )
        backends = self.build_backends(roster, allow_unverified=unverified)
        try:
            worker = self._worker(store, backends, unverified, **worker_kwargs)
            summary = await worker.work(job_id=job_id, until_idle=True)
        finally:
            await self._aclose_backends(backends)
        status = dict(store.job_status(job_id))
        status["worker"] = summary
        return status

    def run(self, job_id: str, **worker_kwargs: Any) -> Dict[str, Any]:
        """Work one job to idle.  The synchronous :meth:`run_async`."""
        return _run_sync(self.run_async(job_id, **worker_kwargs))

    async def evaluate_async(
        self,
        item: Any,
        query: Any = "go_mf",
        profile: Optional[str] = None,
        allow_unverified: bool = False,
        **seat_overrides: Any,
    ) -> Any:
        """Decide ONE item in the foreground and return its verdict.

        Exactly a one-item job worked to idle -- the same queue, the same
        reuse, the same budgets -- so "one evaluation" and "a 20k-item batch"
        cannot behave differently.
        """
        from .council.engine.worker import aevaluate

        unverified = self.allow_unverified or bool(allow_unverified)
        store = self.require_schema()
        spec = self._resolve_query(query)
        tasks = self._as_tasks(item)
        if len(tasks) != 1:
            raise ValueError(f"evaluate decides ONE item; got {len(tasks)}")
        roster = self.roster(profile, **seat_overrides)
        backends = self.build_backends(roster, allow_unverified=unverified)
        try:
            return await aevaluate(
                tasks[0],
                spec,
                roster,
                store,
                backends,
                user=self.user(),
                config=self.config(),
                allow_unverified=unverified,
            )
        finally:
            await self._aclose_backends(backends)

    def evaluate(
        self,
        item: Any,
        query: Any = "go_mf",
        profile: Optional[str] = None,
        allow_unverified: bool = False,
        **seat_overrides: Any,
    ) -> Any:
        """Decide one item.  The synchronous :meth:`evaluate_async`."""
        return _run_sync(
            self.evaluate_async(
                item,
                query=query,
                profile=profile,
                allow_unverified=allow_unverified,
                **seat_overrides,
            )
        )

    # -- job control ------------------------------------------------------

    def job_status(self, job_id: str) -> Dict[str, Any]:
        """Progress, outstanding work by role and endpoint, and blockers."""
        return self.require_schema().job_status(job_id)

    def results(
        self,
        job_id: str,
        include_pending: bool = False,
        as_dataframe: bool = False,
    ) -> Any:
        """A job's decided items with their verdicts, in submission order."""
        rows = self.require_schema().results(job_id, include_pending=include_pending)
        if not as_dataframe:
            return rows
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ImportError(
                "as_dataframe=True needs pandas: pip install pandas"
            ) from exc
        return pd.DataFrame(rows)

    def cancel(self, job_id: str) -> Dict[str, Any]:
        """Cancel a job.  Terminal, idempotent, and it supersedes its calls."""
        store = self.require_schema()
        store.cancel_job(job_id, self.user())
        return store.job_status(job_id)

    def delete(self, job_id: str) -> Dict[str, Any]:
        """Tombstone a cancelled or done job.  No row is physically removed."""
        store = self.require_schema()
        store.delete_job(job_id, self.user())
        job = store.get_job(job_id) or {}
        return {
            "job_id": job_id,
            "status": job.get("status"),
            "deleted_at": str(job.get("deleted_at") or ""),
        }

    def rerun(
        self,
        job_id: str,
        profile: Optional[str] = None,
        query_version: Optional[str] = None,
        label: Optional[str] = None,
        allow_unverified: bool = False,
        **seat_overrides: Any,
    ) -> str:
        """Submit the SAME items again, optionally re-seated or re-versioned.

        The items are rebuilt from the stored item rows (their evidence and
        their closed menu), so a rerun asks the identical questions -- which
        is what makes "the same batch under a different council" a comparison
        rather than two unrelated jobs.

        The parent's id is recorded in the new job's ``options`` under
        ``rerun_of``.  ``jobs.parent_job_id`` stays null: ``CouncilWorker``
        builds the job row itself and the store contract offers no way to set
        that column from here.
        """
        store = self.require_schema()
        job = store.get_job(job_id)
        if job is None:
            raise ValueError(f"no such job: {job_id}")
        items = store.get_items(job_id)
        if not items:
            raise ValueError(f"job {job_id} has no items to rerun")
        query_ref = str(job["query_name"])
        version = query_version or str(job["query_version"])
        spec = self._resolve_query(f"{query_ref}@{version}")
        tasks = [
            {
                "item_id": row["item_id"],
                "evidence": dict(row.get("evidence") or {}),
                "menu": list(row.get("menu") or []),
            }
            for row in items
        ]
        unverified = self.allow_unverified or bool(allow_unverified)
        roster = self.roster(profile or job.get("profile_name"), **seat_overrides)
        backends = self.build_backends(roster, allow_unverified=unverified)

        async def _submit() -> str:
            try:
                worker = self._worker(store, backends, unverified)
                return await worker.asubmit(
                    spec,
                    self._as_tasks(tasks),
                    roster,
                    label=label or f"rerun of {job_id}",
                    priority=int(job.get("priority") or 100),
                    profile_name=profile or job.get("profile_name"),
                    options={"rerun_of": job_id},
                )
            finally:
                await self._aclose_backends(backends)

        return _run_sync(_submit())

    def jobs(
        self,
        all_users: bool = False,
        status: Optional[str] = None,
        include_deleted: bool = False,
    ) -> List[Dict[str, Any]]:
        """Job rows -- this user's by default, tombstones hidden by default."""
        return self.require_schema().list_jobs(
            user=None if all_users else self.user(),
            status=status,
            include_deleted=include_deleted,
        )

    # -- calls and endpoints ----------------------------------------------

    def search_calls(self, **filters: Any) -> List[Dict[str, Any]]:
        """Find call rows across every job and user.

        Filters: ``query_name``, ``canonical_model``, ``entity_hash``,
        ``data_hash``, ``status``, ``limit``.  ``entity`` is a convenience --
        it is hashed here, so a caller searching for an annotation string does
        not have to know how the hash is formed.
        """
        entity = filters.pop("entity", None)
        if entity:
            from .council.types import entity_hash

            filters["entity_hash"] = entity_hash(str(entity))
        return self.require_schema().search_calls(**filters)

    def endpoint_status(self, user: Optional[str] = None) -> List[Dict[str, Any]]:
        """Per-endpoint budget and halt state for one user.

        Every CONFIGURED endpoint is reported, halted or not: "nothing is
        halted" is the answer an operator is usually looking for, and an
        endpoint with no state row has simply never been spent against.
        """
        store = self.require_schema()
        who = user or self.user()
        rows: List[Dict[str, Any]] = []
        for name, endpoint in sorted(self.config().endpoints.items()):
            state = dict(store.get_endpoint_state(who, name))
            state.setdefault("user", who)
            state.setdefault("endpoint", name)
            state["kind"] = endpoint.kind
            state["halted"] = bool(state.get("halt_kind"))
            rows.append(state)
        return rows

    def reset_halt(self, endpoint: str, user: Optional[str] = None) -> Dict[str, Any]:
        """Clear an endpoint's halt.  Never touches the token counters."""
        return self.require_schema().reset_halt(user or self.user(), endpoint)

    def reset_budget(self, endpoint: str, user: Optional[str] = None) -> Dict[str, Any]:
        """Start a new budget epoch, keeping the lifetime total."""
        return self.require_schema().reset_budget(user or self.user(), endpoint)

    # -- doctor -----------------------------------------------------------

    async def _probe_endpoint(self, endpoint: str, kind: str, backend: Any) -> Dict[str, Any]:
        """Everything that can be learned about ONE endpoint for free.

        Probed once per endpoint rather than once per seat: two seats on the
        same endpoint would otherwise mean two ``/models`` GETs and two
        ``--version`` subprocesses, which is wasted work and -- for a CLI --
        two child processes a reader of the doctor's output cannot explain.

        Nothing here can spend a token: ``models()`` is a free GET,
        ``version()`` runs ``--version``, and ``preflight()`` reads config
        files.  ``complete()`` is never called.
        """
        from .council.types import CLI_KINDS

        probe: Dict[str, Any] = {"endpoint": endpoint, "kind": kind, "backend": backend}
        if kind in CLI_KINDS:
            # ``version()`` is CLI-specific and deliberately not part of the
            # ApiType contract -- an HTTP gateway has no ``--version``.
            probe["version"] = await backend.version()
            probe["billing"] = (
                await llm.preflight_api(backend) if probe["version"] else {}
            )
            return probe
        listing = await llm.models_api(backend)
        probe["models"] = {
            str(entry.get("id") or entry.get("name") or "")
            for entry in listing
            if isinstance(entry, Mapping)
        }
        probe["models"].discard("")
        return probe

    def _seat_report(self, role: str, seat: Any, probe: Mapping[str, Any]) -> Dict[str, Any]:
        """Readiness of ONE seat, read off its endpoint's probe.

        HTTP readiness is the endpoint answering ``/models`` AND the served
        model appearing in that listing.  CLI readiness is ``--version``
        having run plus the offline billing preflight saying ``ready``.  An
        unverified seat is ``unknown`` whatever the endpoint says: nobody has
        confirmed that endpoint serves the model the seat names, and a
        reachable endpoint is not evidence that it does.
        """
        from .council.types import CLI_KINDS

        backend = probe["backend"]
        report: Dict[str, Any] = {
            "role": role,
            "seat": seat.seat_name,
            "endpoint": seat.endpoint,
            "endpoint_kind": seat.endpoint_kind,
            "model": seat.model,
            "canonical_model": seat.canonical_model,
            "verified": bool(seat.verified),
            "state": "unknown",
            "detail": "",
        }
        if not seat.verified and not self.allow_unverified:
            report["detail"] = (
                "the model is marked verified: false -- nobody has confirmed this "
                "endpoint serves it; confirm it and set verified: true"
            )
            return report

        if seat.endpoint_kind in CLI_KINDS:
            version = probe.get("version")
            if not version:
                report["state"] = "unavailable"
                report["detail"] = (
                    f"{getattr(backend, 'executable', seat.endpoint)} --version did not "
                    "run; the CLI is not installed or not on PATH"
                )
                return report
            report["cli_version"] = version
            billing = dict(probe.get("billing") or {})
            state = str(billing.get("state") or "unknown")
            report["billing"] = state
            report["detail"] = str(billing.get("detail") or "")
            if state == "ready":
                report["state"] = "ready"
            elif state == "refused":
                report["state"] = "unavailable"
            else:
                report["state"] = "unknown"
            return report

        names = set(probe.get("models") or ())
        if not names:
            report["state"] = "unavailable"
            report["detail"] = (
                f"{getattr(backend, 'base_url', seat.endpoint)} listed no models: it is "
                "unreachable, refused the request, or does not implement /models"
            )
            return report
        report["models_listed"] = len(names)
        if seat.model in names:
            report["state"] = "ready"
            report["detail"] = f"{seat.model} is served by {seat.endpoint}"
        else:
            report["state"] = "unavailable"
            report["detail"] = (
                f"{seat.endpoint} is reachable but does not list {seat.model!r}; it "
                f"lists {sorted(names)[:8]}"
            )
        return report

    async def doctor_async(self, profile: Optional[str] = None) -> Dict[str, Any]:
        """Report whether every seat of a profile could actually be used.

        Spends nothing and sends no prompt: ``complete()`` is never called.
        Also reports the store's reachability and schema version, who the
        submitting user is, and the NAMES of any billing variables a CLI seat
        would drop (names only -- a value is never read).
        """
        from .council.backends.cli_common import dropped_billing_vars

        config = self.config()
        name = profile or config.default_profile
        roster = self.roster(profile)
        backends = self.build_backends(roster)
        warnings: List[str] = []
        try:
            probes: Dict[str, Dict[str, Any]] = {}
            for _role, seat in roster.seats():
                if seat.endpoint not in probes:
                    probes[seat.endpoint] = await self._probe_endpoint(
                        seat.endpoint, seat.endpoint_kind, backends[seat.endpoint]
                    )
            seats = [
                self._seat_report(role, seat, probes[seat.endpoint])
                for role, seat in roster.seats()
            ]
        finally:
            await self._aclose_backends(backends)

        counts = {state: 0 for state in SEAT_STATES}
        for seat in seats:
            counts[seat["state"]] = counts.get(seat["state"], 0) + 1

        store_report: Dict[str, Any] = {
            "url": _safe_store_url(self.store_url()),
            "reachable": False,
            "schema_version": 0,
            "error": None,
        }
        try:
            store_report["schema_version"] = int(self.store().schema_version())
            store_report["reachable"] = True
        except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
            store_report["error"] = f"{type(exc).__name__}: {exc}"
            warnings.append(f"the council store could not be opened: {store_report['error']}")
        else:
            if store_report["schema_version"] == 0:
                warnings.append(NO_SCHEMA_HINT)

        dropped = dropped_billing_vars()
        if dropped:
            warnings.append(
                "a CLI seat will NOT be given " + ", ".join(dropped) + ": a CLI seat is "
                "billed against the interactive subscription and there is no API-billing "
                "mode for it"
            )
        for seat in seats:
            if seat["state"] != "ready":
                warnings.append(f"seat {seat['seat']} ({seat['endpoint']}): {seat['detail']}")

        exit_code = 1 if counts.get("unavailable") else (2 if counts.get("unknown") else 0)
        return {
            "profile": name,
            "user": self.user(),
            "config_source": config.source,
            "store": store_report,
            "seats": seats,
            "summary": counts,
            "dropped_billing_vars": dropped,
            "exit_code": exit_code,
            "warnings": warnings,
        }

    def doctor(self, profile: Optional[str] = None) -> Dict[str, Any]:
        """Check every seat of a profile.  The synchronous :meth:`doctor_async`."""
        return _run_sync(self.doctor_async(profile))

    # -- models -----------------------------------------------------------

    async def models_async(self, endpoint: Optional[str] = None) -> List[Dict[str, Any]]:
        """List models.

        With no *endpoint* this is the CONFIGURED model aliases -- what this
        installation can seat, read from the config and costing nothing.  With
        an *endpoint* it is that endpoint's own live listing (a free GET for
        an HTTP endpoint; neither CLI enumerates its models without spending,
        so both report an empty list).
        """
        config = self.config()
        if endpoint is None:
            return [
                {
                    "alias": alias,
                    "endpoint": model.endpoint,
                    "served_name": model.served_name,
                    "canonical": model.canonical,
                    "verified": bool(model.verified),
                    "kind": config.endpoints[model.endpoint].kind
                    if model.endpoint in config.endpoints
                    else "",
                }
                for alias, model in sorted(config.models.items())
            ]

        mapping = self._endpoint_mapping(endpoint, config, True)
        try:
            return await llm.models(
                str(mapping.get("kind") or ""),
                mapping,
            )
        except KeyError:
            # An unregistered kind: same mistake, same message as everywhere
            # else, rather than a bare KeyError out of the registry.
            self._get_api(endpoint, mapping)
            raise  # pragma: no cover - _get_api always raises first
        finally:
            await llm.aclose_all()

    def models(self, endpoint: Optional[str] = None) -> List[Dict[str, Any]]:
        """List models.  The synchronous :meth:`models_async`."""
        return _run_sync(self.models_async(endpoint))

    # -- the deployed GO council service ----------------------------------

    def service_client(self, **kwargs: Any) -> Any:
        """A client for Filipe Liu's deployed GO council service.

        A separate route from everything above: that service runs its own
        council in-request and returns a comparison, so nothing of it touches
        this store, this roster or this user's budgets.
        """
        from .council.service_client import ConcordiaServiceClient

        return ConcordiaServiceClient(**kwargs)


# ---------------------------------------------------------------------------
# Composition-based implementation
# ---------------------------------------------------------------------------


class CouncilUtilsImpl:
    """Composition-based council utilities.

    Holds ``env`` instead of inheriting from it and delegates to an internal
    :class:`CouncilUtils`, built lazily on first real attribute access so that
    ``KBUtilLib().council`` costs nothing until it is used.  Deliberately NOT
    built on ``ArgoUtilsImpl``: the council reaches ARGO through a backend, and
    inheriting the legacy client would put an ``httpx`` client (and possibly a
    gateway ping) behind a property access.
    """

    def __init__(self, env: Any, **kwargs: Any) -> None:
        self._env = env
        self._init_kwargs = kwargs
        self._delegate: Optional[CouncilUtils] = None

    def _ensure_delegate(self) -> CouncilUtils:
        if self._delegate is None:
            _kwargs: Dict[str, Any] = {
                "config_file": False,
                "token_file": None,
                "kbase_token_file": None,
            }
            try:
                token = self._env.get_token("kbase")
            except Exception:  # noqa: BLE001 - a missing token is not an error
                token = None
            if token:
                _kwargs["token"] = token
            _kwargs.update(self._init_kwargs)
            self._delegate = CouncilUtils(**_kwargs)
        return self._delegate

    @property
    def env(self) -> Any:
        return self._env

    def __getattr__(self, name: str) -> Any:
        # Only reached for names this class does not define itself, so the
        # delegate is built on the first council call and not on construction.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._ensure_delegate(), name)
