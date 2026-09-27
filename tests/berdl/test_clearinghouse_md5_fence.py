"""Fence tests for the three foreign-system md5 assembly hashes.

Three sites compute an md5 that a KBase Assembly object or a BV-BRC genome
record REQUIRES:

  - src/kbutillib/domains/genome/kb_genome_utils.py  (kbase_assembly_md5)
  - src/kbutillib/domains/external/bvbrc_utils.py     (bvbrc_genome_md5)
  - src/kbutillib/domains/kbase/kb_reads_utils.py     (kbase_assembly_md5)

These are foreign-system interop values, NOT clearinghouse entity hashes.
They must not be deleted (deleting them breaks the objects we write), but
they must never be mistaken for platform identity: an md5 is 32 hex
characters and the clearinghouse ``entity_hash`` is a 64-hex sha256, so
:func:`clearinghouse_schema.encode_entity_hash` rejects an md5 outright --
that rejection is the fence, and this file asserts it holds.

These tests replicate the exact md5 recipe at each site (a plain
``hashlib.md5(...).hexdigest()``) rather than importing the heavy utility
classes, so the fence property is checkable without the optional scientific
deps those modules pull in. A static search then asserts the renamed,
provenance-carrying symbols never leak into the clearinghouse (``berdl/``)
tree, where an interop md5 has no business appearing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kbutillib.domains.kbase.berdl.clearinghouse_schema import encode_entity_hash

# The repo's src/ root, derived from this test file's location:
# tests/berdl/<this>  ->  ../../src
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
_BERDL_DIR = _SRC_ROOT / "kbutillib" / "domains" / "kbase" / "berdl"


def _kb_genome_utils_md5() -> str:
    """kb_genome_utils.load_genome_from_local_files: md5 of sorted contig
    sequences joined with the EMPTY string."""
    sorted_seqs = ["ACGTACGT", "TTTTGGGG"]
    return hashlib.md5("".join(sorted_seqs).encode()).hexdigest()


def _bvbrc_genome_md5() -> str:
    """bvbrc_utils: md5 of UNSORTED joined contig sequences."""
    contig_sequences = ["TTTTGGGG", "ACGTACGT"]
    return hashlib.md5("".join(contig_sequences).encode()).hexdigest()


def _kb_reads_assembly_md5() -> str:
    """kb_reads_utils: md5 over the comma-joined sorted per-contig md5s."""
    per_contig = [hashlib.md5(s.encode()).hexdigest() for s in ("ACGTACGT", "TTTTGGGG")]
    return hashlib.md5(",".join(sorted(per_contig)).encode()).hexdigest()


_MD5_SITES = {
    "kb_genome_utils.kbase_assembly_md5": _kb_genome_utils_md5,
    "bvbrc_utils.bvbrc_genome_md5": _bvbrc_genome_md5,
    "kb_reads_utils.kbase_assembly_md5": _kb_reads_assembly_md5,
}


@pytest.mark.parametrize("label", sorted(_MD5_SITES))
def test_assembly_md5_is_32_chars(label):
    """Each foreign-system md5 is a 32-character hex digest."""
    digest = _MD5_SITES[label]()
    assert len(digest) == 32, f"{label}: expected a 32-char md5, got {len(digest)}"
    assert all(c in "0123456789abcdef" for c in digest)


@pytest.mark.parametrize("label", sorted(_MD5_SITES))
def test_encode_entity_hash_rejects_assembly_md5(label):
    """encode_entity_hash requires 64 hex chars, so it rejects a 32-char md5.

    This is the fence: an interop md5 can never be stored as a clearinghouse
    entity_hash, so one cannot silently end up in the identity column.
    """
    digest = _MD5_SITES[label]()
    with pytest.raises(ValueError):
        encode_entity_hash(digest)


def test_renamed_md5_symbols_absent_under_berdl():
    """None of the renamed, provenance-carrying md5 symbols appears under
    the clearinghouse (berdl/) tree.

    A foreign-system interop value has no place in the identity subsystem;
    if one of these names shows up there it is a sign an interop md5 is being
    treated as an entity hash.
    """
    assert _BERDL_DIR.is_dir(), f"berdl dir not found at {_BERDL_DIR}"
    offenders: list[str] = []
    for py in _BERDL_DIR.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for symbol in ("kbase_assembly_md5", "bvbrc_genome_md5"):
            if symbol in text:
                offenders.append(f"{py.relative_to(_SRC_ROOT)}: {symbol}")
    assert not offenders, (
        "renamed foreign-system md5 symbols leaked into the clearinghouse "
        f"tree: {offenders!r}"
    )
