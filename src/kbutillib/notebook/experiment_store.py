"""ExperimentStore and StrainStore — register and retrieve experiments and strains."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .schema.entity import EntityRef
from .schema.experiment import Computation, Experiment, ExternalDataset, Sample
from .schema.strain import Mutation, Strain
from .storage.catalog import Catalog


class ExperimentStore:
    """Register and retrieve experiments."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def register(self, exp: Experiment) -> Experiment:
        """Insert or update an experiment in the catalog."""
        conn = self._catalog.conn
        payload_json = exp.payload.model_dump_json()

        existing = conn.execute(
            "SELECT id FROM experiments WHERE id=?", (exp.id,)
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM experiment_parents WHERE child_id=?", (exp.id,))
            conn.execute(
                "UPDATE experiments SET kind=?, payload_json=?, notebook=?, "
                "description=?, created_at=? WHERE id=?",
                (
                    exp.kind,
                    payload_json,
                    exp.notebook,
                    exp.payload.description if hasattr(exp.payload, "description") else None,
                    exp.created_at.isoformat(),
                    exp.id,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO experiments (id, kind, payload_json, notebook, description, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    exp.id,
                    exp.kind,
                    payload_json,
                    exp.notebook,
                    exp.payload.description if hasattr(exp.payload, "description") else None,
                    exp.created_at.isoformat(),
                ),
            )
        for pid in exp.parents:
            conn.execute(
                "INSERT OR IGNORE INTO experiment_parents (child_id, parent_id) VALUES (?, ?)",
                (exp.id, pid),
            )
        conn.commit()
        return exp

    def register_sample(
        self, sample: Sample, *, parents: tuple[str, ...] = ()
    ) -> Experiment:
        exp = Experiment(
            id=sample.id,
            kind="sample",
            payload=sample,
            parents=list(parents),
            created_at=datetime.now(timezone.utc),
        )
        return self.register(exp)

    def register_computation(
        self, comp: Computation, *, parents: tuple[str, ...] = ()
    ) -> Experiment:
        exp = Experiment(
            id=comp.id,
            kind="computation",
            payload=comp,
            parents=list(parents),
            created_at=datetime.now(timezone.utc),
        )
        return self.register(exp)

    def register_external(
        self, ext: ExternalDataset, *, parents: tuple[str, ...] = ()
    ) -> Experiment:
        exp = Experiment(
            id=ext.id,
            kind="external",
            payload=ext,
            parents=list(parents),
            created_at=datetime.now(timezone.utc),
        )
        return self.register(exp)

    def get(self, id: str) -> Experiment:
        row = self._catalog.conn.execute(
            "SELECT * FROM experiments WHERE id=?", (id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Experiment {id!r} not found")
        return self._row_to_experiment(row)

    def list(self, *, kind: Optional[str] = None) -> list[Experiment]:
        if kind:
            rows = self._catalog.conn.execute(
                "SELECT * FROM experiments WHERE kind=? ORDER BY created_at", (kind,)
            ).fetchall()
        else:
            rows = self._catalog.conn.execute(
                "SELECT * FROM experiments ORDER BY created_at"
            ).fetchall()
        return [self._row_to_experiment(r) for r in rows]

    def _row_to_experiment(self, row) -> Experiment:
        kind = row["kind"]
        payload_data = json.loads(row["payload_json"])
        type_map = {
            "sample": Sample,
            "computation": Computation,
            "external": ExternalDataset,
        }
        payload = type_map[kind].model_validate(payload_data)

        parents = [
            r["parent_id"]
            for r in self._catalog.conn.execute(
                "SELECT parent_id FROM experiment_parents WHERE child_id=?", (row["id"],)
            ).fetchall()
        ]
        return Experiment(
            id=row["id"],
            kind=kind,
            payload=payload,
            notebook=row["notebook"],
            parents=parents,
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class StrainStore:
    """Register and retrieve strains."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def register(self, strain: Strain) -> Strain:
        conn = self._catalog.conn
        existing = conn.execute(
            "SELECT id FROM strains WHERE id=?", (strain.id,)
        ).fetchone()

        if existing:
            conn.execute("DELETE FROM mutations WHERE strain_id=?", (strain.id,))
            conn.execute(
                "UPDATE strains SET parent_genome=?, description=? WHERE id=?",
                (strain.parent_genome, strain.description, strain.id),
            )
        else:
            conn.execute(
                "INSERT INTO strains (id, parent_genome, description) VALUES (?, ?, ?)",
                (strain.id, strain.parent_genome, strain.description),
            )

        for mut in strain.mutations:
            conn.execute(
                "INSERT INTO mutations "
                "(strain_id, kind, target_kind, target_id, target_namespace, "
                "source_organism, source_gene, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    strain.id,
                    mut.kind,
                    mut.target.kind.value,
                    mut.target.id,
                    mut.target.namespace,
                    mut.source_organism,
                    mut.source_gene,
                    mut.description,
                ),
            )
        conn.commit()
        return strain

    def get(self, id: str) -> Strain:
        row = self._catalog.conn.execute(
            "SELECT * FROM strains WHERE id=?", (id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Strain {id!r} not found")
        return self._row_to_strain(row)

    def list(self) -> list[Strain]:
        rows = self._catalog.conn.execute(
            "SELECT * FROM strains ORDER BY id"
        ).fetchall()
        return [self._row_to_strain(r) for r in rows]

    def _row_to_strain(self, row) -> Strain:
        mut_rows = self._catalog.conn.execute(
            "SELECT * FROM mutations WHERE strain_id=?", (row["id"],)
        ).fetchall()
        mutations = [
            Mutation(
                kind=m["kind"],
                target=EntityRef(
                    kind=m["target_kind"],
                    id=m["target_id"],
                    namespace=m["target_namespace"],
                ),
                source_organism=m["source_organism"],
                source_gene=m["source_gene"],
                description=m["description"],
            )
            for m in mut_rows
        ]
        return Strain(
            id=row["id"],
            parent_genome=row["parent_genome"],
            mutations=mutations,
            description=row["description"],
        )
