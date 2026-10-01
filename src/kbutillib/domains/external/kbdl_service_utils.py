"""Client for the KBDL job-running service (``kbdl_service``).

The KBDL job service (see the ``KBDLJobRunningPrototype`` repo, PRD
``kbdl-job-service-v0``) runs longer genome-annotation / model-reconstruction
/ fitness-analysis / SKANI / upload jobs behind a small FastAPI app. The
service binds an ANL-internal interface and is reached directly from any
host inside the ANL perimeter (including KBase JupyterHub pods); no SSH
tunnel is involved. This module is the KBUtilLib-side
client for that service's HTTP contract, so agents that already reach for
KBUtilLib's other external-service clients (:mod:`bvbrc_utils`,
:mod:`kb_uniprot_utils`, :mod:`patric_ws_utils`, :mod:`rcsb_pdb_utils`) find
this capability in the same place instead of hand-writing HTTP calls.

Wire-contract-only, load-bearing: this module MUST NOT import
``kbdl_service`` at any scope. The service and client are independently
deployable and share only the documented JSON/multipart contract (see
``kbdl_service.job_api``, ``kbdl_service.schemas.*``, and
``ops/DEPLOY.md`` in the service repo, read there -- not imported from
here -- while building this client). Request/response shapes are encoded
directly in this module rather than imported.

Endpoint & auth
----------------
The service endpoint is required and has no default: supply it via the
``KBDL_SERVICE_URL`` environment variable or the ``base_url`` constructor
argument. KBDL is reached directly over the ANL-internal network (currently
``http://poplar.cels.anl.gov:8791``, though it is expected to move into
KBase infrastructure); no SSH tunnel is used. Constructing the client with
neither ``base_url`` nor ``KBDL_SERVICE_URL`` set raises ``ValueError``. The
KBase auth token is taken from the
environment the same way :class:`~kbutillib.domains.external.patric_ws_utils.PatricWSUtils`
and friends take theirs -- via :meth:`SharedEnvUtils.get_token` (namespace
``"kbase"``, populated from ``KB_AUTH_TOKEN``/``KBASE_AUTH_TOKEN``,
``~/.kbase/token``, or an explicit ``token=`` kwarg) -- and sent as
``Authorization: Bearer <token>``. There is no ``username`` parameter
anywhere in this module: the service derives identity from the token alone
(see ``kbdl_service.identity``), and a client-supplied username would be
ignored server-side even if one were accepted here.

Endpoints encoded here (as of the service's ``job_api.py``/``schemas/*``
at KBDLJobRunningPrototype commit ``a024b55``)::

    POST   /jobs                    submit a job envelope -> 202 {"job_id"}
    GET    /jobs                    list the caller's jobs
    GET    /jobs/{job_id}           full status record
    GET    /jobs/{job_id}/result    result JSON (409 if not completed)
    DELETE /jobs/{job_id}           clear a finished job -> 204
    POST   /objects                 multipart upload (with an optional
                                     ``transform``) -> 202 {"job_id"} or
                                     201 {"object_id"} if content already
                                     hashes to a registered object
    GET    /objects                 list the caller's objects
    GET    /objects/{object_id}     object metadata
    GET    /objects/{object_id}/files   archive entry listing (409 if the
                                     object is not an archive type)
    DELETE /objects/{object_id}     delete (owner-only) -> 204

Deliberately NOT implemented:

- Any ACL/grant call. The service exposes no grant endpoint (see
  ``job_api.py``'s module docstring in the service repo) --
  ``ObjectStore.grant()`` is not owner-gated and adding a route for it
  would be a privilege-escalation hole, so this client does not invent
  one either.
- Any requeue call. Operator requeue (``kbdl-operator-requeue-v1``)
  deliberately has no HTTP route -- it is admin-CLI only on the service
  host, and owner self-service retry was explicitly deferred. This is
  called out because reading the service's recent commit history could
  reasonably suggest a client method is missing here; it is not missing,
  it is out of scope by design.

Job submission envelope is ``{"schema_version": "1", "job_type": <name>,
"params": {...}}`` for the nine job types: ``KBDLGenomeAnnotation``,
``KBDLModelReconstruction``, ``KBDLFitnessModelAnalysis``, ``KBDLSKANI``,
``KBDLCheckM2``, ``KBDLBuildGenome``, ``KBDLBuildSKANIDB``,
``KBDLStoreLoad``, ``KBDLUploadObject`` (the last of which is only ever
submitted as a side effect of :meth:`KBDLServiceUtils.upload_object`'s
multipart call -- there is no standalone JSON path to it, since its
params alone carry no bytes).

Contract note (``kbdl-atp-safe-at-scale-v1``): the result payloads for
``KBDLFitnessModelAnalysis`` and ``KBDLModelReconstruction`` jobs gained
required fields -- a top-level ``model`` and the effective ``solver`` --
under that PRD. This client returns raw result dicts from
:meth:`KBDLServiceUtils.get_job_result`/:meth:`KBDLServiceUtils.submit_and_wait`
either way, so no code here needed to change; only this documented
contract description was stale.

Errors -- distinguishable, typed, and raised from :meth:`_raise_for_status`:

- 400 with a ``{"supported_versions": [...]}`` body (bad/missing
  ``schema_version``) -> :class:`KBDLUnsupportedSchemaVersionError`.
- 400 with a ``{"code": ..., "message": ...}`` body (per-job-type params
  validation) -> :class:`KBDLInvalidInputError`.
- any other 400 -> :class:`KBDLBadRequestError`.
- 401 (the auth service rejected the token) -> :class:`KBDLAuthenticationError`.
- 403 (the token is valid but the caller is not permitted to perform this
  operation) -> :class:`KBDLNotAuthorizedError`. Deliberately distinct
  from 401 -- a rejected token and a valid-but-unpermitted token are
  different failure modes, and conflating them would send an operator
  chasing fresh tokens forever for what is actually a missing permission.
- 503 (the upstream KBase auth service was unreachable/failing) ->
  :class:`KBDLUpstreamAuthUnavailableError`. Deliberately distinct from 401
  -- see ``kbdl_service.identity``: a token problem and an auth-service
  outage are different failure modes and must not be conflated.
- 404 (no such job/object visible to the caller) -> :class:`KBDLNotFoundError`.
- 409 (state conflict, e.g. result requested before completion, or a
  non-archive object queried for its file listing) -> :class:`KBDLConflictError`.
- 413 (upload exceeded the service's max object size) ->
  :class:`KBDLPayloadTooLargeError`.

Two convenience helpers handle the contract's asynchronous-by-construction
shape (every submit returns only a job id; the caller must poll for
completion):

- :meth:`KBDLServiceUtils.poll_until_terminal` -- polls ``check_job`` until
  the job's ``state`` is ``"completed"`` or ``"failed"``. ``sleep_fn`` and
  ``time_fn`` are injectable (mirroring ``kbdl_service.identity``'s own
  clock-injection pattern) so tests never really sleep.
- :meth:`KBDLServiceUtils.submit_and_wait` -- submits one of the eight
  job types and returns its result, raising :class:`KBDLJobFailedError` if
  the job ends in ``"failed"``.

Contract note: ``KBDLGenomeAnnotationParams`` was narrowed to take exactly
one of ``genome`` (a built-genome JSON) or ``genome_ref`` (an object ref),
plus ``tools``. The previous ``fasta``/``gff``/``genbank``/``features``/
``kbase_genome_id`` parameters are gone -- the service now rejects any
request naming them -- so :meth:`KBDLServiceUtils.submit_genome_annotation`
no longer accepts them either. Genome assembly now happens as its own
``KBDLBuildGenome`` job (:meth:`KBDLServiceUtils.submit_build_genome`),
whose result feeds ``genome``/``genome_ref`` into annotation.
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...core.shared_env_utils import SharedEnvUtils

__all__ = [
    "KBDLServiceUtils",
    "KBDLServiceUtilsImpl",
    "KBDLServiceError",
    "KBDLBadRequestError",
    "KBDLUnsupportedSchemaVersionError",
    "KBDLInvalidInputError",
    "KBDLAuthenticationError",
    "KBDLNotAuthorizedError",
    "KBDLUpstreamAuthUnavailableError",
    "KBDLNotFoundError",
    "KBDLConflictError",
    "KBDLPayloadTooLargeError",
    "KBDLJobFailedError",
]

#: Environment variable supplying the KBDL service endpoint (no default;
#: KBDL is reached directly over the ANL-internal network).
KBDL_SERVICE_URL_ENV_VAR = "KBDL_SERVICE_URL"

#: There is deliberately no built-in default endpoint. KBDL used to be reached
#: through a loopback SSH tunnel (``http://127.0.0.1:8791``), but that posture
#: was abandoned 2026-08-24; the service now binds an ANL-internal interface and
#: is reached directly, so a silent loopback default would only ever produce a
#: connection-refused against a dead address. The name is retained (set to
#: ``None``) because it is part of the module's historical public surface;
#: callers must supply the endpoint via ``base_url`` or ``KBDL_SERVICE_URL``.
DEFAULT_BASE_URL = None

#: The nine job types accepted by ``POST /jobs`` (kbdl_service.schemas.envelope.JobType).
JOB_TYPE_GENOME_ANNOTATION = "KBDLGenomeAnnotation"
JOB_TYPE_MODEL_RECONSTRUCTION = "KBDLModelReconstruction"
JOB_TYPE_FITNESS_MODEL_ANALYSIS = "KBDLFitnessModelAnalysis"
JOB_TYPE_SKANI = "KBDLSKANI"
JOB_TYPE_CHECKM2 = "KBDLCheckM2"
JOB_TYPE_BUILD_GENOME = "KBDLBuildGenome"
JOB_TYPE_BUILD_SKANI_DB = "KBDLBuildSKANIDB"
JOB_TYPE_STORE_LOAD = "KBDLStoreLoad"
JOB_TYPE_UPLOAD_OBJECT = "KBDLUploadObject"
JOB_TYPE_HORIZYN = "KBDLHorizyn"

#: The only schema_version this client (and the v0 service) speaks.
SCHEMA_VERSION = "1"

#: Job states at which ``poll_until_terminal`` stops polling.
_TERMINAL_STATES = frozenset({"completed", "failed"})

#: How many CONSECUTIVE connection-level failures
#: :meth:`KBDLServiceUtils.poll_until_terminal` tolerates before giving up and
#: re-raising. Bounded rather than infinite so a service that is genuinely
#: unreachable still surfaces as an error; reset by any successful poll, so a
#: long job that hits an occasional keep-alive reset is unaffected. Five at the
#: default two-second interval is roughly ten seconds of grace, which covers a
#: connection reset and a worker restart without masking an outage.
_POLL_MAX_CONNECTION_ERRORS = 5


# ── Typed errors ──────────────────────────────────────────────────────────


class KBDLServiceError(Exception):
    """Base class for every typed error raised by :class:`KBDLServiceUtils`."""


class KBDLBadRequestError(KBDLServiceError):
    """HTTP 400 whose body did not match a known shape."""


class KBDLUnsupportedSchemaVersionError(KBDLServiceError):
    """HTTP 400: the envelope's ``schema_version`` was not accepted.

    Carries the service's ``supported_versions`` list from the error body.
    """

    def __init__(self, supported_versions: List[str]) -> None:
        self.supported_versions = list(supported_versions)
        super().__init__(
            f"unsupported schema_version; service supports: {self.supported_versions}"
        )


class KBDLInvalidInputError(KBDLServiceError):
    """HTTP 400: per-job-type ``params`` failed validation.

    Carries the ``code``/``message`` pair from the service's error body.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class KBDLAuthenticationError(KBDLServiceError):
    """HTTP 401: the KBase auth service rejected the token."""


class KBDLNotAuthorizedError(KBDLServiceError):
    """HTTP 403: the token is valid but the caller is not permitted to
    perform this operation.

    Deliberately a different type than :class:`KBDLAuthenticationError` --
    401 means the token itself was rejected, 403 means the token is valid
    but the caller lacks permission for the requested operation. Conflating
    the two would send an operator chasing fresh tokens forever for what is
    actually a missing permission.
    """


class KBDLUpstreamAuthUnavailableError(KBDLServiceError):
    """HTTP 503: the upstream KBase auth service could not be reached.

    Deliberately a different type than :class:`KBDLAuthenticationError` --
    a rejected token and an unreachable auth service are distinct failure
    modes (see ``kbdl_service.identity``).
    """


class KBDLNotFoundError(KBDLServiceError):
    """HTTP 404: no job/object with that id is visible to the caller."""


class KBDLConflictError(KBDLServiceError):
    """HTTP 409: a state conflict (e.g. result requested before completion)."""


class KBDLPayloadTooLargeError(KBDLServiceError):
    """HTTP 413: the uploaded content exceeded the service's max object size."""


class KBDLJobFailedError(KBDLServiceError):
    """Raised by :meth:`KBDLServiceUtils.submit_and_wait` when a job's
    terminal state is ``"failed"``. Carries the job id and the status
    record's ``failure`` field (``{"code": ..., "message": ...}``)."""

    def __init__(self, job_id: str, failure: Optional[Dict[str, Any]]) -> None:
        self.job_id = job_id
        self.failure = failure
        super().__init__(f"job {job_id} failed: {failure}")


# ── Client ────────────────────────────────────────────────────────────────


class KBDLServiceUtils(SharedEnvUtils):
    """Client for the KBDL job-running service's HTTP contract.

    Speaks the documented JSON/multipart contract only -- see the module
    docstring for the endpoint list and error mapping. Never imports
    ``kbdl_service``.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
        session: Optional[requests.Session] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the KBDL service client.

        Args:
            base_url: The KBDL service endpoint (direct ANL-internal URL).
                If None, the ``KBDL_SERVICE_URL`` environment variable is
                used. There is no default: if neither is set, a
                ``ValueError`` is raised. No SSH tunnel is involved.
            timeout: Per-request timeout in seconds.
            session: Optional pre-built ``requests.Session`` (or a
                stand-in with a compatible ``.request()``), so tests can
                inject a fake HTTP transport without making real network
                calls.
            **kwargs: Additional arguments passed to ``SharedEnvUtils``
                (e.g. ``token="..."`` to set the KBase token directly).
        """
        super().__init__(**kwargs)
        resolved = base_url or os.environ.get(KBDL_SERVICE_URL_ENV_VAR)
        if not resolved:
            raise ValueError(
                "KBDL service endpoint is not configured: set KBDL_SERVICE_URL "
                "or pass base_url. KBDL is currently served at "
                "http://poplar.cels.anl.gov:8791 inside the ANL network; "
                "no tunnel is needed."
            )
        self.base_url = resolved.rstrip("/")
        self.timeout = timeout
        self.session = (
            session if session is not None else self._build_retrying_session()
        )

    @staticmethod
    def _build_retrying_session() -> requests.Session:
        """A ``requests.Session`` that retries CONNECTION-level failures.

        The KBDL API is uvicorn, whose ``--timeout-keep-alive`` defaults to 5
        seconds. A pooled keep-alive connection can therefore be closed by the
        server at the same moment this client reuses it, which surfaces as
        ``ConnectionResetError(104)`` on a request that was never served. That
        is not a service failure and must not be reported as one: on
        2026-10-01 it killed a wait over ten KBDLHorizyn jobs that all
        completed normally, while the API logged 200 OK throughout.

        **``POST`` is deliberately NOT retried.** ``urllib3``'s default
        ``allowed_methods`` covers only the idempotent verbs, and that is the
        behaviour wanted here rather than an accident: ``_submit`` is a POST,
        and a blind retry of a submission that did reach the server would
        create a SECOND job. Status codes are not retried either -- a 4xx/5xx
        is mapped to a typed exception by :meth:`_raise_for_status` and is
        meaningful, so swallowing and repeating it would hide it.
        """
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=0,
            backoff_factor=0.5,
            allowed_methods=Retry.DEFAULT_ALLOWED_METHODS,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        built = requests.Session()
        built.mount("http://", adapter)
        built.mount("https://", adapter)
        return built

    # ── internal HTTP plumbing ──────────────────────────────────────────

    def _auth_headers(self) -> Dict[str, str]:
        token = self.get_token(namespace="kbase")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = {**self._auth_headers(), **(kwargs.pop("headers", None) or {})}
        self.log_debug(f"{method} {url}")
        response = self.session.request(
            method, url, timeout=self.timeout, headers=headers, **kwargs
        )
        self._raise_for_status(response)
        return response

    @staticmethod
    def _error_detail(response: requests.Response) -> Any:
        try:
            body = response.json()
        except ValueError:
            return response.text
        if isinstance(body, dict) and "detail" in body:
            return body["detail"]
        return body

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.ok:
            return

        status = response.status_code
        detail = self._error_detail(response)

        if status == 400:
            if isinstance(detail, dict) and "supported_versions" in detail:
                raise KBDLUnsupportedSchemaVersionError(detail["supported_versions"])
            if isinstance(detail, dict) and "code" in detail:
                raise KBDLInvalidInputError(
                    detail.get("code", "invalid_input"), detail.get("message", "")
                )
            raise KBDLBadRequestError(str(detail))
        if status == 401:
            raise KBDLAuthenticationError(
                str(detail) if detail else "authentication rejected"
            )
        if status == 403:
            raise KBDLNotAuthorizedError(str(detail) if detail else "not authorized")
        if status == 404:
            raise KBDLNotFoundError(str(detail) if detail else "not found")
        if status == 409:
            raise KBDLConflictError(str(detail) if detail else "conflict")
        if status == 413:
            raise KBDLPayloadTooLargeError(
                str(detail) if detail else "payload too large"
            )
        if status == 503:
            raise KBDLUpstreamAuthUnavailableError(
                str(detail) if detail else "upstream auth service unavailable"
            )

        # Anything else (e.g. an unexpected 5xx) -- let requests raise its
        # own HTTPError rather than inventing a typed wrapper for it.
        response.raise_for_status()

    def _submit(self, job_type: str, params: Dict[str, Any]) -> str:
        package = {
            "schema_version": SCHEMA_VERSION,
            "job_type": job_type,
            "params": params,
        }
        response = self._request("POST", "/jobs", json=package)
        return response.json()["job_id"]

    # ── job submission (one method per job type) ────────────────────────

    def submit_genome_annotation(
        self,
        tools: Any,
        genome: Optional[Any] = None,
        genome_ref: Optional[Any] = None,
        tax_id: Optional[Any] = None,
    ) -> str:
        """Submit a ``KBDLGenomeAnnotation`` job. Returns the job id.

        See ``kbdl_service.schemas.genome_annotation.KBDLGenomeAnnotationParams``
        in the service repo for the accepted params shape: exactly one of
        ``genome`` (a built-genome JSON, typically the result of a
        ``KBDLBuildGenome`` job) or ``genome_ref`` (an object ref), plus
        ``tools``, with ``tax_id`` as an optional override.

        Deliberately has no catch-all ``**params``: the service narrowed
        this contract to drop ``fasta``/``gff``/``genbank``/``features``/
        ``kbase_genome_id`` entirely, and a request naming any of them is
        now rejected server-side, so this method removed them rather than
        continuing to pass them through. Passing any of those old names is
        a ``TypeError`` (unexpected keyword argument), not a silently
        forwarded param. Build the genome first with
        :meth:`submit_build_genome` if you only have raw sequence/
        annotation files.

        Raises:
            ValueError: If zero or both of ``genome``/``genome_ref`` are
                given. Raised before any request is sent.
        """
        provided = [
            name
            for name, val in (("genome", genome), ("genome_ref", genome_ref))
            if val is not None
        ]
        if len(provided) != 1:
            raise ValueError(
                "submit_genome_annotation requires exactly one of genome/genome_ref, "
                f"got: {provided or 'neither'}"
            )
        payload: Dict[str, Any] = {"tools": tools}
        if genome is not None:
            payload["genome"] = genome
        if genome_ref is not None:
            payload["genome_ref"] = genome_ref
        if tax_id is not None:
            payload["tax_id"] = tax_id
        return self._submit(JOB_TYPE_GENOME_ANNOTATION, payload)

    def submit_build_genome(
        self,
        skani_db: Any,
        fasta: Optional[Any] = None,
        genbank: Optional[Any] = None,
        archive: Optional[Any] = None,
        gff: Optional[Any] = None,
        **params: Any,
    ) -> str:
        """Submit a ``KBDLBuildGenome`` job. Returns the job id.

        See ``kbdl_service.schemas.build_genome.KBDLBuildGenomeParams`` in
        the service repo for the accepted ``params`` shape: exactly one of
        ``fasta``/``genbank``/``archive`` as the genome source, an optional
        ``gff`` alongside ``fasta``, an optional ``fasta`` alongside
        ``genbank``, and a required ``skani_db``.

        Raises:
            ValueError: If ``skani_db`` is missing, if the genome source is
                ambiguous (zero, or two sources other than the documented
                ``genbank``+``fasta`` pairing), or if ``gff`` is given
                without ``fasta``. Raised before any request is sent.
        """
        if skani_db is None:
            raise ValueError("submit_build_genome requires skani_db")

        sources = {"fasta": fasta, "genbank": genbank, "archive": archive}
        provided = {name for name, val in sources.items() if val is not None}
        valid_combinations = ({"fasta"}, {"genbank"}, {"archive"}, {"genbank", "fasta"})
        if provided not in valid_combinations:
            raise ValueError(
                "submit_build_genome requires exactly one of fasta/genbank/archive "
                "(an optional fasta may accompany genbank), got: "
                f"{sorted(provided) or 'none'}"
            )
        if gff is not None and fasta is None:
            raise ValueError("submit_build_genome's gff is only valid alongside fasta")

        payload = dict(params)
        payload["skani_db"] = skani_db
        if fasta is not None:
            payload["fasta"] = fasta
        if genbank is not None:
            payload["genbank"] = genbank
        if archive is not None:
            payload["archive"] = archive
        if gff is not None:
            payload["gff"] = gff
        return self._submit(JOB_TYPE_BUILD_GENOME, payload)

    def submit_model_reconstruction(self, **params: Any) -> str:
        """Submit a ``KBDLModelReconstruction`` job. Returns the job id.

        See ``kbdl_service.schemas.model_reconstruction.KBDLModelReconstructionParams``
        (``mapping``, ``annotations``, ``tools``, ``gapfill_media``,
        ``fva_media``).
        """
        return self._submit(JOB_TYPE_MODEL_RECONSTRUCTION, params)

    def submit_fitness_model_analysis(self, **params: Any) -> str:
        """Submit a ``KBDLFitnessModelAnalysis`` job. Returns the job id.

        See ``kbdl_service.schemas.fitness_analysis.KBDLFitnessModelAnalysisParams``
        (``model``, ``fitness``, ``gene_reactions``).
        """
        return self._submit(JOB_TYPE_FITNESS_MODEL_ANALYSIS, params)

    def submit_skani(self, **params: Any) -> str:
        """Submit a ``KBDLSKANI`` job. Returns the job id.

        See ``kbdl_service.schemas.skani.KBDLSKANIParams`` (``skani_db``,
        exactly one of ``fasta``/``archive``, ``delete_archive_on_completion``).
        """
        return self._submit(JOB_TYPE_SKANI, params)

    def submit_horizyn(self, **params: Any) -> str:
        """Submit a ``KBDLHorizyn`` job. Returns the job id.

        See ``kbdl_service.schemas.horizyn.KBDLHorizynParams`` for the
        accepted ``params`` shape.
        """
        return self._submit(JOB_TYPE_HORIZYN, params)

    def submit_checkm2(self, **params: Any) -> str:
        """Submit a ``KBDLCheckM2`` job. Returns the job id.

        See ``kbdl_service.schemas.checkm2.KBDLCheckM2Params`` (``checkm2_db``,
        exactly one of ``fasta``/``archive``, ``delete_archive_on_completion``).
        """
        return self._submit(JOB_TYPE_CHECKM2, params)

    def submit_store_load(self, source_job_ids: List[str]) -> str:
        """Submit a ``KBDLStoreLoad`` job. Returns the job id.

        See ``kbdl_service.schemas.store_load.KBDLStoreLoadParams`` in the
        service repo for the accepted ``params`` shape: a single
        ``source_job_ids`` list naming the completed jobs whose results
        should be loaded into the store.
        """
        return self._submit(JOB_TYPE_STORE_LOAD, {"source_job_ids": source_job_ids})

    def submit_build_skani_db(self, **params: Any) -> str:
        """Submit a ``KBDLBuildSKANIDB`` job. Returns the job id.

        See ``kbdl_service.schemas.build_skani_db.KBDLBuildSKANIDBParams``
        (``sources``, ``name``, ``visibility``).
        """
        return self._submit(JOB_TYPE_BUILD_SKANI_DB, params)

    # ── job lifecycle ────────────────────────────────────────────────────

    def list_jobs(self) -> List[Dict[str, Any]]:
        """List the caller's jobs (``job_id``, ``job_type``, ``state``,
        ``submitted_at`` only -- the full record is :meth:`check_job`)."""
        return self._request("GET", "/jobs").json()

    def check_job(self, job_id: str) -> Dict[str, Any]:
        """Return the full status record for ``job_id``.

        Raises :class:`KBDLNotFoundError` if the job does not exist or
        belongs to another caller (the service renders both cases as 404).
        """
        return self._request("GET", f"/jobs/{job_id}").json()

    def get_job_result(self, job_id: str) -> Dict[str, Any]:
        """Return the result JSON for a completed job.

        Raises :class:`KBDLConflictError` if the job has not reached
        ``"completed"`` yet, or :class:`KBDLNotFoundError` if it doesn't
        exist / isn't the caller's.
        """
        return self._request("GET", f"/jobs/{job_id}/result").json()

    def clear_job(self, job_id: str) -> None:
        """Clear a finished job's result and workdir (tombstone retained)."""
        self._request("DELETE", f"/jobs/{job_id}")

    def poll_until_terminal(
        self,
        job_id: str,
        interval: float = 2.0,
        timeout: Optional[float] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> Dict[str, Any]:
        """Poll :meth:`check_job` until ``job_id``'s state is terminal.

        Terminal states are ``"completed"`` and ``"failed"`` -- this
        returns as soon as either is reached; it never raises on a
        ``"failed"`` outcome itself (that's :meth:`submit_and_wait`'s job).

        Args:
            job_id: The job to poll.
            interval: Seconds to wait between polls (passed to ``sleep_fn``).
            timeout: Optional overall wall-clock budget in seconds. Raises
                ``TimeoutError`` if exceeded before a terminal state.
            sleep_fn: Injectable sleep function (default ``time.sleep``).
                Tests should pass a no-op so polling loops don't really
                sleep.
            time_fn: Injectable monotonic clock (default
                ``time.monotonic``), used only for the ``timeout`` budget.

        Returns:
            The final status record (same shape as :meth:`check_job`).
        """
        start = time_fn()
        consecutive_connection_errors = 0
        while True:
            try:
                status = self.check_job(job_id)
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                # A LOST CONNECTION IS NOT A LOST JOB. The job runs on the
                # service, not here; an interrupted GET says nothing about it.
                # Reporting failure here is strictly worse than waiting --
                # the caller abandons work that is still progressing. See
                # `_build_retrying_session` for the uvicorn keep-alive race
                # that makes this reachable even against a healthy API.
                consecutive_connection_errors += 1
                if consecutive_connection_errors > _POLL_MAX_CONNECTION_ERRORS:
                    raise
                self.log_warning(
                    f"transient connection error polling job {job_id} "
                    f"({consecutive_connection_errors} of "
                    f"{_POLL_MAX_CONNECTION_ERRORS} tolerated): {exc}"
                )
            else:
                # Only a successful poll clears the streak: the bound is on
                # CONSECUTIVE failures, so a genuinely unreachable service
                # still surfaces instead of being retried forever, while an
                # occasional reset over a long job costs nothing.
                consecutive_connection_errors = 0
                if status.get("state") in _TERMINAL_STATES:
                    return status
            if timeout is not None and (time_fn() - start) >= timeout:
                raise TimeoutError(
                    f"job {job_id} did not reach a terminal state within {timeout}s"
                )
            sleep_fn(interval)

    def submit_and_wait(
        self,
        job_type: str,
        params: Dict[str, Any],
        interval: float = 2.0,
        timeout: Optional[float] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> Dict[str, Any]:
        """Submit ``job_type`` with ``params``, poll to completion, and
        return the result.

        Convenience wrapper around :meth:`_submit` + :meth:`poll_until_terminal`
        + :meth:`get_job_result`. Raises :class:`KBDLJobFailedError` if the
        job's terminal state is ``"failed"``.

        Args:
            job_type: One of ``KBDL_SERVICE_UTILS`` job-type constants
                (``JOB_TYPE_GENOME_ANNOTATION``, etc.) or the equivalent
                literal string.
            params: The job-type-specific params dict.
            interval: Seconds between polls.
            timeout: Optional overall wall-clock budget in seconds.
            sleep_fn: Injectable sleep function (see :meth:`poll_until_terminal`).
            time_fn: Injectable monotonic clock (see :meth:`poll_until_terminal`).

        Returns:
            The job's result JSON (same shape as :meth:`get_job_result`).
        """
        job_id = self._submit(job_type, params)
        status = self.poll_until_terminal(
            job_id,
            interval=interval,
            timeout=timeout,
            sleep_fn=sleep_fn,
            time_fn=time_fn,
        )
        if status.get("state") == "failed":
            raise KBDLJobFailedError(job_id, status.get("failure"))
        return self.get_job_result(job_id)

    # ── object store ─────────────────────────────────────────────────────

    def upload_object(
        self,
        file: Union[str, Path, bytes, bytearray, Any],
        object_type: str,
        name: str,
        visibility: str,
        filename: Optional[str] = None,
        transform: str = "none",
    ) -> Dict[str, Any]:
        """Upload an object via the multipart ``POST /objects`` endpoint.

        This is also how a ``KBDLUploadObject`` job gets submitted -- there
        is no separate JSON-only submission path for it, since its params
        alone carry no bytes.

        Object types fall into two groups, and this method only ever
        creates the first kind:

        - **Caller-uploadable via this method**: ``"SKANIDB"``,
          ``"ModelMapping"``, ``"Media"``, ``"GenomeArchive"``.
        - **Operator-registered on the host, with no HTTP upload route by
          design**: ``"BaktaDB"``, ``"KofamProfiles"``, ``"CheckM2DB"``.
          These appear in :meth:`list_objects`/:meth:`get_object_metadata`
          like any other object, but the service deliberately exposes no
          way to upload one -- they are large, operator-curated reference
          databases seeded on the host, not caller content. Calling this
          method with ``object_type="CheckM2DB"`` (or ``"BaktaDB"``/
          ``"KofamProfiles"``) is a category error; there is no route that
          accepts it.

        Args:
            file: A path (``str``/``Path``), raw ``bytes``/``bytearray``,
                or an open binary file-like object to upload.
            object_type: One of the caller-uploadable object types listed
                above.
            name: Display name for the object.
            visibility: ``"public"`` or ``"private"``.
            filename: Optional filename to send in the multipart part;
                defaults to ``name``.
            transform: Server-side post-processing to apply to the upload,
                default ``"none"``. Two surprising, already-implemented
                server behaviours to know before passing anything else:

                - ``transform != "none"`` is valid ONLY when
                  ``object_type == "GenomeArchive"``; any other
                  ``object_type`` combined with a non-``"none"`` transform
                  is rejected with HTTP 400.
                - ``transform != "none"`` disables the pre-hash dedup
                  short-circuit, so such an upload ALWAYS returns
                  ``{"job_id": ...}`` (202) and never ``{"object_id": ...}``
                  (201), even if identical content was uploaded before.

                Passing the default ``transform="none"`` leaves the
                request byte-identical to this method's pre-``transform``
                behavior, so existing callers are unaffected.

        Returns:
            With ``transform="none"``: ``{"job_id": ...}`` for newly-hashed
            content (HTTP 202), or ``{"object_id": ...}`` if the content
            already hashes to an object the caller can already see (HTTP
            201) -- the upload is discarded server-side in that case and no
            new job is queued. With ``transform != "none"``: always
            ``{"job_id": ...}`` (HTTP 202); the 201/dedup path never
            applies.
        """
        opened: Optional[Any] = None
        if hasattr(file, "read"):
            fileobj = file
        elif isinstance(file, (bytes, bytearray)):
            fileobj = io.BytesIO(file)
        else:
            opened = open(file, "rb")
            fileobj = opened

        try:
            files = {"file": (filename or name, fileobj)}
            data = {"object_type": object_type, "name": name, "visibility": visibility}
            if transform != "none":
                data["transform"] = transform
            response = self._request("POST", "/objects", files=files, data=data)
            return response.json()
        finally:
            if opened is not None:
                opened.close()

    def list_objects(self) -> List[Dict[str, Any]]:
        """List the caller's objects."""
        return self._request("GET", "/objects").json()

    def get_object_metadata(self, object_id: str) -> Dict[str, Any]:
        """Return metadata for a single object.

        Raises :class:`KBDLNotFoundError` if it doesn't exist / isn't
        visible to the caller.
        """
        return self._request("GET", f"/objects/{object_id}").json()

    def list_archive_files(self, object_id: str) -> List[Dict[str, Any]]:
        """List the entries inside an archive-type object.

        Raises :class:`KBDLConflictError` if ``object_id`` is not an
        archive-type object.
        """
        return self._request("GET", f"/objects/{object_id}/files").json()

    def delete_object(self, object_id: str) -> None:
        """Delete an object (owner-only; idempotent if already absent)."""
        self._request("DELETE", f"/objects/{object_id}")


# ── Composition-based implementation ─────────────────────────────────────


class KBDLServiceUtilsImpl:
    """Composition-based version of KBDLServiceUtils.

    Holds ``env: SharedEnvUtils`` instead of inheriting. Delegates all
    method calls to an internal legacy instance.
    """

    def __init__(self, env, **kwargs):
        self._env = env
        # Build kwargs to pass through to legacy constructor
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        # Copy token if env has one
        try:
            _kwargs["token"] = env.get_token("kbase")
        except Exception:
            pass
        _kwargs.update(kwargs)
        self._delegate_kwargs = _kwargs
        # Delegate construction is deferred: KBDLServiceUtils now requires an
        # endpoint (via base_url or KBDL_SERVICE_URL) and raises if none is
        # configured. Building it lazily means merely registering / holding this
        # wrapper (e.g. the toolkit's lazy ``kbdl_service`` property) does not
        # force endpoint resolution -- the clear ValueError surfaces on first
        # real use instead of at wrapper-construction time.
        self._delegate_instance: Optional[KBDLServiceUtils] = None

    @property
    def _delegate(self) -> "KBDLServiceUtils":
        if self._delegate_instance is None:
            self._delegate_instance = KBDLServiceUtils(**self._delegate_kwargs)
        return self._delegate_instance

    @property
    def env(self):
        return self._env

    def __getattr__(self, name):
        # Delegate all attribute access to the legacy instance
        return getattr(self._delegate, name)
