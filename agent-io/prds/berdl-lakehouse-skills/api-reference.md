# BERDL helper signatures — harvested reference

Captured by introspecting the **installed** packages in-pod on `kbhub`
(`jupyter-chenry`), 2026-08-01. This exists so the build does **not** need to run
on the pod: it carries the one thing the pod uniquely provides — real argument
shapes — off the pod.

The published guides show these as bare calls with no signatures. This file is
authoritative over the guides.

## `berdl_notebook_utils` (top level)

```
get_spark_session(app_name=None, local=False, delta_lake=True,
                  scheduler_pool='default', use_s3=True, use_hive=True,
                  settings=None, tenant_name=None, use_spark_connect=True,
                  override=None) -> SparkSession

create_namespace_if_not_exists(spark, namespace='default',
                               tenant_name=None, iceberg=True) -> str

get_databases(spark=None, use_hms=True, return_json=True,
              filter_by_namespace=True, tenant=None, settings=None,
              force_refresh=False) -> str | list[str]

get_tables(database, spark=None, use_hms=True, return_json=True,
           force_refresh=False) -> str | list[str]

get_table_schema(database, table, spark=None, return_json=True,
                 detailed=False, force_refresh=False) -> str | list | list[dict]

get_trino_connection(host=None, port=None, connector='delta_lake',
                     settings=None) -> trino.dbapi.Connection

table_exists(spark, table_name, namespace='default') -> bool
remove_table(spark, table_name, namespace='default') -> None
get_minio_client(settings=None) -> minio.api.Minio
get_s3_client(settings=None) -> minio.api.Minio
list_tenants(force_refresh=False) -> list[TenantSummaryResponse]
get_tenant_members(tenant_name) -> list[TenantMemberResponse]
```

## `berdl_notebook_utils.governance`

```
get_my_groups(force_refresh=False) -> UserGroupsResponse
list_available_groups() -> list[str]
request_tenant_access(tenant_name, permission='read_only',
                      justification=None) -> TenantAccessRequestResponse
get_credentials() -> CredentialsResponse
list_namespace_access(tenant_name=None, namespace=None) -> list[dict]
grant_namespace_access(tenant_name, username, namespace,
                       access_level='read', show_refresh_hint=True) -> dict
get_tenant_stewards(tenant_name) -> list[TenantStewardResponse]
get_my_workspace()
```

## `berdl_notebook_utils.spark`

```
start_spark_connect_server(force_restart=False,
                           skip_if_started_within=None) -> dict
stop_spark_connect_server(timeout=10) -> bool
get_spark_connect_status() -> dict
```

## `data_lakehouse_ingest`

```
ingest(config: str | dict, spark=None, logger=None,
       minio_client=None, dataframes=None) -> dict
```

---

## Traps these signatures expose

These are not visible in any published guide and must be encoded in the skills.

1. **`get_trino_connection` defaults to `connector='delta_lake'`, not Iceberg.**
   The Trino guide never mentions this parameter at all. Taking the default
   silently connects through the legacy Delta connector. `berdl-query` must pass
   the connector explicitly and must not rely on the default.

2. **`get_databases`, `get_tables` and `get_table_schema` all default to
   `return_json=True` and return a JSON *string*, not a list.** The migration
   guide flags this for `get_tables` only; it applies to all three. Any code that
   iterates the result without passing `return_json=False` will iterate
   characters of a string.

3. **`get_my_groups()` returns a `UserGroupsResponse` object, not a list** —
   membership is on `.groups`. By contrast `list_available_groups()` returns a
   plain `list[str]`. The membership decoder consumes both and must not assume a
   uniform shape.

4. **`table_exists(spark, table_name, namespace='default')` takes the namespace
   as a separate argument**, not as part of a dotted table name. Passing a fully
   qualified name as `table_name` will not behave as expected.

5. **`create_namespace_if_not_exists` already defaults to `iceberg=True`.** No
   flag is needed for the Iceberg path; `iceberg=False` is the legacy opt-out.

6. **`start_spark_connect_server` accepts `skip_if_started_within`**, which the
   troubleshooting guide never mentions — useful for making the non-rotating
   repair idempotent rather than restarting on every call.
