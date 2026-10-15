"""Client for Filipe Liu's deployed GO council service on poplar.

The service is ``mfgopredict-service`` 0.1.0, a FastAPI app on
``poplar.cels.anl.gov:8300`` written and operated by **Filipe Liu**.  It maps
free-text protein function labels to GO molecular-function terms: *mfgopredict*
builds a closed menu of candidate GO terms for each annotation string, and the
choice among them is made by **Jose Faria's CONCORDIA runner**
(``scripts/go_council_pilot.py``), dynamically imported by the service -- the
same proposers/critic/meta protocol this package ports, but GO-only and running
on Filipe's own credentials and GPUs.

This client is **optional and comparison-only**.  It exists so a KBUtilLib user
can ask the deployed service the same question the local council was asked and
line the two answers up.  It therefore:

* makes **one request per call and never polls** -- the service completes the
  whole council in-request (10-30 s per council row), so the only knob is the
  900 s default timeout;
* returns **plain dicts**, exactly as the service sent them.  It never
  constructs a :class:`~kbutillib.domains.ai.council.types.CouncilVerdict` and
  nothing it returns is written to a store.  :meth:`
  ConcordiaServiceClient.row_to_comparison` lifts the four semantic fields a
  comparison needs out of a service row and keeps the row itself alongside;
* never logs, stores or quotes the API key.  The key is read per request and
  every message built from a response body goes through
  :func:`~kbutillib.domains.ai.council.backends.http_common.redact`.

The request bodies built here are derived from the service's **live** OpenAPI
document, captured as ``tests/domains/ai/council/fixtures/concordia_openapi.json``
(see its ``.meta.json`` sidecar for the capture time).  The deployed schema has
diverged from the readable source checkout before, so the capture -- not the
source -- is what the tests validate against.

Auth: only ``POST /concordia_go_council`` needs a credential (it spends LLM API
money), as the ``X-API-Key`` header.  Every other endpoint, including the menu
builder, is open.

Example::

    from kbutillib.domains.ai.council import ConcordiaServiceClient

    client = ConcordiaServiceClient()           # COUNCIL_API_KEY from the env
    if client.status()["available"]:
        reply = client.council([{"id": "r1", "text": "Glycerate kinase (EC 2.7.1.31)"}])
        for row in reply["items"]:
            print(ConcordiaServiceClient.row_to_comparison(row)["selected_ids"])
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx

from .backends.http_common import normalise_base_url, redact
from .types import CouncilException

__all__ = [
    "COUNCIL_MAX_ITEMS",
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "ConcordiaServiceClient",
    "CouncilServiceAuthError",
    "CouncilServiceError",
    "CouncilServiceUnavailable",
]

#: Where the service runs.  Plain HTTP on the lab network; no proxy involved.
DEFAULT_BASE_URL = "http://poplar.cels.anl.gov:8300"

#: Environment variable holding the council API key, when one is not passed in.
DEFAULT_API_KEY_ENV = "COUNCIL_API_KEY"

#: One request runs the whole council, so the timeout covers every seat of
#: every item.  20 council rows at ~30 s each is the worst case the service
#: itself allows.
DEFAULT_TIMEOUT_S = 900.0

#: ``CouncilRequest`` in the captured OpenAPI document: "The annotations to
#: judge, at most 20".  The cap is a ``model_validator`` on the service side,
#: so it is *not* in the JSON Schema and a long request is rejected with a 422
#: after the whole body has been sent -- refused here instead.
COUNCIL_MAX_ITEMS = 20


class CouncilServiceError(CouncilException):
    """The GO council service refused or could not serve a request."""


class CouncilServiceAuthError(CouncilServiceError):
    """No usable API key, or the service rejected the one we sent (HTTP 401)."""


class CouncilServiceUnavailable(CouncilServiceError):
    """The service cannot run the council right now (HTTP 503).

    Per the endpoint's own documentation this means the server has no council
    API key configured, has no CONCORDIA checkout, or has no credential for one
    of the seats -- ``GET /concordia_go_council`` says which.  It is a property
    of the deployment, not of the request.
    """


class ConcordiaServiceClient:
    """Synchronous client for the five endpoints a comparison needs.

    Args:
        base_url: Service root.  Defaults to :data:`DEFAULT_BASE_URL`.
        api_key: The council API key, used verbatim.  Normally left unset so
            the key comes from the environment instead.
        api_key_env: Environment variable consulted when *api_key* is unset.
            Defaults to :data:`DEFAULT_API_KEY_ENV`.
        timeout: Seconds for the whole request/response cycle.  The council
            runs in-request, so this is the budget for every model call the
            service makes on our behalf.
        transport: TESTS ONLY.  An :class:`httpx.BaseTransport` -- in practice
            :class:`httpx.MockTransport` -- handed straight to the client, so a
            test drives the real request path without a socket.  Left unset in
            production, which is what :attr:`client_kwargs` lets a test assert.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: Optional[str] = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.base_url = normalise_base_url(base_url) or DEFAULT_BASE_URL
        self.api_key_env = api_key_env
        self.timeout = float(timeout)
        self._api_key = str(api_key) if api_key else None
        self.client_kwargs: Dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if transport is not None:
            self.client_kwargs["transport"] = transport
        self.client = httpx.Client(**self.client_kwargs)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection pool."""
        self.client.close()

    def __enter__(self) -> "ConcordiaServiceClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- credentials -------------------------------------------------------

    def _key(self) -> str:
        """The API key for ``POST /concordia_go_council``, or refuse.

        Resolved per request and never stored anywhere but the constructor
        argument: the key is not logged, not put in an error message and not
        kept on the client's headers (where a repr would show it).
        """
        if self._api_key:
            return self._api_key
        from_env = (os.environ.get(self.api_key_env) or "").strip()
        if from_env:
            return from_env
        raise CouncilServiceAuthError(
            f"the GO council at {self.base_url} needs its API key in the "
            f"X-API-Key header, and none was given: pass api_key=... or set "
            f"${self.api_key_env}. No request was made. The key is Filipe "
            f"Liu's to give out; GET /concordia_go_council reports whether the "
            f"deployment can authorise a council call at all "
            f"(auth.key_configured)."
        )

    # -- the one response path ---------------------------------------------

    def _result(self, response: httpx.Response) -> Any:
        """Map the documented failures, then decode the JSON body.

        The three mapped statuses are the ones the endpoint documents and a
        caller must act on differently: 401 is *our* credential, 503 is the
        *deployment*, 422 is *our body*.  Anything else is left to
        :meth:`httpx.Response.raise_for_status` rather than being guessed at.
        """
        status = response.status_code
        if status == 401:
            raise CouncilServiceAuthError(
                f"the GO council at {self.base_url} rejected the API key "
                f"(HTTP 401). The key comes from "
                f"{'the api_key argument' if self._api_key else '$' + self.api_key_env}."
            )
        if status == 503:
            raise CouncilServiceUnavailable(
                f"the GO council at {self.base_url} cannot run right now "
                f"(HTTP 503): {redact(response.text)}. GET "
                f"/concordia_go_council says which seat, key or checkout is "
                f"missing."
            )
        if status == 422:
            raise ValueError(
                f"the GO council at {self.base_url} rejected the request body "
                f"(HTTP 422): {redact(response.text)}"
            )
        response.raise_for_status()
        return response.json()

    # -- open endpoints ----------------------------------------------------

    def health(self) -> Any:
        """``GET /health``: liveness, plus ``ko_loaded`` / ``ko_terms``."""
        return self._result(self.client.get("/health"))

    def models(self) -> Any:
        """``GET /models``: the model aliases a council seat can take.

        Each entry carries ``alias`` (what goes in ``roles``), ``endpoint``,
        ``status`` and ``online``, so this is how to find out whether a seat's
        model is actually loaded before asking for a council.
        """
        return self._result(self.client.get("/models"))

    def status(self) -> Any:
        """``GET /concordia_go_council``: can the council run here, and how.

        Returns ``available`` / ``reason``, the ``default_roster``, the named
        ``profiles`` a request may pick, ``missing_credentials`` for any seat
        without a key, and ``auth.key_configured`` -- present/absent flags
        only, never a credential value.
        """
        return self._result(self.client.get("/concordia_go_council"))

    def menu(self, items: Sequence[Mapping[str, Any]], **params: Any) -> Any:
        """``POST /concordia/go/menu``: the closed menu, with no model call.

        This is mfgopredict's side of the council -- triage, guard, candidate
        menu and the deterministic answer -- so it spends no tokens and needs
        **no API key**; no auth header is sent.  Useful on its own to compare
        the deployed menu builder against a local query type's candidates.

        Args:
            items: ``[{"id", "text", "source"}]``; only ``text`` is required,
                and ``gene_id`` is also accepted.  The service caps this
                endpoint at 1000 items.
            **params: Menu settings from the captured ``MenuRequest`` schema:
                ``retrieval_k``, ``cap``, ``max_height``, ``pool_by_gene``,
                ``use_layer1``, ``use_gpu``, ``gpu_index``, ``model``.  Unset
                ones keep the service's defaults.
        """
        body = _without_nones({"items": list(items), **params})
        return self._result(self.client.post("/concordia/go/menu", json=body))

    # -- the council (the only endpoint that spends money) -----------------

    def council(
        self,
        items: Sequence[Mapping[str, Any]],
        profile: Optional[str] = None,
        roles: Optional[Mapping[str, Any]] = None,
        force_council: bool = False,
        use_layer1: bool = False,
        **extra: Any,
    ) -> Any:
        """``POST /concordia_go_council``: menu plus the full GO council.

        One request, no polling: the service runs every seat of every item
        before it replies.  The reply is a ``CouncilResponse`` dict --
        ``roster``, ``concordia_commit``, ``counts``, ``usage`` and one
        ``items`` row per input item in input order.  Pass each row through
        :meth:`row_to_comparison` to read the answer.

        Args:
            items: ``[{"id", "text", "source"}]``, at most
                :data:`COUNCIL_MAX_ITEMS`; only ``text`` is required, and
                ``gene_id`` and a caller-supplied ``candidates`` menu are also
                accepted.
            profile: A named roster from the deployment's council config;
                :meth:`status` lists them.  Unset = its default profile.
            roles: Single-seat overrides on top of the profile, e.g.
                ``{"critic": "claudeopus45"}``.
            force_council: Send rows mfgopredict would answer alone (an exact
                EC or name match) to the models anyway.
            use_layer1: Add Layer-1 embedding retrieval to the menu.  Defaults
                to ``False`` here -- EC and name matches only, no embedding
                model and no GPU on Filipe's machine -- which is **not** the
                service's own default of ``True``.
            **extra: Any other field of the captured ``CouncilRequest``
                schema: ``retrieval_k``, ``cap``, ``max_height``,
                ``pool_by_gene``, ``use_gpu``, ``gpu_index``, ``model``.

        Raises:
            ValueError: More than :data:`COUNCIL_MAX_ITEMS` items.  The service
                enforces the same cap in a validator, but only after the whole
                body has been sent.
            CouncilServiceAuthError: No API key is available (raised before any
                request is made), or the service rejected the one we sent.
            CouncilServiceUnavailable: The deployment cannot run the council.
        """
        items = list(items)
        if len(items) > COUNCIL_MAX_ITEMS:
            raise ValueError(
                f"the GO council takes at most {COUNCIL_MAX_ITEMS} items per "
                f"request and {len(items)} were given; split the batch. (Its "
                f"menu endpoint takes 1000, and a local council job has no "
                f"such cap.)"
            )
        key = self._key()  # before the body, so a missing key sends nothing
        body = _without_nones(
            {
                "items": items,
                "profile": profile,
                "roles": dict(roles) if roles is not None else None,
                "force_council": force_council,
                "use_layer1": use_layer1,
                **extra,
            }
        )
        response = self.client.post(
            "/concordia_go_council", json=body, headers={"X-API-Key": key}
        )
        return self._result(response)

    # -- comparison --------------------------------------------------------

    @staticmethod
    def row_to_comparison(row: Mapping[str, Any]) -> Dict[str, Any]:
        """Lift the comparable fields out of one service row.

        The service's ``final_go_ids`` is a list of ``{go_id, name}`` objects,
        while a local verdict's ``selected_ids`` is a list of ids; this is the
        one conversion a side-by-side needs.  ``status``,
        ``decline_reason`` and ``final_confidence`` already carry the same
        meaning in both, and the untouched row is kept under ``service_row``
        so nothing the service said is lost.

        This is deliberately **not** a verdict: no
        :class:`~kbutillib.domains.ai.council.types.CouncilVerdict` is built,
        no roster fingerprint is invented for a roster we did not choose, and
        nothing here is ever written to a store.
        """
        selected: List[str] = [
            term["go_id"] for term in (row.get("final_go_ids") or [])
        ]
        return {
            "selected_ids": selected,
            "status": row.get("status"),
            "decline_reason": row.get("decline_reason"),
            "final_confidence": row.get("final_confidence"),
            "service_row": dict(row),
        }


def _without_nones(body: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop every ``None`` so the service's own default applies instead.

    Sending ``"profile": null`` is accepted by the schema but means "no
    profile"; dropping the key is what "unset" means, and it keeps the body
    the client builds readable in a 422 message.
    """
    return {key: value for key, value in body.items() if value is not None}
