"""Synthetic two-store fixtures for the ``go_tuning`` conflict/sample tests.

Built through the store's OWN write APIs (``create_job``, ``settle_item``),
like :mod:`test_alcf_bench_report_store`, so these tests exercise the real
reader rather than a hand-made dict that happens to have the right keys.

TWO STORES, because that is the shape the extractor has to work on: the ALCF
bench writes its own sqlite file and the ``go_mf@4`` baseline lives in the
production store, and the thing most likely to break is the join BETWEEN
them.  Job ids are given explicitly rather than generated, because the
coalescing rule picks the lexicographically lowest ``(job_id, item_id)`` and
a uuid would make "which row won" unrepeatable.

Nothing here makes a network call, spends a token or needs a GPU.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kbutillib.domains.ai.council.store.sqlite import SqliteStore

from .store_contract import call_row, item_row, job_row

#: The baseline query every eligible baseline row must come from.
GO_MF4 = ("go_mf", "4")


def hash_of(kind: str, label: str) -> str:
    """A stable 64-hex hash for a short test label.

    Full width on purpose: the real stores carry 64-hex ``entity_hash`` and
    ``data_hash``, and a test that keys on ``"e1"`` would not notice a
    truncation bug.
    """
    return hashlib.sha256(f"{kind}:{label}".encode("utf-8")).hexdigest()


def row(
    item_id: str,
    entity: str,
    *,
    data: str = "d0",
    selected: Sequence[str] = (),
    status: str = "ok",
    verdict_status: str = "ok",
    source: str = "tool_a",
    decided_by: str = "unanimous",
    decline_reason: Optional[str] = None,
    with_verdict: bool = True,
    menu: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """One item spec.  ``entity``/``data`` are labels, hashed by :func:`hash_of`.

    ``status="pending"`` leaves the item unsettled; ``with_verdict=False``
    settles it ``ok`` with NO verdict row, which is the shape that must not be
    read as a decline.
    """
    return {
        "item_id": item_id,
        "entity": entity,
        "data": data,
        "selected": list(selected),
        "status": status,
        "verdict_status": verdict_status,
        "source": source,
        "decided_by": decided_by,
        "decline_reason": decline_reason,
        "with_verdict": with_verdict,
        "menu": list(menu) if menu is not None else None,
    }


def make_store(
    path: Any,
    jobs: Sequence[Mapping[str, Any]],
) -> Tuple[SqliteStore, str]:
    """Build one store from ``[{"job_id", "query", "rows"}, ...]``.

    ``query`` defaults to :data:`GO_MF4`.  Returns the open store and its URL;
    the caller closes it.
    """
    url = f"sqlite:///{path}"
    store = SqliteStore(url)
    store.init_schema()
    for job in jobs:
        rows: Sequence[Mapping[str, Any]] = job["rows"]
        name, version = job.get("query", GO_MF4)
        items = [
            item_row(
                r["item_id"],
                entity_hash=hash_of("entity", r["entity"]),
                data_hash=hash_of("data", r["data"]),
                evidence={"text": f"annotation {r['item_id']}", "source": r["source"]},
                menu=r["menu"] if r["menu"] is not None else [
                    {"id": "GO:1", "label": "one", "description": "", "rank": 1,
                     "score": 0.9, "metadata": {}},
                    {"id": "GO:2", "label": "two", "description": "", "rank": 2,
                     "score": 0.5, "metadata": {}},
                ],
            )
            for r in rows
        ]
        calls = [
            call_row(r["item_id"], "p1", entity_hash=hash_of("entity", r["entity"]))
            for r in rows
        ]
        job_id = store.create_job(
            job_row(job_id=job["job_id"], query_name=name, query_version=version),
            items,
            calls,
        )
        for r in rows:
            if r["status"] == "pending":
                continue
            verdict = None
            if r["with_verdict"]:
                verdict = {
                    "status": r["verdict_status"],
                    "item_id": r["item_id"],
                    "selected_ids": r["selected"],
                    "final_confidence": "high",
                    "decided_by": r["decided_by"],
                }
                if r["decline_reason"] is not None:
                    verdict["decline_reason"] = r["decline_reason"]
            store.settle_item(job_id, r["item_id"], r["status"], verdict)
    return store, url


def two_stores(
    tmp_path: Any,
    candidate_jobs: Sequence[Mapping[str, Any]],
    baseline_jobs: Sequence[Mapping[str, Any]],
) -> Tuple[str, List[str], str, List[str]]:
    """``(candidate_url, candidate_job_ids, baseline_url, baseline_job_ids)``.

    Both stores are closed before returning, so the extractor opens them from
    their URLs exactly as the CLI does.
    """
    cstore, curl = make_store(tmp_path / "cand.sqlite", candidate_jobs)
    cstore.close()
    bstore, burl = make_store(tmp_path / "base.sqlite", baseline_jobs)
    bstore.close()
    return (
        curl,
        [j["job_id"] for j in candidate_jobs],
        burl,
        [j["job_id"] for j in baseline_jobs],
    )
