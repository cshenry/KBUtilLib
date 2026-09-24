"""CAC conformance helpers (Responsibility 3).

Two small, domain-agnostic helpers that both consumer apps rely on to
interoperate over the Cross-App Communication (CAC) contract:

  * :func:`module_id` derives the underscore form of an APP ID from its
    canonical hyphen form, mechanically, as ``id.replace("-", "_")`` — never a
    lookup table. The hyphen form is the canonical id (simultaneously the
    distribution name, the console command, the manifest basename and the
    koros ``--project`` token, CAC I4); the underscore form is what Python
    modules and SQL identifiers need. The derivation is one-way and computed,
    so a new app never needs a code change here.

  * :func:`check_contract_version` gates on the CAC ``contract_version``, which
    is an INTEGER (currently 1), NOT a semver string. FJ emits ``int 0`` and
    genKnown emits ``"0"`` and had to change to an int to interoperate — a
    string wire field would break the very interop this module exists to
    provide. The gate compares integers only: equal proceeds; ANY difference
    hard-fails with a :class:`ContractVersionMismatch` naming BOTH versions.

There is deliberately NO minor/warn branch. Section F of the CAC describes
semver-style hard-gate-on-major / soft-warn-on-minor, but leaves the mechanics
an explicit unfilled placeholder, and with an integer wire field there is no
minor component to compare against. Implementing a soft-warn branch would mean
inventing a representation upstream has explicitly not chosen, so this build
implements only the half that is implementable: integer equality.

WHAT ID NORMALISATION APPLIES TO: APP IDS ONLY. It is NEVER applied to a
``kind`` (dotted namespaced strings that hyphen/underscore rewriting would
mutate), to ``subject``, or to ``record_id`` (a hash whose identity depends on
the exact kind and subject that went into it). Normalising any of those would
silently fork the identity space. The entry points are exactly where an app id
becomes a Python module name or a SQL identifier — not manifest filenames, not
CLI tokens, not any record field.
"""

from __future__ import annotations

from .exceptions import ContractVersionMismatch

# The CAC contract version this code targets. An INTEGER, not a semver string
# (FJ emits int 0; genKnown had to move from "0" to int 0 to interoperate).
CONTRACT_VERSION = 1


def module_id(app_id: str) -> str:
    """Derive the underscore form of an APP ID from its canonical hyphen form.

    The canonical id is the hyphen form (distribution name, console command,
    manifest basename, koros ``--project`` token — CAC I4). The underscore form
    is what Python modules and SQL identifiers require, and it is derived
    MECHANICALLY as ``app_id.replace("-", "_")`` — never a lookup table, so a
    new app needs no change here.

    This applies to APP IDS ONLY. Do NOT pass a ``kind`` (dotted namespaced
    strings this would mutate), a ``subject``, or a ``record_id`` (a hash whose
    identity depends on its exact inputs). Normalising any of those would fork
    the identity space.
    """
    return app_id.replace("-", "_")


def check_contract_version(found: int, expected: int = CONTRACT_VERSION) -> None:
    """Gate on the CAC contract version — integer comparison ONLY (S15/S20).

    ``contract_version`` is an INTEGER. Equal versions proceed (return
    ``None``); ANY difference hard-fails by raising
    :class:`ContractVersionMismatch`, whose message names BOTH the expected and
    the found version so a consumer can catch it by name and report the gap.

    There is deliberately no minor/warn branch: with an integer wire field
    there is nothing to compare a minor against, and inventing one would mean
    representing a version upstream has explicitly not chosen. This is the WRITE
    gate applied at :meth:`KorosArcStore.record_analysis`; an app that wants to
    gate its own startup calls this helper directly — that is the consumer's
    decision, not this module's.
    """
    if found != expected:
        raise ContractVersionMismatch(expected=expected, found=found)
