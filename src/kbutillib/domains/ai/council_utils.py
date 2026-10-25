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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from ...core.shared_env_utils import SharedEnvUtils
from . import llm
from .council.servable import SEAT_STATES, SeatUnavailable

__all__ = ["CouncilUtils", "CouncilUtilsImpl", "SEAT_STATES", "SeatUnavailable"]

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
        #: ``{"name@version": "/path/to/file.yaml"}`` for every query type
        #: this object loaded from a configured ``query_dirs`` entry.  Rebuilt
        #: on each store open, so it reports where a spec IS rather than where
        #: it once was.
        self.query_dir_sources: Dict[str, str] = {}
        self._query_dirs_loaded = False

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

        Opening the store is also when the configured ``query_dirs`` are
        loaded (:meth:`_load_query_dirs`), so a spec file a user edited is in
        the store before any verb looks a query type up.  A one-off open
        (``db migrate``) skips that: a migration must not be running
        registrations against a schema it is in the middle of changing.

        The CONFIGURED ARCHIVE IDENTITY (``CouncilConfig.store_id``) is fed
        in here, on EVERY open including a one-off, because this is the only
        place production opens a store.  Without it the store's
        configured-vs-stored comparison
        (``SqliteStore.require_store_identity``) never sees a configured
        value, so the ``StoreIdentityMismatch`` branch is unreachable and a
        store that was copied and then written to under the wrong declared id
        archives silently -- the one case the identity check exists to stop
        (KBUtilLib#93).

        It is passed as its OWN argument ALONGSIDE ``**kwargs`` and is
        deliberately NOT folded into ``kwargs``: the TRUTHINESS of ``kwargs``
        is this method's one-off-open flag, so a ``kwargs["store_id"] = ...``
        would make every open a one-off -- never cached (a fresh
        ``open_store`` per ``.store()`` call) and never query-dir-loaded (so
        every spec file in a configured ``query_dirs`` would go
        unregistered).  Neither of those raises, which is why the obvious
        fix is worse than the bug.

        A caller that passes ``store_id=`` explicitly WINS: an explicit
        argument is a deliberate override of config, and such a call is a
        one-off like any other kwarg-bearing call.
        """
        if self._store is None or kwargs:
            from .council.store import open_store

            # Built separately from `kwargs` so the one-off decision below
            # reads the caller's ORIGINAL kwargs, unmodified.
            options = dict(kwargs)
            if "store_id" not in options:
                options["store_id"] = getattr(self.config(), "store_id", None)

            store = open_store(self.store_url(), **options)
            if kwargs:
                # A one-off open (migrate) is NOT cached: its options are not
                # the ones every other verb wants.
                return store
            self._store = store
            self._load_query_dirs(store)
        return self._store

    def _load_query_dirs(self, store: Any) -> None:
        """Register every spec file in the configured ``query_dirs``.  Once.

        Deliberately NOT a no-op on a store with no schema: it returns
        WITHOUT marking itself done, because ``db_init`` creates the tables
        and then asks again.  Any other verb on a schemaless store is about
        to be refused by :meth:`require_schema` anyway.

        The "done" flag is set BEFORE the registrations run, because
        registering goes through :class:`QueryRegistry` and a future change
        there that re-entered :meth:`store` would otherwise recurse.  A load
        that raises therefore does not retry on this object -- the caller saw
        the error, and silently trying again would hide it.
        """
        if self._query_dirs_loaded:
            return
        dirs = list(getattr(self.config(), "query_dirs", ()) or ())
        if not dirs:
            self._query_dirs_loaded = True
            return
        if int(store.schema_version()) == 0:
            return
        from .council.queries import QueryRegistry, register_query_dirs

        self._query_dirs_loaded = True
        self.query_dir_sources = register_query_dirs(
            QueryRegistry(store), dirs, created_by=self.user()
        )

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
        """Create the schema, seed the builtins, load the query dirs.  Idempotent.

        The query directories are loaded HERE as well as on every store open
        because this is the one verb that runs against a store with no tables:
        the open above found no schema to register anything into, so without
        this a fresh machine's first ``db init`` would not pick up the user's
        own query types.
        """
        from .council.queries import QueryRegistry

        store = self.store()
        store.init_schema()
        seeded = QueryRegistry(store).seed_builtins()
        self._load_query_dirs(store)
        return {
            "store": _safe_store_url(self.store_url()),
            "schema_version": int(store.schema_version()),
            "query_types": [f"{spec.name}@{spec.version}" for spec in seeded],
            "query_dirs": list(self.config().query_dirs),
            "query_dir_types": sorted(self.query_dir_sources),
        }

    def db_migrate(self) -> Dict[str, Any]:
        """Bring the store up to the current schema version.

        Three things guard this, because a migration is the one verb that
        changes the shape of a store somebody else may be reading:

        * The store is opened with ``check_version=False``, because the
          databases a migration exists for are precisely the ones a version
          check refuses to open.
        * The EXECUTION LOCK is held for the whole migration (kind
          ``migrate``), so it cannot run beside a daemon, a ``run`` or an
          ``evaluate``. Held, not waited for: if something else is running
          this raises and names it.
        * The store file (and its ``-wal`` / ``-shm`` siblings) are copied to
          ``council.sqlite.bak-<UTC timestamp>`` BEFORE anything is altered.
          A v1 -> v2 migration drops tables, which is not reversible, so the
          user keeps the file they had.

        Returns a report including ``backup`` -- the path of the copy, or
        ``None`` for an in-memory store or a store file that does not exist
        yet (there is nothing to lose).

        Raises:
            LockHeld: another council process is executing against this store.
        """
        path = self._store_file()
        with self.execution_lock().acquire("migrate"):
            backup = self._backup_store_file(path)
            store = self.store(check_version=False)
            try:
                before = int(store.schema_version())
                # An ALREADY-STAMPED store may still be missing a column added
                # to the same schema version later (see
                # schema.ADDITIVE_COLUMNS).  Asked for here, BEFORE migrate()
                # does it silently, so the report can name what it added
                # rather than saying "migrated: false" about a file it just
                # altered.
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
            "backup": str(backup) if backup is not None else None,
        }

    def execution_lock(self) -> Any:
        """The :class:`~.council.execlock.ExecutionLock` guarding this store.

        ONE lock object per store, built the same way by every caller, so
        ``db migrate``, ``run``, ``evaluate`` and the daemon cannot end up
        excluding each other on three different files.  It lives in the store
        FILE's own directory -- two stores on one machine do not exclude each
        other -- and falls back to the current directory for an in-memory
        store, which has no directory of its own but still must not have two
        councils executing against it.
        """
        from .council.execlock import ExecutionLock

        path = self._store_file()
        directory = path.parent if path is not None else Path.cwd()
        return ExecutionLock(directory, path)

    def daemon(self, **kwargs: Any) -> Any:
        """A :class:`~.council.daemon.CouncilDaemon` over this store.

        The facade's factory for it, so the CLI (and a notebook) do not import
        the daemon module directly and the daemon does not have to import this
        one -- it needs ``config()``, ``store_url()``, ``user()``,
        ``require_schema()``, ``endpoint_api()`` and ``execution_lock()``, and
        takes them structurally.
        """
        from .council.daemon import CouncilDaemon

        return CouncilDaemon(self, **kwargs)

    def endpoint_api(self, endpoint: str, kind: str = "") -> Any:
        """The api type instance serving ONE endpoint alias, or ``None``.

        :meth:`build_backends` answers the same question for a ROSTER; the
        daemon's readiness probe asks it per alias, because the routes it
        keeps probed come from call rows and from several profiles at once
        rather than from one roster.  Both go through :func:`llm.get_api`, so
        there is still exactly one place that turns a ``kind`` into a client.

        ``None`` rather than a raise for an endpoint this installation cannot
        build: the probe's answer for that is ``unknown`` ("there is nothing
        to ask it"), and an exception out of a readiness check would take the
        daemon down over a config entry it was never going to use.

        Args:
            endpoint: the alias.
            kind: the api type to use when the configuration does not declare
                this endpoint at all -- which is the case for an endpoint that
                only exists on an old call row, whose ``endpoint_kind`` column
                is then the only thing that knows what it was.
        """
        config = self.config()
        try:
            mapping = self._endpoint_mapping(endpoint, config, self.allow_unverified)
        except Exception:  # noqa: BLE001 - not a configured endpoint
            mapping = {"name": endpoint, "allow_unverified": bool(self.allow_unverified)}
        if not mapping.get("kind"):
            mapping["kind"] = kind
        if not mapping.get("kind"):
            return None
        try:
            return llm.get_api(str(mapping["kind"]), mapping)
        except Exception as exc:  # noqa: BLE001 - an unknown kind is not fatal
            self.log_debug(
                f"council: no api type for endpoint {endpoint!r} "
                f"(kind {mapping.get('kind')!r}): {exc}"
            )
            return None

    def _store_file(self) -> Optional[Path]:
        """The store's path on disk, or None when there is not one.

        Resolved through the store layer's own URL normalisation rather than
        by parsing the URL here, so ``~``, a bare path and ``sqlite:///...``
        all land in the same place the store itself would use -- and an
        in-memory store answers None.
        """
        from .council.store.sqlite import _resolve_sqlite_url

        try:
            _url, path = _resolve_sqlite_url(self.store_url())
        except Exception:  # noqa: BLE001 - open_store raises the real refusal
            return None
        return path

    @staticmethod
    def _backup_store_file(path: Optional[Path]) -> Optional[Path]:
        """Copy *path* and its WAL siblings aside; return the copy's path.

        ``None`` when there is nothing to copy -- an in-memory store, or a
        file that does not exist yet because this migration is really a
        create.  The ``-wal`` and ``-shm`` files go too: a WAL database is
        that trio, and a copy of only the main file can be missing the most
        recent committed transactions.

        The timestamp is UTC to the second with a counter, so two migrations
        in the same second do not overwrite each other's backup.
        """
        if path is None or not path.exists():
            return None
        import shutil

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = path.with_name(f"{path.name}.bak-{stamp}")
        counter = 1
        while target.exists():
            target = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
            counter += 1
        shutil.copy2(path, target)
        for suffix in ("-wal", "-shm"):
            sibling = path.with_name(path.name + suffix)
            if sibling.exists():
                shutil.copy2(sibling, target.with_name(target.name + suffix))
        return target

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
        """The PROVENANCE label for jobs this object submits.

        The top-level ``user`` key of the ``ai_council`` config section, else
        the OS account.  That is the whole rule: there is no identity in the
        council (review round 2), so this grants nothing and gates nothing --
        it is a string written to ``jobs.submitted_by`` so a verdict can be
        attributed months later.  No token is read and no network call is
        made to find out this machine's own name.

        Changing it affects only jobs submitted AFTERWARDS.  The queue and
        the endpoint budgets are the store's and are keyed on the store's own
        fixed identity, so a rename strands no pending call and resets no
        budget.
        """
        if self._user:
            return self._user
        name = ""
        try:
            name = str(self.config().user or "").strip()
        except Exception as exc:  # noqa: BLE001 - identity must never be fatal
            self.log_debug(f"council: could not read the configured user: {exc}")
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

    async def _servable_seats(
        self,
        roster: Any,
        backends: Mapping[str, Any],
        allow_unverified: bool,
        allow_unavailable: bool,
    ) -> Dict[str, str]:
        """Refuse a job this machine cannot serve.  Returns the blockers.

        Runs the SAME offline check ``kbu council doctor`` runs -- once per
        DISTINCT endpoint in the roster, so five seats on one gateway cost one
        free ``/models`` GET -- and then reads one state per seat off it.
        Nothing here can spend a token: ``complete()`` is never called.

        The reason this is at submit at all: **queues are per machine.**  A
        store is one machine's single-user SQLite file and nothing coordinates
        across hosts (review round 2), so no other machine will ever work a
        seat this one cannot reach.  A call row for an unreachable endpoint is
        not "work waiting for somebody"; it is a job that never finishes.

        * ``unavailable`` (checked, and it is broken) raises
          :class:`SeatUnavailable` naming every affected seat, its endpoint
          and the doctor's detail -- unless *allow_unavailable*, in which case
          the endpoint is returned and recorded on the job, where
          ``job_status`` reports it as an ``unservable`` blocker.
        * ``unknown`` (could not tell) follows the existing
          ``allow_unverified`` rule, which is the same answer the billing
          guard and the verified-seat rule already give for "unconfirmed".
        * The ``verified: false`` rule is NOT applied here: that refusal is
          ``CouncilWorker._check_roster``'s and raises ``RosterError``, so
          the seat states are read with the verification short-circuit off
          and this check reports only what the ENDPOINT said.

        Returns:
            ``{endpoint: detail}`` for each endpoint admitted by
            *allow_unavailable*.  Empty when everything is servable.
        """
        from .council.servable import probe_roster, unservable_message

        _probes, seats = await probe_roster(roster, backends, allow_unverified=True)
        unavailable = [report for report in seats if report["state"] == "unavailable"]
        if unavailable and not allow_unavailable:
            raise SeatUnavailable(
                unservable_message(unavailable, "allow_unavailable=True"), unavailable
            )
        unknown = [report for report in seats if report["state"] == "unknown"]
        if unknown and not allow_unverified:
            raise SeatUnavailable(
                unservable_message(unknown, "allow_unverified=True"), unknown
            )
        blockers: Dict[str, str] = {}
        for report in unavailable:
            blockers.setdefault(str(report["endpoint"]), str(report["detail"]))
        return blockers

    @staticmethod
    def _job_options(
        allow_unverified: bool,
        allow_unavailable: bool,
        unservable: Mapping[str, str],
        **extra: Any,
    ) -> Dict[str, Any]:
        """The permissions a job carries, as its ``options``.

        Persisted on the job because they are a property of THAT job and of
        nothing else: a worker decides per call from the options of the call's
        own job, so one job's ``--allow-unverified`` can never admit another
        job's unknown calls (review round 3).  A flag on the worker object or
        on a cached client would do exactly that, which is why neither is the
        authority any more.
        """
        options: Dict[str, Any] = {
            "allow_unverified": bool(allow_unverified),
            "allow_unavailable": bool(allow_unavailable),
        }
        if unservable:
            options["unservable_endpoints"] = dict(unservable)
        options.update(extra)
        return options

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
        """The saved query-type rows, active ones only by default.

        Each row carries a ``source_file``: the path of the ``query_dirs``
        file it came from, or ``""``.  The ``source`` column already records
        ``builtin`` / ``user`` / ``directory``; the FILE is not in the
        database (an absolute path is machine-specific and would make the same
        spec compare unequal between machines), so it is reported from the
        directory scan this object did when it opened the store.
        """
        from .council.queries import annotate_sources

        rows = self.registry().list(include_retired=include_retired)
        return annotate_sources(rows, self.query_dir_sources)

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
        allow_unavailable: bool = False,
        **seat_overrides: Any,
    ) -> str:
        """Create a job, its items and its first round of proposer calls.

        Every seat's endpoint is checked first, offline and for free, by the
        same probe ``doctor`` uses (:meth:`_servable_seats`): the queue is per
        machine, so a seat this machine cannot reach is a job that would never
        finish.  Both permissions are written into the job's ``options`` and
        apply to THAT job's calls only.

        Args:
            allow_unverified: admit seats and endpoints whose state is
                ``unknown`` -- unverified models, and an unconfirmed billing
                mode.  Never bypasses a REFUSED billing check.
            allow_unavailable: queue the job even though an endpoint is
                unavailable on this machine.  Its calls WAIT: ``job_status``
                reports the endpoint as an ``unservable`` blocker.

        Returns:
            The job id.  Nothing has been spent yet: the calls are pending
            rows, and ``run``/the daemon is what works them.

        Raises:
            SeatUnavailable: a seat's endpoint cannot be served here.
        """
        unverified = self.allow_unverified or bool(allow_unverified)
        store = self.require_schema()
        spec = self._resolve_query(query)
        roster = self.roster(profile, **seat_overrides)
        backends = self.build_backends(roster, allow_unverified=unverified)
        try:
            unservable = await self._servable_seats(
                roster, backends, unverified, bool(allow_unavailable)
            )
            worker = self._worker(store, backends, unverified)
            return await worker.asubmit(
                spec,
                self._as_tasks(items),
                roster,
                label=label,
                priority=priority,
                profile_name=profile or self.config().default_profile,
                options=self._job_options(
                    unverified, bool(allow_unavailable), unservable
                ),
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
        allow_unavailable: bool = False,
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
                allow_unavailable=allow_unavailable,
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
        # The permission is the JOB's, read from the options it was submitted
        # with (review round 3) -- not this object's and not the roster's
        # alone.  ``run`` works exactly one job, so the clients it builds can
        # safely carry that one job's permission; ``CouncilWorker`` decides
        # again per call from the same options, which is what keeps a worker
        # serving two jobs from letting one job's permission admit the
        # other's calls.
        options = dict(job.get("options") or {})
        unverified = (
            self.allow_unverified
            or bool(options.get("allow_unverified"))
            or any(not seat.verified for _role, seat in roster.seats())
        )
        backends = self.build_backends(roster, allow_unverified=unverified)
        try:
            # The EXECUTION LOCK, held for the worker's whole lifetime (review
            # round 3).  A foreground run beside a daemon would have two
            # workers claiming out of one queue, and `kbu council run` is
            # exactly what somebody types when they are wondering why the
            # daemon is slow -- so it refuses, naming the holder's pid and
            # kind, rather than quietly doubling the claim rate.
            with self.execution_lock().acquire("run"):
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
            # Same lock, same reason (review round 3): evaluate() is a
            # one-item job worked to idle, i.e. a worker on the same queue.
            with self.execution_lock().acquire("evaluate"):
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
        store.cancel_job(job_id)
        return store.job_status(job_id)

    def delete(self, job_id: str) -> Dict[str, Any]:
        """Tombstone a cancelled or done job.  No row is physically removed."""
        store = self.require_schema()
        store.delete_job(job_id)
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
        allow_unavailable: bool = False,
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
                unservable = await self._servable_seats(
                    roster, backends, unverified, bool(allow_unavailable)
                )
                worker = self._worker(store, backends, unverified)
                return await worker.asubmit(
                    spec,
                    self._as_tasks(tasks),
                    roster,
                    label=label or f"rerun of {job_id}",
                    priority=int(job.get("priority") or 100),
                    profile_name=profile or job.get("profile_name"),
                    options=self._job_options(
                        unverified,
                        bool(allow_unavailable),
                        unservable,
                        rerun_of=job_id,
                    ),
                )
            finally:
                await self._aclose_backends(backends)

        return _run_sync(_submit())

    def jobs(
        self,
        status: Optional[str] = None,
        include_deleted: bool = False,
    ) -> List[Dict[str, Any]]:
        """Every job in the store; tombstones hidden by default.

        No per-user view and no ``--all-users``: a store is one machine's, so
        every job in it is yours and ``submitted_by`` is a label on it rather
        than a filter (review round 2).
        """
        return self.require_schema().list_jobs(
            status=status,
            include_deleted=include_deleted,
        )

    # -- calls and endpoints ----------------------------------------------

    def search_calls(self, **filters: Any) -> List[Dict[str, Any]]:
        """Find call rows across every job in the store.

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
        """This store's per-endpoint budget and halt state.

        Every CONFIGURED endpoint is reported, halted or not: "nothing is
        halted" is the answer an operator is usually looking for, and an
        endpoint with no state row has simply never been spent against.

        *user* is a deprecated no-op: the rows are the store's, keyed on its
        own fixed identity, not one person's ledger.
        """
        store = self.require_schema()
        rows: List[Dict[str, Any]] = []
        for name, endpoint in sorted(self.config().endpoints.items()):
            state = dict(store.get_endpoint_state(name))
            state.setdefault("endpoint", name)
            state["kind"] = endpoint.kind
            state["halted"] = bool(state.get("halt_kind"))
            rows.append(state)
        return rows

    def reset_halt(self, endpoint: str, user: Optional[str] = None) -> Dict[str, Any]:
        """Clear an endpoint's halt.  Never touches the token counters."""
        return self.require_schema().reset_halt(endpoint)

    def reset_budget(self, endpoint: str, user: Optional[str] = None) -> Dict[str, Any]:
        """Start a new budget epoch, keeping the lifetime total."""
        return self.require_schema().reset_budget(endpoint)

    # -- doctor -----------------------------------------------------------

    async def _probe_endpoint(self, endpoint: str, kind: str, backend: Any) -> Dict[str, Any]:
        """One endpoint's free probe.  See :func:`.council.servable.probe_endpoint`.

        Kept as a method because that is what ``doctor`` reads and what the
        facade's tests patch; the logic itself lives in
        :mod:`.council.servable` because ``submit`` and ``CouncilWorker`` run
        the very same check and a second copy of it could disagree about
        whether a seat is usable.
        """
        from .council.servable import probe_endpoint

        return await probe_endpoint(endpoint, kind, backend)

    def _seat_report(self, role: str, seat: Any, probe: Mapping[str, Any]) -> Dict[str, Any]:
        """One seat's readiness.  See :func:`.council.servable.seat_state`."""
        from .council.servable import seat_state

        return seat_state(role, seat, probe, allow_unverified=self.allow_unverified)

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

    # -- the token model a quote is priced from ---------------------------

    def token_model(
        self,
        profile: Optional[str] = None,
        upper_percentile: Optional[float] = None,
        **seat_overrides: Any,
    ) -> Dict[str, Any]:
        """Fit this store's measured token model for a profile's bench.

        Spends NOTHING and contacts nothing: it is a read of the store's own
        ``attempts``/``calls`` history (see
        :func:`~.council.quote.fit_token_model`).  A seat this store has never
        run is reported with ``basis = "projected"`` and the fallback it was
        drawn from, never as zero.

        Returns:
            The published ``data`` shape: ``profile``, ``bench_fingerprint``,
            ``roster_fingerprint`` and the fit's own
            :meth:`~.council.quote.TokenModel.to_dict` keys
            (``upper_percentile``, ``seats`` with a sample size each,
            ``n_measured``, ``n_projected``).
        """
        from .council.quote import DEFAULT_UPPER_PERCENTILE, fit_token_model
        from .council.types import bench_fingerprint

        store = self.require_schema()
        roster = self.roster(profile, **seat_overrides)
        model = fit_token_model(
            store,
            seats=roster,
            upper_percentile=(
                DEFAULT_UPPER_PERCENTILE if upper_percentile is None else float(upper_percentile)
            ),
        )
        data: Dict[str, Any] = {
            "profile": profile or self.config().default_profile,
            "bench_fingerprint": bench_fingerprint(roster),
            "roster_fingerprint": roster.fingerprint(),
        }
        data.update(model.to_dict())
        return data

    # -- the quote a run is approved against ------------------------------

    def quote_plan(
        self,
        plan: Any,
        profile: Optional[str] = None,
        meta_fire_rate: Optional[float] = None,
        upper_percentile: Optional[float] = None,
        **seat_overrides: Any,
    ) -> Any:
        """Price a run plan.  SPENDS NOTHING and reaches no network.

        Stage 2 of the standing ``function_to_go`` pipeline: it renders every
        proposer prompt locally, counts reuse exactly against the store's
        ``ix_calls_prompt_hash_status`` index, projects only meta and the
        repair pass, and returns a :class:`~.council.quote.Quote` carrying
        the plan hash an approval binds to.

        No backend is built -- not "none is called", none is BUILT: the
        reuse namespace and the effective params come from
        :func:`~.council.types.default_reuse_namespace` and
        :func:`~.council.types.seat_generation_params`, which exist so a
        prompt hash can be computed without instantiating one.

        Args:
            plan: a :class:`~.council.quote.Plan`, a run directory, a path to
                a ``PLAN.json``, or an already-parsed mapping.
            profile: the bench.  Defaults to the plan's own ``profile``, then
                to the configured default.
            meta_fire_rate: fraction of items reaching meta.  Defaults to the
                measured :data:`~.council.quote.META_FIRE_RATE`.
            upper_percentile: percentile for the token model's upper band.
            **seat_overrides: roster overrides, keyed by seat name.

        Returns:
            A :class:`~.council.quote.Quote`.
        """
        from .council.quote import (
            DEFAULT_UPPER_PERCENTILE,
            META_FIRE_RATE,
            fit_token_model,
            load_plan,
            quote_plan,
        )
        from .council.quote import Plan as _Plan

        store = self.require_schema()
        if not isinstance(plan, _Plan):
            plan = load_plan(plan)
        roster = self.roster(profile or plan.profile, **seat_overrides)
        token_model = fit_token_model(
            store,
            seats=roster,
            upper_percentile=(
                DEFAULT_UPPER_PERCENTILE
                if upper_percentile is None
                else float(upper_percentile)
            ),
        )
        return quote_plan(
            plan,
            store=store,
            config=self.config(),
            token_model=token_model,
            meta_fire_rate=(
                META_FIRE_RATE if meta_fire_rate is None else float(meta_fire_rate)
            ),
            roster=roster,
            effective_params=self._effective_params_resolver(),
        )

    def _effective_params_resolver(self) -> Any:
        """``(seat) -> params`` reporting what a seat's api type would send.

        THE REUSE COUNT DEPENDS ON THIS BEING RIGHT.  ``prompt_hash`` keys on
        the effective generation params, and an api type returns the WIRE
        spelling (``max_tokens``) while
        :func:`~.council.types.seat_generation_params` returns the
        provider-independent one (``max_output_tokens``); ARGO additionally
        forces ``temperature: 1`` on some models whatever was asked for.  A
        quote that used the latter would compute a different hash from the
        worker for every HTTP seat and report zero reuse on a corpus that is
        almost entirely already paid for.

        It lives HERE and not in :mod:`.council.quote` because this is the
        object that already builds api types; the quote module may not, so
        that "the quote cannot spend" stays a property of its import graph
        rather than a promise.  ``effective_params`` is specified as
        deterministic, generation-only and call-free
        (:meth:`~.llm.protocol.LLMApi.effective_params`), so resolving it
        opens no socket -- which the quote tests assert with the socket
        module poisoned.

        Falls back per seat to the provider-independent params when no api
        type can be built (an endpoint this installation cannot serve), which
        keeps a quote possible on a machine that could not run the job.
        """
        from .council.types import seat_generation_params

        cache: Dict[str, Any] = {}

        def _resolve(seat: Any) -> Dict[str, Any]:
            endpoint = str(getattr(seat, "endpoint", "") or "")
            if endpoint not in cache:
                cache[endpoint] = self.endpoint_api(
                    endpoint, str(getattr(seat, "endpoint_kind", "") or "")
                )
            api = cache[endpoint]
            if api is None:
                return dict(seat_generation_params(seat))
            try:
                return dict(api.effective_params(self._seat_request(seat)))
            except Exception as exc:  # noqa: BLE001 - never fail a quote over this
                self.log_debug(
                    f"council quote: {endpoint!r} could not report effective "
                    f"params ({exc}); falling back to the seat's own"
                )
                return dict(seat_generation_params(seat))

        return _resolve

    @staticmethod
    def _seat_request(seat: Any) -> Any:
        """A prompt-less :class:`LLMRequest` carrying only *seat*'s params.

        The same shape ``CouncilWorker._seat_request`` builds, and for the
        same reason: ``effective_params`` is computed per seat, before any
        role's prompts exist, so the request it reads carries the parameters
        and no prompts.
        """
        from .llm.types import LLMRequest

        return LLMRequest(
            model=seat.model,
            max_output_tokens=seat.max_output_tokens,
            temperature=seat.temperature,
            params=dict(getattr(seat, "extra", None) or {}),
            timeout_s=seat.timeout_s,
        )

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
