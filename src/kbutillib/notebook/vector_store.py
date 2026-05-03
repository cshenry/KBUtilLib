"""VectorStore — typed numerical data backed by .kbcache/vectors/ + catalog.vectors."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

import pandas as pd

from .detect import get_cell_index, get_cell_source_hash
from .schema.entity import EntityKind
from .schema.vector import Vector, VectorType
from .storage.catalog import Catalog
from .storage.vectors import VectorStorage


class VectorStore:
    """Numerical Vector operations.

    Backed by ``.kbcache/vectors/`` + ``catalog.vectors``.
    """

    def __init__(
        self,
        catalog: Catalog,
        vector_storage: VectorStorage,
        notebook_name: Optional[str] = None,
    ) -> None:
        self._catalog = catalog
        self._storage = vector_storage
        self._notebook = notebook_name

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def from_dataframe(
        self,
        df: pd.DataFrame,
        *,
        id: str,
        experiment_id: str,
        type: VectorType,
        entity_kind: EntityKind,
        entity_namespace: str,
        columns: Optional[list[str]] = None,
    ) -> Vector:
        """Ingest a DataFrame as a Vector. Index = entity_id, columns = replicates/aggregates."""
        if columns is None:
            columns = list(df.columns)
        df_out = df[columns].copy()
        df_out.index.name = "entity_id"

        path, content_hash, n_bytes = self._storage.write(id, df_out)

        vec = Vector(
            id=id,
            type=type,
            experiment_id=experiment_id,
            entity_kind=entity_kind,
            entity_namespace=entity_namespace,
            columns=columns,
            parquet_path=self._storage.relative_path(id),
            content_hash=content_hash,
            created_at=datetime.now(timezone.utc),
        )
        self._upsert_vector(vec)
        self._log("write", id, content_hash)
        return vec

    def from_excel(
        self,
        path: str,
        sheet: Any = 0,
        *,
        id: str,
        experiment_id: str,
        type: VectorType,
        entity_kind: EntityKind,
        entity_namespace: str,
        index_col: int = 0,
        columns: Optional[list[str]] = None,
    ) -> Vector:
        df = pd.read_excel(path, sheet_name=sheet, index_col=index_col)
        return self.from_dataframe(
            df,
            id=id,
            experiment_id=experiment_id,
            type=type,
            entity_kind=entity_kind,
            entity_namespace=entity_namespace,
            columns=columns,
        )

    def from_csv(
        self,
        path: str,
        *,
        id: str,
        experiment_id: str,
        type: VectorType,
        entity_kind: EntityKind,
        entity_namespace: str,
        **kwargs: Any,
    ) -> Vector:
        df = pd.read_csv(path, index_col=0, **kwargs)
        return self.from_dataframe(
            df,
            id=id,
            experiment_id=experiment_id,
            type=type,
            entity_kind=entity_kind,
            entity_namespace=entity_namespace,
        )

    # ------------------------------------------------------------------
    # Derivation
    # ------------------------------------------------------------------

    def aggregate(
        self,
        parents: list[str],
        op: Literal["mean", "median", "max", "min", "sum"],
        *,
        id: str,
    ) -> Vector:
        """Aggregate multiple Vectors into one using *op*."""
        dfs = []
        parent_vecs = []
        for pid in parents:
            vec, df = self.get(pid)
            parent_vecs.append(vec)
            dfs.append(df)

        ref = parent_vecs[0]
        merged = pd.concat(dfs, axis=1)
        result = getattr(merged, op)(axis=1).to_frame(name="aggregated")

        path, content_hash, _ = self._storage.write(id, result)

        vec = Vector(
            id=id,
            type=ref.type,
            experiment_id=ref.experiment_id,
            entity_kind=ref.entity_kind,
            entity_namespace=ref.entity_namespace,
            columns=["aggregated"],
            parquet_path=self._storage.relative_path(id),
            content_hash=content_hash,
            derivation=op,
            parents=parents,
            created_at=datetime.now(timezone.utc),
        )
        self._upsert_vector(vec)
        self._log("write", id, content_hash)
        return vec

    def fold_change(
        self,
        numerator_id: str,
        denominator_id: str,
        *,
        id: str,
        log_base: Optional[int] = 2,
    ) -> Vector:
        """Compute fold-change (optionally log-transformed) between two Vectors."""
        num_vec, num_df = self.get(numerator_id)
        den_vec, den_df = self.get(denominator_id)

        # Align on index
        combined = num_df.join(den_df, lsuffix="_num", rsuffix="_den", how="inner")
        num_col = combined.columns[0]
        den_col = combined.columns[1]

        ratio = combined[num_col] / combined[den_col]
        if log_base:
            ratio = ratio.apply(lambda x: math.log(x, log_base) if x > 0 else float("nan"))
            col_name = f"log{log_base}_fold_change"
            derivation = f"log{log_base}_fold_change"
        else:
            col_name = "fold_change"
            derivation = "fold_change"

        result = ratio.to_frame(name=col_name)
        path, content_hash, _ = self._storage.write(id, result)

        fc_type = VectorType(domain="fold_change", scale="fold_change")
        vec = Vector(
            id=id,
            type=fc_type,
            experiment_id=num_vec.experiment_id,
            entity_kind=num_vec.entity_kind,
            entity_namespace=num_vec.entity_namespace,
            columns=[col_name],
            parquet_path=self._storage.relative_path(id),
            content_hash=content_hash,
            derivation=derivation,
            parents=[numerator_id, denominator_id],
            created_at=datetime.now(timezone.utc),
        )
        self._upsert_vector(vec)
        self._log("write", id, content_hash)
        return vec

    def compute(
        self,
        id: str,
        *,
        experiment_id: str,
        type: VectorType,
        entity_kind: EntityKind,
        entity_namespace: str,
        columns: list[str],
        compute_fn: Callable[[], pd.DataFrame],
        parents: tuple[str, ...] = (),
    ) -> Vector:
        """Compute-or-load. *compute_fn* called only on cache miss."""
        row = self._get_row(id)
        if row is not None:
            vec = self._row_to_vector(row)
            self._log("read", id, vec.content_hash)
            return vec

        df = compute_fn()
        return self.from_dataframe(
            df,
            id=id,
            experiment_id=experiment_id,
            type=type,
            entity_kind=entity_kind,
            entity_namespace=entity_namespace,
            columns=columns,
        )

    def project(
        self,
        parent_id: str,
        *,
        id: str,
        projection: Literal["gpr_max", "gpr_min", "gpr_mean"],
        target_namespace: str,
        model_ref: str,
    ) -> Vector:
        """Project gene-level vector onto reactions via GPR.

        Full implementation deferred (requires model GPR parsing).
        Raises NotImplementedError for now.
        """
        raise NotImplementedError(
            "GPR projection is deferred to a future phase. "
            "Use compute() with a custom compute_fn for now."
        )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get(self, id: str) -> tuple[Vector, pd.DataFrame]:
        """Return (Vector metadata, DataFrame) for the given id."""
        row = self._get_row(id)
        if row is None:
            raise KeyError(f"Vector {id!r} not found")
        vec = self._row_to_vector(row)
        df = self._storage.read(id)
        self._log("read", id, vec.content_hash)
        return vec, df

    def metadata(self, id: str) -> Vector:
        row = self._get_row(id)
        if row is None:
            raise KeyError(f"Vector {id!r} not found")
        return self._row_to_vector(row)

    def list(
        self,
        *,
        experiment_id: Optional[str] = None,
        type: Optional[VectorType] = None,
        entity_kind: Optional[EntityKind] = None,
    ) -> list[Vector]:
        clauses = []
        params: list[Any] = []
        if experiment_id:
            clauses.append("experiment_id=?")
            params.append(experiment_id)
        if type:
            clauses.append("type_domain=? AND type_scale=?")
            params.extend([type.domain, type.scale])
        if entity_kind:
            clauses.append("entity_kind=?")
            params.append(entity_kind.value)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self._catalog.conn.execute(
            f"SELECT * FROM vectors WHERE {where} ORDER BY created_at", params
        ).fetchall()
        return [self._row_to_vector(r) for r in rows]

    def delete(self, id: str) -> None:
        row = self._get_row(id)
        if row is None:
            raise KeyError(f"Vector {id!r} not found")
        self._catalog.conn.execute("DELETE FROM vectors WHERE id=?", (id,))
        self._catalog.conn.commit()
        self._storage.delete(id)
        self._log("delete", id, row["content_hash"])

    def get_many(self, ids: list[str]) -> pd.DataFrame:
        """Concat-merge multiple Vectors into a single wide DataFrame on entity_id."""
        frames = []
        for vid in ids:
            _, df = self.get(vid)
            # Prefix columns with vector id to avoid collision
            df = df.rename(columns={c: f"{vid}:{c}" for c in df.columns})
            frames.append(df)
        if not frames:
            return pd.DataFrame()
        result = frames[0]
        for f in frames[1:]:
            result = result.join(f, how="outer")
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_row(self, id: str):
        return self._catalog.conn.execute(
            "SELECT * FROM vectors WHERE id=?", (id,)
        ).fetchone()

    def _upsert_vector(self, vec: Vector) -> None:
        conn = self._catalog.conn
        existing = self._get_row(vec.id)
        if existing:
            conn.execute("DELETE FROM vector_parents WHERE child_id=?", (vec.id,))
            conn.execute(
                "UPDATE vectors SET experiment_id=?, type_domain=?, type_scale=?, "
                "type_projection=?, entity_kind=?, entity_namespace=?, columns_json=?, "
                "n_entities=?, n_columns=?, parquet_path=?, content_hash=?, derivation=?, "
                "created_at=? WHERE id=?",
                (
                    vec.experiment_id,
                    vec.type.domain,
                    vec.type.scale,
                    vec.type.projection,
                    vec.entity_kind.value,
                    vec.entity_namespace,
                    json.dumps(vec.columns),
                    0,  # n_entities set later if needed
                    len(vec.columns),
                    vec.parquet_path,
                    vec.content_hash,
                    vec.derivation,
                    vec.created_at.isoformat(),
                    vec.id,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO vectors "
                "(id, experiment_id, type_domain, type_scale, type_projection, "
                "entity_kind, entity_namespace, columns_json, n_entities, n_columns, "
                "parquet_path, content_hash, derivation, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    vec.id,
                    vec.experiment_id,
                    vec.type.domain,
                    vec.type.scale,
                    vec.type.projection,
                    vec.entity_kind.value,
                    vec.entity_namespace,
                    json.dumps(vec.columns),
                    0,
                    len(vec.columns),
                    vec.parquet_path,
                    vec.content_hash,
                    vec.derivation,
                    vec.created_at.isoformat(),
                ),
            )

        for pid in vec.parents:
            conn.execute(
                "INSERT OR IGNORE INTO vector_parents (child_id, parent_id) VALUES (?, ?)",
                (vec.id, pid),
            )
        conn.commit()

    def _row_to_vector(self, row) -> Vector:
        parents = [
            r["parent_id"]
            for r in self._catalog.conn.execute(
                "SELECT parent_id FROM vector_parents WHERE child_id=?", (row["id"],)
            ).fetchall()
        ]
        return Vector(
            id=row["id"],
            type=VectorType(
                domain=row["type_domain"],
                scale=row["type_scale"],
                projection=row["type_projection"],
            ),
            experiment_id=row["experiment_id"],
            entity_kind=EntityKind(row["entity_kind"]),
            entity_namespace=row["entity_namespace"],
            columns=json.loads(row["columns_json"]),
            parquet_path=row["parquet_path"],
            content_hash=row["content_hash"],
            derivation=row["derivation"],
            parents=parents,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _log(self, op: str, id: str, content_hash: str) -> None:
        self._catalog.log_access(
            object_id=id,
            object_kind="vector",
            op=op,
            notebook=self._notebook,
            cell_index=get_cell_index(),
            cell_source_hash=get_cell_source_hash(),
            content_hash=content_hash,
        )
