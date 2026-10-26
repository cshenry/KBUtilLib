"""Local PaperBLAST: homolog-to-literature links from an offline snapshot.

PaperBLAST (https://papers.genomics.lbl.gov) answers "which papers describe a
protein similar to mine?".  It has no API, but its whole database is published
as three downloadable files, so this module runs PaperBLAST locally instead of
scraping its web form:

* ``litsearch.db`` (~3.1 GB SQLite) -- the gene/paper/snippet/curation tables;
* ``uniq.faa`` (~400 MB FASTA) -- one representative sequence per unique
  protein sequence, which is what we search against;
* ``stats`` (47 bytes) -- ``nSeq=... date=... nPaper=...``, the snapshot's
  provenance.

The pipeline is three steps, each separately callable:

1. :meth:`PaperBLASTUtils.ensure_snapshot` -- HEAD each file, download what
   has changed (resumable, validated, atomically renamed) and record
   ``snapshot.json``.
2. :meth:`PaperBLASTUtils.search` -- MMseqs2 the query sequences against
   ``uniq.faa`` via the shared
   :class:`~kbutillib.domains.genome.mmseqs_utils.MMSeqsUtils`, caching hits
   per sequence in the shared literature cache.
3. :meth:`PaperBLASTUtils.papers_for_hits` -- join those hits to papers in
   ``litsearch.db`` (opened read-only), expanding ``SeqToDuplicate`` first.

Two details of the PaperBLAST data model drive most of the code here:

Duplicate sequences
    ``uniq.faa`` holds only representatives.  ``SeqToDuplicate`` maps a
    representative to the identical-sequence ids that were folded into it, and
    a paper link may hang off *either* side.  Every id is therefore expanded to
    its whole identical-sequence group before any join, otherwise the papers
    attached to the duplicate are silently lost.

Curated ids
    Curated entries appear in ``uniq.faa`` as ``db::protId``
    (``SwissProt::P12345``), and their papers live in ``CuratedPaper`` keyed by
    ``(db, protId)`` rather than in ``GenePaper`` keyed by ``geneId``.  Such
    links carry ``curated_db``; everything else carries ``curated_db=None``.

The HTTP session, the cache and the MMseqs wrapper are all injectable, which
is how the offline tests drive this without network, downloads or (for most
tests) a real mmseqs binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union
from urllib.request import pathname2url

import requests

from kbutillib.core.shared_env_utils import SharedEnvUtils

from .cache import CACHE_PATH_CONFIG_KEY, LiteratureCache, resolve_cache_path

__all__ = [
    "PaperBLASTUtils",
    "PaperBLASTUtilsImpl",
    "parse_stats",
    "split_db_id",
]

# ── snapshot constants ───────────────────────────────────────────────────

#: Where PaperBLAST publishes its database dump.
PAPERBLAST_DATA_BASE = "https://papers.genomics.lbl.gov/data"

LITSEARCH_DB_FILE = "litsearch.db"
UNIQ_FAA_FILE = "uniq.faa"
STATS_FILE = "stats"

#: The three files a snapshot consists of, smallest-first so that a broken
#: mirror fails on the 47-byte file rather than after 3 GB.
SNAPSHOT_FILES: Tuple[str, ...] = (STATS_FILE, UNIQ_FAA_FILE, LITSEARCH_DB_FILE)

#: Manifest written into the data directory describing the local snapshot.
SNAPSHOT_MANIFEST = "snapshot.json"

#: Config key holding the snapshot location.
DATA_DIR_CONFIG_KEY = "paperblast.data_dir"

#: Location used when config says nothing.  Joined to ``Path.home()`` at call
#: time, not at import time, so a relocated HOME is honoured.
DEFAULT_DATA_RELPATH = Path(".kbutillib") / "paperblast"

#: Subdirectory of the data directory holding built MMseqs2 databases.  Each
#: build lives under the sha of the uniq.faa it was built from, so a snapshot
#: refresh can never be searched against a stale index.
MMSEQS_SUBDIR = "mmseqs"

DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
HASH_CHUNK_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60
USER_AGENT = "KBUtilLib-PaperBLASTUtils/1.0 (KBase; https://kbase.us)"

# ── search constants ─────────────────────────────────────────────────────

#: Cache namespace holding per-sequence search results.
HITS_CACHE_TABLE = "paperblast_hits"

#: Bumped whenever the meaning of a cached hit list changes.  It is part of
#: the cache key, so a bump invalidates every stored entry rather than
#: silently mixing old and new shapes.
SEARCH_IMPL_VERSION = "1"

DEFAULT_EVALUE = 1e-3
DEFAULT_MAX_HITS = 50
DEFAULT_MIN_IDENTITY = 0.30
DEFAULT_MIN_QUERY_COVERAGE = 0.5

#: Keys every hit dict carries.
HIT_KEYS = (
    "subject_id",
    "identity",
    "query_coverage",
    "subject_coverage",
    "evalue",
    "bitscore",
)

#: Keys every paper link carries.
LINK_KEYS = (
    "pmid",
    "pmcid",
    "doi",
    "title",
    "journal",
    "year",
    "homolog_id",
    "homolog_organism",
    "identity",
    "query_coverage",
    "subject_coverage",
    "evalue",
    "snippet",
    "curated_db",
    "source",
)

LINK_SOURCE = "paperblast"

#: Identical-sequence groups are small; this only bounds a pathological or
#: corrupt SeqToDuplicate graph so expansion cannot run away.
MAX_DUPLICATE_GROUP = 1000

#: Columns :meth:`PaperBLASTUtils.papers_for_hits` reads, checked against the
#: snapshot's real schema (``PRAGMA table_info``) before any query runs --
#: PaperBLAST's schema is upstream's to change, and a clear error beats an
#: ``OperationalError`` from the middle of a join.
REQUIRED_SCHEMA: Dict[str, Tuple[str, ...]] = {
    "Gene": ("geneId", "organism", "protId", "desc"),
    "GenePaper": (
        "geneId",
        "queryTerm",
        "pmId",
        "pmcId",
        "doi",
        "title",
        "journal",
        "year",
    ),
    "Snippet": ("geneId", "queryTerm", "pmcId", "pmId", "snippet"),
    "GeneRIF": ("geneId", "pmId", "comment"),
    "CuratedGene": ("db", "protId", "name", "desc", "organism"),
    "CuratedPaper": ("db", "protId", "pmId"),
    "SeqToDuplicate": ("sequence_id", "duplicate_id"),
}

#: Separator between the curation database and the protein id in uniq.faa.
DB_ID_SEPARATOR = "::"

# ── module helpers ───────────────────────────────────────────────────────

_STATS_KEY_RE = re.compile(r"(\w+)=")
_WHITESPACE_RE = re.compile(r"\s+")


def parse_stats(text: str) -> Dict[str, Any]:
    """Parse the PaperBLAST ``stats`` file into a dict.

    The file is one line of ``key=value`` pairs separated by spaces -- but
    values may themselves contain spaces (``date=August 25 2026``), so it
    cannot be split on whitespace.  Each value runs from its ``=`` to the
    start of the next ``key=``.

    Args:
        text: Contents of the ``stats`` file.

    Returns:
        E.g. ``{"nSeq": 842060, "date": "August 25 2026", "nPaper": 1322268}``
        -- digit-only values become ints, everything else stays a string.
        An unparseable file yields an empty dict rather than raising.
    """
    matches = list(_STATS_KEY_RE.finditer(text or ""))
    stats: Dict[str, Any] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        stats[match.group(1)] = int(value) if value.isdigit() else value
    return stats


def split_db_id(identifier: str) -> Tuple[Optional[str], str]:
    """Split a ``uniq.faa`` id into its curation database and protein id.

    Args:
        identifier: A sequence id, e.g. ``"SwissProt::P12345"`` or
            ``"ACIAD0380"``.

    Returns:
        ``(db, prot_id)`` for a curated ``db::id``, else ``(None, identifier)``.
    """
    text = str(identifier)
    if DB_ID_SEPARATOR in text:
        db, _, prot_id = text.partition(DB_ID_SEPARATOR)
        if db and prot_id:
            return db, prot_id
    return None, text


def normalise_sequence(sequence: str) -> str:
    """Strip all whitespace and upper-case a protein sequence.

    Applied before both hashing and searching, so that re-wrapping a FASTA
    record cannot miss the cache or change a hit.
    """
    return _WHITESPACE_RE.sub("", str(sequence or "")).upper()


def sha256_file(path: Union[str, Path], chunk_bytes: int = HASH_CHUNK_BYTES) -> str:
    """Return the hex sha256 of a file, read in chunks (it may be gigabytes)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean(value: Any) -> Optional[str]:
    """Normalise a SQLite text cell to a non-empty string, else None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _year(value: Any) -> Optional[Union[int, str]]:
    """Normalise a publication year to an int when it is one."""
    text = _clean(value)
    if text is None:
        return None
    return int(text) if text.isdigit() else text


def _iter_fasta(path: Union[str, Path]) -> Iterator[Dict[str, str]]:
    """Yield ``{"id", "protein_translation"}`` per record in a FASTA file.

    Streams the file; only the caller's accumulation (if any) is in memory.
    """
    record_id: Optional[str] = None
    pieces: List[str] = []
    with open(path, "r") as handle:
        for line in handle:
            if line.startswith(">"):
                if record_id is not None:
                    yield {
                        "id": record_id,
                        "protein_translation": "".join(pieces),
                    }
                # The id is the first whitespace-delimited token of the header.
                record_id = line[1:].strip().split(None, 1)[0] if line[1:].strip() else ""
                pieces = []
            elif record_id is not None:
                pieces.append(line.strip())
    if record_id is not None:
        yield {"id": record_id, "protein_translation": "".join(pieces)}


class PaperBLASTUtils(SharedEnvUtils):
    """Run PaperBLAST locally against a downloaded snapshot.

    Args:
        data_dir: Snapshot location.  Defaults to ``paperblast.data_dir``
            from config, else ``~/.kbutillib/paperblast``.
        session: HTTP session with ``head(url, ...)`` and
            ``get(url, ..., stream=True)``.  Defaults to a lazily created
            :class:`requests.Session`.
        cache: Shared literature cache.  Defaults to a
            :class:`~kbutillib.domains.literature.cache.LiteratureCache` at
            ``gene_literature.cache_path``.
        mmseqs: Object exposing ``build_search_db`` and ``search_proteins``.
            Defaults to a lazily created
            :class:`~kbutillib.domains.genome.mmseqs_utils.MMSeqsUtils`
            (lazily, because constructing one shells out to ``mmseqs
            version``).
        allow_dropbox: Permit a snapshot inside a Dropbox folder.  Default
            False: 3.5 GB of binary data in a synced folder would be copied
            to every machine on the account, so it is refused unless asked
            for explicitly.
        data_base_url: Override for the download location.
        timeout: Per-request timeout in seconds.
        **kwargs: Passed to :class:`SharedEnvUtils` (``config``,
            ``config_file``, ``token_file``, ...).

    Config:
        ``paperblast.data_dir``: snapshot location;
        ``gene_literature.cache_path``: cache location;
        ``mmseqs.executable``: read by the MMseqs wrapper.

    Example::

        pb = PaperBLASTUtils()
        pb.ensure_snapshot()                       # ~3.5 GB, once
        hits = pb.search({"gene1": "MKTAYIAK..."})
        papers = pb.papers_for_hits(hits)
    """

    def __init__(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        session: Optional[Any] = None,
        cache: Optional[LiteratureCache] = None,
        mmseqs: Optional[Any] = None,
        allow_dropbox: bool = False,
        data_base_url: str = PAPERBLAST_DATA_BASE,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.data_base_url = str(data_base_url).rstrip("/")
        self.timeout = timeout
        self.allow_dropbox = bool(allow_dropbox)

        self._data_dir = data_dir
        self._session = session
        self._cache = cache
        self._mmseqs = mmseqs
        self._lazy_lock = threading.Lock()

    # ── lazily built collaborators ────────────────────────────────────

    @property
    def session(self) -> Any:
        """The HTTP session, created on first use when not injected."""
        if self._session is None:
            with self._lazy_lock:
                if self._session is None:
                    session = requests.Session()
                    session.headers.update({"User-Agent": USER_AGENT})
                    self._session = session
        return self._session

    @property
    def cache(self) -> LiteratureCache:
        """The shared literature cache, opened on first use when not injected."""
        if self._cache is None:
            with self._lazy_lock:
                if self._cache is None:
                    self._cache = LiteratureCache(
                        resolve_cache_path(self.get_config_value(CACHE_PATH_CONFIG_KEY))
                    )
        return self._cache

    @property
    def mmseqs(self) -> Any:
        """The MMseqs2 wrapper, built on first use when not injected.

        Deliberately lazy: :class:`MMSeqsUtils` probes for the binary in its
        constructor, so building one eagerly would make ``ensure_snapshot``
        and ``papers_for_hits`` -- neither of which needs mmseqs -- depend on
        an mmseqs install.
        """
        if self._mmseqs is None:
            with self._lazy_lock:
                if self._mmseqs is None:
                    from ..genome.mmseqs_utils import MMSeqsUtils

                    self._mmseqs = MMSeqsUtils(
                        config=self.export_environment().get("config") or {},
                        config_file=False,
                        token_file=None,
                        kbase_token_file=None,
                    )
        return self._mmseqs

    # ── data directory ────────────────────────────────────────────────

    def resolve_data_dir(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        allow_dropbox: Optional[bool] = None,
    ) -> Path:
        """Resolve, symlink-resolve and vet the snapshot location.

        Args:
            data_dir: Explicit location.  Falls back to the constructor's
                value, then ``paperblast.data_dir``, then
                ``~/.kbutillib/paperblast``.
            allow_dropbox: Permit a Dropbox path for this call, overriding
                the constructor's setting.

        Returns:
            The resolved absolute path (not created).

        Raises:
            ValueError: If the resolved path lies inside a Dropbox folder and
                ``allow_dropbox`` is not True.  Symlinks are resolved
                *before* the check, so a symlink out of /tmp into Dropbox is
                caught too.
        """
        configured = data_dir if data_dir is not None else self._data_dir
        if configured is None:
            configured = self.get_config_value(DATA_DIR_CONFIG_KEY)
        if configured:
            candidate = Path(str(configured)).expanduser()
        else:
            candidate = Path.home() / DEFAULT_DATA_RELPATH

        resolved = Path(os.path.realpath(str(candidate)))

        permitted = self.allow_dropbox if allow_dropbox is None else bool(allow_dropbox)
        if not permitted and any(part == "Dropbox" for part in resolved.parts):
            raise ValueError(
                f"Refusing to put a PaperBLAST snapshot at {resolved}: the path "
                f"is inside a Dropbox folder (it resolves through '/Dropbox/'), "
                f"and the snapshot is ~3.5 GB of binary data that Dropbox would "
                f"replicate to every machine on the account. Choose a local path "
                f"via the data_dir argument or the "
                f"'{DATA_DIR_CONFIG_KEY}' config key, or pass allow_dropbox=True "
                f"if this really is what you want."
            )
        return resolved

    def _url_for(self, name: str) -> str:
        """Download URL for one snapshot file."""
        return f"{self.data_base_url}/{name}"

    # ── snapshot ──────────────────────────────────────────────────────

    def ensure_snapshot(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        force: bool = False,
        allow_dropbox: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Make sure a current PaperBLAST snapshot is on local disk.

        HEADs each published file and compares its size and ``Last-Modified``
        against the recorded manifest.  Files that match are left alone --
        nothing is re-downloaded and, importantly, nothing is re-hashed, so an
        up-to-date 3.5 GB snapshot costs three HEAD requests.  Files that
        differ are downloaded with HTTP Range resume into a ``.part`` file,
        validated, then atomically renamed into place.

        Args:
            data_dir: Snapshot location (see :meth:`resolve_data_dir`).
            force: Re-download every file regardless of the manifest.
            allow_dropbox: Permit a Dropbox path for this call.

        Returns:
            The manifest: ``{"files": {name: {"size", "sha256",
            "last_modified"}}, "stats": {...}, "downloaded_at": iso8601,
            "data_dir": str}``.

        Raises:
            ValueError: If the location is inside Dropbox (see
                :meth:`resolve_data_dir`).
            RuntimeError: If a download is the wrong size, or fails
                validation.
            requests.RequestException: If the HTTP requests fail.
        """
        target = self.resolve_data_dir(data_dir, allow_dropbox)
        target.mkdir(parents=True, exist_ok=True)

        remote = {name: self._head(name) for name in SNAPSHOT_FILES}
        existing = self.load_snapshot(target, required=False) or {}
        existing_files = existing.get("files") or {}

        files: Dict[str, Dict[str, Any]] = {}
        downloaded_any = False

        for name in SNAPSHOT_FILES:
            destination = target / name
            recorded = existing_files.get(name) or {}
            if not force and self._is_current(name, recorded, remote[name], destination):
                self.log_info(f"PaperBLAST {name} is unchanged; skipping download")
                # Carry the recorded entry forward rather than re-hashing
                # gigabytes to learn what we already know.
                files[name] = {
                    "size": recorded.get("size"),
                    "sha256": recorded.get("sha256"),
                    "last_modified": recorded.get("last_modified"),
                }
                continue

            self._download(name, destination, remote[name])
            downloaded_any = True
            files[name] = {
                "size": destination.stat().st_size,
                "sha256": sha256_file(destination),
                "last_modified": remote[name].get("last_modified"),
            }

        stats = parse_stats((target / STATS_FILE).read_text())
        snapshot = {
            "files": files,
            "stats": stats,
            "downloaded_at": (
                datetime.now(timezone.utc).isoformat()
                if downloaded_any
                else existing.get("downloaded_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            "data_dir": str(target),
        }
        self._write_manifest(target, snapshot)
        return snapshot

    def load_snapshot(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        required: bool = True,
        allow_dropbox: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Read the manifest recorded by :meth:`ensure_snapshot`.

        Args:
            data_dir: Snapshot location, or an already-resolved path.
            required: When True, a missing or unreadable manifest raises.
            allow_dropbox: Permit a Dropbox path for this call.

        Returns:
            The manifest dict, or None when it is absent and not required.

        Raises:
            RuntimeError: If ``required`` and no usable manifest is present.
        """
        target = self.resolve_data_dir(data_dir, allow_dropbox)
        manifest_path = target / SNAPSHOT_MANIFEST
        if manifest_path.exists():
            try:
                return json.loads(manifest_path.read_text())
            except ValueError as error:
                if required:
                    raise RuntimeError(
                        f"PaperBLAST manifest {manifest_path} is not valid JSON "
                        f"({error}); re-run ensure_snapshot(force=True)."
                    )
                self.log_warning(f"Ignoring unreadable manifest {manifest_path}: {error}")
                return None
        if required:
            raise RuntimeError(
                f"No PaperBLAST snapshot at {target} (expected "
                f"{SNAPSHOT_MANIFEST}). Call ensure_snapshot() first -- it "
                f"downloads ~3.5 GB, so it is never triggered implicitly."
            )
        return None

    def _head(self, name: str) -> Dict[str, Any]:
        """HEAD one published file for its size and Last-Modified."""
        url = self._url_for(name)
        response = self.session.head(url, timeout=self.timeout, allow_redirects=True)
        response.raise_for_status()
        headers = getattr(response, "headers", {}) or {}
        raw_size = headers.get("Content-Length")
        try:
            size = int(raw_size) if raw_size is not None else None
        except (TypeError, ValueError):
            size = None
        return {
            "url": url,
            "size": size,
            "last_modified": headers.get("Last-Modified"),
            "accept_ranges": headers.get("Accept-Ranges"),
        }

    def _is_current(
        self,
        name: str,
        recorded: Dict[str, Any],
        remote: Dict[str, Any],
        destination: Path,
    ) -> bool:
        """True when the local file matches both the manifest and the server.

        Requires the manifest's size and ``Last-Modified`` to equal the
        server's, the file to exist, and its on-disk size to equal the
        recorded size -- so a truncated file is re-fetched even though the
        manifest looks fine.
        """
        if not recorded or not destination.exists():
            return False
        if recorded.get("sha256") is None:
            return False
        recorded_size = recorded.get("size")
        if recorded_size is None or recorded_size != remote.get("size"):
            return False
        if destination.stat().st_size != recorded_size:
            self.log_warning(
                f"PaperBLAST {name} is {destination.stat().st_size} bytes on disk "
                f"but {recorded_size} in the manifest; re-downloading"
            )
            return False
        recorded_modified = recorded.get("last_modified")
        remote_modified = remote.get("last_modified")
        if remote_modified is None or recorded_modified is None:
            return False
        return str(recorded_modified) == str(remote_modified)

    def _download(self, name: str, destination: Path, remote: Dict[str, Any]) -> None:
        """Download one file: resume into ``.part``, validate, atomic rename.

        Args:
            name: Snapshot file name, which selects the validator.
            destination: Final path.  Only written by the closing rename, so
                a half-downloaded file never masquerades as a snapshot.
            remote: The :meth:`_head` result for this file.

        Raises:
            RuntimeError: On a size mismatch or a failed validation.
        """
        url = remote.get("url") or self._url_for(name)
        part = destination.parent / (destination.name + ".part")
        expected_size = remote.get("size")

        resume_from = part.stat().st_size if part.exists() else 0
        if expected_size is not None and resume_from > expected_size:
            self.log_warning(
                f"Partial {part.name} is larger than the published file; restarting"
            )
            part.unlink()
            resume_from = 0

        if expected_size is not None and resume_from == expected_size and resume_from:
            self.log_info(f"Partial {part.name} is already complete; validating")
        else:
            resume_from = self._stream_to_part(url, part, resume_from)

        if expected_size is not None and part.stat().st_size != expected_size:
            raise RuntimeError(
                f"Downloaded {name} is {part.stat().st_size} bytes, expected "
                f"{expected_size} from {url}; leaving {part} in place to resume."
            )

        self._validate_download(name, part)
        os.replace(str(part), str(destination))
        self.log_info(f"PaperBLAST {name} downloaded to {destination}")

    def _stream_to_part(self, url: str, part: Path, resume_from: int) -> int:
        """Stream ``url`` into ``part``, resuming from ``resume_from`` bytes.

        Servers are allowed to ignore a Range request and answer 200 with the
        whole file; that is detected from the status code and the partial file
        is restarted from zero rather than being appended to (which would
        corrupt it with a duplicated prefix).

        Returns:
            Bytes present in ``part`` afterwards.
        """
        headers = {}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"

        response = self.session.get(
            url, headers=headers, stream=True, timeout=self.timeout
        )
        response.raise_for_status()

        mode = "wb"
        if resume_from:
            if getattr(response, "status_code", 200) == 206:
                mode = "ab"
                self.log_info(f"Resuming {part.name} at {resume_from} bytes")
            else:
                self.log_warning(
                    f"Server ignored the Range request for {url} "
                    f"(HTTP {getattr(response, 'status_code', '?')}); "
                    f"restarting the download from 0"
                )
                resume_from = 0

        part.parent.mkdir(parents=True, exist_ok=True)
        with open(part, mode) as handle:
            for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
                if chunk:
                    handle.write(chunk)
        return part.stat().st_size

    def _validate_download(self, name: str, path: Path) -> None:
        """Check a freshly downloaded file before it is renamed into place.

        A truncated or HTML-error-page download is the common failure mode,
        and it is far cheaper to catch it here than during a join hours
        later.  SQLite files get a bounded integrity check (``quick_check``,
        the reduced form of ``integrity_check``: it catches truncation and
        page corruption without reading every index of a 3 GB file), FASTA
        files get their first header parsed, and ``stats`` must parse.

        Raises:
            RuntimeError: If the file does not look like what it should be.
        """
        if name.endswith(".db"):
            uri = "file:" + pathname2url(str(path)) + "?mode=ro"
            try:
                connection = sqlite3.connect(uri, uri=True)
            except sqlite3.Error as error:
                raise RuntimeError(f"Downloaded {name} is not a SQLite database: {error}")
            try:
                row = connection.execute("PRAGMA quick_check(1)").fetchone()
            except sqlite3.DatabaseError as error:
                raise RuntimeError(
                    f"Downloaded {name} failed its SQLite integrity check: {error}"
                )
            finally:
                connection.close()
            verdict = (row[0] if row else "") or ""
            if str(verdict).lower() != "ok":
                raise RuntimeError(
                    f"Downloaded {name} failed its SQLite integrity check: {verdict}"
                )
            return

        if name.endswith(".faa"):
            with open(path, "r", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    if not line.startswith(">") or not line[1:].strip():
                        raise RuntimeError(
                            f"Downloaded {name} does not start with a FASTA header: "
                            f"{line[:80]!r}"
                        )
                    return
            raise RuntimeError(f"Downloaded {name} is empty")

        if name == STATS_FILE:
            stats = parse_stats(path.read_text(errors="replace"))
            if "nSeq" not in stats:
                raise RuntimeError(
                    f"Downloaded {name} does not look like a PaperBLAST stats "
                    f"line (no nSeq): {path.read_text(errors='replace')[:80]!r}"
                )

    def _write_manifest(self, target: Path, snapshot: Dict[str, Any]) -> None:
        """Write ``snapshot.json`` atomically."""
        manifest_path = target / SNAPSHOT_MANIFEST
        temp_path = target / (SNAPSHOT_MANIFEST + ".tmp")
        temp_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
        os.replace(str(temp_path), str(manifest_path))

    # ── search ────────────────────────────────────────────────────────

    def ensure_mmseqs_db(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        faa_sha: Optional[str] = None,
        threads: int = 1,
        allow_dropbox: Optional[bool] = None,
    ) -> Path:
        """Build (once) and return the MMseqs2 database over ``uniq.faa``.

        The database lives at ``<data_dir>/mmseqs/<faa_sha[:16]>/`` -- the
        directory name is bound to the sha of the FASTA it was built from, so
        a snapshot refresh builds a new database rather than quietly reusing
        an index of the previous one.

        Args:
            data_dir: Snapshot location.
            faa_sha: sha256 of ``uniq.faa``.  Read from the manifest when
                omitted.
            threads: Threads for index creation.
            allow_dropbox: Permit a Dropbox path for this call.

        Returns:
            Path to the MMseqs2 database, ready to pass to
            ``search_proteins``.

        Note:
            ``MMSeqsUtils.build_search_db`` takes a list of protein dicts, so
            building over the full ``uniq.faa`` (~842k sequences) holds them
            in memory for the duration of the build.  That is a one-off cost
            per snapshot; the built database is then reused from disk.
        """
        target = self.resolve_data_dir(data_dir, allow_dropbox)
        if faa_sha is None:
            snapshot = self.load_snapshot(target)
            faa_sha = (snapshot.get("files", {}).get(UNIQ_FAA_FILE) or {}).get("sha256")
        if not faa_sha:
            raise RuntimeError(
                f"The PaperBLAST manifest at {target} records no sha256 for "
                f"{UNIQ_FAA_FILE}; re-run ensure_snapshot(force=True)."
            )

        db_dir = target / MMSEQS_SUBDIR / str(faa_sha)[:16]
        db_path = db_dir / "searchDB"
        if db_path.exists():
            self.log_debug(f"Reusing MMseqs2 database at {db_path}")
            return db_path

        fasta_path = target / UNIQ_FAA_FILE
        if not fasta_path.exists():
            raise RuntimeError(
                f"{fasta_path} is missing; call ensure_snapshot() first."
            )

        self.log_info(f"Building MMseqs2 database from {fasta_path} (one-off)")
        proteins = [
            record
            for record in _iter_fasta(fasta_path)
            if record["id"] and record["protein_translation"]
        ]
        if not proteins:
            raise RuntimeError(f"{fasta_path} contains no sequences")
        return Path(self.mmseqs.build_search_db(proteins, db_dir, threads=threads))

    def _hits_cache_key(
        self,
        litsearch_sha: str,
        faa_sha: str,
        sequence: str,
        evalue: float,
        max_hits: int,
        min_identity: float,
        min_query_coverage: float,
    ) -> str:
        """Cache key for one sequence's hits against one snapshot.

        Every input that can change the answer is in the key -- both snapshot
        file shas, the sequence, all four thresholds and
        :data:`SEARCH_IMPL_VERSION` -- so results are never reused across
        parameter sets or snapshots.
        """
        digest = hashlib.sha256()
        parts = (
            str(litsearch_sha),
            str(faa_sha),
            hashlib.sha256(normalise_sequence(sequence).encode()).hexdigest(),
            repr(float(evalue)),
            repr(int(max_hits)),
            repr(float(min_identity)),
            repr(float(min_query_coverage)),
            SEARCH_IMPL_VERSION,
        )
        for part in parts:
            digest.update(part.encode())
            digest.update(b"\x00")
        return digest.hexdigest()

    def search(
        self,
        queries: Dict[str, str],
        evalue: float = DEFAULT_EVALUE,
        max_hits: int = DEFAULT_MAX_HITS,
        min_identity: float = DEFAULT_MIN_IDENTITY,
        min_query_coverage: float = DEFAULT_MIN_QUERY_COVERAGE,
        data_dir: Optional[Union[str, Path]] = None,
        threads: int = 1,
        allow_dropbox: Optional[bool] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Search query proteins against the snapshot's representatives.

        Args:
            queries: ``{gene_id: protein sequence}``.
            evalue: Maximum e-value (``mmseqs search -e``).
            max_hits: Maximum hits kept per gene (``--max-seqs``).
            min_identity: Minimum identity as a fraction (0-1).
            min_query_coverage: Minimum query coverage as a fraction (0-1).
            data_dir: Snapshot location.
            threads: Threads for MMseqs2.
            allow_dropbox: Permit a Dropbox path for this call.

        Returns:
            ``{gene_id: [hit, ...]}`` for every input gene (``[]`` when
            nothing passed the thresholds), best e-value first.  Each hit
            has exactly: ``subject_id``, ``identity`` (0-1),
            ``query_coverage``, ``subject_coverage``, ``evalue``,
            ``bitscore``.  Query and subject coverage are computed by MMseqs2
            from the alignment against each sequence's own length and are
            kept as two separate numbers -- a short query fully covered by a
            long homolog has query_coverage 1.0 and a low subject_coverage.

        Raises:
            RuntimeError: If no snapshot is present (``search`` never
                downloads ~3.5 GB implicitly), or if MMseqs2 fails.
            ValueError: If a query has an empty sequence, or the data
                directory is inside Dropbox.
        """
        if not queries:
            return {}

        for gene_id, sequence in queries.items():
            if not normalise_sequence(sequence):
                raise ValueError(f"Query {gene_id!r} has an empty protein sequence")

        target = self.resolve_data_dir(data_dir, allow_dropbox)
        snapshot = self.load_snapshot(target)
        snapshot_files = snapshot.get("files") or {}
        litsearch_sha = (snapshot_files.get(LITSEARCH_DB_FILE) or {}).get("sha256")
        faa_sha = (snapshot_files.get(UNIQ_FAA_FILE) or {}).get("sha256")
        if not litsearch_sha or not faa_sha:
            raise RuntimeError(
                f"The PaperBLAST manifest at {target} is missing file hashes; "
                f"re-run ensure_snapshot(force=True)."
            )

        results: Dict[str, List[Dict[str, Any]]] = {}
        keys: Dict[str, str] = {}
        pending: Dict[str, str] = {}

        for gene_id, sequence in queries.items():
            key = self._hits_cache_key(
                litsearch_sha,
                faa_sha,
                sequence,
                evalue,
                max_hits,
                min_identity,
                min_query_coverage,
            )
            keys[gene_id] = key
            cached = self.cache.get(HITS_CACHE_TABLE, key)
            if cached is not None:
                self.log_debug(f"PaperBLAST hit cache hit for {gene_id}")
                results[gene_id] = cached
            else:
                pending[gene_id] = normalise_sequence(sequence)

        if pending:
            db_path = self.ensure_mmseqs_db(
                target, faa_sha=faa_sha, threads=threads, allow_dropbox=allow_dropbox
            )
            proteins = [
                {"id": gene_id, "protein_translation": sequence}
                for gene_id, sequence in pending.items()
            ]
            raw_hits = self.mmseqs.search_proteins(
                proteins,
                db_path,
                min_seq_id=min_identity,
                coverage=min_query_coverage,
                threads=threads,
                evalue=evalue,
                max_hits=max_hits,
                include_subject_coverage=True,
            )

            found: Dict[str, List[Dict[str, Any]]] = {gene_id: [] for gene_id in pending}
            for raw in raw_hits:
                gene_id = raw.get("query_id")
                if gene_id not in found:
                    self.log_warning(
                        f"Ignoring MMseqs2 hit for unknown query {gene_id!r}"
                    )
                    continue
                found[gene_id].append(
                    {
                        "subject_id": raw.get("target_id"),
                        "identity": float(raw.get("identity", 0.0)),
                        "query_coverage": float(raw.get("coverage", 0.0)),
                        # Kept separate from query_coverage on purpose: it is
                        # coverage of the SUBJECT, from the subject's length.
                        "subject_coverage": (
                            float(raw["subject_coverage"])
                            if raw.get("subject_coverage") is not None
                            else None
                        ),
                        "evalue": float(raw.get("evalue", 0.0)),
                        "bitscore": float(raw.get("bits", 0.0)),
                    }
                )

            for gene_id, hits in found.items():
                hits.sort(key=lambda hit: (hit["evalue"], -hit["bitscore"]))
                del hits[max_hits:]
                results[gene_id] = hits
                self.cache.put(HITS_CACHE_TABLE, keys[gene_id], hits)

        return {gene_id: results.get(gene_id) or [] for gene_id in queries}

    # ── literature join ───────────────────────────────────────────────

    def papers_for_hits(
        self,
        hits: Dict[str, List[Dict[str, Any]]],
        data_dir: Optional[Union[str, Path]] = None,
        allow_dropbox: Optional[bool] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Join search hits to the papers that describe them.

        Args:
            hits: ``{gene_id: [hit, ...]}`` as returned by :meth:`search`.
            data_dir: Snapshot location.
            allow_dropbox: Permit a Dropbox path for this call.

        Returns:
            ``{gene_id: [link, ...]}``, best e-value first.  Each link has
            exactly the keys in :data:`LINK_KEYS`: the paper
            (``pmid``/``pmcid``/``doi``/``title``/``journal``/``year``), the
            homolog it came from (``homolog_id``, ``homolog_organism``), the
            alignment that found that homolog (``identity``,
            ``query_coverage``, ``subject_coverage``, ``evalue``), the
            ``snippet`` that mentions it when PaperBLAST recorded one,
            ``curated_db`` for a curation-database link (else None) and
            ``source="paperblast"``.

            ``homolog_id`` is the ``litsearch.db`` id that actually carries
            the link, which after ``SeqToDuplicate`` expansion may differ
            from the hit's ``subject_id`` -- the two ids hold identical
            sequences, and the alignment numbers on the link are the hit's.

            Paper ids are returned as PaperBLAST stores them (bare
            ``"12345"``, ``"PMC12345"``); pass them through
            ``europepmc.canonical_paper_id`` to merge with Europe PMC
            results.

        Raises:
            RuntimeError: If the snapshot or a required table/column is
                missing.
            ValueError: If the data directory is inside Dropbox.
        """
        if not hits:
            return {}

        target = self.resolve_data_dir(data_dir, allow_dropbox)
        db_file = target / LITSEARCH_DB_FILE
        if not db_file.exists():
            raise RuntimeError(
                f"{db_file} is missing; call ensure_snapshot() first."
            )

        # Read-only on purpose: this is a 3 GB shared download, and nothing
        # here has any business writing to it (not even a journal file).
        uri = "file:" + pathname2url(str(db_file)) + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            self._check_schema(connection, db_file)
            results: Dict[str, List[Dict[str, Any]]] = {}
            for gene_id, gene_hits in hits.items():
                links: List[Dict[str, Any]] = []
                seen: Set[Tuple[Any, ...]] = set()
                for hit in gene_hits or []:
                    for link in self._links_for_hit(connection, hit):
                        fingerprint = (
                            link["homolog_id"],
                            link["pmid"],
                            link["pmcid"],
                            link["doi"],
                            link["curated_db"],
                        )
                        if fingerprint in seen:
                            continue
                        seen.add(fingerprint)
                        links.append(link)
                links.sort(key=_link_sort_key)
                results[gene_id] = links
            return results
        finally:
            connection.close()

    def _check_schema(self, connection: sqlite3.Connection, db_file: Path) -> None:
        """Verify the snapshot's real schema carries every column we read.

        PaperBLAST's schema belongs to upstream, so the column names are read
        from the file itself with ``PRAGMA table_info`` rather than assumed.

        Raises:
            RuntimeError: Naming the table and the missing columns.
        """
        for table, required in REQUIRED_SCHEMA.items():
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            present = {row["name"] for row in rows}
            if not present:
                raise RuntimeError(
                    f"{db_file} has no table {table!r}. This does not look like "
                    f"a PaperBLAST litsearch.db; re-run ensure_snapshot(force=True)."
                )
            missing = [column for column in required if column not in present]
            if missing:
                raise RuntimeError(
                    f"{db_file} table {table!r} is missing required column(s) "
                    f"{missing} (it has {sorted(present)}). PaperBLAST's schema "
                    f"may have changed; kbutillib needs updating."
                )

    def _expand_duplicates(
        self, connection: sqlite3.Connection, subject_id: str
    ) -> List[str]:
        """Return the whole identical-sequence group containing ``subject_id``.

        ``uniq.faa`` holds representatives, and ``SeqToDuplicate`` maps a
        representative (``sequence_id``) to the ids folded into it
        (``duplicate_id``).  A paper may be attached to either side, so the
        group is closed over BOTH directions: a hit on a representative picks
        up its duplicates' papers, and a hit on an id that turns out to be a
        duplicate picks up its representative's (and its siblings').

        Returns:
            The group, ``subject_id`` first (so its own links sort first).
        """
        group: List[str] = [str(subject_id)]
        seen: Set[str] = {str(subject_id)}
        frontier = [str(subject_id)]

        while frontier and len(group) < MAX_DUPLICATE_GROUP:
            current = frontier.pop()
            neighbours = [
                row[0]
                for row in connection.execute(
                    "SELECT duplicate_id FROM SeqToDuplicate WHERE sequence_id = ?",
                    (current,),
                ).fetchall()
            ]
            neighbours.extend(
                row[0]
                for row in connection.execute(
                    "SELECT sequence_id FROM SeqToDuplicate WHERE duplicate_id = ?",
                    (current,),
                ).fetchall()
            )
            for neighbour in neighbours:
                identifier = _clean(neighbour)
                if identifier is None or identifier in seen:
                    continue
                seen.add(identifier)
                group.append(identifier)
                frontier.append(identifier)
                if len(group) >= MAX_DUPLICATE_GROUP:
                    self.log_warning(
                        f"SeqToDuplicate group for {subject_id!r} exceeded "
                        f"{MAX_DUPLICATE_GROUP} members; truncating"
                    )
                    break
        return group

    def _links_for_hit(
        self, connection: sqlite3.Connection, hit: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Build every paper link reachable from one search hit."""
        subject_id = _clean(hit.get("subject_id"))
        if subject_id is None:
            return []

        alignment = {
            "identity": hit.get("identity"),
            "query_coverage": hit.get("query_coverage"),
            "subject_coverage": hit.get("subject_coverage"),
            "evalue": hit.get("evalue"),
        }

        links: List[Dict[str, Any]] = []
        for homolog_id in self._expand_duplicates(connection, subject_id):
            db, prot_id = split_db_id(homolog_id)
            # A curated id is stored both ways in practice ('db::protId' in
            # uniq.faa, bare protId in some tables), so look both up.
            gene_keys = [homolog_id]
            if db is not None and prot_id not in gene_keys:
                gene_keys.append(prot_id)

            organism = self._homolog_organism(connection, gene_keys, db, prot_id)
            snippets = self._snippets_for(connection, gene_keys)

            for key in gene_keys:
                for row in connection.execute(
                    "SELECT queryTerm, pmId, pmcId, doi, title, journal, year "
                    "FROM GenePaper WHERE geneId = ?",
                    (key,),
                ).fetchall():
                    pmid = _clean(row["pmId"])
                    pmcid = _clean(row["pmcId"])
                    doi = _clean(row["doi"])
                    if not (pmid or pmcid or doi):
                        continue
                    links.append(
                        _make_link(
                            pmid=pmid,
                            pmcid=pmcid,
                            doi=doi,
                            title=_clean(row["title"]),
                            journal=_clean(row["journal"]),
                            year=_year(row["year"]),
                            homolog_id=homolog_id,
                            homolog_organism=organism,
                            snippet=_pick_snippet(
                                snippets, _clean(row["queryTerm"]), pmid, pmcid
                            ),
                            curated_db=None,
                            **alignment,
                        )
                    )

                # GeneRIF is a second, independent source of paper links; its
                # comment is the only "snippet" such a link has.
                for row in connection.execute(
                    "SELECT pmId, comment FROM GeneRIF WHERE geneId = ?", (key,)
                ).fetchall():
                    pmid = _clean(row["pmId"])
                    if not pmid:
                        continue
                    links.append(
                        _make_link(
                            pmid=pmid,
                            pmcid=None,
                            doi=None,
                            title=None,
                            journal=None,
                            year=None,
                            homolog_id=homolog_id,
                            homolog_organism=organism,
                            snippet=_clean(row["comment"]),
                            curated_db=None,
                            **alignment,
                        )
                    )

            if db is not None:
                for row in connection.execute(
                    "SELECT db, pmId FROM CuratedPaper WHERE db = ? AND protId = ?",
                    (db, prot_id),
                ).fetchall():
                    pmid = _clean(row["pmId"])
                    if not pmid:
                        continue
                    links.append(
                        _make_link(
                            pmid=pmid,
                            pmcid=None,
                            doi=None,
                            title=None,
                            journal=None,
                            year=None,
                            homolog_id=homolog_id,
                            homolog_organism=organism,
                            snippet=None,
                            curated_db=_clean(row["db"]) or db,
                            **alignment,
                        )
                    )

        return links

    def _homolog_organism(
        self,
        connection: sqlite3.Connection,
        gene_keys: Sequence[str],
        db: Optional[str],
        prot_id: str,
    ) -> Optional[str]:
        """Best-known organism for a homolog, from Gene or CuratedGene."""
        for key in gene_keys:
            row = connection.execute(
                "SELECT organism FROM Gene WHERE geneId = ? LIMIT 1", (key,)
            ).fetchone()
            if row is not None and _clean(row["organism"]):
                return _clean(row["organism"])
        if db is not None:
            row = connection.execute(
                "SELECT organism FROM CuratedGene WHERE db = ? AND protId = ? LIMIT 1",
                (db, prot_id),
            ).fetchone()
            if row is not None:
                return _clean(row["organism"])
        return None

    def _snippets_for(
        self, connection: sqlite3.Connection, gene_keys: Sequence[str]
    ) -> List[Dict[str, Any]]:
        """All snippet rows for a homolog, as plain dicts."""
        snippets: List[Dict[str, Any]] = []
        for key in gene_keys:
            for row in connection.execute(
                "SELECT queryTerm, pmcId, pmId, snippet FROM Snippet WHERE geneId = ?",
                (key,),
            ).fetchall():
                snippets.append(
                    {
                        "queryTerm": _clean(row["queryTerm"]),
                        "pmcId": _clean(row["pmcId"]),
                        "pmId": _clean(row["pmId"]),
                        "snippet": _clean(row["snippet"]),
                    }
                )
        return snippets


def _pick_snippet(
    snippets: Sequence[Dict[str, Any]],
    query_term: Optional[str],
    pmid: Optional[str],
    pmcid: Optional[str],
) -> Optional[str]:
    """Choose the snippet matching a paper, preferring an exact queryTerm match.

    Snippet rows are keyed by gene, query term and paper, but either paper id
    may be absent on either side, so a row matches when the ids it does carry
    agree.
    """
    best: Optional[str] = None
    for row in snippets:
        if not row["snippet"]:
            continue
        if pmid and row["pmId"] and row["pmId"] != pmid:
            continue
        if pmcid and row["pmcId"] and row["pmcId"] != pmcid:
            continue
        if not ((pmid and row["pmId"]) or (pmcid and row["pmcId"])):
            continue
        if query_term and row["queryTerm"] and row["queryTerm"] == query_term:
            return row["snippet"]
        if best is None:
            best = row["snippet"]
    return best


def _make_link(**fields: Any) -> Dict[str, Any]:
    """Build a link dict carrying exactly :data:`LINK_KEYS`."""
    link = {key: fields.get(key) for key in LINK_KEYS}
    link["source"] = LINK_SOURCE
    return link


def _link_sort_key(link: Dict[str, Any]) -> Tuple[Any, ...]:
    """Order links best-alignment first, then newest, then by id."""
    evalue = link.get("evalue")
    identity = link.get("identity")
    year = link.get("year")
    return (
        float(evalue) if isinstance(evalue, (int, float)) else float("inf"),
        -(float(identity) if isinstance(identity, (int, float)) else 0.0),
        -(year if isinstance(year, int) else 0),
        str(link.get("pmid") or link.get("pmcid") or link.get("doi") or ""),
    )


# ── Composition-based implementation ─────────────────────────────────────


class PaperBLASTUtilsImpl:
    """Composition-based version of :class:`PaperBLASTUtils`.

    Holds ``env: SharedEnvUtils`` instead of inheriting, and delegates all
    method calls to an internal instance.
    """

    def __init__(self, env, **kwargs):
        self._env = env
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        # Carry the environment's configuration across, so the delegate reads
        # the same paperblast.* / gene_literature.* settings.
        try:
            _kwargs["config"] = env.export_environment().get("config") or {}
        except Exception:
            pass
        _kwargs.update(kwargs)
        self._delegate = PaperBLASTUtils(**_kwargs)

    @property
    def env(self):
        return self._env

    def __getattr__(self, name):
        # Delegate all attribute access to the PaperBLASTUtils instance.
        return getattr(self._delegate, name)
