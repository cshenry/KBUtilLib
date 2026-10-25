"""The PUBLICATION BUNDLE: the per-campaign immutable artifact the publisher requires.

Runs on h100 alongside the export, NOT in the pod.

WHY ``council.*`` ALONE IS NOT ENOUGH. This is the whole justification, and
every field of the bundle exists because of one of these three:

1. ONE COUNCIL ITEM CAN FAN OUT TO SEVERAL ``query_id``s. Distinct
   ``query_id``s sharing one definition text are answered ONCE and fanned
   back out, so the clearinghouse key set is NOT derivable from
   ``council.items`` -- a text shared by three ``query_id``s is one item and
   three published rows. The bundle therefore carries the FULL
   ``query_id -> item_id`` map (see :func:`fanout_map`).
2. RETRIEVAL ABSTENTIONS HAVE NO COUNCIL ITEM AT ALL. The plan declines them
   before anything is submitted, so ``export_rows`` publishes them with
   ``council = null`` (``verdicts.py``'s abstain loop inside
   :func:`~.verdicts.export_rows`). The SUBMITTED set is therefore not
   derivable from the council projection either, which is why the bundle
   carries the abstentions WITH THEIR DECLINE REASONS and a submitted
   inventory.
3. LATER VERDICTS WIN BY DECLARED ORDER. The exporter resolves winners from
   an EXPLICIT ordered job list (``OntologyMappingUtils.council_export``
   puts the retry jobs LAST so their verdicts supersede the chunk jobs'):
   ``parent_job_id`` does not totally order siblings and does not separate
   independent campaigns. Twelve jobs declared ``published`` do not by
   themselves define a campaign OR a unique winner once a second published
   council overlaps the same functions. The bundle therefore carries the
   base and retry job ids IN THE ORDER THAT RESOLVES WINNERS, preserved
   EXACTLY as declared -- not sorted, not de-duplicated.

IMMUTABLE ONCE WRITTEN. A second write for the same ``campaign_id`` with
DIFFERENT content is a CONFLICT to reject, not an update
(:class:`BundleConflictError`). An identical rewrite is a no-op. The reason is
the clearinghouse, not tidiness: its slot key ``(entity_hash, entity_type,
result_type, source, parameter_set_hash)`` deliberately EXCLUDES
``result_type_version``, so a payload shape is frozen on first write and a
corrected payload in the same slot is silently dropped as already-present.
A bundle that could be edited after rows were published under it would
describe a campaign that no longer matches what is in the lake, with nothing
to detect the drift.

BOUND TO A STORE AND TO A COMPLETED SNAPSHOT. A bundle naming a snapshot that
is not COMPLETELY archived is REFUSABLE, and the completeness test is
``council_archive.require_complete_delivery`` -- THE one in the codebase.
There is deliberately no second completeness check here: two would be free to
disagree, and the failure that would permit (publishing against provenance
that reads complete and is not) is precisely what the archiver's load ledger
exists to prevent.

DISPOSITION IS NOT ENOUGH EITHER, IN THE OTHER DIRECTION. The publisher
requires BOTH that a job is BUNDLE-LISTED and that its disposition is
``published`` (:func:`require_publishable`). Bundle listing says "this job
belongs to this campaign and here is where it sits in the winner order";
disposition says "the project declared this job's output a result". Neither
implies the other.

PERSISTED TWICE, ON PURPOSE. On disk (:func:`write_bundle`), because the
delivery rail carries files; and in the lakehouse
(:func:`publish_bundle`), because a consumer with only lakehouse access must
be able to resolve a published row's campaign. The two are compared by
CONTENT DIGEST over the canonicalised whole, so "they agree" is a checkable
statement rather than a convention.

WHERE IT LANDS. :data:`PUBLICATION_BUNDLE_TABLE` in the council namespace,
registered with ``council_sink.register_table`` at import. It is deliberately
NOT a tenth entry in ``council_schema``: that module declares the ARCHIVE of
a council store, and this is a per-campaign artifact authored by the
publisher and derived from no snapshot. Adding it there would make "the
council archive" mean two things.

NOTHING HERE IS VERIFIED AGAINST A LIVE BERDL POD (KBUtilLib#91). Anything
needing the pod is AWAITING OPERATOR ACCEPTANCE.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ...kbase.berdl.council_sink import register_table

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "PUBLICATION_BUNDLE_TABLE",
    "BUNDLE_FILENAME",
    "BUNDLE_CONTENT_FIELDS",
    "PublicationBundleError",
    "BundleConflictError",
    "BundleDigestError",
    "BundleIncompleteSnapshotError",
    "JobNotInBundleError",
    "canonical_json",
    "content_digest",
    "build_bundle",
    "build_bundle_from_run",
    "write_bundle",
    "read_bundle",
    "publish_bundle",
    "read_lakehouse_bundle",
    "fanout_map",
    "bundle_query_ids",
    "abstentions",
    "ordered_job_ids",
    "require_bundle_listed",
    "require_publishable",
]

#: Bumped only on a BREAKING change to the bundle's content shape. The digest
#: covers the version, so a consumer that read a v1 bundle cannot be handed a
#: v2 one under the same digest.
BUNDLE_SCHEMA_VERSION = 1

#: Where the lakehouse copy lands, in the council namespace. See the module
#: docstring's "WHERE IT LANDS".
PUBLICATION_BUNDLE_TABLE = "publication_bundle"

#: The on-disk copy's filename inside a run directory.
BUNDLE_FILENAME = "BUNDLE.json"

#: Every field of a bundle's content, in canonical order. Declared once: the
#: digest is taken over this content and a second field list could disagree
#: with it, which would make two digests for one bundle.
BUNDLE_CONTENT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "campaign_id",
    "store_id",
    "snapshot_id",
    "query",
    "query_name",
    "query_version",
    "mapping_hash",
    "parameter_set",
    "parameter_set_hash",
    "plan_hash",
    "plan",
    "fanout",
    "abstentions",
    "submitted",
    "job_ids",
    "retry_job_ids",
    "ordered_job_ids",
    "written_at",
    "written_by",
)

#: The lakehouse table's columns. ``content`` holds the canonical JSON of the
#: whole bundle, exactly as the on-disk copy's ``content`` does, which is
#: what lets the two be compared by value rather than by convention.
_BUNDLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("campaign_id", "STRING"),
    ("store_id", "STRING"),
    ("snapshot_id", "STRING"),
    ("bundle_digest", "STRING"),
    ("schema_version", "INT"),
    ("query_name", "STRING"),
    ("query_version", "STRING"),
    ("mapping_hash", "STRING"),
    ("parameter_set_hash", "STRING"),
    ("plan_hash", "STRING"),
    ("content", "STRING"),
    ("written_at", "TIMESTAMP"),
    ("written_by", "STRING"),
)

register_table(PUBLICATION_BUNDLE_TABLE, _BUNDLE_COLUMNS)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class PublicationBundleError(RuntimeError):
    """A bundle could not be built, written or published."""


class BundleConflictError(PublicationBundleError):
    """A campaign's bundle already exists with DIFFERENT content.

    Refused, never updated. A bundle describes which jobs resolve which
    functions for a campaign whose rows may already be in the clearinghouse,
    and that slot is frozen on first write -- so an edited bundle would
    describe a campaign that no longer matches the lake, with nothing to
    detect the drift. The message names both digests.
    """


class BundleDigestError(PublicationBundleError):
    """A bundle's declared digest does not match its content.

    Either the file was edited after it was written or it was truncated in
    transit. In both cases the bundle is not the artifact its digest names,
    and a campaign resolved from it would be resolved from something nobody
    authored.
    """


class BundleIncompleteSnapshotError(PublicationBundleError):
    """The bundle names a snapshot that is not completely archived.

    Raised from ``council_archive.require_complete_delivery``'s refusal, so
    the message names which tables did not reconcile. Publishing against a
    partial archive produces rows whose ``council`` reference points into a
    namespace that cannot answer for them.
    """


class JobNotInBundleError(PublicationBundleError):
    """A job named for publication is not listed in the campaign's bundle.

    NAMES THE JOB ID. Being declared ``published`` is not enough: a job can
    be a genuine result of a DIFFERENT campaign, and publishing it under this
    one would attribute its verdicts to the wrong parameter set.
    """


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Canonicalisation and the digest
# --------------------------------------------------------------------------


def canonical_json(content: Mapping[str, Any]) -> str:
    """Render a bundle's content canonically: sorted keys, no whitespace slack.

    ``sort_keys`` sorts KEYS and never list ELEMENTS, which is what makes the
    declared job order survive canonicalisation. That is not incidental: the
    ordered job list is the thing that resolves winners, so a canonical form
    that sorted it would silently change which verdict wins.
    """
    return json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)


def content_digest(content: Mapping[str, Any]) -> str:
    """The sha256 over the canonicalised whole, EXCLUDING the digest field.

    A digest cannot cover itself. Everything else is covered, including
    ``written_at`` and ``written_by``: two bundles built at different times
    from the same plan are different artifacts, and a digest that ignored
    when and by whom could not tell them apart.
    """
    payload = {key: value for key, value in content.items() if key != "bundle_digest"}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def _split_query_ref(ref: str) -> tuple[str, str]:
    name, _, version = str(ref or "").partition("@")
    return name, version


def build_bundle(
    plan: Mapping[str, Any],
    *,
    campaign_id: str,
    store_id: str,
    snapshot_id: str,
    abstain: Iterable[Mapping[str, Any]],
    fanout: Iterable[Mapping[str, Any]],
    job_ids: Sequence[str],
    retry_job_ids: Sequence[str] = (),
    written_by: str = "",
    written_at: str | None = None,
) -> dict[str, Any]:
    """Build one campaign's bundle content from the plan and its two records.

    The ``abstain`` and ``fanout`` arguments take the PLAN'S OWN ROW SHAPES --
    the same ``abstain_file`` / ``fanout_file`` rows
    :func:`~.verdicts.export_rows` consumes. They are not re-derived here:
    the exporter and the bundle must agree on the submitted set exactly, and
    two readers of the same two files is the only way to get that.

    THE ORDERED JOB LIST IS BUILT THE WAY THE EXPORTER BUILDS IT: base jobs
    in their declared order, then the retry jobs LAST, so a retried item's
    verdict supersedes the chunk job's. The inputs are preserved verbatim
    alongside, so a reader can tell what was declared from what was resolved.

    Args:
        plan: ``PLAN.json``'s content, carried whole.
        campaign_id: The campaign this bundle is for. DECLARED, never
            derived -- not from the run directory, not from the parameter set
            hash. A parameter set does not separate independent campaigns
            (two councils can share one), and a derived id would silently
            merge them.
        store_id: The council store's DECLARED identity.
        snapshot_id: The VERIFIED digest of the snapshot this campaign's
            deliberation is archived under. Checked for completeness by
            :func:`publish_bundle`.
        abstain: The plan's abstain rows. Each contributes an abstention
            carrying its decline reason and NO item id.
        fanout: The plan's fanout rows -- one per un-run non-declined
            function, so one ``item_id`` legitimately appears under several
            ``query_id``s.
        job_ids: The base job ids, IN DECLARED ORDER.
        retry_job_ids: The retry-pass job ids, IN DECLARED ORDER.
        written_by: Who built this bundle.
        written_at: ISO-8601 timestamp. Defaults to now (UTC).

    Returns:
        The bundle content, with ``bundle_digest`` set.

    Raises:
        PublicationBundleError: ``campaign_id``, ``store_id`` or
            ``snapshot_id`` is blank; a fanout or abstain row carries no
            ``query_id``; or one ``query_id`` appears in BOTH partitions
            (they are disjoint by construction, so an overlap means the two
            files came from different plans and the campaign would claim two
            answers for one function).
    """
    for label, value in (
        ("campaign_id", campaign_id),
        ("store_id", store_id),
        ("snapshot_id", snapshot_id),
    ):
        if not str(value or "").strip():
            raise PublicationBundleError(
                f"build_bundle: {label} is required and is not derived from "
                "anything. A bundle that could not say which campaign, which "
                "store or which snapshot it belongs to cannot gate a publish."
            )

    query_ref = str(plan.get("query") or "")
    name, version = _split_query_ref(query_ref)

    seen: dict[str, str] = {}

    def _claim(query_id: str, partition: str) -> None:
        previous = seen.get(query_id)
        if previous is not None:
            raise PublicationBundleError(
                f"query_id {query_id[:12]}... appears in both the {previous} "
                f"and the {partition} record. The two partitions are "
                "disjoint by construction, so this means the two files came "
                "from different plans; rebuild against one plan rather than "
                "declaring two answers for one function."
            )
        seen[query_id] = partition

    declines: list[dict[str, Any]] = []
    for row in abstain or ():
        query_id = str(row.get("query_id") or "")
        if not query_id:
            raise PublicationBundleError(
                "an abstain row carries no query_id; query_id is THE join "
                f"key and cannot be reconstructed. Row keys: {sorted(row)}"
            )
        _claim(query_id, "abstain")
        reason = str(row.get("abstain_reason") or "")
        declines.append(
            {
                "query_id": query_id,
                "abstain_reason": reason,
                # The spelling export_rows publishes, used as written rather
                # than re-derived, so the bundle and the export agree.
                "decline_reason": str(
                    row.get("decline_reason") or f"abstained:{reason}"
                ),
                # NO item_id, and the key is present-and-null rather than
                # absent: a retrieval abstention has no council item, and
                # saying so explicitly is the point of carrying it here at
                # all (the submitted set is not derivable from the council
                # projection).
                "item_id": None,
            }
        )

    pairs: list[dict[str, str]] = []
    for row in fanout or ():
        query_id = str(row.get("query_id") or "")
        if not query_id:
            raise PublicationBundleError(
                "a fanout row carries no query_id; query_id is THE join key "
                f"and cannot be reconstructed. Row keys: {sorted(row)}"
            )
        item_id = str(row.get("item_id") or "")
        if not item_id:
            raise PublicationBundleError(
                f"fanout row for query_id {query_id[:12]}... carries no "
                "item_id. The fanout map is what makes the clearinghouse key "
                "set recoverable from the council record; a pair missing "
                "either half recovers nothing."
            )
        _claim(query_id, "fanout")
        pairs.append({"query_id": query_id, "item_id": item_id})

    base = [str(jid) for jid in job_ids or ()]
    retries = [str(jid) for jid in retry_job_ids or ()]
    # The exporter's own resolution order: retries LAST. Measured in that
    # run: 105 of the 113 items that failed on a truncated response came back
    # on a plain retry, and the retry's answer is the run's answer.
    retry_set = set(retries)
    ordered = [jid for jid in base if jid not in retry_set]
    ordered.extend(jid for jid in retries if jid not in set(ordered))

    content: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "campaign_id": str(campaign_id),
        "store_id": str(store_id),
        "snapshot_id": str(snapshot_id),
        "query": query_ref,
        "query_name": name,
        "query_version": version,
        "mapping_hash": str(plan.get("mapping_hash") or ""),
        # The CANONICAL parameter set, carried whole and INCLUDING
        # mapping_hash: the hash is what partitions the corpus, and a bundle
        # that named the set without it could not be checked against the
        # parameter_set row the publisher writes.
        "parameter_set": dict(plan.get("parameter_set") or {}),
        "parameter_set_hash": str(plan.get("parameter_set_hash") or ""),
        "plan_hash": str(plan.get("plan_hash") or ""),
        "plan": dict(plan),
        "fanout": pairs,
        "abstentions": declines,
        # SUBMITTED, not exported. Reconciliation has to be against what was
        # SENT, so that an item which was submitted and produced nothing is
        # visible as a gap rather than absent from both sides.
        "submitted": {
            "query_ids": [pair["query_id"] for pair in pairs]
            + [row["query_id"] for row in declines],
            "counts": {
                "fanout": len(pairs),
                "abstained": len(declines),
                "inputs": len(pairs) + len(declines),
                "items": len({pair["item_id"] for pair in pairs}),
            },
        },
        "job_ids": base,
        "retry_job_ids": retries,
        "ordered_job_ids": ordered,
        "written_at": written_at or _utcnow_iso(),
        "written_by": str(written_by or ""),
    }
    if set(content) != set(BUNDLE_CONTENT_FIELDS):
        raise PublicationBundleError(
            "build_bundle: the content does not match "
            f"BUNDLE_CONTENT_FIELDS. Extra: "
            f"{sorted(set(content) - set(BUNDLE_CONTENT_FIELDS))!r}; missing: "
            f"{sorted(set(BUNDLE_CONTENT_FIELDS) - set(content))!r}."
        )
    content["bundle_digest"] = content_digest(content)
    return content


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


def build_bundle_from_run(
    run_dir: Any,
    *,
    campaign_id: str,
    store_id: str,
    snapshot_id: str,
    job_ids: Sequence[str] | None = None,
    retry_job_ids: Sequence[str] | None = None,
    written_by: str = "",
) -> dict[str, Any]:
    """Build a bundle from a run directory's ``PLAN.json`` and its records.

    Reads the SAME two files the exporter reads -- the plan's
    ``abstain_file`` and ``fanout_file`` -- so the bundle's submitted set and
    the export's ``inputs`` cannot disagree.

    Raises:
        FileNotFoundError: no ``PLAN.json``, or the plan names a record file
            that is gone (``/scratch`` is ephemeral on this host and is not
            backed up).
        PublicationBundleError: the plan names no job ids and none were
            given.
    """
    from kbutillib.domains.ai.council.quote import PLAN_FILENAME

    directory = Path(run_dir).expanduser()
    plan_file = directory / PLAN_FILENAME
    if not plan_file.is_file():
        raise FileNotFoundError(
            f"no plan at {plan_file}. Stage 1 writes it: `kbu ontomap "
            "council-plan`. A bundle without it would have no parameter set, "
            "no mapping hash and no fanout."
        )
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    records: dict[str, list[dict[str, Any]]] = {}
    for key in ("abstain_file", "fanout_file"):
        named = str(plan.get(key) or "")
        if not named:
            raise PublicationBundleError(
                f"{plan_file}: the plan names no {key}. The submitted set is "
                "not derivable from the council projection -- abstentions "
                "have no council item and one item fans out to several "
                "query_ids -- so both records are required."
            )
        path = Path(named).expanduser()
        if not path.is_file():
            path = directory / Path(named).name
        if not path.is_file():
            raise FileNotFoundError(
                f"{plan_file} names {key}={named}, which is gone. /scratch is "
                "ephemeral on this host and is not backed up; re-run stage 1."
            )
        records[key] = _read_jsonl(path)

    resolved_jobs = list(job_ids) if job_ids is not None else [
        str(jid) for jid in (plan.get("job_ids") or ())
    ]
    resolved_retries = list(retry_job_ids) if retry_job_ids is not None else [
        str(jid) for jid in (plan.get("retry_job_ids") or ())
    ]
    if not resolved_jobs:
        raise PublicationBundleError(
            f"build_bundle_from_run: no council job ids for campaign "
            f"{campaign_id!r}. Pass job_ids= explicitly. A bundle listing no "
            "job would make every job unpublishable, which reads as a "
            "completed bundle."
        )
    return build_bundle(
        plan,
        campaign_id=campaign_id,
        store_id=store_id,
        snapshot_id=snapshot_id,
        abstain=records["abstain_file"],
        fanout=records["fanout_file"],
        job_ids=resolved_jobs,
        retry_job_ids=resolved_retries,
        written_by=written_by,
    )


# --------------------------------------------------------------------------
# The on-disk copy
# --------------------------------------------------------------------------


def write_bundle(bundle: Mapping[str, Any], path: Any) -> dict[str, Any]:
    """Write a bundle's CANONICAL form to *path*, atomically.

    The canonical form, not a pretty-printed one: the on-disk bytes are what
    the digest covers, so a reader can verify the file it has rather than
    trusting that a re-serialisation would have produced the same thing.

    REFUSES to overwrite an existing bundle with DIFFERENT content, for the
    same reason :func:`publish_bundle` does. An identical rewrite is allowed
    and is a no-op by content.

    Raises:
        BundleDigestError: the bundle's declared digest does not match it.
        BundleConflictError: *path* holds a different bundle.
    """
    declared = str(bundle.get("bundle_digest") or "")
    actual = content_digest(bundle)
    if declared != actual:
        raise BundleDigestError(
            f"refusing to write a bundle whose declared digest {declared!r} "
            f"does not match its content {actual!r}."
        )
    target = Path(path).expanduser()
    if target.is_file():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if str(existing.get("bundle_digest") or "") != actual:
            raise BundleConflictError(
                f"refusing to rewrite {target} for campaign "
                f"{bundle.get('campaign_id')!r}: it holds a bundle with "
                f"digest {existing.get('bundle_digest')!r} and this one is "
                f"{actual!r}. A bundle is immutable once written -- the "
                "clearinghouse slot its rows land in is frozen on first "
                "write, so an edited bundle would describe a campaign that "
                "no longer matches the lake."
            )
        return {"path": str(target), "bytes": target.stat().st_size, "written": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    text = canonical_json(bundle)
    partial = target.with_name(target.name + ".partial")
    with partial.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(target)  # atomic: a reader never sees a half-written bundle
    return {"path": str(target), "bytes": target.stat().st_size, "written": True}


def read_bundle(path: Any) -> dict[str, Any]:
    """Read a bundle from disk and VERIFY its digest.

    Raises:
        BundleDigestError: the file's declared digest does not match its
            content.
    """
    target = Path(path).expanduser()
    bundle = json.loads(target.read_text(encoding="utf-8"))
    declared = str(bundle.get("bundle_digest") or "")
    actual = content_digest(bundle)
    if declared != actual:
        raise BundleDigestError(
            f"{target}: declared bundle_digest {declared!r} but the content "
            f"hashes to {actual!r}. The file is not the artifact its digest "
            "names -- it was edited after it was written, or truncated in "
            "transit."
        )
    return bundle


# --------------------------------------------------------------------------
# The lakehouse copy
# --------------------------------------------------------------------------


def _bundle_row(bundle: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": str(bundle["campaign_id"]),
        "store_id": str(bundle["store_id"]),
        "snapshot_id": str(bundle["snapshot_id"]),
        "bundle_digest": str(bundle["bundle_digest"]),
        "schema_version": int(bundle["schema_version"]),
        "query_name": str(bundle.get("query_name") or ""),
        "query_version": str(bundle.get("query_version") or ""),
        "mapping_hash": str(bundle.get("mapping_hash") or ""),
        "parameter_set_hash": str(bundle.get("parameter_set_hash") or ""),
        "plan_hash": str(bundle.get("plan_hash") or ""),
        "content": canonical_json(bundle),
        "written_at": str(bundle.get("written_at") or ""),
        "written_by": str(bundle.get("written_by") or ""),
    }


def publish_bundle(
    sink: Any,
    bundle: Mapping[str, Any],
    *,
    archive_state_root: Any = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Write a bundle's lakehouse copy, after checking its snapshot is complete.

    THE SNAPSHOT CHECK IS NOT A SECOND IMPLEMENTATION. It delegates to
    ``council_archive.require_complete_delivery``, the one completeness test
    in this codebase, which reads the archiver's own load ledger. A bundle
    naming a snapshot whose delivery is incomplete is refused with the
    archiver's message, naming which tables did not reconcile.

    REFUSES a conflicting rewrite: a row for this ``campaign_id`` with a
    different ``bundle_digest`` is a :class:`BundleConflictError`. An
    identical re-publish is a no-op.

    Args:
        sink: A council sink (see
            :mod:`~kbutillib.domains.kbase.berdl.council_sink`). Bootstrapped
            here, so the bundle table exists regardless of import order.
        bundle: The bundle content, digest included.
        archive_state_root: Root of the archiver's delivery state. Passed
            through to the completeness check.
        require_complete: Leave this True. It exists so an operator can
            publish a bundle for a snapshot archived on another machine,
            where this machine holds no ledger to read -- and that is a
            DELIBERATE, named exception, not a default.

    Raises:
        BundleDigestError: the bundle's digest does not match its content.
        BundleIncompleteSnapshotError: the snapshot is not completely
            archived.
        BundleConflictError: a different bundle is already published for this
            campaign.
    """
    from ...kbase.berdl.council_archive import (
        DeliveryIncompleteError,
        require_complete_delivery,
    )

    declared = str(bundle.get("bundle_digest") or "")
    actual = content_digest(bundle)
    if declared != actual:
        raise BundleDigestError(
            f"refusing to publish a bundle whose declared digest "
            f"{declared!r} does not match its content {actual!r}."
        )

    completion: dict[str, Any] | None = None
    if require_complete:
        try:
            completion = require_complete_delivery(
                sink,
                store_id=str(bundle["store_id"]),
                snapshot_id=str(bundle["snapshot_id"]),
                state_root=archive_state_root,
            )
        except DeliveryIncompleteError as exc:
            raise BundleIncompleteSnapshotError(
                f"refusing to publish the bundle for campaign "
                f"{bundle.get('campaign_id')!r}: {exc}"
            ) from exc

    sink.bootstrap()
    existing = sink.select_rows(
        PUBLICATION_BUNDLE_TABLE,
        where={"campaign_id": str(bundle["campaign_id"])},
    )
    if existing:
        others = {str(row["bundle_digest"]) for row in existing}
        if others != {actual}:
            raise BundleConflictError(
                f"refusing to republish campaign "
                f"{bundle['campaign_id']!r}: the lakehouse already holds "
                f"bundle digest(s) {sorted(others)!r} and this one is "
                f"{actual!r}. A bundle is immutable once written; a second "
                "write with different content is a conflict, not an update."
            )
        return {
            "campaign_id": str(bundle["campaign_id"]),
            "bundle_digest": actual,
            "written": False,
            "snapshot_complete": bool((completion or {}).get("complete")),
        }
    sink.append_rows(PUBLICATION_BUNDLE_TABLE, [_bundle_row(bundle)])
    return {
        "campaign_id": str(bundle["campaign_id"]),
        "bundle_digest": actual,
        "written": True,
        "snapshot_complete": bool((completion or {}).get("complete")),
        "receipt": completion,
    }


def read_lakehouse_bundle(sink: Any, campaign_id: str) -> dict[str, Any] | None:
    """Read a campaign's bundle back OUT OF the lakehouse, digest verified.

    Returns the same content dict :func:`read_bundle` returns for the on-disk
    copy, which is what lets "the two copies agree" be a value comparison.

    Raises:
        BundleDigestError: the stored content does not hash to the stored
            digest.
    """
    rows = sink.select_rows(
        PUBLICATION_BUNDLE_TABLE, where={"campaign_id": str(campaign_id)}
    )
    if not rows:
        return None
    row = rows[0]
    bundle = json.loads(str(row["content"]))
    actual = content_digest(bundle)
    if actual != str(row["bundle_digest"]):
        raise BundleDigestError(
            f"campaign {campaign_id!r}: the stored content hashes to "
            f"{actual!r} but the row declares {row['bundle_digest']!r}."
        )
    return bundle


# --------------------------------------------------------------------------
# Reading a bundle
# --------------------------------------------------------------------------


def fanout_map(bundle: Mapping[str, Any]) -> dict[str, list[str]]:
    """``{item_id: [query_id, ...]}`` -- the full fanout, nothing collapsed.

    One item legitimately maps to SEVERAL ``query_id``s: distinct
    ``query_id``s sharing one definition text are answered once and fanned
    back out. This is the map that makes the clearinghouse key set
    recoverable, which ``council.items`` cannot do.
    """
    out: dict[str, list[str]] = {}
    for pair in bundle.get("fanout") or ():
        out.setdefault(str(pair["item_id"]), []).append(str(pair["query_id"]))
    return out


def bundle_query_ids(bundle: Mapping[str, Any]) -> list[str]:
    """Every ``query_id`` the campaign submitted -- fanout AND abstentions."""
    return [str(pair["query_id"]) for pair in bundle.get("fanout") or ()] + [
        str(row["query_id"]) for row in bundle.get("abstentions") or ()
    ]


def abstentions(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The retrieval abstentions, with decline reasons and no item ids."""
    return [dict(row) for row in bundle.get("abstentions") or ()]


def ordered_job_ids(bundle: Mapping[str, Any]) -> list[str]:
    """The job ids IN THE ORDER THAT RESOLVES WINNERS.

    Returned as declared: not sorted and not de-duplicated beyond the
    base/retry composition :func:`build_bundle` recorded. A later verdict
    wins by DECLARED ORDER, so re-ordering this list changes which answer is
    published.
    """
    return [str(jid) for jid in bundle.get("ordered_job_ids") or ()]


def require_bundle_listed(
    bundle: Mapping[str, Any], job_ids: Iterable[str]
) -> list[str]:
    """Return *job_ids* unchanged, or refuse, NAMING every job not in the bundle.

    Raises:
        JobNotInBundleError: a job is not listed in this campaign.
    """
    listed = set(ordered_job_ids(bundle))
    wanted = [str(jid) for jid in job_ids]
    missing = [jid for jid in wanted if jid not in listed]
    if missing:
        raise JobNotInBundleError(
            f"refusing to publish under campaign "
            f"{bundle.get('campaign_id')!r}: job(s) {missing!r} are not "
            f"listed in its bundle (it lists {sorted(listed)!r}). A job can "
            "be a genuine result of a DIFFERENT campaign; publishing it here "
            "would attribute its verdicts to the wrong parameter set."
        )
    return wanted


def require_publishable(
    sink: Any,
    bundle: Mapping[str, Any],
    job_ids: Iterable[str],
    *,
    snapshot_id: str | None = None,
) -> list[str]:
    """BOTH gates: bundle-listed AND declared ``published``.

    Neither implies the other, and this is the only function that says so in
    one place. Bundle listing answers "does this job belong to this campaign,
    and where in the winner order"; the disposition answers "did the project
    declare this job's output a result". A job can pass either and fail the
    other.

    Raises:
        JobNotInBundleError: a job is not in the campaign's bundle.
        kbutillib.domains.kbase.berdl.council_disposition.JobNotPublishableError:
            a job is not declared ``published`` (including one with no
            declaration at all).
    """
    from ...kbase.berdl.council_disposition import require_published

    wanted = require_bundle_listed(bundle, job_ids)
    return require_published(
        sink,
        wanted,
        store_id=str(bundle["store_id"]),
        snapshot_id=snapshot_id or str(bundle["snapshot_id"]),
    )
