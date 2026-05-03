"""Tests for SQLite catalog — DDL creation, FK cascade, CHECK constraints, schema_version."""

from pathlib import Path

import pytest

from kbutillib.notebook.storage.catalog import CURRENT_SCHEMA_VERSION, Catalog


class TestCatalogDDL:
    """Verify DDL creates all expected tables and indices."""

    def test_tables_created(self, tmp_path: Path):
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db)
        conn = cat.conn
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "catalog_meta",
            "experiments",
            "experiment_parents",
            "strains",
            "mutations",
            "vectors",
            "vector_parents",
            "cache_objects",
            "access_log",
        }
        assert expected.issubset(tables)
        cat.close()

    def test_schema_version_stored(self, tmp_path: Path):
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db, project_name="test_proj")
        row = cat.conn.execute("SELECT * FROM catalog_meta").fetchone()
        assert row["schema_version"] == CURRENT_SCHEMA_VERSION
        assert row["project_name"] == "test_proj"
        cat.close()

    def test_idempotent_open(self, tmp_path: Path):
        """Opening the same DB twice should not fail or duplicate rows."""
        db = tmp_path / "catalog.sqlite"
        cat1 = Catalog(db, project_name="p1")
        _ = cat1.conn  # Force init
        cat1.close()

        cat2 = Catalog(db, project_name="p1")
        rows = cat2.conn.execute("SELECT COUNT(*) FROM catalog_meta").fetchone()[0]
        assert rows == 1
        cat2.close()


class TestFKCascade:
    """Verify ON DELETE CASCADE behavior."""

    def test_experiment_parents_cascade(self, tmp_path: Path):
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db)
        conn = cat.conn

        # Insert parent and child experiments
        conn.execute(
            "INSERT INTO experiments (id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            ("parent1", "sample", "{}", "2024-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO experiments (id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            ("child1", "computation", "{}", "2024-01-02T00:00:00"),
        )
        conn.execute(
            "INSERT INTO experiment_parents (child_id, parent_id) VALUES (?, ?)",
            ("child1", "parent1"),
        )
        conn.commit()

        # Delete child — should cascade to experiment_parents
        conn.execute("DELETE FROM experiments WHERE id='child1'")
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM experiment_parents WHERE child_id='child1'"
        ).fetchall()
        assert rows == []
        cat.close()

    def test_vector_parents_cascade(self, tmp_path: Path):
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db)
        conn = cat.conn

        # Need an experiment first (FK constraint)
        conn.execute(
            "INSERT INTO experiments (id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            ("exp1", "sample", "{}", "2024-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO vectors (id, experiment_id, type_domain, type_scale, "
            "entity_kind, entity_namespace, columns_json, n_entities, n_columns, "
            "parquet_path, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("v1", "exp1", "transcriptomics", "log2", "gene", "ecoli", "[]", 0, 0, "p", "h1", "2024-01-01"),
        )
        conn.execute(
            "INSERT INTO vectors (id, experiment_id, type_domain, type_scale, "
            "entity_kind, entity_namespace, columns_json, n_entities, n_columns, "
            "parquet_path, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("v2", "exp1", "transcriptomics", "log2", "gene", "ecoli", "[]", 0, 0, "p", "h2", "2024-01-01"),
        )
        conn.execute(
            "INSERT INTO vector_parents (child_id, parent_id) VALUES (?, ?)",
            ("v2", "v1"),
        )
        conn.commit()

        # Delete v2 — should cascade
        conn.execute("DELETE FROM vectors WHERE id='v2'")
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM vector_parents WHERE child_id='v2'"
        ).fetchall()
        assert rows == []
        cat.close()

    def test_mutations_cascade_on_strain_delete(self, tmp_path: Path):
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db)
        conn = cat.conn

        conn.execute(
            "INSERT INTO strains (id, parent_genome) VALUES (?, ?)",
            ("strain1", "genome1"),
        )
        conn.execute(
            "INSERT INTO mutations (strain_id, kind, target_kind, target_id, target_namespace) "
            "VALUES (?, ?, ?, ?, ?)",
            ("strain1", "knockout", "gene", "b0001", "ecoli"),
        )
        conn.commit()

        conn.execute("DELETE FROM strains WHERE id='strain1'")
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM mutations WHERE strain_id='strain1'"
        ).fetchall()
        assert rows == []
        cat.close()


class TestCHECKConstraints:
    """Verify CHECK constraints on experiments and access_log."""

    def test_experiment_kind_check(self, tmp_path: Path):
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db)
        conn = cat.conn
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO experiments (id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
                ("bad", "invalid_kind", "{}", "2024-01-01T00:00:00"),
            )
        cat.close()

    def test_access_log_object_kind_check(self, tmp_path: Path):
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db)
        conn = cat.conn
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO access_log (object_id, object_kind, op, timestamp) "
                "VALUES (?, ?, ?, ?)",
                ("obj1", "invalid_kind", "write", "2024-01-01T00:00:00"),
            )
        cat.close()

    def test_access_log_op_check(self, tmp_path: Path):
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db)
        conn = cat.conn
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO access_log (object_id, object_kind, op, timestamp) "
                "VALUES (?, ?, ?, ?)",
                ("obj1", "cache", "invalid_op", "2024-01-01T00:00:00"),
            )
        cat.close()


class TestSchemaVersion:
    def test_version_is_1(self):
        assert CURRENT_SCHEMA_VERSION == 1

    def test_migration_from_lower_version(self, tmp_path: Path):
        """Simulate a lower schema version and verify migration runs."""
        db = tmp_path / "catalog.sqlite"
        cat = Catalog(db)
        # Force lower version
        cat.conn.execute("UPDATE catalog_meta SET schema_version = 0")
        cat.conn.commit()
        cat.close()

        # Re-open — should migrate
        cat2 = Catalog(db)
        row = cat2.conn.execute("SELECT schema_version FROM catalog_meta").fetchone()
        assert row["schema_version"] == CURRENT_SCHEMA_VERSION
        cat2.close()
