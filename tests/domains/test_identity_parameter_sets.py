"""Tests for domains.identity.parameter_sets — the Clearinghouse's canonical
parameter-set canonicaliser, hash and validator.

These are fixed-vector tests: the canonical *text* is pinned to a literal in
every acceptance case (that text is the contract two independent writers must
agree on), while every hash expectation is derived from the live functions
rather than pasted as an opaque constant. The one exception is
DEFAULT_PARAMETER_SET_HASH, which is a published constant and is asserted
equal to parameter_set_hash({}).

Rejection cases each assert that ParameterSetError is raised *and* that its
message names the path to the offending value, since the path is what makes
the error actionable for a writer.
"""
from __future__ import annotations

import hashlib

import pytest

# ---------------------------------------------------------------------------
# Import path + published constant
# ---------------------------------------------------------------------------


def test_public_names_importable_from_canonical_path() -> None:
    """The four public names are importable from kbutillib.domains.identity."""
    from kbutillib.domains.identity import (  # noqa: PLC0415
        DEFAULT_PARAMETER_SET_HASH,
        ParameterSetError,
        canonical_parameter_set,
        parameter_set_hash,
    )

    assert callable(canonical_parameter_set)
    assert callable(parameter_set_hash)
    assert isinstance(DEFAULT_PARAMETER_SET_HASH, str)
    assert issubclass(ParameterSetError, ValueError)


def test_default_parameter_set_hash_equals_hash_of_empty_mapping() -> None:
    """DEFAULT_PARAMETER_SET_HASH is the published sha256 of the bytes '{}'."""
    from kbutillib.domains.identity import (  # noqa: PLC0415
        DEFAULT_PARAMETER_SET_HASH,
        parameter_set_hash,
    )

    assert (
        DEFAULT_PARAMETER_SET_HASH
        == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert parameter_set_hash({}) == DEFAULT_PARAMETER_SET_HASH


def test_parameter_set_hash_is_lowercase_hex_sha256_of_canonical_text() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        canonical_parameter_set,
        parameter_set_hash,
    )

    params = {"taxonomy_id": "562", "mode": "strict"}
    digest = parameter_set_hash(params)
    expected = hashlib.sha256(
        canonical_parameter_set(params).encode("utf-8")
    ).hexdigest()
    assert digest == expected
    assert len(digest) == 64
    assert digest == digest.lower()


# ---------------------------------------------------------------------------
# Accepted parameter sets — canonical text pinned to a literal
# ---------------------------------------------------------------------------


def test_default_run_canonicalises_to_empty_object() -> None:
    """A default run is {} and canonicalises to exactly two characters."""
    from kbutillib.domains.identity import canonical_parameter_set  # noqa: PLC0415

    assert canonical_parameter_set({}) == "{}"


def test_keys_given_out_of_order_produce_sorted_output() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        canonical_parameter_set,
        parameter_set_hash,
    )

    unsorted = {"zeta": 1, "alpha": 2}
    assert canonical_parameter_set(unsorted) == '{"alpha":2,"zeta":1}'
    assert parameter_set_hash(unsorted) == parameter_set_hash({"alpha": 2, "zeta": 1})


def test_nested_mapping_is_sorted_at_every_depth() -> None:
    from kbutillib.domains.identity import canonical_parameter_set  # noqa: PLC0415

    params = {"outer": {"inner_b": "y", "inner_a": "x"}, "top": 3}
    assert (
        canonical_parameter_set(params)
        == '{"outer":{"inner_a":"x","inner_b":"y"},"top":3}'
    )


def test_list_of_strings_keeps_its_order() -> None:
    """List order is part of the identity of a parameter set, so it survives."""
    from kbutillib.domains.identity import (  # noqa: PLC0415
        canonical_parameter_set,
        parameter_set_hash,
    )

    params = {"opts": ["gamma", "alpha", "beta"]}
    assert canonical_parameter_set(params) == '{"opts":["gamma","alpha","beta"]}'
    assert parameter_set_hash(params) != parameter_set_hash(
        {"opts": ["alpha", "beta", "gamma"]}
    )


def test_int_at_exactly_max_safe_magnitude_is_accepted() -> None:
    from kbutillib.domains.identity import canonical_parameter_set  # noqa: PLC0415

    assert canonical_parameter_set({"n": 2**53 - 1}) == '{"n":9007199254740991}'


def test_negative_int_at_exactly_max_safe_magnitude_is_accepted() -> None:
    from kbutillib.domains.identity import canonical_parameter_set  # noqa: PLC0415

    assert canonical_parameter_set({"n": -(2**53 - 1)}) == '{"n":-9007199254740991}'


def test_non_ascii_string_value_is_emitted_literally_not_escaped() -> None:
    from kbutillib.domains.identity import canonical_parameter_set  # noqa: PLC0415

    canonical = canonical_parameter_set({"label": "β-lactamase"})
    assert canonical == '{"label":"β-lactamase"}'
    assert "\\u" not in canonical


def test_newline_and_tab_in_a_string_are_json_escaped() -> None:
    """Pins JSON escaping: a literal newline becomes the two characters \\n."""
    from kbutillib.domains.identity import canonical_parameter_set  # noqa: PLC0415

    canonical = canonical_parameter_set({"note": "line\nsep\tend"})
    assert canonical == '{"note":"line\\nsep\\tend"}'
    assert "\n" not in canonical
    assert "\t" not in canonical


def test_transyt_taxonomy_id_fixed_key_vector() -> None:
    """The one fixed parameter key: TRANSYT's required taxonomy_id."""
    from kbutillib.domains.identity import (  # noqa: PLC0415
        DEFAULT_PARAMETER_SET_HASH,
        canonical_parameter_set,
        parameter_set_hash,
    )

    params = {"taxonomy_id": "562"}
    assert canonical_parameter_set(params) == '{"taxonomy_id":"562"}'
    assert parameter_set_hash(params) != DEFAULT_PARAMETER_SET_HASH


# ---------------------------------------------------------------------------
# bool is a bool, never an int
# ---------------------------------------------------------------------------


def test_true_is_canonicalised_as_bool_true() -> None:
    from kbutillib.domains.identity import canonical_parameter_set  # noqa: PLC0415

    assert canonical_parameter_set({"a": True}) == '{"a":true}'


def test_int_one_is_canonicalised_as_int_one() -> None:
    from kbutillib.domains.identity import canonical_parameter_set  # noqa: PLC0415

    assert canonical_parameter_set({"a": 1}) == '{"a":1}'


def test_bool_true_and_int_one_produce_different_hashes() -> None:
    from kbutillib.domains.identity import parameter_set_hash  # noqa: PLC0415

    assert parameter_set_hash({"a": True}) != parameter_set_hash({"a": 1})


# ---------------------------------------------------------------------------
# Rejections — each names the path to the offending value
# ---------------------------------------------------------------------------


def test_float_is_rejected_and_message_names_path_and_the_remedy() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError) as excinfo:
        canonical_parameter_set({"threshold": 1e-5})

    message = str(excinfo.value)
    assert "params['threshold']" in message
    assert "float not allowed" in message
    assert "pass decimals as strings" in message


def test_none_is_rejected_and_message_names_path() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError, match=r"params\['opt'\]"):
        canonical_parameter_set({"opt": None})


def test_uppercase_key_is_rejected_and_message_names_path() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError) as excinfo:
        canonical_parameter_set({"Threshold": "1"})

    message = str(excinfo.value)
    assert "params['Threshold']" in message
    assert "^[a-z][a-z0-9_]*$" in message


def test_key_starting_with_a_digit_is_rejected_and_message_names_path() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError, match=r"params\['1st'\]"):
        canonical_parameter_set({"1st": "x"})


def test_nested_key_violation_names_the_full_path() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError, match=r"params\['outer'\]\['Inner'\]"):
        canonical_parameter_set({"outer": {"Inner": "x"}})


def test_int_at_two_to_the_fiftythree_is_rejected() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError) as excinfo:
        canonical_parameter_set({"n": 2**53})

    message = str(excinfo.value)
    assert "params['n']" in message
    assert "2**53 - 1" in message


def test_float_nested_inside_a_list_inside_a_mapping_names_the_index() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError) as excinfo:
        canonical_parameter_set({"opts": ["a", "b", 2.5]})

    message = str(excinfo.value)
    assert "params['opts'][2]" in message
    assert "float not allowed" in message


def test_lone_surrogate_string_is_rejected_and_message_names_path() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError) as excinfo:
        canonical_parameter_set({"label": "bad\ud800end"})

    message = str(excinfo.value)
    assert "params['label']" in message
    assert "surrogate" in message


def test_non_mapping_parameter_set_is_rejected() -> None:
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        canonical_parameter_set,
    )

    with pytest.raises(ParameterSetError, match="params: mapping required"):
        canonical_parameter_set([1, 2])


def test_parameter_set_hash_validates_too_not_just_the_canonicaliser() -> None:
    """Both public entry points enforce the rule, not only the text one."""
    from kbutillib.domains.identity import (  # noqa: PLC0415
        ParameterSetError,
        parameter_set_hash,
    )

    with pytest.raises(ParameterSetError, match=r"params\['threshold'\]"):
        parameter_set_hash({"threshold": 1e-5})


# ---------------------------------------------------------------------------
# Reuse of the standardizers primitives (no re-implementation)
# ---------------------------------------------------------------------------


def test_canonical_parameter_set_agrees_with_canonical_payload() -> None:
    """canonical_parameter_set is canonical_payload, decoded, after validation."""
    from kbutillib.domains.identity import (  # noqa: PLC0415
        canonical_parameter_set,
        canonical_payload,
    )

    params = {"mode": "strict", "opts": ["b", "a"], "depth": {"max_hops": 3}}
    assert canonical_parameter_set(params) == canonical_payload(params).decode("utf-8")


def test_parameter_set_hash_agrees_with_content_hash() -> None:
    """parameter_set_hash is content_hash after validation."""
    from kbutillib.domains.identity import (  # noqa: PLC0415
        content_hash,
        parameter_set_hash,
    )

    params = {"mode": "strict", "opts": ["b", "a"]}
    assert parameter_set_hash(params) == content_hash(params)


# ---------------------------------------------------------------------------
# The documented rule is actually documented
# ---------------------------------------------------------------------------


def test_module_docstring_bolds_the_resource_parameter_prohibition() -> None:
    from kbutillib.domains.identity import parameter_sets  # noqa: PLC0415

    docstring = " ".join((parameter_sets.__doc__ or "").split())
    assert (
        "**Resource parameters (threads, memory, paths, batch sizes, hostnames) "
        "must never appear in a parameter set.**" in docstring
    )


def test_module_docstring_carries_the_transyt_fixed_key_row() -> None:
    from kbutillib.domains.identity import parameter_sets  # noqa: PLC0415

    docstring = parameter_sets.__doc__ or ""
    assert "Fixed parameter keys" in docstring
    assert "TRANSYT" in docstring
    assert "taxonomy_id" in docstring
    assert "NCBI taxonomy id" in docstring
