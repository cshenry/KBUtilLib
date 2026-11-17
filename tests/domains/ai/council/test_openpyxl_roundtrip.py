"""Real-dependency smoke for openpyxl, the `council-tuning` extra's only entry.

KBUtilLib has never used openpyxl, so nothing in this repo establishes that the
four workbook features the council's tuning sheet is built from actually
survive a save/load/edit/save/load cycle in a REAL openpyxl: a frozen header
row, wrap_text on a long cell, a list data-validation dropdown, and a mark a
human typed into that dropdown's column.

Each of those is a separate part of the file format (sheetViews, cellXfs,
dataValidations, sharedStrings), and openpyxl re-serialises the WHOLE workbook
on every save -- it does not patch the file in place. So "I wrote it once and
read it back" is not the property that matters; the property that matters is
that a round trip through openpyxl's own reader and writer preserves what the
previous round trip wrote. That is what this test pins, with no mock anywhere:
openpyxl writes the files and openpyxl reads them.

Skipped (not failed) when openpyxl is absent, because the extra is optional.
"""

from __future__ import annotations

from pathlib import Path

import pytest

openpyxl = pytest.importorskip(
    "openpyxl",
    reason="the council tuning sheet needs the `council-tuning` extra: "
    "pip install 'kbutillib[council-tuning]'",
)

from openpyxl import Workbook, load_workbook  # noqa: E402
from openpyxl.styles import Alignment  # noqa: E402
from openpyxl.worksheet.datavalidation import DataValidation  # noqa: E402

#: The three marks a reviewer may give a candidate: keep, drop, unsure.
MARKS = ("R", "W", "?")

#: openpyxl's own encoding of an explicit value list: one double-quoted,
#: comma-joined string, not a Python list and not a cell range.
MARK_FORMULA = '"R,W,?"'

#: The dropdown covers the mark column for every data row of the sheet.
MARK_RANGE = "C2:C200"

HEADER = ("term", "rationale", "mark")

#: Long enough that a reviewer reading it in Excel needs the cell to wrap
#: rather than run off under the neighbouring column.
LONG_TEXT = (
    "oxidoreductase activity, acting on paired donors, with incorporation or "
    "reduction of molecular oxygen, 2-oxoglutarate as one donor, and "
    "incorporation of one atom each of oxygen into both donors"
)


def _build_tuning_sheet(path: Path) -> None:
    """Write the sheet the way the tuning tool would: header, freeze, wrap, dropdown."""
    wb = Workbook()
    ws = wb.active
    ws.title = "tuning"

    ws.append(list(HEADER))
    # Freeze the header row so it stays visible while a reviewer scrolls.
    ws.freeze_panes = "A2"

    ws["A2"] = "GO:0016706"
    ws["B2"] = LONG_TEXT
    ws["B2"].alignment = Alignment(wrap_text=True, vertical="top")

    dv = DataValidation(type="list", formula1=MARK_FORMULA, allow_blank=True)
    dv.error = "Mark must be one of R, W or ?"
    ws.add_data_validation(dv)
    dv.add(MARK_RANGE)

    wb.save(path)


def _mark_validation(ws) -> DataValidation:
    """The sheet's single list validation, as openpyxl's reader rebuilt it."""
    validations = [
        dv for dv in ws.data_validations.dataValidation if dv.type == "list"
    ]
    assert len(validations) == 1, (
        f"expected exactly one list validation, got {len(validations)}"
    )
    return validations[0]


def test_tuning_sheet_survives_a_full_openpyxl_round_trip(tmp_path: Path) -> None:
    """Freeze, wrap, dropdown and a human's mark all survive write -> edit -> write."""
    path = tmp_path / "tuning.xlsx"
    _build_tuning_sheet(path)
    assert path.exists(), "openpyxl did not write the workbook"

    # ── First reload: everything the builder set must be readable back. ──
    wb = load_workbook(path)
    ws = wb["tuning"]

    assert [c.value for c in ws[1]] == list(HEADER)
    assert ws.freeze_panes == "A2"
    assert ws["B2"].alignment.wrap_text is True
    assert _mark_validation(ws).formula1 == MARK_FORMULA

    # The mark column is empty until a reviewer fills it in.
    assert ws["C2"].value is None

    # ── The human edit: type a value from the dropdown into the mark cell. ──
    ws["C2"] = "R"
    wb.save(path)

    # ── Second reload: the mark round-trips AND the formatting still holds. ──
    wb2 = load_workbook(path)
    ws2 = wb2["tuning"]

    assert ws2["C2"].value == "R"
    assert ws2["C2"].value in MARKS

    # openpyxl rewrote the entire file to save the mark; none of the three
    # formatting features may have been dropped on the way through.
    assert ws2.freeze_panes == "A2"
    assert ws2["B2"].alignment.wrap_text is True
    assert ws2["B2"].value == LONG_TEXT

    dv2 = _mark_validation(ws2)
    assert dv2.formula1 == MARK_FORMULA
    assert MARK_RANGE in str(dv2.sqref)
