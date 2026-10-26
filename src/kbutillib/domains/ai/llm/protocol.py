"""The structural interface one way of reaching models must satisfy.

An *API type* is anything with the right shape: ARGO, an OpenAI-compatible
server, a headless CLI, a test fake, or a plug-in declared by another
distribution through the ``kbutillib.llm_api_types`` entry-point group.  The
wrapper never imports a concrete class, which is what lets a plug-in be added
without touching this package.

Two members are REQUIRED and the registry refuses anything missing either:

* :meth:`ApiType.complete` -- run one call, and never raise for a call
  failure.
* :meth:`ApiType.effective_params` -- what generation parameters the provider
  will actually apply.  Required because callers content-address their calls
  on it: ARGO forces ``temperature: 1`` on some models whatever was asked
  for, and a cache keyed on the REQUESTED parameters would hand one answer to
  two materially different questions.

Four more are OPTIONAL and are therefore not part of the runtime-checkable
protocol -- a fake with no CLI to interrogate is still a valid API type:

* ``async preflight() -> dict`` -- "would this endpoint even work, and how
  would it bill?", answered offline and for free.  An API type without one is
  reported ``{"state": "ready"}``.
* ``async models() -> list[dict]`` -- free enumeration.  ``[]`` means "cannot
  enumerate", which is not an error.
* ``async aclose() -> None`` -- release any client or process resources.
* ``supports_web_search: bool`` -- a CLASS ATTRIBUTE, not a method:
  "can this API type run a call with live web search?"  **Default False**,
  read through ``getattr(api, "supports_web_search", False)`` by
  :func:`kbutillib.domains.ai.llm.supports_web_search`, so a third-party API
  type written before this attribute existed still registers, still passes
  ``isinstance(api, ApiType)``, and simply reports ``False``.

  It is deliberately NOT in :data:`REQUIRED_MEMBERS` and deliberately NOT a
  member of :class:`ApiType` below.  Adding it to either would make every
  pre-existing third-party type fail -- registration refuses anything
  missing a required member, and ``runtime_checkable`` checks that a data
  member EXISTS -- and "I cannot search the web" is a safe default to assume
  about a plug-in, not a defect to refuse it for.

  The opt-in on the other side is ``LLMRequest.params["web_search"] = True``
  (:data:`~.types.WEB_SEARCH_PARAM`).  An API type that reports ``False``
  must refuse such a request with ``ok=False`` and
  ``kind="invalid_output"`` WITHOUT calling the provider, rather than
  silently answering from training data: a caller that asked for the live
  web and was quietly served a guess has no way to tell.

``runtime_checkable`` only checks that members EXIST -- it does not check
signatures -- so an ``isinstance`` pass is a sanity check, not a contract
proof.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable

from .types import LLMRequest, LLMResult

__all__ = ["REQUIRED_MEMBERS", "ApiType", "missing_members"]

#: The members :func:`kbutillib.domains.ai.llm.register_api_type` insists a
#: factory's product carries.  Named here rather than spelled twice so the
#: registry's refusal message and this protocol cannot drift apart.
REQUIRED_MEMBERS = ("complete", "effective_params")


@runtime_checkable
class ApiType(Protocol):
    """One way of reaching models.

    The hard rule is in :meth:`complete`: an API type MUST NOT raise for a
    call failure.  A caller running N models per item would otherwise lose
    the whole item to one bad model, so every failure comes back as an
    :class:`~.types.LLMResult` with ``ok=False`` and ``error`` set.
    """

    kind: str

    async def complete(self, request: LLMRequest) -> LLMResult:
        """Run one invocation.

        Must not raise for a call failure -- returns ``ok=False`` with
        ``error`` set instead.
        """
        ...

    def effective_params(self, request: LLMRequest) -> Dict[str, Any]:
        """The generation parameters this API type would really send.

        Deterministic, and generation-only: no header, no credential, no
        proxy, nothing about how the request is routed.  Those belong to the
        caller's own route accounting.
        """
        ...


def missing_members(api: Any) -> List[str]:
    """Return the :data:`REQUIRED_MEMBERS` *api* does not supply, callably."""
    return [name for name in REQUIRED_MEMBERS if not callable(getattr(api, name, None))]
