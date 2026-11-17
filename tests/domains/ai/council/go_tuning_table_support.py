"""A marked-table fixture: a manifest, its matched rows, and fake results.

The table and calibration modules' contract is over
:class:`~.adjudicate.ItemResult`, so the results here are BUILT rather than
adjudicated -- a council run would make these tests slow and about the
council.  What is NOT faked is the verdict derivation: ``set_a``/``set_b``
come from the manifest's own orientation and the derived verdict and side
come from :func:`adjudicate.derived_verdict` and
:func:`adjudicate.verdict_side`, so a test asserting that the table prints
``neither`` is asserting it over the product's own rule.

The three shapes the live go_mf run produced, mirrored deliberately:

* a META-decided item whose final ids match NEITHER side (the council kept
  one term and stripped one that both sides had), so the ``verdict`` column
  has to render ``neither``;
* an item whose ballot reported ``correct_term_off_menu`` true with a
  description, so the off-menu columns have something legible to show;
* a swap pair, so the ``position swap`` column has both a ``same`` and a
  ``flipped`` case available.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kbutillib.domains.ai.council.go_tuning import adjudicate as A
from kbutillib.domains.ai.council.go_tuning.conflicts import MatchedItem
from kbutillib.domains.ai.council.go_tuning.sample import Plan, build_manifest

from .go_tuning_support import hash_of

#: Four terms at three levels of specificity, so the rival answers differ in
#: a way a reader can actually judge.  ``GO:0015426`` is the one the live run
#: kept when it rejected both sides.
MENU: Tuple[Dict[str, Any], ...] = (
    {
        "id": "GO:0015749",
        "label": "glucose transmembrane transport",
        "description": "The directed movement of glucose across a membrane.",
        "rank": 1,
        "score": 0.91,
        "metadata": {},
    },
    {
        "id": "GO:0015426",
        "label": "ABC-type polar-amino-acid transporter activity",
        "description": "Catalysis of ATP-driven transport.",
        "rank": 2,
        "score": 0.77,
        "metadata": {},
    },
    {
        "id": "GO:0005524",
        "label": "ATP binding",
        "description": "Binding to ATP.",
        "rank": 3,
        "score": 0.55,
        "metadata": {},
    },
    {
        "id": "GO:0016887",
        "label": "ATP hydrolysis activity",
        "description": "Catalysis of ATP hydrolysis.",
        "rank": 4,
        "score": 0.41,
        "metadata": {},
    },
)

MENU_IDS = tuple(entry["id"] for entry in MENU)

#: A term that is NOT on the menu, for the off-menu derivation (C12).
OFF_MENU_ID = "GO:0042626"

#: An annotation beginning with ``=``, which is the C21 case: a spreadsheet
#: application would read it as a formula unless the cell is forced to a
#: string.
FORMULA_ANNOTATION = "=SUM glucose ABC transporter, ATP-binding protein"

#: Small enough to run in a test, with the guard slices off and the floors
#: lowered -- the same shape ``test_go_tuning_adjudicate`` uses.
SMALL_PLAN = Plan(
    pilot_size=6,
    swap_slice=2,
    regression_size=0,
    decline_guard_size=0,
    min_conflicts=2,
)


def matched_item(
    label: str,
    candidate: Sequence[str],
    baseline: Sequence[str],
    *,
    source: str = "prokka",
    text: Optional[str] = None,
) -> MatchedItem:
    return MatchedItem(
        entity_hash=hash_of("entity", label),
        data_hash=hash_of("data", label),
        item_id=f"c-{label}",
        candidate_job_id="j-cand",
        baseline_item_id=f"b-{label}",
        baseline_job_id="j-base",
        candidate_selected=frozenset(candidate),
        baseline_selected=frozenset(baseline),
        candidate_answers=1,
        baseline_answers=1,
        source=source,
        evidence_text=text if text is not None else f"Glucose ABC transporter {label}",
        menu=tuple(MENU),
        kind="disjoint" if not set(candidate) & set(baseline) else "partial",
        item_sub_kind=None if not set(candidate) & set(baseline) else "alcf_superset",
    )


def corpus(n: int = 6) -> List[MatchedItem]:
    """*n* conflicts, alternating class and source tool.

    Two sources so the per-source slice has something to split, and the
    first row carries :data:`FORMULA_ANNOTATION`.
    """
    items: List[MatchedItem] = []
    for i in range(n):
        source = "prokka" if i % 2 else "rast"
        if i % 2:
            items.append(
                matched_item(
                    f"pa{i:02d}",
                    ["GO:0005524", "GO:0016887"],
                    ["GO:0005524"],
                    source=source,
                )
            )
        else:
            items.append(
                matched_item(
                    f"dj{i:02d}",
                    ["GO:0005524"],
                    ["GO:0015749"],
                    source=source,
                    text=FORMULA_ANNOTATION if i == 0 else None,
                )
            )
    return items


def material(seed: str = "seed-1", n: int = 6) -> Tuple[Any, List[MatchedItem]]:
    items = corpus(n)
    return build_manifest(items, seed, SMALL_PLAN), items


def result_for(
    entry: Mapping[str, Any],
    item: MatchedItem,
    final_ids: Sequence[str],
    *,
    decided_by: str = "unanimous",
    prefers: Optional[str] = None,
    off_menu: Optional[bool] = None,
    off_menu_description: str = "",
    failure_type: Optional[str] = "menu_ok_judgement_differs",
    confidence: str = "high",
    rules: Sequence[str] = ("P1", "P2"),
    why: str = "The annotation names the system, so P1 picks the transporter activity.",
    status: str = "ok",
) -> A.ItemResult:
    """One :class:`ItemResult` for a manifest *entry*, verdict derived for real."""
    alcf = A.render_answer(item.menu, item.candidate_selected)
    gomf = A.render_answer(item.menu, item.baseline_selected)
    alcf_is_a = bool(entry.get("alcf_is_A"))
    set_a = A.parse_answer(alcf if alcf_is_a else gomf)
    set_b = A.parse_answer(gomf if alcf_is_a else alcf)
    derived = A.derived_verdict(final_ids, set_a, set_b, prefers)
    seat = "meta-1" if decided_by == "meta" else "proposer-1"
    return A.ItemResult(
        item_id=str(entry["id"]),
        job_id="job-1",
        status=status,
        final_ids=tuple(final_ids),
        decided_by=decided_by,
        meta_skipped=decided_by != "meta",
        set_a=set_a,
        set_b=set_b,
        alcf_is_a=alcf_is_a,
        deciding_prefers=prefers,
        derived_verdict=derived,
        side=A.verdict_side(derived, alcf_is_a),
        failure_type=failure_type,
        failure_type_source="meta" if decided_by == "meta" else "proposer_majority",
        correct_term_off_menu=off_menu,
        rules_cited=tuple(rules),
        confidence=confidence,
        why={seat: why},
        off_menu_description=(
            {seat: off_menu_description} if off_menu_description else {}
        ),
        pool=str(entry.get("pool") or ""),
        swap=bool(entry.get("swap")),
    )


def results_for(
    manifest: Any,
    items: Sequence[MatchedItem],
    *,
    wrong_items: Sequence[str] = (),
    flip_swap: bool = False,
) -> List[A.ItemResult]:
    """A result per pilot ENTRY, including the swapped twins.

    The first un-swapped row is made ``meta``-decided with final ids that
    match neither side (the live run's shape); the second carries an
    off-menu claim.  ``wrong_items`` names entry ids whose council answer is
    the go_mf side rather than the ALCF side, which is how the per-side
    slice gets both values populated.  ``flip_swap`` makes every twin settle
    on the OTHER side, so the swap flip rate is 100% instead of 0%.
    """
    by_item = {i.item_id: i for i in items}
    payload = manifest.as_dict()
    entries = [e for e in payload["entries"] if e["pool"] == "pilot"]
    base = sorted(
        (e for e in entries if not e["swap"]), key=lambda e: str(e["id"])
    )
    out: List[A.ItemResult] = []
    for position, entry in enumerate(base):
        item = by_item[str(entry["item_id"])]
        entry_id = str(entry["id"])
        if position == 0:
            # Meta kept one term and stripped one BOTH sides had: the final
            # set matches neither answer.
            out.append(
                result_for(
                    entry,
                    item,
                    ["GO:0015426"],
                    decided_by="meta",
                    failure_type="retrieval_gap",
                    why="Neither answer applies P1; the system term is GO:0015426.",
                )
            )
            continue
        if position == 1:
            out.append(
                result_for(
                    entry,
                    item,
                    sorted(item.candidate_selected),
                    off_menu=True,
                    off_menu_description=(
                        "The right term is the ABC transporter complex's own "
                        "activity, which the menu does not offer."
                    ),
                    failure_type="retrieval_gap",
                )
            )
            continue
        winner = (
            item.baseline_selected if entry_id in wrong_items else item.candidate_selected
        )
        out.append(result_for(entry, item, sorted(winner), off_menu=False))

    by_base = {str(r.item_id): r for r in out}
    for entry in entries:
        if not entry["swap"]:
            continue
        item = by_item[str(entry["item_id"])]
        twin_of = by_base.get(str(entry["item_id"]))
        if twin_of is None:
            continue
        if flip_swap:
            same_side = twin_of.side
            other = (
                item.baseline_selected
                if same_side == "alcf"
                else item.candidate_selected
            )
            final = sorted(other)
        else:
            final = list(twin_of.final_ids)
        out.append(result_for(entry, item, final, off_menu=False))
    return out


def mark(
    xlsx_path: Any,
    marks: Mapping[str, str],
    notes: Optional[Mapping[str, str]] = None,
) -> None:
    """Mark the workbook the way a human would: open it, type, save.

    Through ``openpyxl`` on the saved file rather than by rebuilding it, so
    the round trip under test is the real one -- a load, an edit of two
    cells per row, and a save that rewrites every other cell from what the
    loader read.
    """
    import openpyxl

    from kbutillib.domains.ai.council.go_tuning.table import (
        COLUMNS,
        MARK_COLUMN,
        NOTE_COLUMN,
        TABLE_SHEET,
    )

    headers = [header for header, _ in COLUMNS]
    mark_col = 1 + headers.index(MARK_COLUMN)
    note_col = 1 + headers.index(NOTE_COLUMN)
    id_col = 1 + headers.index("item id")

    workbook = openpyxl.load_workbook(xlsx_path)
    sheet = workbook[TABLE_SHEET]
    for row in range(2, sheet.max_row + 1):
        item_id = str(sheet.cell(row=row, column=id_col).value or "")
        if item_id in marks:
            sheet.cell(row=row, column=mark_col).value = marks[item_id]
        if notes and item_id in notes:
            sheet.cell(row=row, column=note_col).value = notes[item_id]
    workbook.save(xlsx_path)


def row_ids(manifest: Any) -> List[str]:
    """The un-swapped pilot entry ids, in the order the table writes them."""
    return sorted(
        str(e["id"])
        for e in manifest.as_dict()["entries"]
        if e["pool"] == "pilot" and not e["swap"]
    )
