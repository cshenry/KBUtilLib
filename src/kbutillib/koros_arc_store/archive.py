"""Durable gzip archive for analysis payloads that exist nowhere else.

The run index records that an analysis HAPPENED and summarises it. That is not
the same as keeping it, and for some producers the index summary is the only
thing that survives:

* **KBDL job results EXPIRE.** ``get_job_result`` starts returning
  ``KBDLNotFoundError: result ... is no longer available``. Measured on kbhub
  2026-10-02: of 453 genome-creation / annotation / modelling jobs, **25 had
  already expired**, and the annotation count rose from 14 to 15 during a single
  four-minute archive run -- so this is a live, ongoing loss, not a historical
  one. SKANI is already gone entirely: 0 of 96,276 results retrievable.
* **Annotation and model results have no second copy.** The KBDL object store
  carries no ``Annotation`` type and no model type; ``GenomeArchive`` holds only
  a raw ``.fna``, and Chris confirmed 2026-10-02 that annotations are not in the
  archives. Only genome creation is independently durable (its ``Genome`` /
  ``GenomeFasta`` objects persist and the index points at them).

So a producer whose payload is ephemeral must hand it to :func:`archive_payload`
at record time. Compression earns its place: annotation results average 3.0 MB
raw and 0.51 MB gzipped (5.9x), model reconstructions 5.2 MB -> 0.27 MB (18.9x).
The full kbhub backfill is 144.5 MB on disk for 421 results.

Design constraints, all load-bearing:

* **Atomic.** Written to ``<name>.gz.part`` and ``os.replace``d into place, so an
  interrupted run never leaves a truncated file that the idempotency check below
  would then trust forever.
* **Idempotent.** An existing non-empty archive is left alone and reported as
  already present, so re-running is cheap and never re-downloads.
* **Soft-fail.** Returns ``None`` rather than raising. An archive is strictly
  additive to the record; losing the archive must never also lose the index
  entry, which is the same reasoning that makes the rest of this store
  soft-fail.
* **Streamed.** ``json.dump`` writes straight into the gzip stream, so a payload
  is never serialised to a second in-memory copy. Results reach **271 MB**.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Environment variable overriding the archive root.
ARCHIVE_ROOT_ENV_VAR = "KBDL_RESULT_ARCHIVE"

#: Default archive root, beside the run index (``~/.kbdl/runs.sqlite``) so the
#: index and the payloads it points at move together.
DEFAULT_ARCHIVE_RELPATH = Path(".kbdl") / "job-results"

#: Artifact key used for a record's archived raw payload.
ARTIFACT_KEY = "result"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def resolve_archive_root(root: Optional[Path | str] = None) -> Path:
    """Resolve the archive root: explicit arg, else the env var, else the default.

    The directory is NOT created here -- that happens on the write path, so a
    failure takes the soft-fail route rather than erroring at import or
    construction time.
    """
    if root is not None:
        return Path(root).expanduser()
    env = os.environ.get(ARCHIVE_ROOT_ENV_VAR)
    if env and env.strip():
        return Path(env.strip()).expanduser()
    return Path.home() / DEFAULT_ARCHIVE_RELPATH


def _safe(part: str) -> str:
    """Make *part* safe as a single path segment.

    ``kind`` and ``run_uid`` reach here from producers (a KBDL job id, a verb
    name, a uuid). Sanitising rather than trusting keeps a stray ``../`` or a
    separator from escaping the archive root.
    """
    cleaned = _UNSAFE.sub("_", part.strip()) or "unknown"
    return cleaned[:120]


def archive_path(kind: str, run_uid: str, root: Optional[Path | str] = None) -> Path:
    """Where :func:`archive_payload` would put this payload. Pure; no I/O."""
    return resolve_archive_root(root) / _safe(kind) / f"{_safe(run_uid)}.json.gz"


def archive_payload(
    kind: str,
    run_uid: str,
    payload: Any,
    root: Optional[Path | str] = None,
) -> Optional[Path]:
    """Gzip *payload* as JSON under the archive root. Returns the path, or ``None``.

    ``(kind, run_uid)`` is the same pair the record's identity derives from, so
    an archive lines up 1:1 with the record that points at it and a re-run
    overwrites nothing.

    Returns ``None`` on any failure, having logged it -- never raises. See the
    module docstring on why this is additive-only.
    """
    dest = archive_path(kind, run_uid, root)
    try:
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
            json.dump(payload, fh)
        os.replace(tmp, dest)
        return dest
    except MemoryError:
        # Called out separately because the realistic cause is a single huge
        # payload (271 MB observed) rather than a broken path, and the operator
        # response differs.
        log.warning(
            "archive out of memory for kind=%s run_uid=%s (payload too large "
            "to serialise); record is unaffected",
            kind,
            run_uid,
        )
        return None
    except Exception as exc:  # noqa: BLE001 -- additive step, must not kill the record
        log.warning(
            "archive failed for kind=%s run_uid=%s at %s: %r; record is unaffected",
            kind,
            run_uid,
            dest,
            exc,
        )
        try:
            tmp = dest.with_name(dest.name + ".part")
            if tmp.exists():
                tmp.unlink()
        except Exception:  # pragma: no cover -- cleanup is best effort
            pass
        return None
