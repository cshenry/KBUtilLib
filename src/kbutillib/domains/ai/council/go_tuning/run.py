"""ONE ISOLATED STORE PER LOGICALLY INDEPENDENT RUN (C14).

The engine's reuse key is a content address of the model invocation -- the
canonical model, the endpoint kind, the reuse namespace, the effective
generation params, the answer schema, the query ``name@version``, the
renderer id and the rendered system/user text (``types.prompt_hash``).  The
JOB ID IS NOT IN IT, and it must not be: cross-job, cross-user reuse is the
whole reason the store keeps answered calls at all.

That is correct for production and fatal for measurement.  Two ``go_mf@4``
runs over the same items in one store are not two measurements of ``go_mf@4``
-- the second one reads the first one's answers back out of ``calls`` and
reports them as its own.  Every number a tuning loop computes from such a
pair (the noise floor above all, since it is literally "how much do two
identical baselines disagree") collapses to zero, and nothing in the job's
status, usage or verdicts says so.

So each of these gets its OWN sqlite file, with nothing in it but the spec
and that one job:

* the two iteration-0 baselines, whose disagreement IS the noise floor,
* each iteration's fresh ``go_mf@4``,
* each candidate,
* each tuning-parent run.

:func:`fresh_run` is the only way this package starts one.  It creates the
store, copies the spec in from the loop store, submits, works the job, and
then CHECKS the claim rather than asserting it: a run with any call carrying
``reused_from`` is rejected as not fresh (see :func:`reused_calls`).  The
check reads the store, not a log line, because the reuse path is quiet --
``_settle_reused`` writes the source call id into ``calls.reused_from`` and
files the attempt with ``outcome_kind='reused'`` and zero usage, and the job
settles ``ok`` either way.

WHERE THE STORE GOES IS THE CALLER'S, AND A REFUSAL IS A REFUSAL
================================================================
*run_dir* comes from the caller and nothing here moves it.  The store refuses
two locations outright -- a path with a ``Dropbox`` component, and a path on a
network filesystem (NFS, CIFS, sshfs: SQLite cannot trust their locks) -- and
on h100 both the home directory and the Dropbox tree are exactly those cases,
which is why every council store on that machine lives under ``/scratch``.
:func:`fresh_run` turns that refusal into a :class:`~.conflicts.Refused`
naming the directory it was given and does NOT pick a different directory:
silently relocating would scatter a run's stores across two filesystems and
hide the one fact the operator needs to fix.

A STORE FILE THAT ALREADY EXISTS IS A REFUSAL TOO
=================================================
The isolation claim is "nothing in this store but this job", and the cheapest
way to break it is to re-run a command and land on yesterday's file.  So the
store path must not exist: :func:`fresh_run` refuses rather than opening it,
because opening it is indistinguishable from the thing C14 exists to prevent.
Give the run a different *label* (they are named ``<label>.sqlite`` under
``run_dir/stores/``) and the previous measurement stays on disk where it can
still be read.

Nothing here opens a network connection by itself; what the submitted job
costs is the backends' business, and a test supplies its own.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..types import canonical_json
from .conflicts import Refused

__all__ = [
    "STORES_DIR",
    "FreshRun",
    "fresh_run",
    "go_mf_tasks",
    "isolated_store",
    "reused_calls",
    "store_path_for",
]

#: Where :func:`fresh_run` puts its stores, under the run directory.
STORES_DIR = "stores"


class FreshRun(Dict[str, Any]):
    """The report :func:`fresh_run` returns -- a plain dict, with accessors.

    A dict because it is written straight into a ledger record and printed by
    the CLI, and the accessors exist so the scorer can read the two fields it
    actually branches on without stringly-typed lookups.
    """

    @property
    def store(self) -> str:
        return str(self.get("store") or "")

    @property
    def job_id(self) -> str:
        return str(self.get("job_id") or "")

    @property
    def fresh(self) -> bool:
        return bool(self.get("fresh"))


def store_path_for(run_dir: Any, label: str) -> Path:
    """``run_dir/stores/<label>.sqlite`` -- where a run's isolated store goes.

    Exposed so a caller (or a test) can say where a run WILL live without
    creating it, and so the ledger record and the filename cannot drift apart.
    """
    clean = str(label or "").strip()
    if not clean:
        raise Refused("a fresh run needs a label; it names the store file")
    if "/" in clean or clean in (".", ".."):
        raise Refused(
            f"run label {clean!r} is not a usable filename component; a label "
            "names one store file inside the run directory"
        )
    return Path(run_dir) / STORES_DIR / f"{clean}.sqlite"


def go_mf_tasks(
    manifest: Any,
    matched: Any,
    pools: Optional[Sequence[str]] = None,
) -> List[Any]:
    """The ``go_mf`` items a manifest's *pools* call for, in manifest order.

    A ``go_mf`` question is the annotation string, its source tool and the
    retrieved menu, all three of which the extract already froze on the
    matched row -- so a run of ``go_mf@N`` over a manifest asks exactly the
    question the conflict was found on, and the evidence/menu hashes the
    scorer compares (C15) match by construction rather than by luck.

    De-duplicated by ``item_id``: a swapped pilot entry is a second
    ORIENTATION of one question, and ``go_mf`` has no orientation to swap.

    Args:
        manifest: the manifest mapping (or object) :mod:`.sample` produced.
        matched: the :class:`~.conflicts.MatchedItem` rows it was sampled
            from, or the dicts ``extract`` wrote.
        pools: which pools to build.  ``None`` means every pool in the
            manifest -- which is the normal case here, because a scored
            iteration needs heldout, pilot and both guards run TOGETHER as
            one job over one frozen item set.

    Raises:
        Refused: a manifest entry names an item the matched rows do not hold.
    """
    from ..types import CouncilTask, MenuEntry

    by_item: Dict[str, Any] = {}
    for row in matched or ():
        item_id = row.get("item_id") if isinstance(row, Mapping) else getattr(row, "item_id", None)
        if item_id is not None:
            by_item[str(item_id)] = row

    entries = (
        manifest.get("entries") if isinstance(manifest, Mapping) else getattr(manifest, "entries", ())
    ) or ()
    wanted = None if pools is None else {str(p) for p in pools}

    tasks: List[Any] = []
    missing: List[str] = []
    seen = set()
    for entry in entries:
        if wanted is not None and str(entry.get("pool")) not in wanted:
            continue
        item_id = str(entry.get("item_id"))
        if item_id in seen:
            continue
        seen.add(item_id)
        row = by_item.get(item_id)
        if row is None:
            missing.append(item_id)
            continue
        if isinstance(row, Mapping):
            text = str(row.get("evidence_text") or "")
            source = str(row.get("source") or "")
            menu = list(row.get("menu") or ())
        else:
            text = str(getattr(row, "evidence_text", "") or "")
            source = str(getattr(row, "source", "") or "")
            menu = list(getattr(row, "menu", ()) or ())
        tasks.append(
            CouncilTask(
                item_id=item_id,
                evidence={"text": text, "source": source},
                menu=[MenuEntry.model_validate(dict(e)) for e in menu],
            )
        )
    if missing:
        raise Refused(
            f"{len(missing)} manifest item(s) are not in the matched rows, so their "
            "evidence and menu are unknown and the item cannot be built",
            missing,
        )
    return tasks


def isolated_store(path: Any) -> Any:
    """Create and open an EMPTY store at *path*, or refuse.

    Raises:
        Refused: the path already exists (reusing it would import a call
            cache), or the store will not open there -- a Dropbox component
            or a network filesystem.  The refusal names the directory; it
            never substitutes another one.
    """
    from ..store.errors import StorePathRefused
    from ..store.sqlite import SqliteStore

    target = Path(path)
    if target.exists():
        raise Refused(
            f"refusing to run in an existing store {str(target)!r}: a fresh run "
            "must start from an empty store, and an existing one may already hold "
            "answers this run would silently reuse (the engine's reuse key ignores "
            "the job id). Give the run a different label"
        )
    try:
        store = SqliteStore(f"sqlite:///{target}")
        store.init_schema()
    except StorePathRefused as exc:
        raise Refused(
            f"the council store will not open under {str(target.parent)!r}: {exc} "
            "Pass a --run-dir on local disk (on h100 that means /scratch, not "
            "$HOME and not the Dropbox tree); nothing here will relocate it for you"
        ) from exc
    return store


def _recordable(value: Any) -> Any:
    """*value* reduced to JSON-native types, so a ledger append cannot fail.

    The second ``run`` record is written AFTER the job has been worked, so the
    spend is already made by the time it lands -- and a ``TypeError`` from one
    unserialisable field would lose the record that says the run was (or was
    not) fresh.  ``CouncilUtils.run`` returns the job status with the WORKER'S
    OWN SUMMARY attached, which is a shape this module does not control and
    has no reason to trust to be JSON-native.

    The reduction is ``canonical_json``'s, the same one every hash in this
    package uses: a JSON-native value survives unchanged and anything else
    arrives as its ``str``.
    """
    return json.loads(canonical_json(value))


def reused_calls(store: Any, job_id: str) -> List[str]:
    """The ids of *job_id*'s calls that answered out of the cache, sorted.

    Read off ``calls.reused_from``, which the engine sets to the SOURCE call
    id on both reuse paths -- the store lookup and the in-worker single-flight
    follow -- and which is the only durable evidence either happened.
    """
    return sorted(
        str(call["call_id"])
        for call in store.get_calls(job_id)
        if call.get("reused_from")
    )


def fresh_run(
    spec_source_store: Any,
    query: str,
    version: str,
    items: Sequence[Any],
    profile: Optional[str],
    run_dir: Any,
    *,
    ledger: Any,
    label: Optional[str] = None,
    council_factory: Any = None,
    work: bool = True,
    submitted_label: Optional[str] = None,
) -> FreshRun:
    """Run *items* through ``query@version`` in a store of this run's own.

    Args:
        spec_source_store: the LOOP store, or an open store over it, holding
            the spec to run.  Copied in rather than re-derived: a candidate's
            spec was registered into the loop store by whoever proposed it,
            and re-reading it from a packaged YAML would run a different
            prompt under the same version number.
        query: the query type's name, e.g. ``go_mf``.
        version: its version, e.g. ``4`` for the baseline.
        items: the ``CouncilTask``s (or task mappings) to run.
        profile: the seating profile.  THE ROSTER IS THE OPERATOR'S: it is
            resolved from configuration by this name and nothing here builds
            or edits one.
        run_dir: the run directory.  The store is created at
            ``run_dir/stores/<label>.sqlite``; see the module docstring for
            why it is never relocated.
        ledger: the run's :class:`~.ledger.Ledger`.  Required, and written
            BEFORE the job is worked: the store and job identities are the
            only way a later reader can tell which file holds which
            measurement, and a run whose identities were recorded after the
            spend would lose them to a crash that cost money.
        label: names the store file and the run.  Defaults to
            ``<query>-<version>-<12 hex>``, which is unique but
            unrepeatable; pass a meaningful one (``iter3-candidate``) for a
            run whose store somebody will have to find again.
        council_factory: ``(store_url) -> CouncilUtils``-shaped facade, for a
            caller (or test) that builds its own.  A FACTORY and not a
            facade, unlike :func:`~.adjudicate.run_tranches`: the store this
            run submits into does not exist until this function makes it, so
            a facade handed in ready-made would have had to open (and
            therefore create) the path that :func:`isolated_store` must find
            ABSENT.  Defaults to ``CouncilUtils(store_url=...)``, and
            whatever it returns is closed here.
        work: work the job to idle before checking freshness.  ``False``
            submits only -- and then nothing has been measured yet, so the
            report says ``fresh=None`` rather than claiming a check it did
            not make.
        submitted_label: the job's own ``label`` column.  Defaults to the
            run label, which is what makes a job findable from its store.

    Returns:
        A :class:`FreshRun`: ``label``, ``store``, ``job_id``, ``query``,
        ``profile``, ``n_items``, ``status``, ``fresh``, ``reused`` and
        ``reused_call_ids``.

    Raises:
        Refused: the store path is refused or already exists; the spec is not
            in *spec_source_store*; or the finished job reused a call, which
            means this is not an independent measurement.
    """
    from ..queries import QueryRegistry
    from .conflicts import _opened

    run_label = str(label or f"{query}-{version}-{uuid.uuid4().hex[:12]}")
    path = store_path_for(run_dir, run_label)
    tasks = list(items)

    with _opened(spec_source_store) as source:
        try:
            spec = QueryRegistry(source).get(str(query), str(version))
        except KeyError as exc:
            raise Refused(
                f"no query type {query}@{version!s} in the spec source store "
                f"{getattr(source, 'url', source)!r}; a fresh run copies its spec "
                "from the loop store rather than re-deriving it, so the spec has "
                "to be registered there first"
            ) from exc

    store = isolated_store(path)
    council: Any = None
    try:
        QueryRegistry(store).register(spec, created_by="go_tuning.fresh_run")
        if council_factory is None:

            def council_factory(store_url: str) -> Any:  # noqa: F811
                from ...council_utils import CouncilUtils

                return CouncilUtils(store_url=store_url)

        council = council_factory(f"sqlite:///{path}")
        job_id = council.submit(
            f"{spec.name}@{spec.version}",
            tasks,
            label=str(submitted_label or run_label),
            profile=profile,
        )
        report = FreshRun(
            label=run_label,
            store=f"sqlite:///{path}",
            job_id=str(job_id),
            query=f"{spec.name}@{spec.version}",
            profile=profile,
            n_items=len(tasks),
            status=None,
            fresh=None,
            reused=None,
            reused_call_ids=[],
        )
        # Identities BEFORE the spend.  Everything after this line can crash
        # without losing which file holds which measurement.
        ledger.append(
            "run",
            phase="submitted",
            run_label=run_label,
            store=report["store"],
            job_id=report["job_id"],
            query=report["query"],
            profile=profile,
            n_items=report["n_items"],
        )
        if not work:
            return report

        status = council.run(str(job_id))
        report["status"] = _recordable(dict(status) if isinstance(status, Mapping) else status)
        offenders = reused_calls(store, str(job_id))
        other_jobs = sorted(
            str(job["job_id"])
            for job in store.list_jobs()
            if str(job["job_id"]) != str(job_id)
        )
        report["reused"] = len(offenders)
        report["reused_call_ids"] = offenders
        report["other_jobs"] = other_jobs
        report["fresh"] = not offenders and not other_jobs
        ledger.append(
            "run",
            phase="complete" if report["fresh"] else "not_fresh",
            run_label=run_label,
            store=report["store"],
            job_id=report["job_id"],
            query=report["query"],
            reused=len(offenders),
            reused_call_ids=offenders,
            other_jobs=other_jobs,
            job_status=report["status"],
        )
        if other_jobs:
            raise Refused(
                f"{report['store']} holds {len(other_jobs)} job(s) besides "
                f"{job_id}: the store was meant to hold this run alone, and any "
                "other job in it is a call cache this run's answers may have come "
                "out of",
                other_jobs,
            )
        if offenders:
            raise Refused(
                f"{len(offenders)} of job {job_id}'s calls answered out of the "
                "cache (calls.reused_from is set), so this is not an independent "
                f"run of {report['query']}: it reports another run's answers as "
                "its own. Run it in a store of its own with no imported cache",
                offenders,
            )
        return report
    finally:
        if council is not None:
            close = getattr(council, "close", None)
            if callable(close):
                close()
        close = getattr(store, "close", None)
        if callable(close):
            close()
