"""The pilot table a human marks, and the reader that turns marks into gold.

This is the one artefact in the tuning pass whose audience is a PERSON.
Everything else here is written for a resume, a gate or a score; this is
written to be opened in Excel on a laptop, read down a column, and marked.
So the shape is driven by reading rather than by parsing: every GO id is
printed as ``GO:id | name`` one per line rather than as a comma-joined id
list, the header is frozen, text wraps, and the mark column carries a
``R``/``W``/``?`` dropdown so the three legal answers are the three the sheet
offers.

ONE ROW PER PILOT ITEM, NEVER PER MANIFEST ENTRY
================================================
A swapped pilot row is TWO manifest entries over one item -- the item and its
position-swapped twin -- and a table with both in it would ask the marker to
adjudicate the same annotation twice and then quietly double its weight in
the agreement figure.  The twin is therefore not a row: it is the *position
swap* COLUMN of its own item's row, reporting whether the two orders reached
the same source-normalised verdict (``same``) or not (``flipped``).

THE .MD COPY IS THE SAME TABLE, NOT A SUMMARY
=============================================
:func:`write_pilot` writes both files from one row build, so the markdown is
the same columns in the same order with the same cell text (newlines inside
a cell become ``<br>``, which is the only concession a pipe table forces).
It exists for reading in a terminal and diffing in git; the ``.xlsx`` is the
one that gets marked.

WHY A HIDDEN PROVENANCE SHEET (C21)
===================================
The workbook makes a round trip through a spreadsheet application and a
human, and the two accidents that silently corrupt it are a *sorted* column
(which re-pairs every mark with the wrong item) and a *pasted-over* answer
cell.  Neither changes the row count, so neither is visible to a reader.  So
a hidden sheet records the manifest id, the scope, the item ids in row order
and a sha256 over the DISPLAYED answer columns, and :func:`read_marks`
refuses a workbook whose hash, id list or manifest id does not match.

That is an INTEGRITY check against damage, not a security boundary: a hand
that can edit the answer columns can edit the hidden sheet too.  What makes
the pilot case stronger than the hash alone is that the manifest is an
independent witness -- :func:`read_marks` checks the row count and the id set
against the manifest's own pilot entries, which are not in the file being
marked.  A spot-check table can only be checked for being a SUBSET of that
pilot set, because which subset was drawn is the tranche's business and the
manifest does not record it.

STRINGS ARE WRITTEN AS STRINGS (C21)
====================================
``openpyxl`` infers a formula from a leading ``=``, and a GO label or an
annotation can begin with any character at all.  Every cell this module
writes is therefore forced to ``data_type == "s"`` after assignment, so an
annotation reading ``=DIV`` lands as the four characters it is rather than as
``#NAME?``.

WHAT A MARK MEANS, AND WHAT IT DOES NOT (C12)
=============================================
``R``    the verdict AND the correct-terms column are right.  Gold is the
         adjudicated id set, read back off the sheet, provenance ``judge``.
``W``    wrong.  If the note names valid GO ids they ARE the gold set,
         provenance ``human``, and ``off_menu`` is re-derived from whether
         those ids were on the item's retrieved menu.  If it names none, the
         row is an explicit exclusion: it says the judge was wrong, which
         still counts against agreement, and it supplies no gold ids.
``?``    unsure.  An explicit exclusion from BOTH gold and agreement.
blank    unreviewed, which is not an answer.  :mod:`.calibrate` refuses to
         report until every pilot row carries one of the three.

``W`` without ids and ``?`` NEVER fall back to the judge's own ids.  A gold
set taken from the answer a human has just called wrong (or declined to
call) would score the judge against itself and read as agreement.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from ..code_version import code_version
from .conflicts import MatchedItem, MatchResult, Refused

__all__ = [
    "ANSWER_COLUMNS",
    "COLUMNS",
    "HOW_TO_MARK",
    "HOW_TO_MARK_SHEET",
    "MARKS",
    "MARK_COLUMN",
    "NOTE_COLUMN",
    "PROVENANCE_SHEET",
    "TABLE_SHEET",
    "Gold",
    "GoldRecord",
    "Row",
    "answer_digest",
    "go_ids",
    "pilot_entries",
    "read_marks",
    "render_terms",
    "write_pilot",
    "write_spot_check",
]


# ---------------------------------------------------------------------------
# the columns
# ---------------------------------------------------------------------------

#: The sheet holding the table.
TABLE_SHEET = "pilot"

#: The sheet holding the marking instructions (C16).
HOW_TO_MARK_SHEET = "How to mark"

#: The hidden sheet holding the manifest id, the scope, the row ids and the
#: answer digest (C21).
PROVENANCE_SHEET = "_provenance"

#: The three legal marks, which are also the dropdown's three options.
MARKS = ("R", "W", "?")

MARK_COLUMN = "Chris mark"
NOTE_COLUMN = "Chris note"

#: The columns, in the order they are written.  ``(header, width)``; the
#: width is Excel character units and is a READING decision -- the GO-term
#: columns are wide because they hold several ``GO:id | name`` lines, and
#: ``why`` is the widest because it holds prose.
COLUMNS: Tuple[Tuple[str, int], ...] = (
    ("item id", 22),
    ("annotation", 46),
    ("source tool", 12),
    ("class", 18),
    ("ALCF terms", 38),
    ("go_mf terms", 38),
    ("verdict", 15),
    ("correct terms", 38),
    ("off-menu flag", 11),
    ("off-menu description", 34),
    ("failure type", 20),
    ("rules cited", 14),
    ("confidence", 11),
    ("why", 60),
    ("split", 11),
    ("position swap", 13),
    (MARK_COLUMN, 11),
    (NOTE_COLUMN, 40),
)

#: The columns the answer digest covers: everything the COUNCIL put in the
#: sheet.  The two mark columns are excluded because they are what the human
#: is there to change; every other cell is a fact about the adjudication and
#: a change to one makes the sheet a different question.
ANSWER_COLUMNS: Tuple[str, ...] = tuple(
    header for header, _ in COLUMNS if header not in (MARK_COLUMN, NOTE_COLUMN)
)

#: What the three marks mean, as the workbook states it (C16).  A line per
#: row of the instructions sheet.
HOW_TO_MARK: Tuple[str, ...] = (
    "Mark every row in the '" + MARK_COLUMN + "' column with R, W or ?.",
    "",
    "R  = right.  The verdict AND the 'correct terms' column are correct.",
    "W  = wrong.  Put the right GO ids in the '"
    + NOTE_COLUMN
    + "' column if you know them",
    "     (any format: 'GO:0015749' anywhere in the note is picked up). If you do",
    "     not know them, still mark W -- the row counts against agreement and simply",
    "     contributes no gold terms.",
    "?  = unsure.  The row is excluded from both agreement and gold.",
    "",
    "A blank mark is NOT an answer: it means the row has not been reviewed, and the",
    "calibration report refuses to run until every row carries R, W or ?.",
    "",
    "'both acceptable' in the verdict column is DESCRIPTIVE ONLY -- it records that",
    "the adjudicators found both candidate answers defensible. It does not make the",
    "row ambiguous for you: every row has ONE canonical correct set of GO terms, and",
    "that is what 'correct terms' claims and what R/W is about.",
    "",
    "Do not sort, insert or delete rows, and do not edit any column other than the",
    "two mark columns. The reader checks the row ids and a hash of the answer",
    "columns against the manifest and will refuse a disturbed sheet.",
)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

#: A GO id as it is written anywhere a human may type one.  Seven digits is
#: the GO identifier's own width; a looser pattern would read a number out of
#: ordinary prose ("GO:1 or so") and call it gold.
_GO_RE = re.compile(r"\bGO:\d{7}\b")

#: How a derived verdict is DISPLAYED.  The stored vocabulary is
#: ``alcf``/``gomf``/``both_acceptable``/``neither``; a marker reads the
#: pipelines' names.
_VERDICT_LABEL = {
    "alcf": "ALCF",
    "gomf": "go_mf",
    "both_acceptable": "both acceptable",
    "neither": "neither",
}

#: ...and back, for :func:`read_marks`, which only ever sees the sheet.
_VERDICT_SIDE = {label: side for side, label in _VERDICT_LABEL.items()}


def go_ids(text: Any) -> Tuple[str, ...]:
    """Every ``GO:#######`` in *text*, de-duplicated, first-seen order kept.

    Order is the order they were WRITTEN rather than sorted, because a note
    is prose and the first id a marker names is usually the one they mean
    most; nothing downstream depends on it, but a reader comparing a note to
    a gold set should not have to re-sort in their head.
    """
    seen: List[str] = []
    for match in _GO_RE.finditer(str(text or "")):
        if match.group(0) not in seen:
            seen.append(match.group(0))
    return tuple(seen)


def render_terms(
    menu: Sequence[Mapping[str, Any]], ids: Iterable[str]
) -> str:
    """``GO:id | name`` per line, in MENU ORDER, names from the menu labels.

    Menu order rather than sorted order so the two answer columns of one row
    list their shared terms in the same places and a reader can compare them
    down the column.  An id the menu does not hold is still printed -- with
    no name, because there is none -- and sorted after the menu's own: an
    off-menu id in a settled answer is exactly the thing the table must not
    hide.
    """
    wanted = {str(i) for i in ids}
    lines: List[str] = []
    for entry in menu:
        term = str(entry.get("id") or "")
        if term in wanted:
            label = str(entry.get("label") or "").strip()
            lines.append(f"{term} | {label}" if label else term)
            wanted.discard(term)
    lines.extend(sorted(wanted))
    return "\n".join(lines)


def _by_seat(mapping: Mapping[str, Any]) -> str:
    """A role-keyed text mapping as ``seat: text`` lines, empties dropped."""
    out: List[str] = []
    for seat in sorted(mapping or {}):
        text = str(mapping[seat] or "").strip()
        if text:
            out.append(f"{seat}: {text}")
    return "\n".join(out)


def _flag(value: Any) -> str:
    """A tri-state boolean as a human reads it: ``yes``/``no``/blank."""
    if value is None:
        return ""
    return "yes" if bool(value) else "no"


def _split(decided_by: Any) -> str:
    """``unanimous`` / ``meta``, or whatever else the verdict actually said.

    Passed through rather than normalised to one of the two: a
    ``deterministic_empty`` verdict is neither, and printing it as
    ``unanimous`` would tell the marker a council agreed when none was asked.
    """
    return str(decided_by or "")


# ---------------------------------------------------------------------------
# the rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One table row: the cells, plus what :mod:`.calibrate` needs by key.

    ``cells`` is keyed by COLUMN HEADER rather than by index, so a column
    inserted in :data:`COLUMNS` cannot silently shift a value into the wrong
    place.
    """

    item_id: str
    cells: Mapping[str, str]
    menu_ids: Tuple[str, ...]

    def values(self) -> List[str]:
        return [self.cells.get(header, "") for header, _ in COLUMNS]


def pilot_entries(manifest: Any, pool: str = "pilot") -> List[Dict[str, Any]]:
    """The manifest's *pool* entries that are ROWS: the un-swapped ones.

    Raises:
        Refused: the manifest holds no such entries, which means the table
            would be empty and the refusal should name the pool rather than
            let an empty workbook be written.
    """
    payload = manifest.as_dict() if hasattr(manifest, "as_dict") else dict(manifest)
    entries = [
        dict(entry)
        for entry in payload.get("entries") or ()
        if str(entry.get("pool")) == str(pool) and not entry.get("swap")
    ]
    if not entries:
        raise Refused(
            f"the manifest holds no un-swapped {pool!r} entries, so there is no "
            "table to write"
        )
    entries.sort(key=lambda e: str(e["id"]))
    return entries


def _matched_index(matched: Any) -> Dict[str, MatchedItem]:
    items = matched.items if isinstance(matched, MatchResult) else matched
    return {item.item_id: item for item in items}


def _swap_id(entry: Mapping[str, Any]) -> str:
    """The entry id the sampler gives a row's position-swapped twin."""
    return f"{entry['item_id']}:swap"


def _position_swap(base: Any, twin: Any) -> str:
    """``same``/``flipped`` from the two orders' SOURCE-NORMALISED verdicts.

    Compared after mapping back to ALCF/go_mf, which is the whole point: A
    and B are opposite in the twin by construction, so comparing the raw
    ``A``/``B`` of the two orders would report every stable item as flipped.
    """
    if twin is None or base is None:
        return ""
    return "same" if str(base.side) == str(twin.side) else "flipped"


def build_rows(
    results: Any,
    manifest: Any,
    matched: Any,
    item_ids: Optional[Sequence[str]] = None,
    pool: str = "pilot",
) -> List[Row]:
    """One :class:`Row` per pilot item, in the manifest's entry-id order.

    Args:
        results: the :class:`~.adjudicate.ItemResult` list ``read_results``
            returned.  Indexed by ``item_id``, which for a swapped twin is
            the ``"<item_id>:swap"`` entry id -- that is how the twin is
            found for the position-swap column.
        manifest: the manifest the items came from, or its JSON.
        matched: the ``MatchResult``/``MatchedItem`` list the manifest was
            sampled from, which is where the annotation, the two rival
            answers and the MENU LABELS come from.
        item_ids: restrict to these item ids (a tranche's spot-check set);
            ``None`` for every row of the pool.
        pool: the manifest pool to table.

    Raises:
        Refused: a row's item is missing from *matched* or from *results*,
            or *item_ids* names an id the pool does not hold.  All three are
            a mismatched input rather than a missing answer, and a table
            with a blank row in it is a table whose agreement figure has a
            hole nobody can see.
    """
    entries = pilot_entries(manifest, pool)
    by_item = _matched_index(matched)
    by_result = {str(r.item_id): r for r in results}

    if item_ids is not None:
        wanted = [str(i) for i in item_ids]
        known = {str(e["id"]) for e in entries}
        unknown = sorted(set(wanted) - known)
        if unknown:
            raise Refused(
                f"{len(unknown)} requested id(s) are not un-swapped {pool!r} rows of "
                "this manifest",
                unknown,
            )
        entries = [e for e in entries if str(e["id"]) in set(wanted)]

    rows: List[Row] = []
    missing_item: List[str] = []
    missing_result: List[str] = []
    for entry in entries:
        entry_id = str(entry["id"])
        item = by_item.get(str(entry["item_id"]))
        result = by_result.get(entry_id)
        if item is None:
            missing_item.append(str(entry["item_id"]))
            continue
        if result is None:
            missing_result.append(entry_id)
            continue
        twin = by_result.get(_swap_id(entry)) if entry.get("swap") is not None else None
        kind = str(entry.get("class") or "")
        sub = str(entry.get("sub_kind") or "")
        cells = {
            "item id": entry_id,
            "annotation": str(item.evidence_text or ""),
            "source tool": str(item.source or ""),
            "class": f"{kind} ({sub})" if sub else kind,
            "ALCF terms": render_terms(item.menu, item.candidate_selected),
            "go_mf terms": render_terms(item.menu, item.baseline_selected),
            "verdict": _VERDICT_LABEL.get(str(result.side), str(result.side)),
            "correct terms": render_terms(item.menu, result.final_ids),
            "off-menu flag": _flag(result.correct_term_off_menu),
            "off-menu description": _by_seat(result.off_menu_description),
            "failure type": str(result.failure_type or ""),
            "rules cited": ", ".join(result.rules_cited),
            "confidence": str(result.confidence or ""),
            "why": _by_seat(result.why),
            "split": _split(result.decided_by),
            "position swap": _position_swap(result, twin),
            MARK_COLUMN: "",
            NOTE_COLUMN: "",
        }
        rows.append(
            Row(
                item_id=entry_id,
                cells=cells,
                menu_ids=tuple(str(e.get("id") or "") for e in item.menu),
            )
        )
    if missing_item:
        raise Refused(
            f"{len(missing_item)} manifest row(s) name an item the matched input "
            "does not hold; the table must be written from the SAME extract the "
            "manifest was sampled from",
            sorted(set(missing_item)),
        )
    if missing_result:
        raise Refused(
            f"{len(missing_result)} row(s) of the pool have no adjudication result; "
            "a table with a blank row hides a hole in the agreement denominator",
            sorted(set(missing_result)),
        )
    return rows


# ---------------------------------------------------------------------------
# the digest
# ---------------------------------------------------------------------------


def answer_digest(rows: Sequence[Row]) -> str:
    """SHA-256 over the DISPLAYED answer columns, in row order.

    Over the display strings rather than over the underlying results, so the
    thing hashed is the thing the sheet shows and the digest survives a round
    trip through a spreadsheet application without needing the results again
    to re-verify.  The two mark columns are out (see
    :data:`ANSWER_COLUMNS`), and the separators are control characters that
    cannot occur in a cell, so no concatenation of cell values can collide
    with a different one.
    """
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.item_id.encode("utf-8"))
        for header in ANSWER_COLUMNS:
            digest.update(b"\x1f")
            digest.update(str(row.cells.get(header, "")).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _openpyxl() -> Any:
    try:
        import openpyxl  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise Refused(
            "the pilot table needs openpyxl, which is the council-tuning extra: "
            "pip install 'kbutillib[council-tuning]'"
        ) from exc
    return openpyxl


def _text(cell: Any, value: Any) -> Any:
    """Assign *value* and force it to be a STRING.

    ``openpyxl`` reads a leading ``=`` as a formula, and an annotation or a
    GO label may begin with anything; see the module docstring (C21).
    """
    cell.value = "" if value is None else str(value)
    cell.data_type = "s"
    return cell


def _write_workbook(
    rows: Sequence[Row],
    xlsx_path: Any,
    *,
    manifest_id: str,
    scope: str,
) -> Path:
    """The ``.xlsx``: the table, the instructions sheet, the hidden sheet."""
    openpyxl = _openpyxl()
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = TABLE_SHEET

    header_font = Font(bold=True)
    header_align = Alignment(vertical="center", wrap_text=True)
    body_align = Alignment(vertical="top", wrap_text=True)

    for index, (header, width) in enumerate(COLUMNS, start=1):
        cell = _text(sheet.cell(row=1, column=index), header)
        cell.font = header_font
        cell.alignment = header_align
        sheet.column_dimensions[get_column_letter(index)].width = width

    for offset, row in enumerate(rows, start=2):
        for index, value in enumerate(row.values(), start=1):
            _text(sheet.cell(row=offset, column=index), value).alignment = body_align

    # Frozen header, and the mark column's dropdown over exactly the data
    # rows: offering R/W/? on an empty row below the table would invite a
    # mark the reader has no item to attach to.
    sheet.freeze_panes = "A2"
    mark_column = get_column_letter(
        1 + [header for header, _ in COLUMNS].index(MARK_COLUMN)
    )
    if rows:
        validation = DataValidation(
            type="list",
            formula1='"' + ",".join(MARKS) + '"',
            allow_blank=True,
            showDropDown=False,
            errorTitle="Not a mark",
            error="Use R (right), W (wrong) or ? (unsure).",
            promptTitle="Mark",
            prompt="R = right, W = wrong (put the right GO ids in the note), ? = unsure.",
        )
        sheet.add_data_validation(validation)
        validation.add(f"{mark_column}2:{mark_column}{len(rows) + 1}")

    guide = workbook.create_sheet(HOW_TO_MARK_SHEET)
    guide.column_dimensions["A"].width = 100
    for offset, line in enumerate(HOW_TO_MARK, start=1):
        _text(guide.cell(row=offset, column=1), line).alignment = Alignment(
            vertical="top", wrap_text=False
        )

    provenance = workbook.create_sheet(PROVENANCE_SHEET)
    provenance.sheet_state = "hidden"
    meta: List[Tuple[str, str]] = [
        ("manifest_id", str(manifest_id)),
        ("scope", str(scope)),
        ("answer_sha256", answer_digest(rows)),
        ("rows", str(len(rows))),
        # Comma-joined, not with a control character: openpyxl refuses the
        # control range in a cell value, and no column header holds a comma.
        ("columns", ", ".join(header for header, _ in COLUMNS)),
        ("code", str(code_version().get("git_commit") or "")),
    ]
    for offset, (key, value) in enumerate(meta, start=1):
        _text(provenance.cell(row=offset, column=1), key)
        _text(provenance.cell(row=offset, column=2), value)
    first = len(meta) + 2
    _text(provenance.cell(row=first, column=1), "item id")
    _text(provenance.cell(row=first, column=2), "menu ids")
    for offset, row in enumerate(rows, start=first + 1):
        _text(provenance.cell(row=offset, column=1), row.item_id)
        _text(provenance.cell(row=offset, column=2), ",".join(row.menu_ids))

    out = Path(xlsx_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(out)
    return out


def _write_markdown(
    rows: Sequence[Row], md_path: Any, *, manifest_id: str, scope: str
) -> Path:
    """The ``.md`` copy: the same columns, as a pipe table."""
    lines: List[str] = [
        f"# go_mf adjudication -- {scope} table",
        "",
        f"manifest `{manifest_id or '(unidentified)'}`, {len(rows)} row(s).",
        "",
        "## How to mark",
        "",
    ]
    lines.extend(f"    {line}" if line else "" for line in HOW_TO_MARK)
    lines.extend(["", "## Table", ""])
    headers = [header for header, _ in COLUMNS]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(v) for v in row.values()) + " |")
    out = Path(md_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _md_cell(value: Any) -> str:
    """One cell for a pipe table: newlines become ``<br>``, pipes escaped."""
    text = str(value or "")
    return text.replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")


def write_pilot(
    results: Any,
    manifest: Any,
    matched: Any,
    xlsx_path: Any,
    md_path: Any,
) -> Dict[str, Any]:
    """Write the pilot table as ``.xlsx`` (to mark) and ``.md`` (to read).

    One row per un-swapped pilot entry; a swapped entry contributes only its
    row's *position swap* cell.  Returns ``{"rows", "xlsx", "md",
    "manifest_id", "answer_sha256", "item_ids"}``.

    Raises:
        Refused: openpyxl is not installed, the manifest holds no pilot
            rows, or a row's item or result is missing.
    """
    return _write(results, manifest, matched, xlsx_path, md_path, scope="pilot")


def write_spot_check(
    results: Any,
    manifest: Any,
    matched: Any,
    item_ids: Sequence[str],
    xlsx_path: Any,
    md_path: Any,
) -> Dict[str, Any]:
    """The same table over one tranche's persisted spot-check ids.

    The scope is recorded in the hidden sheet so :func:`read_marks` knows to
    check the ids for being a SUBSET of the manifest's pilot rows rather than
    for being all of them.
    """
    if not list(item_ids):
        raise Refused(
            "no spot-check ids were given; a spot-check table over nothing would "
            "read as a tranche with no obligation"
        )
    return _write(
        results,
        manifest,
        matched,
        xlsx_path,
        md_path,
        scope="spot_check",
        item_ids=item_ids,
    )


def _write(
    results: Any,
    manifest: Any,
    matched: Any,
    xlsx_path: Any,
    md_path: Any,
    *,
    scope: str,
    item_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    payload = manifest.as_dict() if hasattr(manifest, "as_dict") else dict(manifest)
    manifest_id = str(payload.get("manifest_id") or "")
    rows = build_rows(results, manifest, matched, item_ids=item_ids)
    xlsx = _write_workbook(rows, xlsx_path, manifest_id=manifest_id, scope=scope)
    md = _write_markdown(rows, md_path, manifest_id=manifest_id, scope=scope)
    return {
        "rows": len(rows),
        "xlsx": str(xlsx),
        "md": str(md),
        "scope": scope,
        "manifest_id": manifest_id,
        "answer_sha256": answer_digest(rows),
        "item_ids": [row.item_id for row in rows],
    }


# ---------------------------------------------------------------------------
# reading the marks back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldRecord:
    """One row's gold terms, and where they came from (C12).

    ``provenance`` is ``judge`` when the marker accepted the adjudicated set
    and ``human`` when they replaced it, which is the distinction a later
    scoring pass needs: a corpus whose gold is mostly ``judge`` is measuring
    the judge against its own agreement, and that is worth knowing before
    anyone tunes against it.
    """

    item_id: str
    mark: str
    provenance: str
    ids: Tuple[str, ...]
    off_menu: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "mark": self.mark,
            "provenance": self.provenance,
            "ids": list(self.ids),
            "off_menu": self.off_menu,
        }


@dataclass(frozen=True)
class Gold:
    """What one marked table says: a mark for every row, gold for some.

    ``marks`` covers EVERY row including the blank ones, because agreement
    is computed over the marks and a dict that held only the answered rows
    would silently shrink the denominator.  ``records`` holds only the rows
    that produced gold terms, and ``excluded`` says why each of the others
    did not.
    """

    manifest_id: str
    scope: str
    marks: Mapping[str, str]
    records: Tuple[GoldRecord, ...] = ()
    excluded: Mapping[str, str] = field(default_factory=dict)
    unreviewed: Tuple[str, ...] = ()
    notes: Mapping[str, str] = field(default_factory=dict)
    answer_sha256: str = ""

    @property
    def counts(self) -> Dict[str, int]:
        """``R``/``W``/``?``/``blank`` over every row, plus the derived totals."""
        out = {mark: 0 for mark in MARKS}
        out["blank"] = 0
        for mark in self.marks.values():
            out[mark if mark in MARKS else "blank"] += 1
        out["rows"] = len(self.marks)
        out["gold"] = len(self.records)
        out["excluded"] = len(self.excluded)
        return out

    def by_item(self) -> Dict[str, GoldRecord]:
        return {record.item_id: record for record in self.records}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "scope": self.scope,
            "marks": dict(self.marks),
            "records": [record.as_dict() for record in self.records],
            "excluded": dict(self.excluded),
            "unreviewed": list(self.unreviewed),
            "notes": dict(self.notes),
            "answer_sha256": self.answer_sha256,
            "counts": self.counts,
        }


def _normalise_mark(value: Any) -> str:
    """A cell's mark, upper-cased and trimmed.  ``""`` for anything empty.

    Raises:
        ValueError: the cell holds something that is not one of the three.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text not in MARKS:
        raise ValueError(text)
    return text


def read_marks(xlsx_path: Any, manifest: Any) -> Gold:
    """Read a marked table back as :class:`Gold`, or refuse it.

    The refusals, all of which are a DISTURBED sheet rather than a bad mark:

    * a missing table or provenance sheet, or a changed column order;
    * a manifest id that is not this manifest's;
    * a duplicate item id, or an id the manifest's pilot pool does not hold;
    * for a ``pilot`` scope, a row count or id set that is not the manifest's
      whole un-swapped pilot set;
    * an answer-column digest that does not match the hidden sheet's;
    * a mark cell holding anything other than ``R``, ``W``, ``?`` or blank.

    Raises:
        Refused: any of the above.
    """
    openpyxl = _openpyxl()
    path = Path(xlsx_path)
    if not path.exists():
        raise Refused(f"no marked table at {path}")
    workbook = openpyxl.load_workbook(path, data_only=True)
    for name in (TABLE_SHEET, PROVENANCE_SHEET):
        if name not in workbook.sheetnames:
            raise Refused(
                f"{path} has no {name!r} sheet, so it is not a table this reader "
                f"wrote; it holds {', '.join(workbook.sheetnames)}"
            )
    sheet = workbook[TABLE_SHEET]
    meta, recorded_menus = _read_provenance(workbook[PROVENANCE_SHEET], path)

    headers = [
        str(sheet.cell(row=1, column=i).value or "") for i in range(1, len(COLUMNS) + 1)
    ]
    expected = [header for header, _ in COLUMNS]
    if headers != expected:
        raise Refused(
            f"{path} has a different column order than this reader writes, so a "
            "cell cannot be attributed to a column; re-export the table rather "
            "than repairing it"
        )

    manifest_payload = (
        manifest.as_dict() if hasattr(manifest, "as_dict") else dict(manifest)
    )
    manifest_id = str(manifest_payload.get("manifest_id") or "")
    if str(meta.get("manifest_id") or "") != manifest_id:
        raise Refused(
            f"{path} was written for manifest {meta.get('manifest_id') or '(none)'!r} "
            f"but was read against {manifest_id!r}; marks collected against another "
            "sample say nothing about this one"
        )
    scope = str(meta.get("scope") or "pilot")

    rows, marks, notes, duplicates = _read_rows(sheet, expected)
    if duplicates:
        raise Refused(
            f"{len(duplicates)} item id(s) appear more than once in {path}, so a "
            "mark cannot be attributed to one row",
            duplicates,
        )

    pool = {str(e["id"]) for e in pilot_entries(manifest)}
    found = [row.item_id for row in rows]
    foreign = sorted(set(found) - pool)
    if foreign:
        raise Refused(
            f"{len(foreign)} row(s) of {path} are not un-swapped pilot rows of "
            f"manifest {manifest_id or '(unidentified)'}",
            foreign,
        )
    if scope == "pilot":
        if len(found) != len(pool) or set(found) != pool:
            missing = sorted(pool - set(found))
            raise Refused(
                f"{path} holds {len(found)} row(s) but manifest "
                f"{manifest_id or '(unidentified)'} has {len(pool)} pilot row(s); "
                "a table that has gained or lost rows cannot be paired with the "
                "sample it was drawn from",
                missing,
            )
    if meta.get("rows") and int(meta["rows"]) != len(found):
        raise Refused(
            f"{path} holds {len(found)} row(s) but its own provenance sheet records "
            f"{meta['rows']}; rows have been inserted or deleted"
        )

    digest = answer_digest(rows)
    if str(meta.get("answer_sha256") or "") != digest:
        raise Refused(
            f"the answer columns of {path} hash to {digest[:12]}... but its "
            f"provenance sheet records {str(meta.get('answer_sha256') or '')[:12]}"
            "...; a column has been sorted or an answer cell overwritten, so the "
            "marks can no longer be trusted to line up with the adjudications"
        )

    unknown_menu = sorted(r.item_id for r in rows if r.item_id not in recorded_menus)
    if unknown_menu:
        raise Refused(
            f"{len(unknown_menu)} row(s) of {path} are not listed on its provenance "
            "sheet, so the menu a replacement GO id would be checked against is not "
            "known; off-menu could not be derived",
            unknown_menu,
        )

    records: List[GoldRecord] = []
    excluded: Dict[str, str] = {}
    unreviewed: List[str] = []
    for row in rows:
        item_id = row.item_id
        mark = marks[item_id]
        menu = recorded_menus[item_id]
        if mark == "R":
            ids = go_ids(row.cells.get("correct terms", ""))
            records.append(
                GoldRecord(
                    item_id=item_id,
                    mark="R",
                    provenance="judge",
                    ids=ids,
                    off_menu=any(i not in menu for i in ids),
                )
            )
            continue
        if mark == "W":
            ids = go_ids(notes.get(item_id, ""))
            if ids:
                records.append(
                    GoldRecord(
                        item_id=item_id,
                        mark="W",
                        provenance="human",
                        ids=ids,
                        off_menu=any(i not in menu for i in ids),
                    )
                )
            else:
                excluded[item_id] = "wrong_without_ids"
            continue
        if mark == "?":
            excluded[item_id] = "unsure"
            continue
        excluded[item_id] = "unmarked"
        unreviewed.append(item_id)

    return Gold(
        manifest_id=manifest_id,
        scope=scope,
        marks=marks,
        records=tuple(records),
        excluded=excluded,
        unreviewed=tuple(unreviewed),
        notes={k: v for k, v in notes.items() if v},
        answer_sha256=digest,
    )


def _read_provenance(
    sheet: Any, path: Path
) -> Tuple[Dict[str, str], Dict[str, Tuple[str, ...]]]:
    """The hidden sheet's ``key -> value`` block and its per-item menu ids."""
    meta: Dict[str, str] = {}
    menus: Dict[str, Tuple[str, ...]] = {}
    in_table = False
    for row in sheet.iter_rows(min_col=1, max_col=2, values_only=True):
        key = str((row[0] if row else None) or "").strip()
        value = str((row[1] if len(row) > 1 else None) or "")
        if not key:
            continue
        if key == "item id":
            in_table = True
            continue
        if in_table:
            menus[key] = tuple(i for i in value.split(",") if i)
        else:
            meta[key] = value
    if "answer_sha256" not in meta:
        raise Refused(
            f"the provenance sheet of {path} records no answer_sha256, so the "
            "table cannot be checked for having been disturbed"
        )
    return meta, menus


def _read_rows(
    sheet: Any, headers: Sequence[str]
) -> Tuple[List[Row], Dict[str, str], Dict[str, str], List[str]]:
    """``(rows, marks, notes, duplicate ids)`` off the table sheet.

    Rows are rebuilt as :class:`Row` with the two mark columns BLANKED, so
    :func:`answer_digest` over them is comparable with the digest taken
    before a human touched the file.
    """
    rows: List[Row] = []
    marks: Dict[str, str] = {}
    notes: Dict[str, str] = {}
    duplicates: List[str] = []
    bad: List[str] = []
    for values in sheet.iter_rows(
        min_row=2, min_col=1, max_col=len(headers), values_only=True
    ):
        cells = {
            header: ("" if value is None else str(value))
            for header, value in zip(headers, values)
        }
        item_id = cells.get("item id", "").strip()
        if not item_id and not any(v.strip() for v in cells.values()):
            continue
        if not item_id:
            raise Refused(
                "a row of the table carries cells but no item id, so its mark "
                "belongs to no adjudication"
            )
        if item_id in marks:
            duplicates.append(item_id)
            continue
        try:
            marks[item_id] = _normalise_mark(cells.get(MARK_COLUMN))
        except ValueError as exc:
            bad.append(f"{item_id}={exc.args[0]!r}")
            continue
        notes[item_id] = str(cells.get(NOTE_COLUMN, "")).strip()
        cells[MARK_COLUMN] = ""
        cells[NOTE_COLUMN] = ""
        rows.append(Row(item_id=item_id, cells=cells, menu_ids=()))
    if bad:
        raise Refused(
            f"{len(bad)} mark cell(s) hold something other than "
            f"{', '.join(MARKS)} or blank: {', '.join(sorted(bad))}"
        )
    return rows, marks, notes, sorted(set(duplicates))
