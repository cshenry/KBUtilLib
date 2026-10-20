"""Offline tests for local PaperBLAST.

Every test here runs without network and (with one deliberate exception)
without a real mmseqs binary:

* the HTTP session is faked at ``head``/``get``, which is how the snapshot
  download, resume and skip-when-unchanged paths are driven;
* the MMseqs wrapper is faked at ``build_search_db``/``search_proteins``,
  which is how the hit plumbing and caching are driven;
* ``litsearch.db`` is a real SQLite file built here, a few rows per table,
  with the duplicate indirection and a curated entry that the join has to
  get right.

:class:`TestRealMMseqsPath` is the exception: it injects nothing at all and
runs ``search`` -> ``papers_for_hits`` through the real
:class:`MMSeqsUtils` and the real binary over a tiny snapshot, skipping only
when no mmseqs executable is installed.  Everything else can pass while the
one seam that matters -- the shape of the data actually coming back from
mmseqs -- is wrong, so that test is what proves the pipeline.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from kbutillib.domains.literature.cache import LiteratureCache
from kbutillib.domains.literature.paperblast import (
    HITS_CACHE_TABLE,
    LINK_KEYS,
    LITSEARCH_DB_FILE,
    SNAPSHOT_MANIFEST,
    STATS_FILE,
    UNIQ_FAA_FILE,
    PaperBLASTUtils,
    PaperBLASTUtilsImpl,
    parse_stats,
    sha256_file,
    split_db_id,
)

# ── fixture data ─────────────────────────────────────────────────────────

STATS_TEXT = "nSeq=3 date=August 25 2026 nPaper=4\n"

# Two unrelated sequences, long enough for mmseqs to align.
REP_SEQ = (
    "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVK"
    "ALPDAQFEVVHSLAKWKRQTLGQHDFSAGEGLYTHMKALRPDEDRLSPLHSVYVDQWDWE"
)
CURATED_SEQ = (
    "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHG"
    "KKVADALTNAVAHVDDMPNALSALSDLHAHKLRVDPVNFKLLSHCLLVTLAAHLPAEFTP"
)
UNRELATED_SEQ = (
    "MWQRNIRHSQQSDYLKQLYCIKDDIGGKTNEFWIYRQSNSFFTDAHEHFMMRPQWYTTCG"
    "GDVQKLPNIWGFDSNHKEEYVNNVGMWKRRAFQRTPLEDDNSSHWAIKKMCEYNQHGDLV"
)

#: The id that PaperBLAST actually folded away: it is NOT in uniq.faa, and the
#: only way to reach its paper is through SeqToDuplicate.
DUPLICATE_ID = "dupA"
REPRESENTATIVE_ID = "repA"
CURATED_ID = "SwissProt::P12345"

#: Real litsearch.db column names, including columns this code never reads
#: (authors, isOpen, id2, comment) so that the schema check and the queries
#: are exercised against a realistic table rather than a minimal one.
TABLES = {
    "Gene": ["geneId", "organism", "protId", "desc"],
    "GenePaper": [
        "geneId",
        "queryTerm",
        "pmId",
        "pmcId",
        "doi",
        "title",
        "authors",
        "journal",
        "year",
        "isOpen",
    ],
    "Snippet": ["geneId", "queryTerm", "pmcId", "pmId", "snippet"],
    "GeneRIF": ["geneId", "pmId", "comment"],
    "CuratedGene": ["db", "protId", "id2", "name", "desc", "organism", "comment"],
    "CuratedPaper": ["db", "protId", "pmId"],
    "SeqToDuplicate": ["sequence_id", "duplicate_id"],
}

ROWS = {
    "Gene": [
        {
            "geneId": REPRESENTATIVE_ID,
            "organism": "Acinetobacter baylyi ADP1",
            "protId": "P0001",
            "desc": "replication initiator",
        },
        {
            "geneId": DUPLICATE_ID,
            "organism": "Escherichia coli K-12",
            "protId": "P0002",
            "desc": "identical sequence, folded into repA",
        },
        {
            "geneId": "unrelated1",
            "organism": "Unrelated organism",
            "protId": "P0003",
            "desc": "unrelated protein",
        },
    ],
    "GenePaper": [
        {
            "geneId": REPRESENTATIVE_ID,
            "queryTerm": "dnaA",
            "pmId": "1001",
            "pmcId": "PMC1001",
            "doi": "10.1/direct",
            "title": "Paper attached directly to the representative",
            "authors": "Author A",
            "journal": "Journal One",
            "year": "2020",
            "isOpen": 1,
        },
        {
            "geneId": DUPLICATE_ID,
            "queryTerm": "dnaA",
            "pmId": "2002",
            "pmcId": "PMC2002",
            "doi": "10.1/duplicate",
            "title": "Paper reachable ONLY through SeqToDuplicate",
            "authors": "Author B",
            "journal": "Journal Two",
            "year": "2021",
            "isOpen": 1,
        },
        {
            "geneId": "unrelated1",
            "queryTerm": "xyz",
            "pmId": "9009",
            "pmcId": None,
            "doi": None,
            "title": "Paper about something else entirely",
            "authors": "Author Z",
            "journal": "Journal Nine",
            "year": "1999",
            "isOpen": 0,
        },
    ],
    "Snippet": [
        {
            "geneId": DUPLICATE_ID,
            "queryTerm": "dnaA",
            "pmcId": "PMC2002",
            "pmId": "2002",
            "snippet": "the dnaA homolog was essential for growth",
        }
    ],
    "GeneRIF": [
        {
            "geneId": REPRESENTATIVE_ID,
            "pmId": "3003",
            "comment": "GeneRIF: initiates chromosomal replication",
        }
    ],
    "CuratedGene": [
        {
            "db": "SwissProt",
            "protId": "P12345",
            "id2": "",
            "name": "curA",
            "desc": "Curated protein A",
            "organism": "Bacillus subtilis",
            "comment": "curated by hand",
        }
    ],
    "CuratedPaper": [{"db": "SwissProt", "protId": "P12345", "pmId": "4004"}],
    "SeqToDuplicate": [
        {"sequence_id": REPRESENTATIVE_ID, "duplicate_id": DUPLICATE_ID}
    ],
}


def build_litsearch_db(path, omit_table=None, omit_column=None):
    """Build a tiny but schema-faithful litsearch.db at *path*.

    Args:
        path: Where to write the SQLite file.
        omit_table: Table to leave out entirely (for the schema check).
        omit_column: ``(table, column)`` to leave out (for the schema check).

    Returns:
        The path, as a :class:`Path`.
    """
    path = Path(path)
    # Rebuilt in place by some tests, so start from nothing.
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(str(path))
    try:
        for table, columns in TABLES.items():
            if table == omit_table:
                continue
            if omit_column and omit_column[0] == table:
                columns = [c for c in columns if c != omit_column[1]]
            connection.execute(
                "CREATE TABLE {} ({})".format(
                    table, ", ".join('"{}" TEXT'.format(c) for c in columns)
                )
            )
            for row in ROWS[table]:
                values = [row.get(c) for c in columns]
                connection.execute(
                    "INSERT INTO {} ({}) VALUES ({})".format(
                        table,
                        ", ".join('"{}"'.format(c) for c in columns),
                        ", ".join("?" for _ in columns),
                    ),
                    values,
                )
        connection.commit()
    finally:
        connection.close()
    return path


def fasta_text(records):
    """Render ``[(id, sequence)]`` as FASTA."""
    return "".join(">{}\n{}\n".format(name, seq) for name, seq in records)


DEFAULT_FAA_RECORDS = [
    (REPRESENTATIVE_ID, REP_SEQ),
    (CURATED_ID, CURATED_SEQ),
    ("unrelated1", UNRELATED_SEQ),
]


def make_snapshot(directory, last_modified="Tue, 26 Aug 2026 01:02:03 GMT", **kwargs):
    """Write a complete local snapshot (three files plus a manifest).

    Returns:
        The directory, as a :class:`Path`.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    build_litsearch_db(directory / LITSEARCH_DB_FILE, **kwargs)
    (directory / UNIQ_FAA_FILE).write_text(fasta_text(DEFAULT_FAA_RECORDS))
    (directory / STATS_FILE).write_text(STATS_TEXT)

    files = {}
    for name in (LITSEARCH_DB_FILE, UNIQ_FAA_FILE, STATS_FILE):
        target = directory / name
        files[name] = {
            "size": target.stat().st_size,
            "sha256": sha256_file(target),
            "last_modified": last_modified,
        }
    (directory / SNAPSHOT_MANIFEST).write_text(
        json.dumps(
            {
                "files": files,
                "stats": parse_stats(STATS_TEXT),
                "downloaded_at": "2026-08-26T00:00:00+00:00",
                "data_dir": str(directory),
            }
        )
    )
    return directory


# ── test doubles ─────────────────────────────────────────────────────────


class FakeMMSeqs:
    """Stands in for MMSeqsUtils at the two methods PaperBLASTUtils calls."""

    def __init__(self, hits=None, fail_on_search=False):
        self.hits = list(hits or [])
        self.fail_on_search = fail_on_search
        self.build_calls = []
        self.search_calls = []

    def build_search_db(self, proteins, out_dir, threads=1):
        self.build_calls.append(
            {"proteins": list(proteins), "out_dir": Path(out_dir), "threads": threads}
        )
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        db_path = out_path / "searchDB"
        db_path.write_text("fake mmseqs db")
        return db_path

    def search_proteins(self, proteins, db_path, **kwargs):
        if self.fail_on_search:
            raise AssertionError("search_proteins should not have been called")
        self.search_calls.append(
            {
                "proteins": list(proteins),
                "db_path": Path(db_path),
                "kwargs": dict(kwargs),
            }
        )
        return [dict(hit) for hit in self.hits]


class FakeHeadResponse:
    """Minimal stand-in for a requests HEAD response."""

    def __init__(self, size, last_modified, status_code=200):
        self.status_code = status_code
        self.headers = {
            "Content-Length": str(size),
            "Last-Modified": last_modified,
            "Accept-Ranges": "bytes",
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("unexpected HTTP {}".format(self.status_code))


class FakeGetResponse:
    """Minimal stand-in for a streaming requests GET response."""

    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(body))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("unexpected HTTP {}".format(self.status_code))

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[start:start + chunk_size]


class FakeHTTPSession:
    """Stands in for requests.Session at ``head`` and ``get``.

    Args:
        bodies: ``{file name: bytes}`` the server is serving.
        last_modified: Value returned in the ``Last-Modified`` header.
        fail_on_get: Raise if any download is attempted -- which is how the
            skip-when-unchanged test proves no bytes moved.
        honour_range: When False, answer a Range request with HTTP 200 and
            the whole body, like a server that does not support ranges.
    """

    def __init__(
        self, bodies, last_modified="Tue, 26 Aug 2026 01:02:03 GMT",
        fail_on_get=False, honour_range=True,
    ):
        self.bodies = dict(bodies)
        self.last_modified = last_modified
        self.fail_on_get = fail_on_get
        self.honour_range = honour_range
        self.head_calls = []
        self.get_calls = []

    @staticmethod
    def _name(url):
        return url.rsplit("/", 1)[-1]

    def head(self, url, timeout=None, allow_redirects=False):
        name = self._name(url)
        self.head_calls.append(name)
        body = self.bodies[name]
        return FakeHeadResponse(len(body), self.last_modified)

    def get(self, url, headers=None, stream=False, timeout=None):
        name = self._name(url)
        if self.fail_on_get:
            raise AssertionError("no download expected, but GET {} happened".format(name))
        self.get_calls.append((name, dict(headers or {})))
        body = self.bodies[name]
        range_header = (headers or {}).get("Range")
        if range_header and self.honour_range:
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            return FakeGetResponse(body[start:], status_code=206)
        return FakeGetResponse(body, status_code=200)


def make_utils(tmp_path, data_dir=None, mmseqs=None, session=None, **kwargs):
    """Build a PaperBLASTUtils isolated from the host's config and HOME."""
    return PaperBLASTUtils(
        data_dir=data_dir,
        session=session,
        cache=LiteratureCache(Path(tmp_path) / "cache.sqlite"),
        mmseqs=mmseqs,
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def hit(
    subject_id=REPRESENTATIVE_ID,
    identity=0.97,
    query_coverage=0.99,
    subject_coverage=0.42,
    evalue=1e-50,
    bitscore=300.0,
):
    """A search hit, as :meth:`PaperBLASTUtils.search` returns them."""
    return {
        "subject_id": subject_id,
        "identity": identity,
        "query_coverage": query_coverage,
        "subject_coverage": subject_coverage,
        "evalue": evalue,
        "bitscore": bitscore,
    }


def raw_hit(
    query_id="gene1",
    target_id=REPRESENTATIVE_ID,
    identity=0.97,
    coverage=0.99,
    subject_coverage=0.42,
    evalue=1e-50,
    bits=300.0,
):
    """A hit in the shape MMSeqsUtils.search_proteins returns."""
    return {
        "query_id": query_id,
        "target_id": target_id,
        "identity": identity,
        "coverage": coverage,
        "subject_coverage": subject_coverage,
        "evalue": evalue,
        "bits": bits,
    }


# ── helpers ──────────────────────────────────────────────────────────────


class TestHelpers:
    """The small parsers that the snapshot and the joins depend on."""

    def test_parse_stats_keeps_a_value_containing_spaces(self):
        stats = parse_stats("nSeq=842060 date=August 25 2026 nPaper=1322268")
        assert stats == {
            "nSeq": 842060,
            "date": "August 25 2026",
            "nPaper": 1322268,
        }

    def test_parse_stats_of_junk_is_empty(self):
        assert parse_stats("<html>404</html>") == {}

    def test_split_db_id(self):
        assert split_db_id("SwissProt::P12345") == ("SwissProt", "P12345")
        assert split_db_id("ACIAD0380") == (None, "ACIAD0380")
        # A trailing separator is not a db prefix.
        assert split_db_id("weird::") == (None, "weird::")


# ── data directory ───────────────────────────────────────────────────────


class TestDataDirRefusals:
    """A 3.5 GB snapshot must not land in a synced folder."""

    def test_dropbox_path_is_refused(self, tmp_path):
        utils = make_utils(tmp_path)
        dropbox_dir = tmp_path / "Dropbox" / "Projects" / "paperblast"
        with pytest.raises(ValueError, match="Dropbox"):
            utils.resolve_data_dir(dropbox_dir)

    def test_dropbox_path_is_refused_by_ensure_snapshot(self, tmp_path):
        session = FakeHTTPSession({}, fail_on_get=True)
        utils = make_utils(tmp_path, session=session)
        with pytest.raises(ValueError, match="Dropbox"):
            utils.ensure_snapshot(tmp_path / "Dropbox" / "pb")
        assert session.head_calls == []

    def test_dropbox_path_is_refused_by_search_and_papers_for_hits(self, tmp_path):
        utils = make_utils(tmp_path, mmseqs=FakeMMSeqs(fail_on_search=True))
        dropbox_dir = tmp_path / "Dropbox" / "pb"
        with pytest.raises(ValueError, match="Dropbox"):
            utils.search({"gene1": REP_SEQ}, data_dir=dropbox_dir)
        with pytest.raises(ValueError, match="Dropbox"):
            utils.papers_for_hits({"gene1": [hit()]}, data_dir=dropbox_dir)

    def test_symlink_into_dropbox_is_refused(self, tmp_path):
        """The check runs after realpath, so a symlink cannot smuggle it in."""
        real = tmp_path / "Dropbox" / "pb"
        real.mkdir(parents=True)
        link = tmp_path / "innocent-looking"
        link.symlink_to(real)

        utils = make_utils(tmp_path)
        with pytest.raises(ValueError, match="Dropbox"):
            utils.resolve_data_dir(link)

    def test_dropbox_path_is_allowed_when_asked_for_explicitly(self, tmp_path):
        utils = make_utils(tmp_path)
        dropbox_dir = tmp_path / "Dropbox" / "pb"
        resolved = utils.resolve_data_dir(dropbox_dir, allow_dropbox=True)
        assert resolved.name == "pb"

    def test_constructor_flag_also_allows_dropbox(self, tmp_path):
        utils = make_utils(tmp_path, allow_dropbox=True)
        assert utils.resolve_data_dir(tmp_path / "Dropbox" / "pb").name == "pb"

    def test_data_dir_comes_from_config_then_home(self, tmp_path, monkeypatch):
        configured = tmp_path / "from-config"
        utils = PaperBLASTUtils(
            config={"paperblast": {"data_dir": str(configured)}},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        assert utils.resolve_data_dir() == Path(configured.resolve())

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        plain = PaperBLASTUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )
        assert plain.resolve_data_dir().match("*/.kbutillib/paperblast")


# ── snapshot ─────────────────────────────────────────────────────────────


class TestEnsureSnapshot:
    """Downloading, resuming, validating and skipping the three files."""

    @staticmethod
    def _bodies(tmp_path):
        """The three published files, as the bytes a server would serve."""
        staging = tmp_path / "staging"
        make_snapshot(staging)
        return {
            LITSEARCH_DB_FILE: (staging / LITSEARCH_DB_FILE).read_bytes(),
            UNIQ_FAA_FILE: (staging / UNIQ_FAA_FILE).read_bytes(),
            STATS_FILE: (staging / STATS_FILE).read_bytes(),
        }

    def test_downloads_validates_and_records_a_manifest(self, tmp_path):
        bodies = self._bodies(tmp_path)
        session = FakeHTTPSession(bodies)
        data_dir = tmp_path / "pb"
        utils = make_utils(tmp_path, session=session)

        snapshot = utils.ensure_snapshot(data_dir)

        for name, body in bodies.items():
            assert (data_dir / name).read_bytes() == body
            assert snapshot["files"][name]["size"] == len(body)
            assert snapshot["files"][name]["sha256"] == sha256_file(data_dir / name)
            assert snapshot["files"][name]["last_modified"] == session.last_modified
            # Nothing is left staged.
            assert not (data_dir / (name + ".part")).exists()

        assert snapshot["stats"]["nSeq"] == 3
        assert snapshot["stats"]["date"] == "August 25 2026"
        assert snapshot["downloaded_at"]
        assert json.loads((data_dir / SNAPSHOT_MANIFEST).read_text()) == snapshot
        assert sorted(session.head_calls) == sorted(bodies)

    def test_skips_download_when_sizes_and_last_modified_match(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        bodies = {
            name: (data_dir / name).read_bytes()
            for name in (LITSEARCH_DB_FILE, UNIQ_FAA_FILE, STATS_FILE)
        }
        # fail_on_get: any byte moved is a test failure.
        session = FakeHTTPSession(bodies, fail_on_get=True)
        utils = make_utils(tmp_path, session=session)

        before = json.loads((data_dir / SNAPSHOT_MANIFEST).read_text())
        snapshot = utils.ensure_snapshot(data_dir)

        assert len(session.head_calls) == 3
        assert session.get_calls == []
        assert snapshot["files"] == before["files"]

    def test_redownloads_when_last_modified_changed(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        bodies = {
            name: (data_dir / name).read_bytes()
            for name in (LITSEARCH_DB_FILE, UNIQ_FAA_FILE, STATS_FILE)
        }
        session = FakeHTTPSession(bodies, last_modified="Wed, 27 Aug 2026 00:00:00 GMT")
        utils = make_utils(tmp_path, session=session)

        snapshot = utils.ensure_snapshot(data_dir)

        assert len(session.get_calls) == 3
        for entry in snapshot["files"].values():
            assert entry["last_modified"] == "Wed, 27 Aug 2026 00:00:00 GMT"

    def test_redownloads_when_the_local_file_is_truncated(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        bodies = {
            name: (data_dir / name).read_bytes()
            for name in (LITSEARCH_DB_FILE, UNIQ_FAA_FILE, STATS_FILE)
        }
        # Manifest still says the file is whole; the file on disk is not.
        (data_dir / UNIQ_FAA_FILE).write_text(">truncated\n")
        session = FakeHTTPSession(bodies)
        utils = make_utils(tmp_path, session=session)

        utils.ensure_snapshot(data_dir)

        assert [name for name, _ in session.get_calls] == [UNIQ_FAA_FILE]
        assert (data_dir / UNIQ_FAA_FILE).read_bytes() == bodies[UNIQ_FAA_FILE]

    def test_force_redownloads_everything(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        bodies = {
            name: (data_dir / name).read_bytes()
            for name in (LITSEARCH_DB_FILE, UNIQ_FAA_FILE, STATS_FILE)
        }
        session = FakeHTTPSession(bodies)
        utils = make_utils(tmp_path, session=session)

        utils.ensure_snapshot(data_dir, force=True)

        assert sorted(name for name, _ in session.get_calls) == sorted(bodies)

    def test_resumes_a_partial_download_with_a_range_request(self, tmp_path):
        bodies = self._bodies(tmp_path)
        data_dir = tmp_path / "pb"
        data_dir.mkdir()
        faa_body = bodies[UNIQ_FAA_FILE]
        (data_dir / (UNIQ_FAA_FILE + ".part")).write_bytes(faa_body[:20])

        session = FakeHTTPSession(bodies)
        utils = make_utils(tmp_path, session=session)
        utils.ensure_snapshot(data_dir)

        ranges = dict(session.get_calls)
        assert ranges[UNIQ_FAA_FILE].get("Range") == "bytes=20-"
        assert (data_dir / UNIQ_FAA_FILE).read_bytes() == faa_body

    def test_restarts_when_the_server_ignores_the_range_request(self, tmp_path):
        bodies = self._bodies(tmp_path)
        data_dir = tmp_path / "pb"
        data_dir.mkdir()
        faa_body = bodies[UNIQ_FAA_FILE]
        (data_dir / (UNIQ_FAA_FILE + ".part")).write_bytes(faa_body[:20])

        # honour_range=False: HTTP 200 with the whole body. Appending that to
        # the partial file would duplicate the first 20 bytes.
        session = FakeHTTPSession(bodies, honour_range=False)
        utils = make_utils(tmp_path, session=session)
        utils.ensure_snapshot(data_dir)

        assert (data_dir / UNIQ_FAA_FILE).read_bytes() == faa_body

    def test_a_complete_partial_file_is_not_refetched(self, tmp_path):
        bodies = self._bodies(tmp_path)
        data_dir = tmp_path / "pb"
        data_dir.mkdir()
        (data_dir / (STATS_FILE + ".part")).write_bytes(bodies[STATS_FILE])

        session = FakeHTTPSession(bodies)
        utils = make_utils(tmp_path, session=session)
        utils.ensure_snapshot(data_dir)

        assert STATS_FILE not in [name for name, _ in session.get_calls]
        assert (data_dir / STATS_FILE).read_bytes() == bodies[STATS_FILE]

    def test_a_corrupt_sqlite_download_is_rejected_and_not_renamed(self, tmp_path):
        bodies = self._bodies(tmp_path)
        bodies[LITSEARCH_DB_FILE] = b"<html>502 Bad Gateway</html>"
        session = FakeHTTPSession(bodies)
        data_dir = tmp_path / "pb"
        utils = make_utils(tmp_path, session=session)

        with pytest.raises(RuntimeError, match="SQLite"):
            utils.ensure_snapshot(data_dir)

        assert not (data_dir / LITSEARCH_DB_FILE).exists()
        assert not (data_dir / SNAPSHOT_MANIFEST).exists()

    def test_a_non_fasta_download_is_rejected(self, tmp_path):
        bodies = self._bodies(tmp_path)
        bodies[UNIQ_FAA_FILE] = b"not a fasta file at all\n"
        session = FakeHTTPSession(bodies)
        data_dir = tmp_path / "pb"
        utils = make_utils(tmp_path, session=session)

        with pytest.raises(RuntimeError, match="FASTA header"):
            utils.ensure_snapshot(data_dir)
        assert not (data_dir / UNIQ_FAA_FILE).exists()

    def test_a_bogus_stats_download_is_rejected(self, tmp_path):
        bodies = self._bodies(tmp_path)
        bodies[STATS_FILE] = b"<html>404</html>"
        session = FakeHTTPSession(bodies)
        utils = make_utils(tmp_path, session=session)

        with pytest.raises(RuntimeError, match="nSeq"):
            utils.ensure_snapshot(tmp_path / "pb")

    def test_a_wrong_size_download_is_rejected_and_the_part_kept(self, tmp_path):
        bodies = self._bodies(tmp_path)
        session = FakeHTTPSession(bodies)
        # Lie about the length in the HEAD so the GET comes up short.
        real_head = session.head

        def short_head(url, **kwargs):
            response = real_head(url, **kwargs)
            if url.endswith(STATS_FILE):
                response.headers["Content-Length"] = "99999"
            return response

        session.head = short_head
        data_dir = tmp_path / "pb"
        utils = make_utils(tmp_path, session=session)

        with pytest.raises(RuntimeError, match="expected 99999"):
            utils.ensure_snapshot(data_dir)

        assert not (data_dir / STATS_FILE).exists()
        # Kept so the next run can resume rather than restart.
        assert (data_dir / (STATS_FILE + ".part")).exists()

    def test_load_snapshot_requires_a_manifest(self, tmp_path):
        utils = make_utils(tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        assert utils.load_snapshot(empty, required=False) is None
        with pytest.raises(RuntimeError, match="ensure_snapshot"):
            utils.load_snapshot(empty)


# ── search ───────────────────────────────────────────────────────────────


class TestSearch:
    """Hit shaping, threshold plumbing, truncation and caching."""

    def test_returns_hits_with_exactly_the_documented_keys(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit()])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        results = utils.search({"gene1": REP_SEQ})

        assert list(results) == ["gene1"]
        assert len(results["gene1"]) == 1
        found = results["gene1"][0]
        assert set(found) == {
            "subject_id",
            "identity",
            "query_coverage",
            "subject_coverage",
            "evalue",
            "bitscore",
        }
        assert found["subject_id"] == REPRESENTATIVE_ID
        assert found["identity"] == pytest.approx(0.97)
        assert found["bitscore"] == pytest.approx(300.0)

    def test_query_and_subject_coverage_are_kept_separate(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit(coverage=0.99, subject_coverage=0.42)])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        found = utils.search({"gene1": REP_SEQ})["gene1"][0]

        assert found["query_coverage"] == pytest.approx(0.99)
        assert found["subject_coverage"] == pytest.approx(0.42)
        assert found["query_coverage"] != found["subject_coverage"]

    def test_thresholds_and_subject_coverage_are_passed_to_mmseqs(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit()])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        utils.search(
            {"gene1": REP_SEQ},
            evalue=1e-7,
            max_hits=11,
            min_identity=0.44,
            min_query_coverage=0.55,
            threads=3,
        )

        kwargs = mmseqs.search_calls[0]["kwargs"]
        assert kwargs["min_seq_id"] == 0.44
        assert kwargs["coverage"] == 0.55
        assert kwargs["evalue"] == 1e-7
        assert kwargs["max_hits"] == 11
        assert kwargs["threads"] == 3
        assert kwargs["include_subject_coverage"] is True

    def test_hits_are_sorted_by_evalue_and_truncated_to_max_hits(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs(
            [
                raw_hit(target_id="third", evalue=1e-3, bits=50.0),
                raw_hit(target_id="first", evalue=1e-80, bits=400.0),
                raw_hit(target_id="second", evalue=1e-20, bits=200.0),
            ]
        )
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        results = utils.search({"gene1": REP_SEQ}, max_hits=2)

        assert [h["subject_id"] for h in results["gene1"]] == ["first", "second"]

    def test_a_gene_with_no_hits_gets_an_empty_list(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit(query_id="gene1")])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        results = utils.search({"gene1": REP_SEQ, "gene2": CURATED_SEQ})

        assert results["gene2"] == []

    def test_the_mmseqs_db_is_built_once_from_uniq_faa(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit()])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        utils.search({"gene1": REP_SEQ})
        utils.search({"gene2": CURATED_SEQ})

        assert len(mmseqs.build_calls) == 1
        built = mmseqs.build_calls[0]
        assert [p["id"] for p in built["proteins"]] == [
            REPRESENTATIVE_ID,
            CURATED_ID,
            "unrelated1",
        ]
        assert built["proteins"][0]["protein_translation"] == REP_SEQ
        # The database directory is bound to the uniq.faa sha.
        faa_sha = sha256_file(data_dir / UNIQ_FAA_FILE)
        assert built["out_dir"].name == faa_sha[:16]

    def test_the_mmseqs_db_is_rebuilt_when_uniq_faa_changes(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit()])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)
        utils.search({"gene1": REP_SEQ})

        # A new snapshot with a different uniq.faa, as a refresh would leave.
        make_snapshot(
            data_dir, last_modified="Wed, 27 Aug 2026 00:00:00 GMT"
        )
        (data_dir / UNIQ_FAA_FILE).write_text(
            fasta_text(DEFAULT_FAA_RECORDS + [("extra1", UNRELATED_SEQ)])
        )
        manifest = json.loads((data_dir / SNAPSHOT_MANIFEST).read_text())
        manifest["files"][UNIQ_FAA_FILE]["sha256"] = sha256_file(
            data_dir / UNIQ_FAA_FILE
        )
        (data_dir / SNAPSHOT_MANIFEST).write_text(json.dumps(manifest))

        utils.search({"gene1": REP_SEQ})

        assert len(mmseqs.build_calls) == 2
        assert mmseqs.build_calls[0]["out_dir"] != mmseqs.build_calls[1]["out_dir"]

    def test_hits_are_cached_per_sequence(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit()])
        cache = LiteratureCache(tmp_path / "cache.sqlite")
        utils = PaperBLASTUtils(
            data_dir=data_dir,
            cache=cache,
            mmseqs=mmseqs,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )

        first = utils.search({"gene1": REP_SEQ})
        second = utils.search({"gene1": REP_SEQ})

        assert first == second
        assert len(mmseqs.search_calls) == 1

        # The entry really is in the shared cache table, under a sha key.
        with sqlite3.connect(str(tmp_path / "cache.sqlite")) as connection:
            keys = [
                row[0]
                for row in connection.execute(
                    'SELECT key FROM "{}"'.format(HITS_CACHE_TABLE)
                )
            ]
        assert len(keys) == 1 and len(keys[0]) == 64

    def test_whitespace_in_a_sequence_does_not_miss_the_cache(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit()])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        utils.search({"gene1": REP_SEQ})
        utils.search({"gene1": REP_SEQ[:30] + "\n" + REP_SEQ[30:].lower()})

        assert len(mmseqs.search_calls) == 1

    def test_only_uncached_sequences_are_searched(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit(query_id="gene2", target_id=CURATED_ID)])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        utils.search({"gene1": REP_SEQ})
        results = utils.search({"gene1": REP_SEQ, "gene2": CURATED_SEQ})

        assert len(mmseqs.search_calls) == 2
        assert [p["id"] for p in mmseqs.search_calls[1]["proteins"]] == ["gene2"]
        assert results["gene2"][0]["subject_id"] == CURATED_ID

    @pytest.mark.parametrize(
        "changed",
        [
            {"evalue": 1e-10},
            {"max_hits": 10},
            {"min_identity": 0.9},
            {"min_query_coverage": 0.9},
        ],
    )
    def test_no_cross_parameter_cache_reuse(self, tmp_path, changed):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit()])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)

        utils.search({"gene1": REP_SEQ})
        utils.search({"gene1": REP_SEQ}, **changed)

        assert len(mmseqs.search_calls) == 2

    def test_a_new_snapshot_invalidates_cached_hits(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        mmseqs = FakeMMSeqs([raw_hit()])
        utils = make_utils(tmp_path, data_dir=data_dir, mmseqs=mmseqs)
        utils.search({"gene1": REP_SEQ})

        manifest = json.loads((data_dir / SNAPSHOT_MANIFEST).read_text())
        manifest["files"][LITSEARCH_DB_FILE]["sha256"] = "f" * 64
        (data_dir / SNAPSHOT_MANIFEST).write_text(json.dumps(manifest))

        utils.search({"gene1": REP_SEQ})

        assert len(mmseqs.search_calls) == 2

    def test_search_never_downloads_implicitly(self, tmp_path):
        """No snapshot means a clear error, not a surprise 3.5 GB download."""
        utils = make_utils(tmp_path, mmseqs=FakeMMSeqs(fail_on_search=True))
        with pytest.raises(RuntimeError, match="ensure_snapshot"):
            utils.search({"gene1": REP_SEQ}, data_dir=tmp_path / "absent")

    def test_empty_queries_touch_nothing(self, tmp_path):
        utils = make_utils(tmp_path, mmseqs=FakeMMSeqs(fail_on_search=True))
        assert utils.search({}) == {}

    def test_an_empty_sequence_is_rejected(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(
            tmp_path, data_dir=data_dir, mmseqs=FakeMMSeqs(fail_on_search=True)
        )
        with pytest.raises(ValueError, match="empty protein sequence"):
            utils.search({"gene1": "   \n  "})


# ── literature join ──────────────────────────────────────────────────────


class TestPapersForHits:
    """Joining hits to papers: duplicates, curation, snippets and schema."""

    def test_duplicate_expansion_finds_the_duplicates_paper(self, tmp_path):
        """The core of the data model: the paper hangs off dupA, not repA.

        dupA is not in uniq.faa at all, so without SeqToDuplicate expansion
        this paper is invisible.
        """
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits({"gene1": [hit(REPRESENTATIVE_ID)]})["gene1"]

        by_pmid = {link["pmid"]: link for link in links}
        assert "2002" in by_pmid, "the duplicate's paper was not reached"
        duplicate_link = by_pmid["2002"]
        assert duplicate_link["homolog_id"] == DUPLICATE_ID
        assert duplicate_link["pmcid"] == "PMC2002"
        assert duplicate_link["doi"] == "10.1/duplicate"
        assert duplicate_link["title"] == "Paper reachable ONLY through SeqToDuplicate"
        assert duplicate_link["journal"] == "Journal Two"
        assert duplicate_link["year"] == 2021
        assert duplicate_link["homolog_organism"] == "Escherichia coli K-12"
        assert duplicate_link["snippet"] == (
            "the dnaA homolog was essential for growth"
        )
        assert duplicate_link["curated_db"] is None
        assert duplicate_link["source"] == "paperblast"

        # The representative's own paper is there too.
        assert by_pmid["1001"]["homolog_id"] == REPRESENTATIVE_ID

    def test_expansion_works_from_the_duplicate_side_too(self, tmp_path):
        """A link may be on either side, so the group is closed both ways."""
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits({"gene1": [hit(DUPLICATE_ID)]})["gene1"]

        pmids = {link["pmid"] for link in links}
        assert {"1001", "2002"} <= pmids

    def test_unrelated_genes_are_not_dragged_in(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits({"gene1": [hit(REPRESENTATIVE_ID)]})["gene1"]

        assert "9009" not in {link["pmid"] for link in links}

    def test_curated_hits_carry_curated_db(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits({"gene1": [hit(CURATED_ID)]})["gene1"]

        curated = [link for link in links if link["pmid"] == "4004"]
        assert len(curated) == 1
        assert curated[0]["curated_db"] == "SwissProt"
        assert curated[0]["homolog_id"] == CURATED_ID
        assert curated[0]["homolog_organism"] == "Bacillus subtilis"

    def test_non_curated_links_have_curated_db_none(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits({"gene1": [hit(REPRESENTATIVE_ID)]})["gene1"]

        assert all(link["curated_db"] is None for link in links)

    def test_generif_comments_become_links(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits({"gene1": [hit(REPRESENTATIVE_ID)]})["gene1"]

        generif = [link for link in links if link["pmid"] == "3003"]
        assert len(generif) == 1
        assert generif[0]["snippet"].startswith("GeneRIF:")

    def test_links_carry_exactly_the_documented_keys(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits({"gene1": [hit(REPRESENTATIVE_ID)]})["gene1"]

        assert links
        for link in links:
            assert set(link) == set(LINK_KEYS)

    def test_the_alignment_numbers_are_carried_onto_every_link(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        source = hit(
            REPRESENTATIVE_ID,
            identity=0.81,
            query_coverage=0.95,
            subject_coverage=0.33,
            evalue=1e-30,
        )
        links = utils.papers_for_hits({"gene1": [source]})["gene1"]

        assert links
        for link in links:
            assert link["identity"] == pytest.approx(0.81)
            # The two coverages stay distinct all the way through the join.
            assert link["query_coverage"] == pytest.approx(0.95)
            assert link["subject_coverage"] == pytest.approx(0.33)
            assert link["query_coverage"] != link["subject_coverage"]
            assert link["evalue"] == pytest.approx(1e-30)

    def test_links_are_sorted_best_evalue_first(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits(
            {
                "gene1": [
                    hit(CURATED_ID, evalue=1e-5),
                    hit(REPRESENTATIVE_ID, evalue=1e-60),
                ]
            }
        )["gene1"]

        assert [link["evalue"] for link in links] == sorted(
            link["evalue"] for link in links
        )
        assert links[0]["homolog_id"] in {REPRESENTATIVE_ID, DUPLICATE_ID}

    def test_the_same_paper_from_two_hits_is_not_duplicated(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        links = utils.papers_for_hits(
            {"gene1": [hit(REPRESENTATIVE_ID), hit(DUPLICATE_ID)]}
        )["gene1"]

        fingerprints = [
            (link["homolog_id"], link["pmid"], link["curated_db"]) for link in links
        ]
        assert len(fingerprints) == len(set(fingerprints))

    def test_every_input_gene_appears_in_the_result(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        results = utils.papers_for_hits(
            {"gene1": [hit(REPRESENTATIVE_ID)], "gene2": [], "gene3": [hit("absent")]}
        )

        assert set(results) == {"gene1", "gene2", "gene3"}
        assert results["gene2"] == []
        assert results["gene3"] == []

    def test_empty_input_touches_nothing(self, tmp_path):
        utils = make_utils(tmp_path, data_dir=tmp_path / "absent")
        assert utils.papers_for_hits({}) == {}

    def test_the_database_is_opened_read_only(self, tmp_path, monkeypatch):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = make_utils(tmp_path, data_dir=data_dir)

        from kbutillib.domains.literature import paperblast as module

        captured = {}
        real_connect = sqlite3.connect

        def spy(target, *args, **kwargs):
            captured["target"] = target
            captured["kwargs"] = dict(kwargs)
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(module.sqlite3, "connect", spy)
        utils.papers_for_hits({"gene1": [hit(REPRESENTATIVE_ID)]})

        assert captured["kwargs"].get("uri") is True
        assert "mode=ro" in captured["target"]

        # And prove it: the connection it opens cannot write.
        connection = sqlite3.connect(captured["target"], uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("DELETE FROM GenePaper")
        finally:
            connection.close()

    def test_a_missing_snapshot_is_a_clear_error(self, tmp_path):
        utils = make_utils(tmp_path)
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(RuntimeError, match="ensure_snapshot"):
            utils.papers_for_hits({"gene1": [hit()]}, data_dir=empty)

    def test_a_missing_required_column_names_the_column(self, tmp_path):
        data_dir = tmp_path / "pb"
        make_snapshot(data_dir)
        build_litsearch_db(
            data_dir / LITSEARCH_DB_FILE, omit_column=("GenePaper", "pmcId")
        )
        utils = make_utils(tmp_path, data_dir=data_dir)

        with pytest.raises(RuntimeError, match="pmcId"):
            utils.papers_for_hits({"gene1": [hit(REPRESENTATIVE_ID)]})

    def test_a_missing_required_table_names_the_table(self, tmp_path):
        data_dir = tmp_path / "pb"
        make_snapshot(data_dir)
        build_litsearch_db(
            data_dir / LITSEARCH_DB_FILE, omit_table="SeqToDuplicate"
        )
        utils = make_utils(tmp_path, data_dir=data_dir)

        with pytest.raises(RuntimeError, match="SeqToDuplicate"):
            utils.papers_for_hits({"gene1": [hit(REPRESENTATIVE_ID)]})


class TestImplWrapper:
    """The composition wrapper delegates and carries config across."""

    def test_impl_delegates_and_keeps_the_env(self, tmp_path):
        from kbutillib.core.shared_env_utils import SharedEnvUtils

        data_dir = tmp_path / "configured"
        env = SharedEnvUtils(
            config={"paperblast": {"data_dir": str(data_dir)}},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        impl = PaperBLASTUtilsImpl(env)

        assert impl.env is env
        # The paperblast.data_dir from the env's config reached the delegate.
        assert impl.resolve_data_dir() == Path(str(data_dir))
        assert callable(impl.ensure_snapshot)
        assert callable(impl.papers_for_hits)


# ── no injection: the real MMseqs2 path ──────────────────────────────────


def _mmseqs_installed():
    """True when an mmseqs binary is on PATH and answers ``version``."""
    if shutil.which("mmseqs") is None:
        return False
    try:
        return (
            subprocess.run(
                ["mmseqs", "version"], capture_output=True, timeout=60
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _mmseqs_installed(), reason="no mmseqs executable found")
class TestRealMMseqsPath:
    """search -> papers_for_hits with NOTHING injected.

    No fake mmseqs, no fake search output: a real ``mmseqs createdb /
    createindex / search / convertalis`` over the fixture ``uniq.faa``, and
    the real hits it returns fed into the real join.  This is the test that
    would catch a wrong convertalis column order or a misread tcov -- a fake
    search output cannot.
    """

    def test_identical_query_finds_its_homolog_and_its_papers(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = PaperBLASTUtils(
            data_dir=data_dir,
            cache=LiteratureCache(tmp_path / "cache.sqlite"),
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )

        hits = utils.search(
            {"gene1": REP_SEQ},
            evalue=1e-3,
            max_hits=10,
            min_identity=0.9,
            min_query_coverage=0.9,
        )

        assert hits["gene1"], "the real mmseqs search found no hit on an identical sequence"
        best = hits["gene1"][0]
        assert best["subject_id"] == REPRESENTATIVE_ID
        assert 0.9 <= best["identity"] <= 1.0
        assert 0.9 <= best["query_coverage"] <= 1.0
        # tcov really came back from convertalis, as a fraction.
        assert best["subject_coverage"] is not None
        assert 0.0 < best["subject_coverage"] <= 1.0
        assert best["bitscore"] > 0.0
        # The thresholds filtered the unrelated sequence out.
        assert all(h["subject_id"] != "unrelated1" for h in hits["gene1"])

        links = utils.papers_for_hits(hits)["gene1"]
        pmids = {link["pmid"] for link in links}
        assert "1001" in pmids
        assert "2002" in pmids, "SeqToDuplicate expansion failed on the real path"
        for link in links:
            assert set(link) == set(LINK_KEYS)
            assert link["source"] == "paperblast"

    def test_the_built_database_is_reused_on_a_second_search(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = PaperBLASTUtils(
            data_dir=data_dir,
            cache=LiteratureCache(tmp_path / "cache.sqlite"),
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )

        first = utils.search({"gene1": REP_SEQ}, min_identity=0.9)
        # A different gene id and sequence: a cache miss, so it searches
        # again -- but against the database already on disk.
        second = utils.search({"gene2": CURATED_SEQ}, min_identity=0.9)

        assert first["gene1"][0]["subject_id"] == REPRESENTATIVE_ID
        assert second["gene2"][0]["subject_id"] == CURATED_ID
        db_dirs = list((data_dir / "mmseqs").iterdir())
        assert len(db_dirs) == 1
