"""``go_mf@4`` pinned to the bytes poplar's live council store already holds.

``tests/.../test_go_mf_v4_builtin.py`` asserts that the committed
``builtin/go_mf_v4.yaml`` is ``@3`` plus one schema field.  That is a claim
about the YAML's RELATION to ``@3``; it is not a claim about its bytes, and
every assertion in it would still pass after an edit that reworded a prompt in
both files at once.

What is load-bearing on poplar is narrower and absolute: the live store's
``query_types`` table already holds a ``go_mf@4`` row, written there before the
spec was ever committed, and 28 finished jobs with 47,185 decided items were
produced under it.  ``QueryRegistry.seed_builtins`` runs on every council
start and re-puts the shipped spec, and ``put_query_type`` is immutable --
an existing ``(name, version)`` whose canonical spec DIFFERS raises
:class:`QueryTypeExists`.  So a one-character edit to ``go_mf_v4.yaml``, or a
change to ``QueryType.to_spec`` that moves how any spec normalises, does not
degrade anything gracefully: it makes every council start on poplar raise, and
it re-identifies the question those 47,185 items were decided under.

Hence the fixture beside this module, ``go_mf_v4_snapshot_spec.json``.  It is
the ``spec`` column of that store row, and the digest asserted below is the
digest of the row.  The fixture is the REFERENCE, never the thing under test:
if a test here fails, the shipped spec moved, and the fixture and the expected
digest are the two things that must NOT be edited to make it pass.

Nothing here makes a network call or touches the live store.  The only I/O is
reading the fixture and a SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from kbutillib.domains.ai.council.queries import QueryRegistry
from kbutillib.domains.ai.council.queries.builtin import load_builtin_spec
from kbutillib.domains.ai.council.queries.registry import validate_spec
from kbutillib.domains.ai.council.store import open_store
from kbutillib.domains.ai.council.store.errors import QueryTypeExists
from kbutillib.domains.ai.council.types import canonical_json
from kbutillib.domains.ai.llm.apis.schema_compat import strict_problems

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "go_mf_v4_snapshot_spec.json"

#: SHA-256 of :func:`canonical_json` of the ``go_mf@4`` row in poplar's store.
#:
#: Measured on the store snapshot, not computed from the committed YAML.  It
#: is the expected value on BOTH sides of the comparison below, which is the
#: only reason the comparison means anything: a digest recomputed from the
#: thing under test proves nothing.  NEVER edit it to make a test pass -- a
#: mismatch means the shipped spec moved away from the decided corpus.
SNAPSHOT_SHA256 = "77dab25a779f30d98f89ace4b38b6ea2c0fe6dc04434bf1f83c045d22dff2349"

ROLES = ("proposer", "critic", "meta")


@pytest.fixture
def snapshot():
    """The ``spec`` column of poplar's ``go_mf@4`` row, as stored."""
    return json.loads(SNAPSHOT_PATH.read_text())


@pytest.fixture
def shipped():
    """``builtin/go_mf_v4.yaml`` normalised the way the store normalises it."""
    return validate_spec(load_builtin_spec("go_mf", "4")).to_spec()


class TestTheSnapshotFixture:
    def test_the_fixture_is_the_row_the_digest_names(self, snapshot) -> None:
        """Checked FIRST, and separately from anything the package produces.

        This is the fixture's own integrity check: it says the committed JSON
        is still the store row, independently of whether the shipped YAML
        agrees with it.  Without it, a later "helpful" edit to the fixture
        would silently move the reference and the equality test below would go
        on passing against the wrong target.
        """
        digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()

        assert digest == SNAPSHOT_SHA256

    def test_it_is_a_go_mf_4_spec_and_says_so(self, snapshot) -> None:
        assert (snapshot["name"], snapshot["version"]) == ("go_mf", "4")


class TestTheShippedSpecStillMatchesTheStore:
    def test_to_spec_is_canonically_equal_to_the_snapshot(
        self, snapshot, shipped
    ) -> None:
        """The comparison ``put_query_type`` itself makes, made as a test.

        ``put_query_type`` compares ``canonical_json`` of the incoming spec
        against ``canonical_json`` of the stored one, and the incoming spec is
        ``validate_spec(...).to_spec()`` -- never the raw YAML mapping.  So
        this is the equality that decides whether a council start on poplar
        proceeds or raises, and it is asserted in exactly that form.

        The RAW YAML does NOT match, by 45 characters: the snapshot carries
        ``fragments: {}`` on each of the three roles and the YAML omits them,
        because ``to_spec`` fills the default in.  That difference is
        presentational -- an absent optional map and an empty one are the same
        spec -- but it means a test written against the raw load would pin the
        wrong bytes and would not catch a normalisation change in ``to_spec``,
        which is half of what can break the live store.
        """
        assert canonical_json(shipped) == canonical_json(snapshot)

    def test_the_raw_yaml_differs_from_the_store_row_only_by_those_defaults(
        self, snapshot
    ) -> None:
        """Pins WHICH difference the normalised form is allowed to absorb.

        Named explicitly so that the test above cannot be "fixed" one day by
        pointing it at the raw load: the only gap between the file and the row
        is three empty ``fragments`` maps.  Anything else appearing here is a
        real divergence.
        """
        raw = load_builtin_spec("go_mf", "4")

        for role in ROLES:
            assert "fragments" not in raw["roles"][role]
            assert snapshot["roles"][role]["fragments"] == {}

        patched = copy.deepcopy(raw)
        for role in ROLES:
            patched["roles"][role]["fragments"] = {}
        assert canonical_json(patched) == canonical_json(snapshot)


class TestSeedingOverPoplarsExistingRow:
    """The failure mode this module exists to prevent, reproduced end to end.

    Poplar's row was written by a live installation with ``source='user'``,
    not by ``seed_builtins``.  So the real question is not "do two builtins
    agree" but "does the shipped builtin agree with a row that was already
    there", and the only honest way to ask it is to put that row into a real
    store the same way and run the seeding.
    """

    def _store_holding_poplars_row(self, tmp_path: Path, spec):
        store = open_store(f"sqlite:///{tmp_path}/council.sqlite")
        store.init_schema()
        store.put_query_type(
            "go_mf",
            "4",
            spec,
            source="user",
            created_by="poplar",
        )
        return store

    def test_seeding_over_it_does_not_raise_and_leaves_it_alone(
        self, tmp_path: Path, snapshot
    ) -> None:
        store = self._store_holding_poplars_row(tmp_path, snapshot)

        QueryRegistry(store).seed_builtins()

        row = store.get_query_type("go_mf", "4")
        assert canonical_json(row["spec"]) == canonical_json(snapshot)
        # The identical re-put is a no-op in the store, so the row keeps the
        # provenance it was written with rather than being restamped builtin.
        assert row["source"] == "user"
        assert row["created_by"] == "poplar"
        assert QueryRegistry(store).get("go_mf", "4").to_spec() == snapshot

    def test_a_row_that_disagrees_by_one_character_is_what_raises(
        self, tmp_path: Path, snapshot
    ) -> None:
        """Proves the test above is not passing vacuously.

        If ``put_query_type`` tolerated a differing spec, the no-raise
        assertion would hold no matter what the shipped YAML said.  It does
        not: one changed character in one prompt is enough to halt the start,
        which is the whole reason the digest above is pinned.
        """
        drifted = copy.deepcopy(snapshot)
        drifted["roles"]["proposer"]["system"] += "."
        store = self._store_holding_poplars_row(tmp_path, drifted)

        with pytest.raises(QueryTypeExists):
            QueryRegistry(store).seed_builtins()


class TestTheStoreRowsSeatCompatibility:
    """Which seats the DECIDED spec can be served to, read off the row itself.

    ``tests/domains/ai/llm/test_schema_compat.py`` runs the same lint over the
    packaged YAML; this runs it over the store snapshot, which is the artefact
    the 47,185 items were decided under.  The two are canonically equal by
    ``TestTheShippedSpecStillMatchesTheStore``, so the point of repeating it
    here is that the compatibility fact is a property of the PINNED spec: if
    the pin ever has to move, this says what moving it costs.
    """

    @pytest.mark.parametrize("role", ["proposer", "critic"])
    def test_the_proposer_and_critic_rows_are_strict_clean(
        self, snapshot, role: str
    ) -> None:
        assert strict_problems(snapshot["roles"][role]["schema"]) == []

    def test_the_meta_row_refuses_a_strict_seat_before_any_paid_call(
        self, snapshot
    ) -> None:
        """A codex META seat under ``@4`` is unservable, and known locally.

        ``@4`` relaxed the meta schema to ``additionalProperties: true`` on
        purpose, to stop discarding complete answers that carried an extra
        commentary key.  OpenAI strict structured outputs require exactly
        ``false``, and enforce it server-side at 400 AFTER the codex CLI has
        accepted the schema file and opened a thread -- so without this lint
        the refusal costs a paid round trip per item.  The lint names the
        offending keyword so a quote-time refusal can say which field it is.
        """
        problems = strict_problems(snapshot["roles"]["meta"]["schema"])

        assert len(problems) == 1, problems
        assert "additionalProperties" in problems[0]
