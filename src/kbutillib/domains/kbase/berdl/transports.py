"""BERDL transports: locus-specific access behind a common interface.

The published BERDL guides write every helper call bare and claim the
functions are "automatically imported, no imports needed." That is true
*only* inside a notebook kernel, where the platform's ``startup.py``
injects the names into the global namespace. Code running in a terminal or
subprocess -- which is what a KBUtilLib skill or a Maestro task does -- gets
``ImportError`` on the first call. See
``agent-io/prds/berdl-lakehouse-skills/fullprompt.md`` ("The import map")
and ``agent-io/prds/berdl-lakehouse-skills/api-reference.md`` for the
verified import paths and harvested signatures this module follows.

Two transports, one interface:

- :class:`InPodTransport` wraps ``berdl_notebook_utils`` (and its
  ``governance``, ``spark``, and ``refresh`` submodules). Full read and
  write. Only usable inside the BERDL JupyterHub pod, where the package is
  installed and the pod's own environment (``KBASE_AUTH_TOKEN``,
  ``SPARK_CONNECT_URL``, ``S3_ACCESS_KEY``) is present.
- :class:`OffPodTransport` is a pure-``requests`` REST client, read-only by
  construction -- it defines no write method at all. It reuses
  :class:`~kbutillib.domains.kbase.kb_berdl_utils.KBBERDLUtils` internally
  and resolves its token through :mod:`kbutillib.domains.kbase.berdl.tokens`
  rather than that class's own (env-blind) token lookup.

Both transports route raw platform database names through
:func:`kbutillib.domains.kbase.berdl.naming.normalize_databases` rather than
returning the platform's raw, dual-form (dotted Iceberg / underscored
legacy Delta) list.

``InPodTransport`` MUST NOT be constructed off-pod, and this module MUST NOT
import ``berdl_notebook_utils`` at module scope: that package is pod-only
and does not exist off-pod. A module-scope import here would break every
off-pod import of the ``berdl`` subpackage, including
:class:`OffPodTransport`'s. The import is deferred to
``InPodTransport.__init__`` instead, so merely importing this module (or
the ``berdl`` package) never requires the pod package to be installed.

Locus detection, the choice of which transport to construct, and the
``BerdlCapability`` deep module that sits above both transports are out of
scope here (see the PRD's "Modules to build" section) and are built
separately.
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .naming import NormalizedDatabase, normalize_databases
from .tokens import DEFAULT_TOKEN_FILE, require_token

if TYPE_CHECKING:
    # Only for type checkers: never imported at runtime, so this annotation
    # never forces berdl_notebook_utils to exist off-pod, and never forces
    # KBBERDLUtils to be imported at module scope either.
    from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils


class BerdlTransport(abc.ABC):
    """Common read interface shared by :class:`InPodTransport` and
    :class:`OffPodTransport`.

    This is intentionally small: both transports also expose locus-specific
    members (write operations, governance, tenancy, spark/trino session
    management on :class:`InPodTransport`; nothing beyond read on
    :class:`OffPodTransport`) that are not part of this shared contract.
    ``BerdlCapability`` (built separately) is what picks a transport and
    exposes a single stable surface over the union.
    """

    #: ``'in_pod'`` or ``'off_pod'``. Set by each subclass.
    LOCUS: str

    @abc.abstractmethod
    def databases(self) -> list[NormalizedDatabase]:
        """List reachable databases, normalized and deduplicated.

        Returns:
            One :class:`~kbutillib.domains.kbase.berdl.naming.NormalizedDatabase`
            per distinct logical dataset -- never the platform's raw,
            dual-form (dotted Iceberg / underscored legacy Delta) list.
        """

    @abc.abstractmethod
    def tables(self, database: str) -> list[str]:
        """List tables in ``database``."""

    @abc.abstractmethod
    def table_schema(self, database: str, table: str) -> Any:
        """Get the schema of ``table`` in ``database``."""


class InPodTransport(BerdlTransport):
    """Transport wrapping the pod-only ``berdl_notebook_utils`` package.

    Every helper is called through the exact submodule path verified
    against the installed package (see the PRD's import map) -- never as a
    bare, notebook-style call, since a bare call only resolves inside a
    notebook kernel where ``startup.py`` has injected the name into the
    global namespace.

    The ``berdl_notebook_utils`` import (and its ``governance``, ``spark``,
    and ``refresh`` submodules) happens here, in ``__init__``, not at module
    scope: constructing this class off-pod raises a normal ``ImportError``,
    but merely importing this module -- or the ``berdl`` package -- does
    not.

    Cluster management (``create_cluster``, ``delete_cluster``,
    ``get_cluster_status``) and the deprecated ``share_table`` /
    ``unshare_table`` / ``make_table_public`` / ``make_table_private``
    family are deliberately not wrapped here: the former is out of scope
    for this PRD, and the latter is deprecated sharing doctrine that the
    skills must actively steer away from (sharing is achieved by creating
    in a tenant catalog, refined with namespace ACLs).
    """

    LOCUS = "in_pod"

    def __init__(self) -> None:
        try:
            import berdl_notebook_utils as _bnu
            from berdl_notebook_utils import governance as _governance
            from berdl_notebook_utils import refresh as _refresh
            from berdl_notebook_utils import spark as _spark
        except ImportError as exc:  # pragma: no cover - requires the pod
            raise ImportError(
                "InPodTransport requires the pod-only 'berdl_notebook_utils' "
                "package, which is not installed. This transport only works "
                "inside the BERDL JupyterHub pod (kbhub). Off-pod, use "
                "OffPodTransport instead."
            ) from exc

        self._bnu = _bnu
        self._governance = _governance
        self._spark = _spark
        self._refresh = _refresh

    # -- berdl_notebook_utils (top level) -----------------------------

    def spark_session(
        self,
        app_name: str | None = None,
        local: bool = False,
        delta_lake: bool = True,
        scheduler_pool: str = "default",
        use_s3: bool = True,
        use_hive: bool = True,
        settings: Any = None,
        tenant_name: str | None = None,
        use_spark_connect: bool = True,
        override: Any = None,
    ) -> Any:
        """Get (or create) a Spark session. ``berdl_notebook_utils.get_spark_session``."""
        return self._bnu.get_spark_session(
            app_name=app_name,
            local=local,
            delta_lake=delta_lake,
            scheduler_pool=scheduler_pool,
            use_s3=use_s3,
            use_hive=use_hive,
            settings=settings,
            tenant_name=tenant_name,
            use_spark_connect=use_spark_connect,
            override=override,
        )

    def create_namespace_if_not_exists(
        self,
        spark: Any,
        namespace: str = "default",
        tenant_name: str | None = None,
        iceberg: bool = True,
    ) -> str:
        """Create ``namespace`` if it does not already exist.

        ``iceberg=True`` is already the default -- ``iceberg=False`` is the
        legacy Delta opt-out, not something that needs to be requested for
        the Iceberg path. ``berdl_notebook_utils.create_namespace_if_not_exists``.
        """
        return self._bnu.create_namespace_if_not_exists(
            spark, namespace=namespace, tenant_name=tenant_name, iceberg=iceberg
        )

    def databases(
        self,
        spark: Any = None,
        use_hms: bool = True,
        filter_by_namespace: bool = True,
        tenant: str | None = None,
        settings: Any = None,
        force_refresh: bool = False,
    ) -> list[NormalizedDatabase]:
        """List reachable databases, normalized and deduplicated.

        ``berdl_notebook_utils.get_databases`` defaults to
        ``return_json=True`` and returns a JSON *string*, not a list;
        ``return_json=False`` is passed explicitly here to get a Python
        list before normalizing it through
        :func:`~kbutillib.domains.kbase.berdl.naming.normalize_databases`.
        """
        raw = self._bnu.get_databases(
            spark=spark,
            use_hms=use_hms,
            return_json=False,
            filter_by_namespace=filter_by_namespace,
            tenant=tenant,
            settings=settings,
            force_refresh=force_refresh,
        )
        return normalize_databases(raw)

    def tables(
        self,
        database: str,
        spark: Any = None,
        use_hms: bool = True,
        force_refresh: bool = False,
    ) -> list[str]:
        """List tables in ``database``.

        Same JSON-string trap as :meth:`databases`: ``return_json=False``
        is passed explicitly. ``berdl_notebook_utils.get_tables``.
        """
        return self._bnu.get_tables(
            database,
            spark=spark,
            use_hms=use_hms,
            return_json=False,
            force_refresh=force_refresh,
        )

    def table_schema(
        self,
        database: str,
        table: str,
        spark: Any = None,
        detailed: bool = False,
        force_refresh: bool = False,
    ) -> Any:
        """Get the schema of ``table`` in ``database``.

        Same JSON-string trap as :meth:`databases`: ``return_json=False``
        is passed explicitly. ``berdl_notebook_utils.get_table_schema``.
        """
        return self._bnu.get_table_schema(
            database,
            table,
            spark=spark,
            return_json=False,
            detailed=detailed,
            force_refresh=force_refresh,
        )

    def db_structure(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.get_db_structure`` (signature not harvested
        in ``api-reference.md``; arguments are passed through as given).
        """
        return self._bnu.get_db_structure(*args, **kwargs)

    def trino_connection(
        self,
        connector: str,
        host: str | None = None,
        port: int | None = None,
        settings: Any = None,
    ) -> Any:
        """Get a Trino connection. ``berdl_notebook_utils.get_trino_connection``.

        ``connector`` has no default here on purpose: the underlying
        helper defaults to ``connector='delta_lake'`` (legacy), not
        Iceberg, and the guides never mention the parameter at all. Taking
        that default would silently connect through the legacy connector,
        so callers must state their choice explicitly (typically
        ``'iceberg'``) rather than relying on this wrapper to pick one.
        """
        return self._bnu.get_trino_connection(
            host=host, port=port, connector=connector, settings=settings
        )

    def refresh_spark_environment(self, *args: Any, **kwargs: Any) -> Any:
        """Rotate S3/Polaris credentials and refresh the Spark environment.

        ``berdl_notebook_utils.refresh_spark_environment`` (top-level path;
        also reachable via the ``refresh`` submodule, see
        :meth:`rotate_credentials`).

        **Rotating.** Per the credential escalation ladder in the PRD, this
        invalidates credentials in use by other kernels, running scripts,
        and remote connections using the old ones. It is never the first
        repair move -- try :meth:`credentials` (non-rotating) first.
        """
        return self._bnu.refresh_spark_environment(*args, **kwargs)

    def table_exists(self, spark: Any, table_name: str, namespace: str = "default") -> bool:
        """Check whether ``table_name`` exists in ``namespace``.

        ``namespace`` is a separate argument, not part of a dotted
        ``table_name`` -- passing a fully qualified name as ``table_name``
        will not behave as expected. ``berdl_notebook_utils.table_exists``.
        """
        return self._bnu.table_exists(spark, table_name, namespace=namespace)

    def remove_table(self, spark: Any, table_name: str, namespace: str = "default") -> None:
        """Remove a table. ``berdl_notebook_utils.remove_table``."""
        return self._bnu.remove_table(spark, table_name, namespace=namespace)

    def minio_client(self, settings: Any = None) -> Any:
        """Get a MinIO client. ``berdl_notebook_utils.get_minio_client``."""
        return self._bnu.get_minio_client(settings=settings)

    def s3_client(self, settings: Any = None) -> Any:
        """Get an S3 client. ``berdl_notebook_utils.get_s3_client``."""
        return self._bnu.get_s3_client(settings=settings)

    # -- berdl_notebook_utils (top level) -- tenancy v2 / stewardship --

    def list_tenants(self, force_refresh: bool = False) -> list[Any]:
        """List all tenants. ``berdl_notebook_utils.list_tenants``."""
        return self._bnu.list_tenants(force_refresh=force_refresh)

    def get_tenant_detail(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.get_tenant_detail`` (signature not
        harvested; arguments are passed through as given).
        """
        return self._bnu.get_tenant_detail(*args, **kwargs)

    def tenant_members(self, tenant_name: str) -> list[Any]:
        """List a tenant's members. ``berdl_notebook_utils.get_tenant_members``."""
        return self._bnu.get_tenant_members(tenant_name)

    def add_tenant_member(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.add_tenant_member`` (signature not
        harvested; arguments are passed through as given). Mutation --
        requires explicit confirmation upstream in the calling skill.
        """
        return self._bnu.add_tenant_member(*args, **kwargs)

    def remove_tenant_member(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.remove_tenant_member`` (signature not
        harvested; arguments are passed through as given). Mutation --
        requires explicit confirmation upstream in the calling skill.
        """
        return self._bnu.remove_tenant_member(*args, **kwargs)

    def update_tenant_metadata(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.update_tenant_metadata`` (signature not
        harvested; arguments are passed through as given).
        """
        return self._bnu.update_tenant_metadata(*args, **kwargs)

    def show_my_tenants(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.show_my_tenants`` (signature not
        harvested; arguments are passed through as given).
        """
        return self._bnu.show_my_tenants(*args, **kwargs)

    def assign_steward(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.assign_steward`` (signature not
        harvested; arguments are passed through as given). Mutation --
        requires explicit confirmation upstream in the calling skill.
        """
        return self._bnu.assign_steward(*args, **kwargs)

    def remove_steward(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.remove_steward`` (signature not
        harvested; arguments are passed through as given). Mutation --
        requires explicit confirmation upstream in the calling skill.
        """
        return self._bnu.remove_steward(*args, **kwargs)

    def get_my_steward_tenants(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.get_my_steward_tenants`` (signature not
        harvested; arguments are passed through as given).
        """
        return self._bnu.get_my_steward_tenants(*args, **kwargs)

    # -- berdl_notebook_utils.governance -------------------------------

    def my_groups(self, force_refresh: bool = False) -> Any:
        """Get the caller's raw group membership.

        Returns a ``UserGroupsResponse`` *object*, not a list -- membership
        is on ``.groups``. Decode it with
        :func:`kbutillib.domains.kbase.berdl.membership.decode_memberships`
        against :meth:`available_groups`, not by inspecting this return
        value directly. ``berdl_notebook_utils.governance.get_my_groups``.
        """
        return self._governance.get_my_groups(force_refresh=force_refresh)

    def my_workspace(self) -> Any:
        """``berdl_notebook_utils.governance.get_my_workspace``."""
        return self._governance.get_my_workspace()

    def credentials(self) -> Any:
        """Fetch current credentials without rotating them.

        The **non-rotating** first step of the credential escalation
        ladder -- safe to call repeatedly; unlike
        :meth:`refresh_spark_environment` it cannot invalidate credentials
        in use elsewhere. ``berdl_notebook_utils.governance.get_credentials``.
        """
        return self._governance.get_credentials()

    def request_tenant_access(
        self,
        tenant_name: str,
        permission: str = "read_only",
        justification: str | None = None,
    ) -> Any:
        """Request access to a tenant.

        This is an **asynchronous human approval step** (typically same-day
        during business hours via Slack) -- the caller should set that
        expectation and not poll.
        ``berdl_notebook_utils.governance.request_tenant_access``.
        """
        return self._governance.request_tenant_access(
            tenant_name, permission=permission, justification=justification
        )

    def available_groups(self) -> list[str]:
        """List all real tenant/group names, as a plain list.

        Unlike :meth:`my_groups`, this returns a plain ``list[str]`` -- the
        two do not share a uniform shape.
        ``berdl_notebook_utils.governance.list_available_groups``.
        """
        return self._governance.list_available_groups()

    def namespace_access(
        self, tenant_name: str | None = None, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """List who has access to a namespace (or tenant).

        Inspection is always available (no confirmation gate).
        ``berdl_notebook_utils.governance.list_namespace_access``.
        """
        return self._governance.list_namespace_access(
            tenant_name=tenant_name, namespace=namespace
        )

    def grant_namespace_access(
        self,
        tenant_name: str,
        username: str,
        namespace: str,
        access_level: str = "read",
        show_refresh_hint: bool = True,
    ) -> dict[str, Any]:
        """Grant namespace access. Mutation -- requires explicit
        confirmation upstream in the calling skill: it can casually widen
        access to shared, sensitive tenant data.
        ``berdl_notebook_utils.governance.grant_namespace_access``.
        """
        return self._governance.grant_namespace_access(
            tenant_name,
            username,
            namespace,
            access_level=access_level,
            show_refresh_hint=show_refresh_hint,
        )

    def revoke_namespace_access(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.governance.revoke_namespace_access``
        (signature not harvested; arguments are passed through as given).
        Mutation -- requires explicit confirmation upstream in the calling
        skill.
        """
        return self._governance.revoke_namespace_access(*args, **kwargs)

    def tenant_stewards(self, tenant_name: str) -> list[Any]:
        """List a tenant's stewards -- who to ask for access changes.
        ``berdl_notebook_utils.governance.get_tenant_stewards``.
        """
        return self._governance.get_tenant_stewards(tenant_name)

    # -- berdl_notebook_utils.governance -- admin operations -----------

    def list_users(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.governance.list_users`` (admin operation;
        signature not harvested; arguments are passed through as given).
        """
        return self._governance.list_users(*args, **kwargs)

    def list_groups(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.governance.list_groups`` (admin operation;
        signature not harvested; arguments are passed through as given).
        """
        return self._governance.list_groups(*args, **kwargs)

    def add_group_member(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.governance.add_group_member`` (admin
        operation; signature not harvested; arguments are passed through
        as given). Mutation -- requires explicit confirmation upstream.
        """
        return self._governance.add_group_member(*args, **kwargs)

    def remove_group_member(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.governance.remove_group_member`` (admin
        operation; signature not harvested; arguments are passed through
        as given). Mutation -- requires explicit confirmation upstream.
        """
        return self._governance.remove_group_member(*args, **kwargs)

    def create_tenant_and_assign_users(self, *args: Any, **kwargs: Any) -> Any:
        """``berdl_notebook_utils.governance.create_tenant_and_assign_users``
        (admin operation; signature not harvested; arguments are passed
        through as given). Mutation -- requires explicit confirmation
        upstream.
        """
        return self._governance.create_tenant_and_assign_users(*args, **kwargs)

    # -- berdl_notebook_utils.spark --------------------------------------

    def start_spark_connect_server(
        self,
        force_restart: bool = False,
        skip_if_started_within: Any = None,
    ) -> dict[str, Any]:
        """Start the Spark Connect server.

        ``skip_if_started_within`` makes the non-rotating repair step
        idempotent rather than restarting on every call -- undocumented in
        the troubleshooting guide.
        ``berdl_notebook_utils.spark.start_spark_connect_server``.
        """
        return self._spark.start_spark_connect_server(
            force_restart=force_restart,
            skip_if_started_within=skip_if_started_within,
        )

    def stop_spark_connect_server(self, timeout: int = 10) -> bool:
        """``berdl_notebook_utils.spark.stop_spark_connect_server``."""
        return self._spark.stop_spark_connect_server(timeout=timeout)

    def spark_connect_status(self) -> dict[str, Any]:
        """``berdl_notebook_utils.spark.get_spark_connect_status``."""
        return self._spark.get_spark_connect_status()

    # -- berdl_notebook_utils.refresh -------------------------------------

    def rotate_credentials(self, *args: Any, **kwargs: Any) -> Any:
        """Rotate credentials via the ``refresh`` submodule path.

        **Rotating** -- same caution as :meth:`refresh_spark_environment`:
        never the first repair move, since it invalidates credentials in
        use elsewhere. Signature not harvested; arguments are passed
        through as given.
        ``berdl_notebook_utils.refresh.rotate_credentials``.
        """
        return self._refresh.rotate_credentials(*args, **kwargs)


class OffPodTransport(BerdlTransport):
    """Read-only REST transport, usable anywhere ``berdl_notebook_utils`` is
    not installed.

    Read-only **by construction**: this class defines no write method at
    all, so there is nothing to accidentally call off-pod that would fail
    (or worse, appear to work) without a Spark session. Off-pod write
    requests belong to ``BerdlCapability.load()``, which is expected to
    refuse with actionable pod guidance rather than being routed here.

    This module never imports ``berdl_notebook_utils`` -- not at module
    scope, not lazily, not anywhere -- because that package does not exist
    off-pod and this transport has no legitimate reason to need it.

    Credentials are resolved through
    :mod:`kbutillib.domains.kbase.berdl.tokens` (``KBASE_AUTH_TOKEN`` env
    var, then ``~/.kbase/token``, then a supplied config value), not
    through :class:`~kbutillib.domains.kbase.kb_berdl_utils.KBBERDLUtils`'s
    own resolution: that class looks up its bearer token under the
    ``"berdl"`` namespace, while the environment variable and the standard
    token file are stored under ``"kbase"`` -- the verified token-precedence
    gap ``tokens.py`` exists to fix. Rather than reimplement the REST calls,
    this transport constructs (or accepts) a ``KBBERDLUtils`` instance and
    seeds its ``"berdl"``-namespaced token directly from the resolved
    value, then delegates to it for the actual HTTP calls.

    **Known limitation.** At design time the REST surface
    (``KBBERDLUtils``'s ``databases/list`` endpoint) returned only tenant
    catalogs -- the personal (``my``) catalog did not appear. Off-pod
    discovery of personal tables through this transport must not be
    promised; see ``agent-io/prds/berdl-lakehouse-skills/fullprompt.md``
    ("Off-pod is currently unproven end to end").
    """

    LOCUS = "off_pod"

    def __init__(
        self,
        *,
        token: str | None = None,
        token_file: str | Path | None = DEFAULT_TOKEN_FILE,
        config_token: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        timeout: int | None = None,
        client: "KBBERDLUtils | None" = None,
    ) -> None:
        """Construct the transport.

        Args:
            token: A pre-resolved token, bypassing ``tokens.resolve_token``.
                Rarely needed -- prefer letting ``token_file``/
                ``config_token`` drive resolution.
            token_file: Passed to
                :func:`~kbutillib.domains.kbase.berdl.tokens.require_token`
                as the on-disk fallback source. Ignored if ``token`` is
                given.
            config_token: Passed to
                :func:`~kbutillib.domains.kbase.berdl.tokens.require_token`
                as the last-resort config-supplied value. Ignored if
                ``token`` is given.
            base_url: Overrides the ``KBBERDLUtils`` default BERDL API base
                URL. Ignored if ``client`` is given.
            api_path: Overrides the ``KBBERDLUtils`` default API path.
                Ignored if ``client`` is given.
            timeout: Overrides the ``KBBERDLUtils`` default request
                timeout, in seconds. Ignored if ``client`` is given.
            client: A pre-constructed ``KBBERDLUtils`` instance to reuse
                (e.g. one already carrying a caller-managed token or a
                non-default logger/config). When given, ``base_url``,
                ``api_path``, and ``timeout`` are ignored; its token is
                still overwritten with the value resolved here so that
                token precedence stays centralized in ``tokens.py``.
        """
        resolved_token = token or require_token(
            token_file=token_file, config_token=config_token
        )

        if client is None:
            from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils

            berdl_config: dict[str, Any] = {}
            if base_url is not None:
                berdl_config["base_url"] = base_url
            if api_path is not None:
                berdl_config["api_path"] = api_path
            if timeout is not None:
                berdl_config["timeout"] = timeout

            client = KBBERDLUtils(
                config={"berdl": berdl_config} if berdl_config else {},
                config_file=False,
                token_file=None,
                kbase_token_file=None,
            )

        # See the class docstring: KBBERDLUtils._get_headers() reads the
        # "berdl" namespace, which nothing else populates. Seed it
        # directly instead of relying on KBBERDLUtils's own resolution.
        client.set_token(resolved_token, namespace="berdl", save_file=False)
        self._client = client

    def databases(self) -> list[NormalizedDatabase]:
        """List reachable tenant-catalog databases, normalized.

        Delegates to ``KBBERDLUtils.get_database_list()``. Per the class
        docstring, the personal catalog is not known to appear in this
        result.
        """
        result = self._client.get_database_list()
        if not result.get("success"):
            raise RuntimeError(
                f"BERDL REST databases/list failed: {result.get('error')}"
            )
        return normalize_databases(result["databases"])

    def tables(self, database: str) -> list[str]:
        """List tables in ``database``.

        Delegates to ``KBBERDLUtils.get_database_tables()``.
        """
        result = self._client.get_database_tables(database)
        if not result.get("success"):
            raise RuntimeError(
                f"BERDL REST databases/tables/list failed for "
                f"{database!r}: {result.get('error')}"
            )
        return result["tables"]

    def table_schema(self, database: str, table: str) -> list[dict[str, Any]]:
        """Get the schema of ``table`` in ``database``.

        Delegates to ``KBBERDLUtils.get_table_columns()``.
        """
        result = self._client.get_table_columns(database, table)
        if not result.get("success"):
            raise RuntimeError(
                f"BERDL REST databases/tables/schema failed for "
                f"{database}.{table}: {result.get('error')}"
            )
        return result["columns"]

    def query(
        self,
        sql: str,
        limit: int | None = None,
        offset: int = 0,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run a read-only SQL query. Delegates to ``KBBERDLUtils.query()``.

        Read-only by platform design (the REST/Trino surface rejects
        writes), not by any enforcement in this method -- this is a thin
        passthrough of whatever SQL is given.
        """
        return self._client.query(sql, limit=limit, offset=offset, timeout=timeout)
