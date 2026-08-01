"""``BerdlCapability`` -- the deep module over the BERDL transports.

Per ``agent-io/prds/berdl-lakehouse-skills/fullprompt.md`` ("Modules to
build"), this is a small, stable interface over substantial behavior:

- :meth:`BerdlCapability.locus` -- ``'in_pod'`` or ``'off_pod'``.
- :meth:`BerdlCapability.databases` -- normalized, deduplicated database
  list.
- :meth:`BerdlCapability.memberships` -- ``{tenant: 'rw' | 'ro'}``.
- :meth:`BerdlCapability.load` -- in-pod only; raises off-pod with
  actionable guidance.
- :meth:`BerdlCapability.query` -- routed to Trino, Spark, or REST by
  locus and intent.

Locus detection tests **importability** of the pod-only
``berdl_notebook_utils`` package (see :func:`berdl_notebook_utils_importable`),
not only the pod's environment variables -- the variables can be set
without the package being installed, and checking only those would
misclassify that case as in-pod (fullprompt.md, "Locus model").

All writes route through ``data_lakehouse_ingest.ingest``, never a raw
``writeTo``, because ``ingest`` applies schema enforcement in both of its
source modes (fullprompt.md, "Write path"). This module never calls
``writeTo`` directly and never imports ``data_lakehouse_ingest`` at module
scope -- that package is pod-only, deferred to inside :meth:`load`, mirroring
how ``transports.py`` defers ``berdl_notebook_utils``.

The pure config-building and mode-selection logic
(:func:`build_ingest_config`, :func:`select_write_mode`) is factored out as
free functions specifically so it can be unit-tested without a live pod,
a Spark session, or any network access -- see the PRD's "Testing
Decisions" section.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Literal, Mapping, MutableMapping, Sequence

from .membership import PermissionLevel, decode_memberships
from .naming import NormalizedDatabase
from .transports import BerdlTransport, InPodTransport, OffPodTransport

Locus = Literal["in_pod", "off_pod"]

#: Machine where in-pod writes (and full read/write locus) must run.
POD_MACHINE = "kbhub"

#: The only two write modes ``data_lakehouse_ingest.ingest`` accepts.
_VALID_WRITE_MODES = ("overwrite", "append")


def berdl_notebook_utils_importable() -> bool:
    """Test whether the pod-only ``berdl_notebook_utils`` package is importable.

    Per the PRD ("Locus model"), locus detection must test *importability*
    of the platform package, not only environment variables: the pod's
    environment variables (``KBASE_AUTH_TOKEN``, ``SPARK_CONNECT_URL``,
    ``S3_ACCESS_KEY``) can be present without the package actually being
    installed, and a variables-only check would misclassify that case as
    in-pod.

    Uses :func:`importlib.util.find_spec`, which *locates* the package
    without importing (and therefore without executing) it -- unlike
    ``InPodTransport.__init__``'s real import, this check has no side
    effects, makes no network calls, and is safe to call off-pod in
    ordinary CI.

    Returns:
        ``True`` if ``berdl_notebook_utils`` can be found on the import
        path, ``False`` otherwise (including when a broken parent package
        makes the lookup itself fail).
    """
    try:
        return importlib.util.find_spec("berdl_notebook_utils") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        # find_spec can raise if a parent package is malformed; that is
        # not importable either, just via a different failure mode.
        return False


class BerdlLoadRefusedError(RuntimeError):
    """Raised when :meth:`BerdlCapability.load` is refused off-pod.

    Per the PRD ("Off-pod write refusal"), the message must name: the
    reason (writes require a Spark session, which exists only in the
    pod), the target machine (:data:`POD_MACHINE`), and the exact command
    to run there. Raising this must be the *only* effect of an off-pod
    ``load()`` call -- no data is staged, no artifact is generated, and no
    work is dispatched.
    """


class BerdlMembershipUnavailableError(RuntimeError):
    """Raised when tenant membership cannot be determined.

    Membership decoding (:mod:`.membership`) needs ``get_my_groups()`` and
    ``list_available_groups()``, both governance calls only
    :class:`~.transports.InPodTransport` exposes. :class:`~.transports.OffPodTransport`
    has no governance surface, so :meth:`BerdlCapability.memberships` raises
    this off-pod rather than guessing or silently returning an empty
    mapping.
    """


def build_ingest_config(
    dataset: str,
    tables: Sequence[Mapping[str, Any]],
    *,
    dataframes: Mapping[str, Any] | None = None,
    paths: Mapping[str, Any] | None = None,
    tenant: str | None = None,
    is_tenant: bool | None = None,
    pipeline_name: str | None = None,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``data_lakehouse_ingest.ingest`` config dict.

    Two source modes, selected by whether ``dataframes`` is given (PRD,
    "Write path"):

    - **DataFrame mode** (``dataframes`` truthy) -- the frames are passed
      to ``ingest(config, dataframes=...)`` as a *separate* argument, not
      embedded in the config. This short-circuits the bronze read
      entirely, so no MinIO staging happens and the returned config MUST
      NOT contain a ``'paths'`` key -- even if the caller also passed
      ``paths``, it is dropped, because nothing will ever stage to it.
    - **Bronze mode** (``dataframes`` falsy) -- files are read from S3 or
      local storage. ``paths`` MUST be given and MUST contain
      ``'bronze_base'``; each table's own ``'bronze_path'``/``'format'``
      are expected to already be present in that table's dict (not
      validated here -- see :meth:`BerdlCapability.load`, which builds
      those per-table dicts).

    Args:
        dataset: The top-level ``'dataset'`` key. Required.
        tables: Per-table config dicts. Each must at minimum have
            ``'name'``; whichever of ``'comment'``, ``'schema'``/
            ``'schema_sql'``, ``'enabled'``, ``'bronze_path'``,
            ``'format'``, ``'mode'``, ``'partition_by'`` apply are passed
            through unchanged.
        dataframes: DataFrame-mode frames keyed by table name. Selects
            DataFrame mode when truthy. Not embedded in the returned
            config -- callers pass this same mapping to
            ``ingest(config, dataframes=dataframes)`` directly.
        paths: Bronze-mode ``'paths'`` section (``bronze_base`` required,
            ``silver_base`` optional). Ignored entirely in DataFrame mode.
        tenant: Optional top-level ``'tenant'`` key.
        is_tenant: Optional top-level ``'is_tenant'`` key.
        pipeline_name: Optional top-level ``'pipeline_name'`` key.
        defaults: Optional top-level ``'defaults'`` key.

    Returns:
        The config dict to pass as ``ingest(config, ...)``.

    Raises:
        ValueError: If ``dataset`` is empty, ``tables`` is empty, any
            table entry lacks a ``'name'``, or bronze mode is selected
            (``dataframes`` falsy) without a ``'bronze_base'`` in
            ``paths``.
    """
    if not dataset:
        raise ValueError("build_ingest_config: 'dataset' is required.")
    if not tables:
        raise ValueError("build_ingest_config: at least one table is required.")
    for table in tables:
        if not table.get("name"):
            raise ValueError(
                f"build_ingest_config: table entry missing 'name': {table!r}"
            )

    config: dict[str, Any] = {
        "dataset": dataset,
        "tables": [dict(table) for table in tables],
    }
    if tenant is not None:
        config["tenant"] = tenant
    if is_tenant is not None:
        config["is_tenant"] = is_tenant
    if pipeline_name is not None:
        config["pipeline_name"] = pipeline_name
    if defaults is not None:
        config["defaults"] = dict(defaults)

    if dataframes:
        # DataFrame mode: the bronze read is short-circuited entirely, so
        # 'paths' MUST be omitted -- never included, regardless of what
        # the caller passed.
        return config

    # Bronze mode: paths.bronze_base is required.
    if not paths or not paths.get("bronze_base"):
        raise ValueError(
            "build_ingest_config: bronze mode (no 'dataframes' given) "
            "requires 'paths' with a 'bronze_base'."
        )
    config["paths"] = dict(paths)
    return config


def select_write_mode(requested_mode: str, *, table_exists: bool) -> str:
    """Resolve the effective ``ingest`` write mode for one table.

    Per the PRD ("Write semantics"): mode is only ``'overwrite'`` or
    ``'append'``; appending to a non-existent table raises ``ValueError``
    inside ``ingest`` itself. A re-runnable loader must therefore detect
    table existence *before* calling ``ingest`` and choose ``'overwrite'``
    for first creation, so the first execution of a load does not fail.

    Args:
        requested_mode: The caller's requested mode, ``'overwrite'`` or
            ``'append'``.
        table_exists: Whether the target table already exists.

    Returns:
        ``'overwrite'`` when ``requested_mode == 'append'`` and the table
        does not exist yet (the first-creation case); ``requested_mode``
        unchanged in every other case.

    Raises:
        ValueError: If ``requested_mode`` is not ``'overwrite'`` or
            ``'append'``.
    """
    if requested_mode not in _VALID_WRITE_MODES:
        raise ValueError(
            "select_write_mode: mode must be one of "
            f"{_VALID_WRITE_MODES}, got {requested_mode!r}."
        )
    if requested_mode == "append" and not table_exists:
        return "overwrite"
    return requested_mode


def _normalize_partition_by(value: str | Sequence[str] | None) -> list[str] | None:
    """Normalize ``partition_by`` to a list, accepting a bare string too.

    Per the PRD ("Write semantics"): ``partition_by`` accepts a string or
    a list.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


class BerdlCapability:
    """Locus-aware BERDL capability: databases, memberships, load, query.

    Selects :class:`~.transports.InPodTransport` or
    :class:`~.transports.OffPodTransport` based on :meth:`locus`, and
    exposes one small surface over both. The transport is constructed
    lazily, on first use, not in ``__init__`` -- so simply constructing a
    ``BerdlCapability`` never requires the pod package or a network call.

    Args:
        off_pod_kwargs: Keyword arguments forwarded to
            :class:`~.transports.OffPodTransport` when off-pod (``token``,
            ``token_file``, ``config_token``, ``base_url``, ``api_path``,
            ``timeout``, ``client``). Ignored in-pod.
    """

    def __init__(self, *, off_pod_kwargs: Mapping[str, Any] | None = None) -> None:
        self._off_pod_kwargs: dict[str, Any] = (
            dict(off_pod_kwargs) if off_pod_kwargs else {}
        )
        self._transport: BerdlTransport | None = None

    def locus(self) -> Locus:
        """Detect the current execution locus.

        Tests importability of ``berdl_notebook_utils`` (see
        :func:`berdl_notebook_utils_importable`) -- never only environment
        variables.

        Returns:
            ``'in_pod'`` if ``berdl_notebook_utils`` is importable,
            ``'off_pod'`` otherwise.
        """
        return "in_pod" if berdl_notebook_utils_importable() else "off_pod"

    def _get_transport(self) -> BerdlTransport:
        """Return the locus-appropriate transport, constructing it once."""
        if self._transport is None:
            if self.locus() == "in_pod":
                self._transport = InPodTransport()
            else:
                self._transport = OffPodTransport(**self._off_pod_kwargs)
        return self._transport

    def databases(self) -> list[NormalizedDatabase]:
        """List reachable databases, normalized and deduplicated.

        Delegates to the current transport's ``databases()``, which both
        transports already return through
        :func:`kbutillib.domains.kbase.berdl.naming.normalize_databases`.
        """
        return self._get_transport().databases()

    def memberships(self) -> dict[str, PermissionLevel]:
        """Return ``{tenant: 'rw' | 'ro'}`` for the current user.

        In-pod only: decodes ``InPodTransport.my_groups().groups`` against
        ``InPodTransport.available_groups()`` via
        :func:`kutillib.domains.kbase.berdl.membership.decode_memberships`.

        The locus check happens *before* any transport is constructed, so
        this raises off-pod even when no off-pod token is configured --
        it never reaches (and does not depend on) off-pod token
        resolution.

        Raises:
            BerdlMembershipUnavailableError: Off-pod, where
                :class:`~.transports.OffPodTransport` has no governance
                surface to decode membership from.
        """
        if self.locus() != "in_pod":
            raise BerdlMembershipUnavailableError(
                "memberships() requires governance access "
                "('get_my_groups'/'list_available_groups'), which only "
                f"exists in-pod. This process is off-pod; check membership "
                f"from inside the pod ({POD_MACHINE}) instead."
            )
        transport = self._get_transport()
        assert isinstance(
            transport, InPodTransport
        )  # guaranteed by the locus check above
        my_groups = transport.my_groups().groups
        available_groups = transport.available_groups()
        return decode_memberships(my_groups, available_groups)

    def load(
        self,
        *,
        dataset: str,
        tables: Sequence[MutableMapping[str, Any]],
        tenant: str | None = None,
        is_tenant: bool | None = None,
        pipeline_name: str | None = None,
        dataframes: Mapping[str, Any] | None = None,
        paths: Mapping[str, Any] | None = None,
        defaults: Mapping[str, Any] | None = None,
        namespace: str = "default",
        spark: Any = None,
        minio_client: Any = None,
        logger: Any = None,
    ) -> dict[str, Any]:
        """Load one or more tables via ``data_lakehouse_ingest.ingest``.

        In-pod only -- see :class:`BerdlLoadRefusedError`. Never calls a
        raw ``writeTo``: every write, in either source mode, routes
        through ``data_lakehouse_ingest.ingest`` (PRD, "Write path"),
        because that is what applies schema enforcement in both modes.

        Preflight (before any staging or writing): resolve locus and
        refuse off-pod; confirm read-write membership on ``tenant`` (or
        ``dataset`` when ``tenant`` is not given); resolve, for each
        table, whether it exists and whether the operation will create,
        append, or replace it (via :func:`select_write_mode`).

        Postflight: verify each table by row count and by reading its
        Iceberg snapshot history, and report the new snapshot.

        Args:
            dataset: The ``ingest`` config's top-level ``'dataset'``.
            tables: One dict per table. Each must have ``'name'`` and a
                requested ``'mode'`` (``'overwrite'`` or ``'append'``,
                default ``'append'``). DataFrame mode additionally
                requires the table's name to be a key of ``dataframes``.
                Bronze mode additionally requires ``'bronze_path'`` and
                ``'format'`` on the table dict. ``'partition_by'`` may be
                a string or a list. Table dicts are not mutated in place;
                :meth:`load` builds new dicts for the ``ingest`` config.
            tenant: Target tenant, used for the membership check and
                passed through as the config's ``'tenant'``.
            is_tenant: Passed through as the config's ``'is_tenant'``.
            pipeline_name: Passed through as the config's
                ``'pipeline_name'``.
            dataframes: DataFrame-mode frames keyed by table name.
                Presence selects DataFrame mode for the whole call.
            paths: Bronze-mode ``'paths'`` section (``'bronze_base'``
                required). Required when ``dataframes`` is not given.
            defaults: Passed through as the config's ``'defaults'``.
            namespace: The Iceberg namespace tables live in, used for the
                existence check.
            spark: An existing Spark session. When not given, one is
                obtained from the in-pod transport.
            minio_client: Forwarded to ``ingest`` (bronze mode).
            logger: Forwarded to ``ingest``.

        Returns:
            A report dict with the ``ingest`` result plus, per table: the
            resolved write mode, whether it existed beforehand, and (when
            obtainable) the post-load row count and new snapshot id.

        Raises:
            BerdlLoadRefusedError: Off-pod. No data is staged, no artifact
                is generated, and no work is dispatched.
            PermissionError: When the caller does not hold read-write
                membership on the target tenant.
            ValueError: Invalid table/config shape -- see
                :func:`build_ingest_config` and :func:`select_write_mode`.
        """
        locus = self.locus()
        if locus != "in_pod":
            raise BerdlLoadRefusedError(
                "BerdlCapability.load() requires a Spark session, and Spark "
                f"only exists inside the BERDL JupyterHub pod ({POD_MACHINE}). "
                "This process is off-pod ('berdl_notebook_utils' is not "
                "importable here), so nothing has been staged and no write "
                "has been attempted. Run this load from inside the pod "
                f"instead, e.g. from a {POD_MACHINE} notebook or terminal:\n"
                '  python -c "from kbutillib.domains.kbase.berdl.capability '
                "import BerdlCapability; "
                'BerdlCapability().load(dataset=%r, tables=[...])"' % dataset
            )

        transport = self._get_transport()
        assert isinstance(transport, InPodTransport)  # guaranteed by locus == 'in_pod'

        # -- Preflight: membership -----------------------------------
        target_tenant = tenant or dataset
        memberships = self.memberships()
        permission = memberships.get(target_tenant)
        if permission != "rw":
            raise PermissionError(
                f"BerdlCapability.load() refused: no read-write membership on "
                f"tenant {target_tenant!r} (current permission: "
                f"{permission or 'none'!r}). Request read-write access before "
                "retrying; this is an asynchronous human approval step."
            )

        load_spark = spark if spark is not None else transport.spark_session()

        # -- Preflight: per-table existence + mode resolution ---------
        resolved_tables: list[dict[str, Any]] = []
        table_reports: list[dict[str, Any]] = []
        for raw_table in tables:
            table = dict(raw_table)
            name = table["name"]
            requested_mode = table.get("mode", "append")
            exists = transport.table_exists(load_spark, name, namespace=namespace)
            effective_mode = select_write_mode(requested_mode, table_exists=exists)
            table["mode"] = effective_mode
            if "partition_by" in table:
                table["partition_by"] = _normalize_partition_by(table["partition_by"])

            resolved_tables.append(table)
            table_reports.append(
                {
                    "name": name,
                    "existed_before": exists,
                    "requested_mode": requested_mode,
                    "effective_mode": effective_mode,
                    "operation": "create"
                    if not exists
                    else ("replace" if effective_mode == "overwrite" else "append"),
                }
            )

        config = build_ingest_config(
            dataset,
            resolved_tables,
            dataframes=dataframes,
            paths=paths,
            tenant=tenant,
            is_tenant=is_tenant,
            pipeline_name=pipeline_name,
            defaults=defaults,
        )

        # data_lakehouse_ingest is pod-only; deferred import mirrors
        # transports.py's deferral of berdl_notebook_utils.
        from data_lakehouse_ingest import ingest  # noqa: PLC0415

        result = ingest(
            config,
            spark=load_spark,
            logger=logger,
            minio_client=minio_client,
            dataframes=dataframes,
        )

        # -- Postflight: verify by row count and snapshot history -----
        for report in table_reports:
            fqn = f"`{namespace}`.`{report['name']}`"
            try:
                count_rows = load_spark.sql(
                    f"SELECT COUNT(*) AS n FROM {fqn}"
                ).collect()
                report["row_count"] = count_rows[0]["n"] if count_rows else None
            except Exception as exc:  # pragma: no cover - requires live pod
                report["row_count"] = None
                report["row_count_error"] = str(exc)
            try:
                history_rows = load_spark.sql(
                    f"SELECT * FROM {fqn}.history ORDER BY made_current_at DESC LIMIT 1"
                ).collect()
                report["new_snapshot_id"] = (
                    history_rows[0]["snapshot_id"] if history_rows else None
                )
            except Exception as exc:  # pragma: no cover - requires live pod
                report["new_snapshot_id"] = None
                report["snapshot_history_error"] = str(exc)

        return {"ingest_result": result, "tables": table_reports}

    def query(self, sql: str, **kwargs: Any) -> Any:
        """Run a SQL query, routed by locus.

        In-pod: routed to Trino by default (interactive reads,
        cross-catalog joins) using the ``'iceberg'`` connector explicitly
        -- ``get_trino_connection`` defaults to the legacy
        ``'delta_lake'`` connector, which this deliberately does not rely
        on. Pass ``engine='spark'`` to route through the Spark session
        instead (needed for anything beyond a plain read, e.g. schema
        evolution or time-travel syntax that Trino does not support).

        Off-pod: routed to :meth:`~.transports.OffPodTransport.query`
        (REST), which is read-only by construction.

        Args:
            sql: The SQL text to run.
            **kwargs: In-pod with the default Trino engine: forwarded to
                the DB-API cursor's ``execute``/fetch is not applicable
                here -- unused. In-pod with ``engine='spark'``: unused.
                Off-pod: forwarded to
                :meth:`~.transports.OffPodTransport.query` (``limit``,
                ``offset``, ``timeout``).

        Returns:
            In-pod/Trino: a list of result rows. In-pod/Spark: the
            collected Spark ``Row`` list. Off-pod: the REST client's
            result dict.
        """
        transport = self._get_transport()
        if isinstance(transport, InPodTransport):
            engine = kwargs.pop("engine", "trino")
            if engine == "spark":
                return transport.spark_session().sql(sql).collect()
            connection = transport.trino_connection(connector="iceberg")
            cursor = connection.cursor()
            cursor.execute(sql)
            return cursor.fetchall()
        return transport.query(sql, **kwargs)
