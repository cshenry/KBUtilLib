"""Tests for ``InPodTransport``'s kwarg contract with the installed BERDL image.

THE DEFECT, in one sentence: the BERDL image dated 2026-10-19 removed
parameters that ``InPodTransport`` had always passed, so every
``BerdlCapability.query`` and every clearinghouse verb touching Spark raised
``TypeError`` before doing anything (KBUtilLib#91).

WHY THE FAKES HERE HAVE NO ``**kwargs``. The fix reads the installed helper's
signature and passes only what it accepts, so the ONLY thing that can prove it
is a fake that genuinely refuses an unaccepted name. A permissive fake -- one
declaring ``**kwargs`` -- accepts the broken call too and would pass against
the ORIGINAL, defective ``transports.py``. Every fake below is therefore a
plain function with the exact signature harvested from the image it stands for,
and the new-image tests fail with ``TypeError`` if the fix is reverted.

Two images are faked, because the fix must leave the old one untouched:

- NEW (2026-10-19): ``get_spark_session`` without ``delta_lake`` / ``use_hive``
  / ``tenant_name``; ``get_trino_connection`` without ``connector``.
- OLD (pre-2026-10-19): every parameter still present. Asserted by VALUE, not
  merely by call count, so a filter that started dropping kwargs on the old
  image would be caught.

``InPodTransport`` is constructed WITHOUT the pod the same way
``tests/berdl/test_capability.py``'s ``_FakeInPodTransport`` does it --
bypassing ``__init__``, which imports the pod-only package -- and only
``._bnu`` is supplied, since that is the attribute these two methods read.
"""

from __future__ import annotations

import types

import pytest

from kbutillib.domains.kbase.berdl import capability as capability_module
from kbutillib.domains.kbase.berdl.capability import BerdlCapability
from kbutillib.domains.kbase.berdl.transports import InPodTransport

THRESHOLD_KEY = "spark.sql.session.localRelationCacheThreshold"


class _FakeConf:
    """Records every ``spark.conf.set`` the transport performs."""

    def __init__(self) -> None:
        self.sets: list[tuple[str, object]] = []

    def set(self, key, value):
        self.sets.append((key, value))


class _FakeSession:
    """The object a faked ``get_spark_session`` hands back."""

    def __init__(self, received: dict) -> None:
        self.received = received
        self.conf = _FakeConf()
        self.statements: list[str] = []

    def sql(self, statement):
        self.statements.append(statement)
        return _FakeCollectable()


class _FakeCollectable:
    def collect(self):
        return []


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple] = []
        self.description = [("one",)]

    def execute(self, sql, *args):
        self.executed.append((sql, args))

    def fetchall(self):
        return [(1,)]


class _FakeConnection:
    def __init__(self, received: dict) -> None:
        self.received = received
        self._cursor = _FakeCursor()

    def cursor(self):
        return self._cursor


def _new_image() -> types.SimpleNamespace:
    """The 2026-10-19 image: no delta_lake, use_hive, tenant_name, connector.

    The signatures are written out in full rather than taken from a helper,
    because they ARE the contract under test.
    """
    calls: dict[str, list[dict]] = {"spark": [], "trino": []}

    def get_spark_session(
        app_name=None,
        local=False,
        scheduler_pool="default",
        use_s3=True,
        settings=None,
        use_spark_connect=True,
        override=None,
    ):
        received = dict(
            app_name=app_name,
            local=local,
            scheduler_pool=scheduler_pool,
            use_s3=use_s3,
            settings=settings,
            use_spark_connect=use_spark_connect,
            override=override,
        )
        calls["spark"].append(received)
        return _FakeSession(received)

    def get_trino_connection(host=None, port=None, settings=None):
        received = dict(host=host, port=port, settings=settings)
        calls["trino"].append(received)
        return _FakeConnection(received)

    return types.SimpleNamespace(
        get_spark_session=get_spark_session,
        get_trino_connection=get_trino_connection,
        calls=calls,
    )


def _old_image() -> types.SimpleNamespace:
    """The pre-2026-10-19 image: every parameter still accepted."""
    calls: dict[str, list[dict]] = {"spark": [], "trino": []}

    def get_spark_session(
        app_name=None,
        local=False,
        delta_lake=True,
        scheduler_pool="default",
        use_s3=True,
        use_hive=True,
        settings=None,
        tenant_name=None,
        use_spark_connect=True,
        override=None,
    ):
        received = dict(
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
        calls["spark"].append(received)
        return _FakeSession(received)

    def get_trino_connection(host=None, port=None, connector="delta_lake", settings=None):
        received = dict(host=host, port=port, connector=connector, settings=settings)
        calls["trino"].append(received)
        return _FakeConnection(received)

    return types.SimpleNamespace(
        get_spark_session=get_spark_session,
        get_trino_connection=get_trino_connection,
        calls=calls,
    )


def _transport(image) -> InPodTransport:
    """A real ``InPodTransport`` over a faked ``berdl_notebook_utils``.

    Bypasses ``__init__`` deliberately: it imports the pod-only package, and
    the two methods under test read only ``._bnu``.
    """
    transport = InPodTransport.__new__(InPodTransport)
    transport._bnu = image
    return transport


# -- the new image: the removed kwargs must not be passed -------------------


def test_new_image_spark_session_drops_the_removed_kwargs():
    """delta_lake and use_hive select catalogs the new image no longer has.

    Their value cannot change any outcome there, so they are dropped whatever
    it is -- and if they were still passed this call would be the production
    TypeError.
    """
    image = _new_image()
    session = _transport(image).spark_session(app_name="kbu")
    received = image.calls["spark"][0]
    assert "delta_lake" not in received and "use_hive" not in received
    assert "tenant_name" not in received
    assert received["app_name"] == "kbu"
    # Everything the new image DOES accept still arrives, at its default.
    assert received["local"] is False
    assert received["scheduler_pool"] == "default"
    assert received["use_s3"] is True
    assert received["use_spark_connect"] is True
    assert received["settings"] is None and received["override"] is None
    assert session.received is received


def test_new_image_spark_session_raises_the_local_relation_cache_threshold():
    """64 MiB on the session, or a real backfill dies in the AddArtifacts RPC.

    The server sets the threshold to 1 MiB, above which createDataFrame ships
    the relation as an artifact -- and that RPC fails deterministically.
    """
    image = _new_image()
    session = _transport(image).spark_session()
    assert (THRESHOLD_KEY, str(64 * 1024 * 1024)) in session.conf.sets
    assert (THRESHOLD_KEY, "67108864") in session.conf.sets


def test_new_image_spark_session_refuses_a_non_none_tenant_name():
    """A tenant has no equivalent in the new image, so it is never dropped.

    Dropping it would write to the personal catalog instead of the tenant's,
    which is a silent redirection of someone's data.
    """
    image = _new_image()
    with pytest.raises(ValueError, match="tenant_name"):
        _transport(image).spark_session(tenant_name="x")
    assert image.calls["spark"] == []


def test_new_image_spark_session_refusal_names_the_value_and_the_issue():
    image = _new_image()
    with pytest.raises(ValueError) as caught:
        _transport(image).spark_session(tenant_name="some_tenant")
    message = str(caught.value)
    assert "tenant_name='some_tenant'" in message
    assert "KBUtilLib#91" in message
    assert "InPodTransport.spark_session" in message


def test_new_image_spark_session_accepts_tenant_name_none_explicitly():
    """The OTHER side of the refusal: None is the droppable value."""
    image = _new_image()
    _transport(image).spark_session(tenant_name=None)
    assert len(image.calls["spark"]) == 1
    assert "tenant_name" not in image.calls["spark"][0]


def test_new_image_trino_connection_drops_connector_iceberg():
    """Iceberg-only image: 'iceberg' is what the call gets anyway."""
    image = _new_image()
    connection = _transport(image).trino_connection(connector="iceberg")
    received = image.calls["trino"][0]
    assert "connector" not in received
    assert received == {"host": None, "port": None, "settings": None}
    assert connection.received is received


def test_new_image_trino_connection_refuses_any_other_connector():
    """delta_lake would be silently upgraded to Iceberg -- refuse instead."""
    image = _new_image()
    with pytest.raises(ValueError) as caught:
        _transport(image).trino_connection(connector="delta_lake")
    message = str(caught.value)
    assert "connector='delta_lake'" in message
    assert "KBUtilLib#91" in message
    assert "InPodTransport.trino_connection" in message
    assert image.calls["trino"] == []


def test_new_image_trino_connection_passes_host_and_port_through():
    image = _new_image()
    _transport(image).trino_connection(connector="iceberg", host="h", port=8080)
    assert image.calls["trino"][0] == {"host": "h", "port": 8080, "settings": None}


# -- the old image: nothing changes -----------------------------------------


def test_old_image_spark_session_passes_every_kwarg_unchanged():
    """Byte-for-byte what it has always been, asserted by VALUE.

    If the filter ever started dropping on an image that accepts the name,
    this is what catches it.
    """
    image = _old_image()
    settings = object()
    override = object()
    _transport(image).spark_session(
        app_name="kbu",
        local=True,
        delta_lake=False,
        scheduler_pool="pool7",
        use_s3=False,
        use_hive=False,
        settings=settings,
        tenant_name="tenant7",
        use_spark_connect=False,
        override=override,
    )
    assert image.calls["spark"][0] == {
        "app_name": "kbu",
        "local": True,
        "delta_lake": False,
        "scheduler_pool": "pool7",
        "use_s3": False,
        "use_hive": False,
        "settings": settings,
        "tenant_name": "tenant7",
        "use_spark_connect": False,
        "override": override,
    }


def test_old_image_spark_session_defaults_are_unchanged_too():
    image = _old_image()
    _transport(image).spark_session()
    assert image.calls["spark"][0] == {
        "app_name": None,
        "local": False,
        "delta_lake": True,
        "scheduler_pool": "default",
        "use_s3": True,
        "use_hive": True,
        "settings": None,
        "tenant_name": None,
        "use_spark_connect": True,
        "override": None,
    }


def test_old_image_trino_connection_still_passes_connector():
    """Both values: the old image accepts the name, so NEITHER is refused."""
    image = _old_image()
    _transport(image).trino_connection(connector="iceberg", host="h", port=1)
    _transport(image).trino_connection(connector="delta_lake")
    assert image.calls["trino"] == [
        {"host": "h", "port": 1, "connector": "iceberg", "settings": None},
        {"host": None, "port": None, "connector": "delta_lake", "settings": None},
    ]


def test_old_image_accepts_a_tenant_name_it_can_honour():
    """The non-None tenant_name refused on the new image is fine here."""
    image = _old_image()
    _transport(image).spark_session(tenant_name="tenant7")
    assert image.calls["spark"][0]["tenant_name"] == "tenant7"


# -- through BerdlCapability, so the real caller is covered ------------------


def _capability(image) -> BerdlCapability:
    """A real ``BerdlCapability`` over a real ``InPodTransport``, pod faked.

    The transport is pre-seeded rather than monkeypatching locus detection:
    ``_get_transport`` returns an already-constructed transport untouched,
    and ``query``'s in-pod branch is an ``isinstance(transport,
    InPodTransport)`` test, which a real instance satisfies.
    """
    capability = BerdlCapability()
    capability._transport = _transport(image)
    return capability


def test_capability_query_trino_works_against_the_new_image():
    """capability.py's ``trino_connection(connector='iceberg')`` call site.

    This is the caller that broke in production: the capability picks the
    connector, so the transport-level test alone would not cover it.
    """
    image = _new_image()
    rows = _capability(image).query("SELECT 1")
    assert rows == [{"one": 1}]
    assert "connector" not in image.calls["trino"][0]


def test_capability_query_spark_works_against_the_new_image():
    image = _new_image()
    assert _capability(image).query("SELECT 1", engine="spark") == []
    received = image.calls["spark"][0]
    assert "delta_lake" not in received and "use_hive" not in received


def test_capability_spark_session_works_against_the_new_image(monkeypatch):
    """``BerdlCapability.spark_session()`` is the DataFrame-builder's entry.

    Unlike ``query``, this one gates on ``locus()`` before touching the
    transport, so locus detection is forced in-pod -- the same thing
    ``test_capability.py``'s ``force_in_pod`` fixture does -- rather than
    depending on whether the suite happens to run on the pod.
    """
    monkeypatch.setattr(
        capability_module, "berdl_notebook_utils_importable", lambda: True
    )
    image = _new_image()
    session = _capability(image).spark_session()
    assert (THRESHOLD_KEY, "67108864") in session.conf.sets


def test_capability_query_trino_still_passes_connector_on_the_old_image():
    image = _old_image()
    assert _capability(image).query("SELECT 1") == [{"one": 1}]
    assert image.calls["trino"][0]["connector"] == "iceberg"
