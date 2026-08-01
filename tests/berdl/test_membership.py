"""Unit tests for kbutillib.domains.kbase.berdl.membership.

Pure logic, no network: ro-suffix decoding of the flat get_my_groups()
list against an available-groups fixture, including the negative case
where a tenant's real name legitimately ends in the suffix letters.
"""

from kbutillib.domains.kbase.berdl.membership import decode_memberships

# Standing membership at design time (fullprompt.md, "Membership decoding"):
# rw on aiale, kbaseincubator, kbase, globalusers, ideas, kescience;
# ro on enigma, microbialdiscoveryforge, planetmicrobe, refdata.
AVAILABLE_GROUPS = [
    "aiale",
    "kbaseincubator",
    "kbase",
    "globalusers",
    "ideas",
    "kescience",
    "enigma",
    "microbialdiscoveryforge",
    "planetmicrobe",
    "refdata",
    "cairo",  # a real tenant whose name happens to end in "ro"
]


class TestDecodeMemberships:
    def test_ro_suffixed_group_decodes_to_read_only(self):
        """'enigmaro' means read-only membership on 'enigma'."""
        result = decode_memberships(["enigmaro"], AVAILABLE_GROUPS)
        assert result == {"enigma": "ro"}

    def test_plain_group_decodes_to_read_write(self):
        """A name with no ro suffix is a plain read-write membership."""
        result = decode_memberships(["aiale"], AVAILABLE_GROUPS)
        assert result == {"aiale": "rw"}

    def test_full_standing_membership_matrix(self):
        """The whole design-time membership snapshot decodes correctly."""
        my_groups = [
            "aiale",
            "kbaseincubator",
            "kbase",
            "globalusers",
            "ideas",
            "kescience",
            "enigmaro",
            "microbialdiscoveryforgero",
            "planetmicrobero",
            "refdataro",
        ]

        result = decode_memberships(my_groups, AVAILABLE_GROUPS)

        assert result == {
            "aiale": "rw",
            "kbaseincubator": "rw",
            "kbase": "rw",
            "globalusers": "rw",
            "ideas": "rw",
            "kescience": "rw",
            "enigma": "ro",
            "microbialdiscoveryforge": "ro",
            "planetmicrobe": "ro",
            "refdata": "ro",
        }

    def test_tenant_name_legitimately_ending_in_ro_is_not_misclassified(self):
        """A real tenant literally named 'cairo' must NOT decode as ro-on-'cai'.

        This is the case a naive `endswith("ro")` check gets wrong: 'cai' is
        not a real group, so the trailing 'ro' in 'cairo' is part of the
        genuine tenant name, not a read-only marker.
        """
        result = decode_memberships(["cairo"], AVAILABLE_GROUPS)

        assert result == {"cairo": "rw"}
        assert "cai" not in result

    def test_naive_endswith_check_would_have_been_wrong(self):
        """Sanity check that the fixture actually exercises the trap.

        A naive `name.endswith("ro")` strip-and-accept (without checking the
        remainder against available groups) would produce {'cai': 'ro'} for
        this input -- documenting exactly the bug the safe decode avoids.
        """
        raw = "cairo"
        naive_stem = raw[:-2]
        assert naive_stem == "cai"
        assert naive_stem not in AVAILABLE_GROUPS

        result = decode_memberships([raw], AVAILABLE_GROUPS)
        assert result != {naive_stem: "ro"}

    def test_mixed_rw_and_ro_with_trap_tenant_present(self):
        """The trap tenant coexists correctly alongside a genuine ro entry."""
        result = decode_memberships(["cairo", "enigmaro"], AVAILABLE_GROUPS)

        assert result == {"cairo": "rw", "enigma": "ro"}

    def test_empty_input(self):
        assert decode_memberships([], AVAILABLE_GROUPS) == {}

    def test_ro_suffix_on_unknown_stem_falls_back_to_read_write(self):
        """If the stripped stem isn't a known group either, treat the raw name as rw.

        E.g. some other suffixed-looking name that isn't a genuine ro-marker
        and also doesn't correspond to any real group -- there's no safe
        reinterpretation, so it's recorded verbatim.
        """
        result = decode_memberships(["totallymadeupro"], AVAILABLE_GROUPS)
        assert result == {"totallymadeupro": "rw"}
