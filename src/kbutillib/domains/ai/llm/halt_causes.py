"""The closed vocabulary of CAUSE CODES for an endpoint halt.

A halt record already says WHERE it happened (``endpoint``), WHAT taxonomy
bucket it fell in (``halt_kind``) and what the provider said
(``halt_message``).  None of those answer the only question an automatic
resume has: *may this be retried at all?*  Both of the real council halts on
record (2026-10-30 and 2026-10-31) were ``halt_kind='auth'``; one cleared
itself in four hours and the other was a permanent administrative state, so
``halt_kind`` cannot tell them apart and the message is prose nothing may
parse.  This module is the third field: a closed code whose membership in
:data:`RESUMABLE_HALT_CAUSES` is the whole decision.

The precedent is RFC 6749 section 5.2, which splits credential failures the
same way: ``invalid_token`` means refresh and retry once, ``invalid_grant``
means never retry.  A code is in one set or the other, and a caller needs no
knowledge of the provider to act on it.

WHY THIS LIVES IN THE LLM LAYER, not the council's.  Classification needs the
HTTP response -- status, the provider's structured error object -- and that is
only in hand here, where the ``LLMError`` is constructed.  This layer is
domain-neutral and must not import the council (see :mod:`..llm.types`), so
the vocabulary is defined here and re-exported by
``council.halt_causes``, which is the module council-side callers and the
campaign watchdog read it from.

TWO RULES govern every classification:

* **Structured signals only.**  HTTP status, the provider's JSON
  ``error.type`` / ``error.code`` / ``error.provider_specific_fields``, and
  the exception class for a network failure.  Free-text matching is reserved
  for the detectors that already exist (ARGO's HTTP-200 guard-rail table in
  :data:`..apis.http.ARGO_REFUSAL_SIGNATURES`, and Anthropic's
  operator-spend-limit prefix); those pass their OWN outcome in rather than
  having their prose re-matched here.
* **Never guess a resumable cause.**  An unrecognised failure gets ``None``,
  which a watchdog treats as not resumable.  The cost of ``None`` is a human
  clearing a halt that would have cleared itself; the cost of a wrong
  ``token_expired`` is an automatic resume looping against a dead credential.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from .types import LLMError

__all__ = [
    "ADMINISTRATIVELY_DISABLED",
    "CREDENTIALS_REVOKED",
    "HALT_CAUSES",
    "INSUFFICIENT_SCOPE",
    "NETWORK_ERROR",
    "QUOTA_EXHAUSTED",
    "RESUMABLE_HALT_CAUSES",
    "TOKEN_EXPIRED",
    "UNCLASSIFIED_AUTH_DENIAL",
    "UPSTREAM_5XX",
    "classify_http_halt_cause",
    "with_halt_cause",
]

# -- the vocabulary ---------------------------------------------------------

#: The credential was good and is now past its lifetime; a refresh fixes it.
TOKEN_EXPIRED = "token_expired"

#: An auth denial whose reason the provider did not state in a structured
#: field.  Resumable because the ONE real instance of it -- ARGO's HTTP-200
#: authentication notice on 2026-10-30 -- cleared itself in 4h10m with no
#: operator action, and because an unstated reason is not evidence of a
#: terminal state.
UNCLASSIFIED_AUTH_DENIAL = "unclassified_auth_denial"

#: The gateway or the model behind it failed, not the credential.
UPSTREAM_5XX = "upstream_5xx"

#: The request never reached the provider: connection refused, DNS, timeout.
NETWORK_ERROR = "network_error"

#: The credential was withdrawn.  A refresh cannot help; only a new one can.
CREDENTIALS_REVOKED = "credentials_revoked"

#: The credential is valid but is not permitted to do this.
INSUFFICIENT_SCOPE = "insufficient_scope"

#: The account's quota or billing allowance is spent.
QUOTA_EXHAUSTED = "quota_exhausted"

#: An administrator turned the project or account off.  The 2026-10-31 ``mag``
#: halt: an AmSC project disabled in the VO, which no retry and no credit
#: refresh affects.
ADMINISTRATIVELY_DISABLED = "administratively_disabled"

#: Every cause code a halt record may carry, in no significant order.  A
#: value outside this tuple is a bug, not an extension point: the whole value
#: of the field is that a reader needs no provider knowledge to act on it.
HALT_CAUSES: Tuple[str, ...] = (
    TOKEN_EXPIRED,
    UNCLASSIFIED_AUTH_DENIAL,
    UPSTREAM_5XX,
    NETWORK_ERROR,
    CREDENTIALS_REVOKED,
    INSUFFICIENT_SCOPE,
    QUOTA_EXHAUSTED,
    ADMINISTRATIVELY_DISABLED,
)

#: The causes an automatic resume may act on.  Everything else in
#: :data:`HALT_CAUSES` is TERMINAL and needs a human, and so does a halt
#: carrying no cause at all.
RESUMABLE_HALT_CAUSES: Tuple[str, ...] = (
    TOKEN_EXPIRED,
    UNCLASSIFIED_AUTH_DENIAL,
    UPSTREAM_5XX,
    NETWORK_ERROR,
)


# -- the structured signals -------------------------------------------------
#
# Each set holds values a provider puts in ``error.type``, ``error.code`` or a
# recognised ``provider_specific_fields`` entry.  Matched case-insensitively
# and by EQUALITY, never as a substring: "expired" as a substring would match
# an ``invalid_request`` message mentioning an expired project.

#: AmSC's administrative-state signals.  ``amsc_project_disabled`` is the
#: ``error.type`` the Genesis Model Access Gateway sent on 2026-10-31;
#: ``project_disabled`` is the ``provider_specific_fields.amsc_reason`` from
#: the same body.  Either alone is enough.
ADMINISTRATIVE_SIGNALS = frozenset({"amsc_project_disabled", "project_disabled"})

#: RFC 6749 ``invalid_grant`` and the revocation vocabularies around it.
REVOKED_SIGNALS = frozenset(
    {
        "invalid_grant",
        "revoked",
        "token_revoked",
        "credentials_revoked",
        "key_revoked",
        "access_revoked",
    }
)

#: RFC 6750 ``insufficient_scope``.
SCOPE_SIGNALS = frozenset({"insufficient_scope"})

#: Spend that is GONE, as opposed to a windowed rate limit (which is a 429 and
#: is retried, never a halt cause -- see :func:`classify_http_halt_cause`).
QUOTA_SIGNALS = frozenset(
    {
        "insufficient_quota",
        "quota_exceeded",
        "quota_exhausted",
        "billing_hard_limit_reached",
        "credit_balance_too_low",
        "enforced_spend_limit_reached",
    }
)

#: RFC 6750 ``invalid_token`` -- "refresh and retry once" -- and the explicit
#: expiry vocabularies.
EXPIRED_SIGNALS = frozenset(
    {
        "invalid_token",
        "expired",
        "expired_token",
        "token_expired",
        "expired_access_token",
    }
)

#: ``provider_specific_fields`` keys whose VALUE is a reason code rather than
#: free text.  Only these are read; anything else in that object is prose or
#: context (the AmSC body also carries ``amsc_project``, a project NAME) and
#: matching on it would be the free-text rule this module forbids.
REASON_FIELDS = ("amsc_reason", "reason", "reason_code")


def _error_object(body: Any) -> Mapping[str, Any]:
    """Return the provider's error object out of a parsed response body.

    Both shapes in use here are handled: OpenAI-compatible servers (ARGO, the
    Model Access Gateway, vLLM) nest it under ``"error"``, while the Anthropic
    SDK hands ``APIStatusError.body`` over already unwrapped in some versions.
    Anything that is not a mapping yields ``{}`` -- a body that could not be
    parsed contributes no signal, which is not the same as contributing a
    denial.
    """
    if not isinstance(body, Mapping):
        return {}
    nested = body.get("error")
    if isinstance(nested, Mapping):
        return nested
    return body


def _signals(error: Mapping[str, Any]) -> frozenset:
    """Every structured signal value *error* offers, lowercased.

    ``type`` and ``code`` are the two fields every provider in use puts a
    machine-readable reason in; the recognised :data:`REASON_FIELDS` of
    ``provider_specific_fields`` are the third.  A numeric ``code`` (the AmSC
    gateway sends ``"403"``) is harmless: it matches nothing.
    """
    found = set()
    for key in ("type", "code"):
        value = error.get(key)
        if isinstance(value, (str, int)):
            found.add(str(value).strip().lower())
    extra = error.get("provider_specific_fields")
    if isinstance(extra, Mapping):
        for key in REASON_FIELDS:
            value = extra.get(key)
            if isinstance(value, str):
                found.add(value.strip().lower())
    return frozenset(found - {""})


def classify_http_halt_cause(status: int, body: Any = None) -> Optional[str]:
    """Return the halt cause for an HTTP failure, or ``None`` if unknown.

    Args:
        status: the HTTP status code of the response.
        body: the PARSED response body, if it could be parsed.  ``None`` or
            any non-mapping contributes no signal.

    Returns:
        A member of :data:`HALT_CAUSES`, or ``None``.

    Order is the contract, most specific signal first.  The terminal signals
    are tested before the status rules, because an administratively disabled
    project and an expired token arrive with the SAME 403/401 and the
    difference is only in the error object.  A 429 is deliberately absent from
    the status rules: a windowed rate limit is retried rather than halted (see
    ``engine.worker._note_rate_limit``, which halts only on a SUSTAINED run of
    them, and does so with no cause -- a windowed limit is neither resumable
    on the next tick nor permanently terminal, so neither set is honest about
    it).
    """
    error = _error_object(body)
    signals = _signals(error)

    if signals & ADMINISTRATIVE_SIGNALS:
        return ADMINISTRATIVELY_DISABLED
    if signals & REVOKED_SIGNALS:
        return CREDENTIALS_REVOKED
    if signals & SCOPE_SIGNALS:
        return INSUFFICIENT_SCOPE
    if signals & QUOTA_SIGNALS:
        return QUOTA_EXHAUSTED
    if int(status) == 401 and signals & EXPIRED_SIGNALS:
        return TOKEN_EXPIRED
    if int(status) in (401, 403):
        return UNCLASSIFIED_AUTH_DENIAL
    if int(status) >= 500:
        return UPSTREAM_5XX
    return None


def with_halt_cause(error: LLMError, cause: Optional[str]) -> LLMError:
    """Return *error* carrying *cause*, or unchanged if *cause* is ``None``.

    A copy rather than a mutation, following the ``rate_limited`` flag it sits
    beside: an :class:`~.types.LLMError` is a record that gets persisted, and
    every construction site in this layer builds one and returns it in the
    same expression.
    """
    if cause is None:
        return error
    return error.model_copy(update={"halt_cause": cause})
