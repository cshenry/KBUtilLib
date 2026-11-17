"""``go_tuning.table`` -- the pilot table, and the marks read back off it.

The round-trip test is the one that matters: write the workbook, mark it
through ``openpyxl`` the way a person marking it in Excel would (open, type
in two columns, save), and read it back as gold.  Everything the module does
to make the sheet markable -- the frozen header, the wrapped cells, the
dropdown, the hidden provenance sheet -- only earns its keep if the file
survives that round trip, and the only way to know is to do it.

The refusals get their own tests because each one is a DIFFERENT accident: a
deleted row, a sorted answer column, a duplicated id, a sheet marked against
the wrong manifest.  None of them changes anything a reader would notice, so
each has to be caught by construction rather than by eye.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("openpyxl")

import openpyxl  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from kbutillib.domains.ai.council.go_tuning import table as T  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.conflicts import Refused  # noqa: E402

from .go_tuning_table_support import (  # noqa: E402
    FORMULA_ANNOTATION,
    MENU,
    MENU_IDS,
    OFF_MENU_ID,
    mark,
    material,
    results_for,
    row_ids,
)


@pytest.fixture()
def written(tmp_path: Any) -> Dict[str, Any]:
    """A written pilot table, plus everything it was written from."""
    manifest, items = material()
    results = results_for(manifest, items)
    out = T.write_pilot(
        results, manifest, items, tmp_path / "pilot.xlsx", tmp_path / "pilot.md"
    )
    return {
        "manifest": manifest,
        "items": items,
        "results": results,
        "out": out,
        "xlsx": tmp_path / "pilot.xlsx",
        "md": tmp_path / "pilot.md",
    }


def _sheet(path: Any, name: str = T.TABLE_SHEET) -> Any:
    return openpyxl.load_workbook(path)[name]


def _column(sheet: Any, header: str) -> List[str]:
    index = 1 + [h for h, _ in T.COLUMNS].index(header)
    return [
        str(sheet.cell(row=row, column=index).value or "")
        for row in range(2, sheet.max_row + 1)
    ]


# ---------------------------------------------------------------------------
# the shape of the table
# ---------------------------------------------------------------------------


class TestShape:
    def test_one_row_per_pilot_item_and_not_per_manifest_entry(self, written) -> None:
        """A swapped row is ONE row; its twin is the position-swap column."""
        manifest = written["manifest"]
        entries = [e for e in manifest.as_dict()["entries"] if e["pool"] == "pilot"]
        swapped = [e for e in entries if e["swap"]]
        assert swapped, "the manifest sampled no swap slice, so nothing is tested"
        assert written["out"]["rows"] == len(entries) - len(swapped)
        sheet = _sheet(written["xlsx"])
        assert _column(sheet, "item id") == row_ids(manifest)
        assert not any(i.endswith(":swap") for i in _column(sheet, "item id"))

    def test_the_columns_are_in_the_specified_order(self, written) -> None:
        sheet = _sheet(written["xlsx"])
        headers = [
            str(sheet.cell(row=1, column=i).value) for i in range(1, len(T.COLUMNS) + 1)
        ]
        assert headers == [
            "item id",
            "annotation",
            "source tool",
            "class",
            "ALCF terms",
            "go_mf terms",
            "verdict",
            "correct terms",
            "off-menu flag",
            "off-menu description",
            "failure type",
            "rules cited",
            "confidence",
            "why",
            "split",
            "position swap",
            "Chris mark",
            "Chris note",
        ]

    def test_every_go_id_is_printed_as_id_pipe_name_one_per_line(self, written) -> None:
        sheet = _sheet(written["xlsx"])
        labels = {e["id"]: e["label"] for e in MENU}
        seen = 0
        for header in ("ALCF terms", "go_mf terms", "correct terms"):
            for cell in _column(sheet, header):
                if not cell:
                    continue
                for line in cell.splitlines():
                    term, _, name = line.partition(" | ")
                    assert term in labels, line
                    assert name == labels[term], line
                    seen += 1
        assert seen > 0

    def test_a_meta_decided_item_matching_neither_side_renders_neither(
        self, written
    ) -> None:
        """The live run's shape: the council kept a term both sides lacked."""
        sheet = _sheet(written["xlsx"])
        verdicts = _column(sheet, "verdict")
        splits = _column(sheet, "split")
        assert "neither" in verdicts
        position = verdicts.index("neither")
        assert splits[position] == "meta"
        assert _column(sheet, "correct terms")[position].startswith("GO:0015426 | ")

    def test_an_off_menu_claim_is_legible(self, written) -> None:
        sheet = _sheet(written["xlsx"])
        flags = _column(sheet, "off-menu flag")
        assert "yes" in flags
        position = flags.index("yes")
        assert "ABC transporter" in _column(sheet, "off-menu description")[position]
        assert set(flags) <= {"yes", "no", ""}

    def test_the_position_swap_column_is_blank_unless_the_row_was_swapped(
        self, written
    ) -> None:
        manifest = written["manifest"]
        swapped = {
            str(e["item_id"])
            for e in manifest.as_dict()["entries"]
            if e["pool"] == "pilot" and e["swap"]
        }
        sheet = _sheet(written["xlsx"])
        pairs = dict(zip(_column(sheet, "item id"), _column(sheet, "position swap")))
        for item_id, value in pairs.items():
            if item_id in swapped:
                assert value == "same", item_id
            else:
                assert value == "", item_id

    def test_a_flipped_swap_pair_reads_flipped(self, tmp_path) -> None:
        manifest, items = material()
        results = results_for(manifest, items, flip_swap=True)
        T.write_pilot(
            results, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md"
        )
        assert "flipped" in _column(_sheet(tmp_path / "p.xlsx"), "position swap")

    def test_the_header_is_frozen_the_cells_wrap_and_widths_are_set(
        self, written
    ) -> None:
        sheet = _sheet(written["xlsx"])
        assert sheet.freeze_panes == "A2"
        assert sheet.cell(row=2, column=2).alignment.wrap_text is True
        assert sheet.cell(row=1, column=2).alignment.wrap_text is True
        widths = {
            letter: dim.width for letter, dim in sheet.column_dimensions.items()
        }
        assert len(widths) == len(T.COLUMNS)
        assert all(w and w > 0 for w in widths.values())

    def test_the_mark_column_carries_an_r_w_question_dropdown(self, written) -> None:
        sheet = _sheet(written["xlsx"])
        validations = sheet.data_validations.dataValidation
        assert len(validations) == 1
        validation = validations[0]
        assert validation.type == "list"
        assert validation.formula1 == '"R,W,?"'
        mark_letter = get_column_letter(
            1 + [h for h, _ in T.COLUMNS].index(T.MARK_COLUMN)
        )
        rows = written["out"]["rows"]
        assert str(validation.sqref) == f"{mark_letter}2:{mark_letter}{rows + 1}"

    def test_a_how_to_mark_sheet_states_what_the_three_marks_mean(
        self, written
    ) -> None:
        guide = "\n".join(
            str(row[0] or "")
            for row in _sheet(written["xlsx"], T.HOW_TO_MARK_SHEET).iter_rows(
                min_col=1, max_col=1, values_only=True
            )
        )
        assert "R  = right" in guide
        assert "W  = wrong" in guide
        assert "?  = unsure" in guide
        assert "both acceptable" in guide and "DESCRIPTIVE ONLY" in guide
        assert "ONE canonical correct set" in guide
        assert "blank mark is NOT an answer" in guide

    def test_text_is_written_as_a_literal_string_not_a_formula(self, written) -> None:
        """C21: an annotation starting with ``=`` is four characters, not a formula."""
        sheet = _sheet(written["xlsx"])
        annotations = _column(sheet, "annotation")
        assert FORMULA_ANNOTATION in annotations
        index = 1 + [h for h, _ in T.COLUMNS].index("annotation")
        for row in range(2, sheet.max_row + 1):
            assert sheet.cell(row=row, column=index).data_type == "s"

    def test_the_hidden_sheet_holds_the_manifest_id_and_the_answer_hash(
        self, written
    ) -> None:
        workbook = openpyxl.load_workbook(written["xlsx"])
        assert workbook[T.PROVENANCE_SHEET].sheet_state == "hidden"
        meta = {
            str(row[0]): str(row[1] or "")
            for row in workbook[T.PROVENANCE_SHEET].iter_rows(
                min_col=1, max_col=2, values_only=True
            )
            if row[0]
        }
        assert meta["manifest_id"] == written["manifest"].manifest_id
        assert meta["scope"] == "pilot"
        assert len(meta["answer_sha256"]) == 64
        assert meta["answer_sha256"] == written["out"]["answer_sha256"]


class TestMarkdownCopy:
    def test_the_md_copy_is_a_pipe_table_with_the_same_columns(self, written) -> None:
        text = written["md"].read_text(encoding="utf-8")
        headers = [h for h, _ in T.COLUMNS]
        assert "| " + " | ".join(headers) + " |" in text
        body = [
            line
            for line in text.splitlines()
            if line.startswith("| ") and not line.startswith("| item id")
            and set(line.replace("|", "").strip()) != {"-", " "}
        ]
        assert len(body) == written["out"]["rows"]
        for line in body:
            # Unescaped pipes only: a ``GO:id | name`` cell carries ``\|``,
            # which is exactly why the writer escapes it.
            assert len(re.findall(r"(?<!\\)\|", line)) == len(headers) + 1

    def test_a_multi_line_cell_becomes_br_rather_than_breaking_the_table(
        self, written
    ) -> None:
        text = written["md"].read_text(encoding="utf-8")
        assert "<br>GO:" in text


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_write_mark_read(self, written) -> None:
        """R keeps the judge's ids; W with ids in the note replaces them."""
        manifest = written["manifest"]
        ids = row_ids(manifest)
        marks = {item_id: "R" for item_id in ids}
        marks[ids[0]] = "W"
        marks[ids[1]] = "W"
        marks[ids[2]] = "?"
        notes = {ids[0]: f"should be GO:0015749 and {OFF_MENU_ID}"}
        mark(written["xlsx"], marks, notes)

        gold = T.read_marks(written["xlsx"], manifest)

        assert gold.manifest_id == manifest.manifest_id
        assert gold.scope == "pilot"
        assert gold.marks == marks
        assert gold.unreviewed == ()
        assert gold.counts["R"] == len(ids) - 3
        assert gold.counts["W"] == 2
        assert gold.counts["?"] == 1
        assert gold.counts["blank"] == 0

        by_item = gold.by_item()
        human = by_item[ids[0]]
        assert human.mark == "W"
        assert human.provenance == "human"
        assert human.ids == ("GO:0015749", OFF_MENU_ID)
        assert human.off_menu is True, "a replacement id not on the menu is off-menu"

        judge = by_item[ids[3]]
        assert judge.mark == "R"
        assert judge.provenance == "judge"
        assert judge.ids and all(i in MENU_IDS for i in judge.ids)
        assert judge.off_menu is False

        assert gold.excluded == {ids[1]: "wrong_without_ids", ids[2]: "unsure"}
        assert ids[1] not in by_item and ids[2] not in by_item

    def test_an_r_mark_keeps_the_adjudicated_ids_including_an_off_menu_one(
        self, tmp_path
    ) -> None:
        """R's gold is the sheet's own ``correct terms``, off-menu and all."""
        manifest, items = material()
        results = results_for(manifest, items)
        ids = row_ids(manifest)
        # Give one row a settled answer carrying a term the menu never held.
        patched = [
            replace(result, final_ids=(OFF_MENU_ID,))
            if result.item_id == ids[4]
            else result
            for result in results
        ]
        T.write_pilot(
            patched, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md"
        )
        mark(tmp_path / "p.xlsx", {i: "R" for i in ids})
        gold = T.read_marks(tmp_path / "p.xlsx", manifest)
        record = gold.by_item()[ids[4]]
        assert record.ids == (OFF_MENU_ID,)
        assert record.off_menu is True

    def test_a_w_without_ids_a_question_and_a_blank_never_borrow_judge_gold(
        self, written
    ) -> None:
        """C12: the three explicit exclusions supply no ids at all."""
        ids = row_ids(written["manifest"])
        mark(
            written["xlsx"],
            {ids[0]: "W", ids[1]: "?", ids[2]: "R"},
            {ids[0]: "wrong but I do not know the right term"},
        )
        gold = T.read_marks(written["xlsx"], written["manifest"])
        assert gold.by_item().keys() == {ids[2]}
        assert gold.excluded[ids[0]] == "wrong_without_ids"
        assert gold.excluded[ids[1]] == "unsure"
        for item_id in ids[3:]:
            assert gold.excluded[item_id] == "unmarked"
        assert set(gold.unreviewed) == set(ids[3:])
        assert gold.counts["blank"] == len(ids) - 3

    def test_a_note_without_a_well_formed_go_id_is_not_gold(self, written) -> None:
        ids = row_ids(written["manifest"])
        mark(written["xlsx"], {i: "R" for i in ids})
        mark(written["xlsx"], {ids[0]: "W"}, {ids[0]: "try GO:123 or maybe GO:abc"})
        gold = T.read_marks(written["xlsx"], written["manifest"])
        assert ids[0] not in gold.by_item()
        assert gold.excluded[ids[0]] == "wrong_without_ids"

    def test_lower_case_and_padded_marks_are_accepted(self, written) -> None:
        ids = row_ids(written["manifest"])
        mark(written["xlsx"], {i: "R" for i in ids})
        mark(written["xlsx"], {ids[0]: " w ", ids[1]: "r"})
        gold = T.read_marks(written["xlsx"], written["manifest"])
        assert gold.marks[ids[0]] == "W"
        assert gold.marks[ids[1]] == "R"


# ---------------------------------------------------------------------------
# the refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_deleted_row_is_refused(self, written) -> None:
        """A tampered row count: the manifest is the independent witness."""
        workbook = openpyxl.load_workbook(written["xlsx"])
        workbook[T.TABLE_SHEET].delete_rows(3)
        workbook.save(written["xlsx"])
        with pytest.raises(Refused, match="pilot row"):
            T.read_marks(written["xlsx"], written["manifest"])

    def test_an_inserted_row_is_refused(self, written) -> None:
        workbook = openpyxl.load_workbook(written["xlsx"])
        sheet = workbook[T.TABLE_SHEET]
        sheet.insert_rows(2)
        sheet.cell(row=2, column=1).value = "c-invented"
        workbook.save(written["xlsx"])
        with pytest.raises(Refused) as exc:
            T.read_marks(written["xlsx"], written["manifest"])
        assert "c-invented" in str(exc.value) or "c-invented" in exc.value.ids

    def test_a_duplicated_item_id_is_refused(self, written) -> None:
        workbook = openpyxl.load_workbook(written["xlsx"])
        sheet = workbook[T.TABLE_SHEET]
        sheet.cell(row=3, column=1).value = str(sheet.cell(row=2, column=1).value)
        workbook.save(written["xlsx"])
        with pytest.raises(Refused, match="more than once"):
            T.read_marks(written["xlsx"], written["manifest"])

    def test_an_overwritten_answer_cell_is_refused_by_the_hash(self, written) -> None:
        workbook = openpyxl.load_workbook(written["xlsx"])
        index = 1 + [h for h, _ in T.COLUMNS].index("correct terms")
        workbook[T.TABLE_SHEET].cell(row=2, column=index).value = "GO:0000001 | mine"
        workbook.save(written["xlsx"])
        with pytest.raises(Refused, match="hash"):
            T.read_marks(written["xlsx"], written["manifest"])

    def test_a_sorted_answer_column_is_refused_by_the_hash(self, written) -> None:
        """The accident that silently re-pairs every mark with another row."""
        workbook = openpyxl.load_workbook(written["xlsx"])
        sheet = workbook[T.TABLE_SHEET]
        index = 1 + [h for h, _ in T.COLUMNS].index("annotation")
        values = [
            sheet.cell(row=row, column=index).value
            for row in range(2, sheet.max_row + 1)
        ]
        for row, value in zip(range(2, sheet.max_row + 1), sorted(values, key=str)):
            sheet.cell(row=row, column=index).value = value
        workbook.save(written["xlsx"])
        with pytest.raises(Refused, match="hash"):
            T.read_marks(written["xlsx"], written["manifest"])

    def test_another_manifest_is_refused(self, written) -> None:
        other, _items = material(seed="seed-2")
        assert other.manifest_id != written["manifest"].manifest_id
        with pytest.raises(Refused, match="manifest"):
            T.read_marks(written["xlsx"], other)

    def test_a_mark_that_is_not_one_of_the_three_is_refused(self, written) -> None:
        ids = row_ids(written["manifest"])
        mark(written["xlsx"], {ids[0]: "maybe"})
        with pytest.raises(Refused, match="mark cell"):
            T.read_marks(written["xlsx"], written["manifest"])

    def test_a_workbook_that_is_not_one_of_ours_is_refused(self, tmp_path) -> None:
        manifest, _items = material()
        path = tmp_path / "random.xlsx"
        openpyxl.Workbook().save(path)
        with pytest.raises(Refused, match="sheet"):
            T.read_marks(path, manifest)

    def test_a_missing_file_is_refused(self, tmp_path) -> None:
        manifest, _items = material()
        with pytest.raises(Refused, match="no marked table"):
            T.read_marks(tmp_path / "absent.xlsx", manifest)

    def test_a_row_whose_result_is_missing_is_refused(self, tmp_path) -> None:
        manifest, items = material()
        results = results_for(manifest, items)
        keep = [r for r in results if r.item_id != row_ids(manifest)[0]]
        with pytest.raises(Refused, match="no adjudication result"):
            T.write_pilot(
                keep, manifest, items, tmp_path / "p.xlsx", tmp_path / "p.md"
            )

    def test_a_row_whose_matched_item_is_missing_is_refused(self, tmp_path) -> None:
        manifest, items = material()
        results = results_for(manifest, items)
        with pytest.raises(Refused, match="matched input"):
            T.write_pilot(
                results, manifest, items[:2], tmp_path / "p.xlsx", tmp_path / "p.md"
            )


# ---------------------------------------------------------------------------
# the spot-check table
# ---------------------------------------------------------------------------


class TestSpotCheck:
    def test_a_spot_check_table_holds_only_the_named_ids(self, tmp_path) -> None:
        manifest, items = material()
        results = results_for(manifest, items)
        wanted = row_ids(manifest)[:2]
        out = T.write_spot_check(
            results, manifest, items, wanted, tmp_path / "s.xlsx", tmp_path / "s.md"
        )
        assert out["rows"] == 2
        assert out["scope"] == "spot_check"
        assert _column(_sheet(tmp_path / "s.xlsx"), "item id") == wanted

    def test_a_spot_check_table_reads_back_without_the_whole_pilot(
        self, tmp_path
    ) -> None:
        """The pilot row-count rule must not refuse a legitimate subset."""
        manifest, items = material()
        results = results_for(manifest, items)
        wanted = row_ids(manifest)[:2]
        T.write_spot_check(
            results, manifest, items, wanted, tmp_path / "s.xlsx", tmp_path / "s.md"
        )
        mark(tmp_path / "s.xlsx", {wanted[0]: "R", wanted[1]: "W"})
        gold = T.read_marks(tmp_path / "s.xlsx", manifest)
        assert gold.scope == "spot_check"
        assert set(gold.marks) == set(wanted)
        assert gold.counts["rows"] == 2

    def test_an_id_outside_the_pilot_pool_is_refused(self, tmp_path) -> None:
        manifest, items = material()
        results = results_for(manifest, items)
        with pytest.raises(Refused, match="not un-swapped"):
            T.write_spot_check(
                results,
                manifest,
                items,
                ["c-nonesuch"],
                tmp_path / "s.xlsx",
                tmp_path / "s.md",
            )

    def test_an_empty_spot_check_set_is_refused(self, tmp_path) -> None:
        manifest, items = material()
        results = results_for(manifest, items)
        with pytest.raises(Refused, match="no spot-check ids"):
            T.write_spot_check(
                results, manifest, items, [], tmp_path / "s.xlsx", tmp_path / "s.md"
            )


# ---------------------------------------------------------------------------
# the id scanner
# ---------------------------------------------------------------------------


class TestGoIds:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("GO:0015749", ("GO:0015749",)),
            ("should be GO:0015749, not GO:0005524", ("GO:0015749", "GO:0005524")),
            ("GO:0015749 and GO:0015749 again", ("GO:0015749",)),
            ("GO:123", ()),
            ("GO:00157499", ()),
            ("", ()),
            (None, ()),
        ],
    )
    def test_only_well_formed_seven_digit_ids_are_read(self, text, expected) -> None:
        assert T.go_ids(text) == expected
