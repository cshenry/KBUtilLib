"""Tests for :class:`EcRoleResolver` (EC number -> ModelSEED role name).

All fixtures are synthetic ``Roles.tsv`` files written to ``tmp_path`` --
none of these tests depend on a real ModelSEED checkout being present.

Core invariants under test:
  * An EC number embedded in a role name (as ``"(EC x.x.x.x)"``) is parsed
    out correctly and used as the index key.
  * One EC mapping to several roles returns all of them.
  * A wildcard EC such as ``"1.1.1.-"`` does NOT match concrete ECs sharing
    its prefix (``"1.1.1.1"``, ``"1.1.1.2"``, ...) -- no prefix expansion.
  * A role name with an unrelated trailing parenthetical is not mistaken for
    an EC-bearing role.
  * An EC with no matching role returns ``[]``, never raises.
  * Loading is lazy and cached (the TSV is parsed at most once per instance).
"""

from __future__ import annotations

from kbutillib.domains.modeling.ec_role_resolver import EcRoleResolver

_HEADER = "id\tname\tsource\tfeatures\taliases\n"


def _write_roles_fixture(tmp_path, rows):
    """Write a synthetic Annotations/Roles.tsv with the given data rows and
    return its path."""
    path = tmp_path / "Roles.tsv"
    path.write_text(_HEADER + "".join(rows))
    return str(path)


def test_ec_parsed_correctly_out_of_role_name(tmp_path):
    path = _write_roles_fixture(
        tmp_path,
        ["role1\tArylsulfatase B precursor (EC 3.1.6.12)\tSSO\t\t\n"],
    )
    resolver = EcRoleResolver(path)
    assert resolver.roles_for_ec("3.1.6.12") == [
        "Arylsulfatase B precursor (EC 3.1.6.12)"
    ]


def test_one_ec_maps_to_several_roles_returns_all(tmp_path):
    path = _write_roles_fixture(
        tmp_path,
        [
            "role1\tAlcohol dehydrogenase (EC 1.1.1.1)\tSSO\t\t\n",
            "role2\tAlcohol dehydrogenase, propanol-preferring (EC 1.1.1.1)\tSSO\t\t\n",
            "role3\tNAD-dependent alcohol dehydrogenase (EC 1.1.1.1)\tSSO\t\t\n",
        ],
    )
    resolver = EcRoleResolver(path)
    assert resolver.roles_for_ec("1.1.1.1") == [
        "Alcohol dehydrogenase (EC 1.1.1.1)",
        "Alcohol dehydrogenase, propanol-preferring (EC 1.1.1.1)",
        "NAD-dependent alcohol dehydrogenase (EC 1.1.1.1)",
    ]


def test_wildcard_ec_does_not_match_concrete_siblings(tmp_path):
    path = _write_roles_fixture(
        tmp_path,
        [
            "role1\tGeneric short-chain dehydrogenase (EC 1.1.1.-)\tSSO\t\t\n",
            "role2\tAlcohol dehydrogenase (EC 1.1.1.1)\tSSO\t\t\n",
            "role3\tLactate dehydrogenase (EC 1.1.1.2)\tSSO\t\t\n",
            "role4\tAnother wildcard-only role (EC 1.1.1.-)\tSSO\t\t\n",
        ],
    )
    resolver = EcRoleResolver(path)

    wildcard_hits = resolver.roles_for_ec("1.1.1.-")
    assert wildcard_hits == [
        "Generic short-chain dehydrogenase (EC 1.1.1.-)",
        "Another wildcard-only role (EC 1.1.1.-)",
    ]

    # The wildcard must never leak into concrete ECs sharing its prefix.
    assert resolver.roles_for_ec("1.1.1.1") == ["Alcohol dehydrogenase (EC 1.1.1.1)"]
    assert resolver.roles_for_ec("1.1.1.2") == ["Lactate dehydrogenase (EC 1.1.1.2)"]

    # ...nor the other direction: a concrete query must never pick up the
    # wildcard-only roles.
    assert "Generic short-chain dehydrogenase (EC 1.1.1.-)" not in resolver.roles_for_ec(
        "1.1.1.1"
    )


def test_unrelated_trailing_parenthetical_not_mistaken_for_ec(tmp_path):
    path = _write_roles_fixture(
        tmp_path,
        [
            # Trailing parenthetical is NOT an EC suffix -- must not be
            # picked up as one, and this role must not appear under any EC.
            "role1\tHypothetical protein (putative)\tSSO\t\t\n",
            # EC-bearing role followed by an unrelated trailing parenthetical
            # -- the EC-specific pattern must still be found, and the
            # trailing "(putative)" must not be treated as a second EC.
            "role2\tArylsulfatase B precursor (EC 3.1.6.12) (putative)\tSSO\t\t\n",
        ],
    )
    resolver = EcRoleResolver(path)
    assert resolver.roles_for_ec("3.1.6.12") == [
        "Arylsulfatase B precursor (EC 3.1.6.12) (putative)"
    ]
    # The non-EC-bearing role is unreachable under any EC query.
    assert resolver.roles_for_ec("putative") == []


def test_unmatched_ec_returns_empty_list(tmp_path):
    path = _write_roles_fixture(
        tmp_path,
        ["role1\tArylsulfatase B precursor (EC 3.1.6.12)\tSSO\t\t\n"],
    )
    resolver = EcRoleResolver(path)
    assert resolver.roles_for_ec("9.9.9.9") == []


def test_malformed_ec_query_returns_empty_list_not_raise(tmp_path):
    path = _write_roles_fixture(
        tmp_path,
        ["role1\tArylsulfatase B precursor (EC 3.1.6.12)\tSSO\t\t\n"],
    )
    resolver = EcRoleResolver(path)
    assert resolver.roles_for_ec("not-an-ec-number") == []
    assert resolver.roles_for_ec("") == []


def test_ec_prefix_tolerated_in_query(tmp_path):
    path = _write_roles_fixture(
        tmp_path,
        ["role1\tArylsulfatase B precursor (EC 3.1.6.12)\tSSO\t\t\n"],
    )
    resolver = EcRoleResolver(path)
    assert resolver.roles_for_ec("EC 3.1.6.12") == [
        "Arylsulfatase B precursor (EC 3.1.6.12)"
    ]
    assert resolver.roles_for_ec("ec3.1.6.12") == [
        "Arylsulfatase B precursor (EC 3.1.6.12)"
    ]


def test_loading_is_lazy_and_cached(tmp_path, monkeypatch):
    path = _write_roles_fixture(
        tmp_path,
        ["role1\tArylsulfatase B precursor (EC 3.1.6.12)\tSSO\t\t\n"],
    )
    resolver = EcRoleResolver(path)
    # Not loaded yet -- constructor never touches the filesystem.
    assert resolver._index is None

    calls = {"n": 0}
    real_build = resolver._build_index

    def _counting_build():
        calls["n"] += 1
        return real_build()

    monkeypatch.setattr(resolver, "_build_index", _counting_build)

    resolver.roles_for_ec("3.1.6.12")
    resolver.roles_for_ec("3.1.6.12")
    resolver.roles_for_ec("9.9.9.9")
    assert calls["n"] == 1


def test_multiple_ecs_in_one_role_name_both_indexed(tmp_path):
    path = _write_roles_fixture(
        tmp_path,
        [
            "role1\tBifunctional enzyme (EC 2.7.7.3) / (EC 2.7.1.24)\tSSO\t\t\n",
        ],
    )
    resolver = EcRoleResolver(path)
    assert resolver.roles_for_ec("2.7.7.3") == [
        "Bifunctional enzyme (EC 2.7.7.3) / (EC 2.7.1.24)"
    ]
    assert resolver.roles_for_ec("2.7.1.24") == [
        "Bifunctional enzyme (EC 2.7.7.3) / (EC 2.7.1.24)"
    ]
