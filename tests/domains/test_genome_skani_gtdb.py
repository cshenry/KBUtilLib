"""Tests for SKANIUtils' GTDB-Tk reference package support (register_gtdb_database, classify_genomes_gtdb).

Offline: skani is never invoked; subprocess.run is patched to write skani search output."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.domains.genome.skani_utils import SKANIUtils

BAC = "d__Bacteria;p__Gemmatimonadota;c__Gemmatimonadetes;o__Gemmatimonadales;f__Gemmatimonadaceae;g__JABFXC01;s__JABFXC01 sp036392515"
ARC = "d__Archaea;p__Methanobacteriota;c__Methanobacteria;o__Methanobacteriales;f__Methanobacteriaceae;g__Methanobacterium_A;s__Methanobacterium_A sp017883605"
OTHER = "d__Bacteria;p__Pseudomonadota;c__Gammaproteobacteria;o__Burkholderiales;f__Burkholderiaceae;g__Ralstonia;s__Ralstonia pickettii"


def make_package(root: Path) -> Path:
    """A minimal GTDB-Tk reference package layout (release232/) with a fake skani database."""
    pkg = root / "release232"
    db = pkg / "skani" / "database"
    db.mkdir(parents=True)
    for f in ("sketches.db", "index.db", "markers.bin"):
        (db / f).write_bytes(b"")
    (db / "metadata.tsv").write_text("skani_version:0.3.1\ndate_created:2026-03-27T19:35:55\nnum_genomes_sketches:3\n")
    (pkg / "metadata").mkdir()
    (pkg / "metadata" / "metadata.txt").write_text('RED_DIST_BAC_DICT={"d__": 0.00}\nVERSION_DATA=r232\n')
    (pkg / "taxonomy").mkdir()
    (pkg / "taxonomy" / "gtdb_taxonomy.tsv").write_text(
        f"GB_GCA_036392515.1\t{BAC}\nGB_GCA_017883605.1\t{ARC}\nRS_GCF_000000001.1\t{OTHER}\n")
    (pkg / "radii").mkdir()
    (pkg / "radii" / "gtdb_radii.tsv").write_text(
        "s__JABFXC01 sp036392515\tGB_GCA_036392515.1\t95.000000\n"
        "s__Methanobacterium_A sp017883605\tGB_GCA_017883605.1\t96.500000\n"
        "s__Ralstonia pickettii\tRS_GCF_000000001.1\t95.000000\n")
    return pkg


@pytest.fixture
def utils(tmp_path):
    with patch.object(SKANIUtils, "_check_skani_availability"):
        u = SKANIUtils(cache_file=str(tmp_path / "skani_databases.json"), skani_executable="/opt/skani",
                       config_file=False, token_file=None, kbase_token_file=None)
    u.skani_available = True
    return u


def fake_search(rows):
    """subprocess.run stand-in that writes skani search output (rows: ref, query, ANI, AF_ref, AF_query)."""
    def run(cmd, **kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        lines = ["Ref_file\tQuery_file\tANI\tAlign_fraction_ref\tAlign_fraction_query\tRef_name\tQuery_name"]
        lines += ["\t".join(map(str, r)) + "\tref\tquery" for r in rows]
        out.write_text("\n".join(lines) + "\n")
        return MagicMock(returncode=0, stdout="", stderr="")
    return run


class TestCanonicalIds:
    @pytest.mark.parametrize("gid", ["GCF_005435135.1", "RS_GCF_005435135.1", "GB_GCA_005435135.1",
                                     "GCF_005435135.1_genomic.fna.gz", "database/GCF_005435135.1_genomic.fna.gz"])
    def test_canonical_forms(self, gid):
        assert SKANIUtils.canonical_gtdb_id(gid) == "G005435135"

    def test_genome_id_strips_fasta_and_gz(self):
        assert SKANIUtils._genome_id("/x/bin.001.fasta__SRR10127268.fna") == "bin.001.fasta__SRR10127268"
        assert SKANIUtils._genome_id("/x/genome.fa.gz") == "genome"


class TestRegisterGtdbDatabase:
    def test_registers_package(self, utils, tmp_path):
        pkg = make_package(tmp_path)
        res = utils.register_gtdb_database(pkg)
        assert res == {"database_name": "gtdb_r232", "database_path": str(pkg / "skani" / "database"),
                       "gtdb_release": "r232", "genome_count": 3, "registered": True}
        info = utils.get_database_info("gtdb_r232")
        assert info["metadata"]["type"] == "gtdb"
        assert info["metadata"]["skani_sketch_version"] == "0.3.1"

    def test_existing_entry_kept_unless_overwrite(self, utils, tmp_path):
        pkg = make_package(tmp_path)
        utils.register_gtdb_database(pkg)
        assert utils.register_gtdb_database(pkg)["registered"] is False
        assert utils.register_gtdb_database(pkg, overwrite=True)["registered"] is True

    def test_rejects_directory_without_skani_database(self, utils, tmp_path):
        with pytest.raises(ValueError, match="missing"):
            utils.register_gtdb_database(tmp_path)


class TestClassifyGenomesGtdb:
    def setup_queries(self, utils, tmp_path, names):
        pkg = make_package(tmp_path)
        utils.register_gtdb_database(pkg)
        paths = []
        for n in names:
            p = tmp_path / f"{n}.fna"
            p.write_text(">c1\nACGT\n")
            paths.append(p)
        return pkg, paths

    def test_gtdbtk_ani_screen_rule(self, utils, tmp_path):
        pkg, (a, b, c, d, e) = self.setup_queries(utils, tmp_path, ["assigned", "below", "lowaf", "nohit", "best"])
        db = pkg / "skani" / "database"
        rows = [
            # assigned: ANI 99.72 >= radius 95, AF = max(98.35, 60.0) = 0.9835
            (db / "GCA_036392515.1_genomic.fna.gz", a.resolve(), 99.72, 98.35, 60.0),
            # below radius: archaeal rep has radius 96.5
            (db / "GCA_017883605.1_genomic.fna.gz", b.resolve(), 96.0, 80.0, 70.0),
            # low AF: both AFs < 50
            (db / "GCF_000000001.1_genomic.fna.gz", c.resolve(), 99.0, 30.0, 40.0),
            # best: a low-AF higher-ANI hit is ignored; the AF-passing hit is used
            (db / "GCF_000000001.1_genomic.fna.gz", e.resolve(), 99.9, 10.0, 20.0),
            (db / "GCA_036392515.1_genomic.fna.gz", e.resolve(), 97.0, 55.0, 10.0),
        ]
        with patch("subprocess.run", side_effect=fake_search(rows)) as run:
            res = utils.classify_genomes_gtdb([a, b, c, d, e], "gtdb_r232", threads=4)
        cmd = run.call_args[0][0]
        assert cmd[:4] == ["/opt/skani", "search", "-d", str(db)] and "--ql" in cmd and "--short-header" in cmd
        assert res["assigned"]["status"] == "species assigned" and res["assigned"]["classification"] == BAC
        assert res["assigned"]["af"] == pytest.approx(0.9835)
        assert res["below"]["status"] == "below radius" and res["below"]["classification"] is None
        assert res["below"]["radius"] == 96.5
        assert res["lowaf"]["status"] == "low AF"
        assert res["nohit"] == {"status": "no hit", "classification": None, "reference": None, "ani": None,
                                "af": None, "radius": None, "reference_taxonomy": None, "hits": []}
        assert res["best"]["status"] == "species assigned" and res["best"]["reference"] == "GCA_036392515.1"
        assert [h["reference"] for h in res["best"]["hits"]] == ["GCF_000000001.1", "GCA_036392515.1"]

    def test_requires_registered_gtdb_database(self, utils, tmp_path):
        p = tmp_path / "q.fna"
        p.write_text(">c\nA\n")
        with pytest.raises(ValueError, match="register_gtdb_database"):
            utils.classify_genomes_gtdb(p, "nope")

    def test_skani_failure_raises(self, utils, tmp_path):
        _, (a,) = self.setup_queries(utils, tmp_path, ["x"])
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
            with pytest.raises(RuntimeError, match="boom"):
                utils.classify_genomes_gtdb(a, "gtdb_r232")

    def test_duplicate_genome_ids_rejected(self, utils, tmp_path):
        _, (a,) = self.setup_queries(utils, tmp_path, ["dup"])
        other = tmp_path / "sub"
        other.mkdir()
        (other / "dup.fna").write_text(">c\nA\n")
        with pytest.raises(ValueError, match="same genome ID"):
            utils.classify_genomes_gtdb([a, other / "dup.fna"], "gtdb_r232")
