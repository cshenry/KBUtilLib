"""RAST genome annotation via the token-free tutorial JSON-RPC service.

Implements :class:`RastUtils`, an :class:`~kbutillib.domains.genome.annotation
.annotator_utils.AnnotatorUtils` sibling of ``ProkkaUtils``/``TransytUtils``
that wraps ``modelseedpy.core.rast_client.RastClient`` to annotate a
caller-supplied proteome using the SEED "kmer_v2 + similarity" RAST pipeline.

PRIVACY NOTICE — read before calling ``annotate()``
----------------------------------------------------
Every call to :meth:`RastUtils.annotate` sends the caller's protein
sequences, over plain HTTPS, to a **third-party, externally hosted**
service: ``https://tutorial.theseed.org/services/genome_annotation``. This
is not a KBase-authenticated or Argonne-hosted endpoint; it is the public
SEED tutorial RAST server. Do not pass sequences derived from
NDA-covered, embargoed, or otherwise sensitive genomes through this
module.

Why this API, specifically
---------------------------
``RastClient`` builds its ``RPCClient`` with no ``token`` argument, and
``RPCClient`` only attaches an ``AUTHORIZATION`` header when a token is
given (see ``modelseedpy/core/rpcclient.py``). So this path needs **no
KBase credential** — the entire reason this API was chosen over a
KBase-authenticated annotation service. This module preserves that
property: it never passes a ``token`` to ``RastClient``/``RPCClient``,
and there is no constructor argument to add one.

Protein input, not nucleotide
------------------------------
``annotate()`` takes **amino-acid (protein)** sequences, exactly like
``TransytUtils``/``DRAM2Utils`` and unlike ``ProkkaUtils`` (which takes
nucleotide CDS sequences and calls ORFs itself). Input is validated with
the shared :func:`~kbutillib.domains.genome.annotation.annotator_utils.
_guard_protein` alphabet guard; passing nucleotide sequences raises
``ValueError`` rather than silently producing empty annotations — mixing
this up has caused a real defect in a downstream consumer before.

Multi-role splitting and the ``"RAST"`` ontology key
-----------------------------------------------------
A single RAST ``function`` string may encode multiple roles, e.g.
``"Phosphopantetheine adenylyltransferase (EC 2.7.7.3) / Dephospho-CoA
kinase (EC 2.7.1.24)"``. This module splits on the same regex
``RastClient.annotate_genome`` uses in its live code path — ``"; | / | @"``
— **not** the superset ``"; | / | @ | => "`` used by
``modelseedpy.core.rast_client.aux_rast_result``, which that module's own
source comments mark ``### delete this after ####`` (i.e. dead code
slated for removal, not the maintained path). Each split role is filed as
a :class:`~kbutillib.domains.genome.annotation.annotator_utils.Term` with
``namespace="RAST"``, mirroring ``feature.add_ontology_term("RAST", ...)``
in ``annotate_genome``.

Availability probe does not touch the network
------------------------------------------------
:meth:`is_available` only checks that ``modelseedpy`` is importable
(``importlib.util.find_spec``). It deliberately does **not** probe
``tutorial.theseed.org`` — that is a side-effecting, potentially slow (up
to the RPC timeout) call to a third-party service outside our control,
which is not what a "is this tool installed" check should cost. Network
and service failures are instead surfaced distinctly and specifically by
:meth:`annotate` itself as :class:`RastServiceError`.

Batching
--------
By default, ``annotate()`` sends the *entire* input proteome in a single
JSON-RPC call, exactly like the real-world precedent this module follows
(``cobrakbase.core.build_metabolic_model.build_metabolic_model``, which
calls ``RastClient.f(p_features)`` once over a full bacterial proteome —
typically 4,000-5,000 proteins). The default RPC timeout is 1800s
(``30*60``, matching ``RPCClient``'s own default), configurable via the
``rast.timeout`` config key. Callers with unusually large inputs, or a
tighter per-call timeout budget, can pass ``chunk_size=<n>`` to
:meth:`annotate` to split the proteome into sequential sub-calls of at
most ``n`` proteins each; results are merged. Chunking is opt-in and
explicit — this module never silently truncates or drops input proteins.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from typing import Any

from ..genome.annotation.annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    AnnotatorUtils,
    Term,
    ToolUnavailableError,
    _guard_protein,
)

__all__ = ["RastUtils", "RastUtilsImpl", "RastServiceError"]

_TOOL = "rast"
_INSTALL_HINT = "pip install modelseedpy"
_RPC_METHOD = "GenomeAnnotation.run_pipeline"

# Default RAST annotation pipeline: k-mer v2 first pass, then a
# similarity pass restricted to genes the k-mer pass left hypothetical.
# Verbatim from modelseedpy.core.rast_client.RastClient.__init__.
_STAGES: list[dict[str, Any]] = [
    {"name": "annotate_proteins_kmer_v2", "kmer_v2_parameters": {}},
    {
        "name": "annotate_proteins_similarity",
        "similarity_parameters": {"annotate_hypothetical_only": 1},
    },
]

# Multi-role delimiter used by RastClient.annotate_genome's live code path.
# (aux_rast_result's "; | / | @ | => " variant is explicitly dead code in
# modelseedpy — see module docstring.)
_ROLE_SPLIT_RE = re.compile(r"; | / | @")

# RPCClient's own default timeout (modelseedpy/core/rpcclient.py).
_DEFAULT_TIMEOUT_S = 30 * 60


class RastServiceError(Exception):
    """Raised when the RAST JSON-RPC call fails or returns a malformed result.

    Covers three distinct failure modes, each named in the message:

    * A network-level failure reaching ``tutorial.theseed.org`` (DNS,
      connection refused, timeout, TLS, ...).
    * A JSON-RPC ``ServerError`` reported by the RAST service itself.
    * A response that does not have the expected shape (e.g. an empty/
      ``None`` result, or a payload missing the ``"features"`` key) —
      this module never substitutes a well-shaped empty
      :class:`~kbutillib.domains.genome.annotation.annotator_utils.
      AnnotationResult` for a response it could not actually parse.
    """


def _split_role_terms(function: str) -> list[Term]:
    """Split a RAST multi-role ``function`` string into ``Term`` objects.

    Args:
        function: The raw ``"function"`` string RAST returned for one
            feature (may encode multiple roles).

    Returns:
        One ``Term(namespace="RAST", id=None, value=role, evidence={})``
        per non-empty role, in order. Whitespace-only roles are dropped.
    """
    terms: list[Term] = []
    for role in _ROLE_SPLIT_RE.split(function):
        role = role.strip()
        if role:
            terms.append(Term(namespace="RAST", id=None, value=role, evidence={}))
    return terms


def _chunked(items: list[tuple[str, str]], size: int) -> list[list[tuple[str, str]]]:
    """Split *items* into consecutive chunks of at most *size* elements.

    Args:
        items: List of ``(caller_id, sequence)`` pairs, in caller order.
        size: Maximum chunk length. Must be a positive integer.

    Returns:
        List of chunks; the last chunk may be shorter than *size*. Never
        drops or reorders items.
    """
    return [items[i : i + size] for i in range(0, len(items), size)]


class RastUtils(AnnotatorUtils):
    """Annotate a proteome using RAST via the token-free tutorial JSON-RPC API.

    Wraps ``modelseedpy.core.rast_client.RastClient``, which talks to
    ``https://tutorial.theseed.org/services/genome_annotation`` with no
    KBase token. See the module docstring for the privacy notice, the
    protein-vs-nucleotide distinction, and the multi-role split rule.

    Config keys (read via ``get_config_value``):
        ``rast.timeout`` — Max seconds to wait for one RPC call (default
            1800, matching ``RPCClient``'s own default).

    Example::

        from kbutillib import RastUtils

        ru = RastUtils()
        if ru.is_available():
            result = ru.annotate({"gene1": "MKTAYIAKQ...", "gene2": "MNFSTPD..."})
            for rec in result.records:
                print(rec.gene_id, [t.value for t in rec.terms])

    Raises:
        ValueError: If *proteins* is empty, contains a blank sequence, or
            contains a nucleotide-looking sequence.
        ToolUnavailableError: If ``modelseedpy`` is not importable.
        RastServiceError: If the RPC call fails or returns a malformed
            response.
    """

    _tool_name: str = _TOOL
    _install_hint: str = _INSTALL_HINT

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._timeout: int = int(
            self.get_config_value("rast.timeout", default=_DEFAULT_TIMEOUT_S)
        )

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if ``modelseedpy`` (hence ``RastClient``) is importable.

        Side-effect-free: does not import ``modelseedpy``, does not touch
        the network. See the module docstring for why this probe stops at
        "is the dependency installed" rather than "is the remote service
        reachable right now".
        """
        return importlib.util.find_spec("modelseedpy") is not None

    # ------------------------------------------------------------------
    # Public annotate method
    # ------------------------------------------------------------------

    def annotate(  # type: ignore[override]
        self,
        proteins: dict[str, str],
        chunk_size: int | None = None,
        split_terms: bool = True,
        **params: Any,
    ) -> AnnotationResult:
        """Annotate protein sequences using the RAST tutorial JSON-RPC service.

        Sends ``proteins`` to ``GenomeAnnotation.run_pipeline`` (k-mer v2 +
        similarity stages) and maps each returned ``"function"`` string
        back to the caller's original gene ids.

        Args:
            proteins: Mapping ``{caller_id: amino_acid_sequence}``. Values
                must be non-empty amino-acid (protein) sequences;
                nucleotide-looking sequences raise ``ValueError`` (RAST's
                pipeline expects protein input, unlike ``ProkkaUtils``).
            chunk_size: If given, split *proteins* into sequential RPC
                calls of at most this many proteins each and merge the
                results. ``None`` (the default) sends the whole input in
                one call, matching the real-world precedent
                (``cobrakbase.core.build_metabolic_model``). Chunking
                trades one long call for several shorter ones (more HTTP
                round trips, but each individually less likely to
                approach ``rast.timeout``); it never drops input.
            split_terms: If True (default, matching
                ``RastClient.annotate_genome``'s default), split each
                returned multi-role ``function`` string into separate
                ``Term`` objects. If False, keep the raw, unsplit
                ``function`` string as a single ``Term``.
            **params: Ignored extra keyword arguments (for API
                compatibility with the base class).

        Returns:
            An ``AnnotationResult`` with:
            - ``tool = "rast"``
            - ``tool_version = None`` / ``db_version = None`` — the
              tutorial RPC service exposes no version endpoint via
              ``GenomeAnnotation.run_pipeline``.
            - ``records`` keyed by the caller's original ids. Genes RAST
              returned with no ``"function"`` (i.e. it found nothing to
              call) are absent from ``records`` — this is RAST reporting
              "no hit", not a wrapper failure, and matches the
              zero-annotation convention used by ``ProkkaUtils``/
              ``DRAM2Utils``.

        Raises:
            ValueError: If ``proteins`` is empty, contains a blank
                sequence, or fails the protein alphabet guard.
            ToolUnavailableError: If ``modelseedpy`` is not importable.
            RastServiceError: If the RPC call fails (network error,
                ``ServerError``) or the response is malformed.
        """
        if not proteins:
            raise ValueError("proteins must not be empty")
        blank_ids = [gid for gid, seq in proteins.items() if not seq or not seq.strip()]
        if blank_ids:
            raise ValueError(
                f"proteins contains blank sequence(s) for id(s): {sorted(blank_ids)}"
            )
        if chunk_size is not None and chunk_size < 1:
            raise ValueError(f"chunk_size must be a positive integer, got {chunk_size!r}")

        # Guard before availability check so callers get meaningful errors
        # for bad inputs even when modelseedpy is absent (matches
        # TransytUtils.annotate's ordering).
        _guard_protein(proteins)
        self._require_available()

        rast_client = self._build_rast_client()

        items = list(proteins.items())
        batches = _chunked(items, chunk_size) if chunk_size else [items]

        records: list[AnnotationRecord] = []
        analysis_events: list[Any] = []
        for batch in batches:
            payload = self._call_rast(rast_client, batch)
            analysis_events.append(payload.get("analysis_events"))
            records.extend(self._records_from_payload(payload, split_terms=split_terms))

        parameters: dict[str, Any] = {
            "num_proteins": len(proteins),
            "chunk_size": chunk_size,
            "num_calls": len(batches),
            "split_terms": split_terms,
            "stages": [stage["name"] for stage in _STAGES],
            "timeout": self._timeout,
            "analysis_events": analysis_events,
        }

        return AnnotationResult(
            tool=_TOOL,
            tool_version=None,
            db_version=None,
            run_id=uuid.uuid4().hex,
            command=(
                f"RPC {_RPC_METHOD} to {rast_client.rpc_client.url} "
                f"(stages={','.join(parameters['stages'])}; "
                f"features={len(proteins)}; calls={len(batches)}; "
                f"timeout={self._timeout}s)"
            ),
            parameters=parameters,
            records=records,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_rast_client(self) -> Any:
        """Construct a token-free ``RastClient`` with the configured timeout.

        Returns:
            A ``modelseedpy.core.rast_client.RastClient`` instance. Its
            ``rpc_client.timeout`` is set from ``rast.timeout`` (the
            constructor itself takes no arguments and always uses
            ``RPCClient``'s hardcoded default).

        Raises:
            ToolUnavailableError: If ``modelseedpy`` fails to import here
                despite ``is_available()`` having returned True moments
                earlier (e.g. a partially broken install).
        """
        try:
            from modelseedpy.core.rast_client import RastClient
        except ImportError as exc:
            raise ToolUnavailableError(
                tool=self._tool_name,
                detail=f"modelseedpy import failed: {exc}",
                hint=self._install_hint,
            ) from exc

        rast_client = RastClient()
        rast_client.rpc_client.timeout = self._timeout
        return rast_client

    def _call_rast(self, rast_client: Any, batch: list[tuple[str, str]]) -> dict[str, Any]:
        """Issue one ``GenomeAnnotation.run_pipeline`` call and validate the result.

        Args:
            rast_client: A constructed ``RastClient``.
            batch: ``(caller_id, sequence)`` pairs for this call.

        Returns:
            ``result[0]`` from the RPC response — a dict expected to
            contain at least a ``"features"`` key.

        Raises:
            RastServiceError: On network failure, ``ServerError``, or a
                response that is empty/None or missing ``"features"``.
        """
        import requests
        from modelseedpy.core.rpcclient import ServerError

        p_features = [
            {"id": caller_id, "protein_translation": seq} for caller_id, seq in batch
        ]
        params = [{"features": p_features}, {"stages": _STAGES}]

        try:
            result = rast_client.rpc_client.call(_RPC_METHOD, params)
        except ServerError as exc:
            raise RastServiceError(
                f"RAST server returned an error for {_RPC_METHOD} "
                f"({len(batch)} proteins): {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RastServiceError(
                f"Network error calling the RAST service at "
                f"{rast_client.rpc_client.url} for {_RPC_METHOD} "
                f"({len(batch)} proteins): {exc}"
            ) from exc

        if not result or not isinstance(result, list):
            raise RastServiceError(
                f"RAST returned an empty or malformed result for {_RPC_METHOD} "
                f"({len(batch)} proteins); got {result!r}"
            )
        payload = result[0]
        if not isinstance(payload, dict) or "features" not in payload:
            raise RastServiceError(
                f"RAST response for {_RPC_METHOD} is missing the expected "
                f"'features' key; got keys: "
                f"{sorted(payload.keys()) if isinstance(payload, dict) else type(payload)}"
            )
        return payload

    def _records_from_payload(
        self, payload: dict[str, Any], split_terms: bool
    ) -> list[AnnotationRecord]:
        """Convert one RPC response payload into ``AnnotationRecord`` objects.

        Args:
            payload: ``result[0]`` from the RPC response, as validated by
                :meth:`_call_rast`.
            split_terms: Whether to split multi-role ``function`` strings.

        Returns:
            List of ``AnnotationRecord``, one per feature that RAST
            returned a non-empty ``"function"`` for. Features with no
            ``"function"`` key (or an empty one) are omitted — not an
            error, matching the zero-hit convention used elsewhere in
            this package.
        """
        records: list[AnnotationRecord] = []
        for feature in payload["features"]:
            gene_id = feature.get("id")
            function = feature.get("function")
            if not function:
                continue
            terms = (
                _split_role_terms(function)
                if split_terms
                else [Term(namespace="RAST", id=None, value=function, evidence={})]
            )
            if terms:
                records.append(AnnotationRecord(gene_id=gene_id, terms=terms))
        return records


# ---------------------------------------------------------------------------
# Composition-based Impl wrapper (mirrors PatricWSUtilsImpl / BVBRCUtilsImpl)
# ---------------------------------------------------------------------------


class RastUtilsImpl:
    """Composition-based version of RastUtils.

    Holds ``env: SharedEnvUtils`` instead of inheriting. Delegates all
    attribute access to an internal ``RastUtils`` instance. RAST needs no
    KBase token (see module docstring), so unlike ``PatricWSUtilsImpl``/
    ``BVBRCUtilsImpl`` this constructor does not attempt to read one.
    """

    def __init__(self, env: Any, **kwargs: Any) -> None:
        self._env = env
        _kwargs: dict[str, Any] = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        _kwargs.update(kwargs)
        self._delegate = RastUtils(**_kwargs)

    @property
    def env(self) -> Any:
        return self._env

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)
