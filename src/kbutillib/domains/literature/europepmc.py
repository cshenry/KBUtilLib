"""Europe PMC REST client for gene-literature research.

Europe PMC is the entry point of the gene-literature pipeline: given a gene
alias and the organism it belongs to, it answers "which papers mention this?",
and given a publication id it answers "what is this paper?".

The client is deliberately small and boring:

* one endpoint (``/search``) with ``format=json&resultType=core`` and
  ``cursorMark`` paging;
* a process-wide request throttle, because Europe PMC asks clients to keep
  to a couple of requests per second;
* exponential-backoff retry on 429 and 5xx;
* every request URL+params cached to the shared :class:`LiteratureCache`
  ``http_cache`` table, so a repeated run makes no network calls at all.

The clock, the sleep function, the HTTP session and the cache are all
injectable, which is how the offline tests drive it.

Paper identifiers are normalised to one of three canonical forms --
``PMID:<n>``, ``PMC:<PMCn>``, ``DOI:<lowercased doi>`` -- by
:func:`canonical_paper_id` and :func:`normalise_paper_id`, with
:func:`merge_ids` unioning records that turn out to be the same paper.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence
from urllib.parse import urlencode

import requests

from kbutillib.core.shared_env_utils import SharedEnvUtils

from .cache import CACHE_PATH_CONFIG_KEY, LiteratureCache, resolve_cache_path

# ── service constants ────────────────────────────────────────────────────

EUROPEPMC_REST_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EUROPEPMC_SEARCH_URL = f"{EUROPEPMC_REST_BASE}/search"

#: Cache namespace holding raw request URL+params -> response JSON.
HTTP_CACHE_TABLE = "http_cache"

#: Config key for the request rate (requests per second).
RPS_CONFIG_KEY = "gene_literature.europepmc_rps"
DEFAULT_REQUESTS_PER_SECOND = 2.0

#: Europe PMC refuses pageSize above 1000.
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_RESULTS = 1000

#: Retry budget and backoff for 429/5xx responses.
MAX_TRIES = 5
BACKOFF_BASE_SECONDS = 1.0

#: Ids OR-ed into a single ``get()`` request.
IDS_PER_REQUEST = 50

DEFAULT_TIMEOUT_SECONDS = 30
USER_AGENT = "KBUtilLib-EuropePMCClient/1.0 (KBase; https://kbase.us)"

# ── identifier handling ──────────────────────────────────────────────────

_PMID_RE = re.compile(r"^\d+$")
_PMCID_RE = re.compile(r"^PMC\d+$", re.IGNORECASE)
# Loose on the registrant: real DOIs are 10.<digits>/..., but test and
# placeholder DOIs such as 10.x/y must normalise too.
_DOI_RE = re.compile(r"^10\.[^\s/]+/\S+$")
_PUBMED_URL_RE = re.compile(
    r"^https?://(?:www\.)?pubmed\.ncbi\.nlm\.nih\.gov/(\d+)/?$", re.IGNORECASE
)
_DOI_URL_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/(\S+)$", re.IGNORECASE)

# Locus-tag-shaped aliases: ACIAD0380, ACIAD_RS00005, b1234, SACE_RS01234.
_LOCUS_TAG_RE = re.compile(r"^[A-Za-z]+_?(RS)?_?\d{3,}$")

#: Aliases this short ("ab", "x") match everything and are not searchable.
MIN_ALIAS_LENGTH = 3


def _escape_quotes(value: str) -> str:
    """Backslash-escape double quotes for a Europe PMC quoted phrase.

    A gene alias or organism name containing a double quote would otherwise
    terminate the phrase early and corrupt the whole query.
    """
    return str(value).replace('"', '\\"')


def canonical_paper_id(
    pmid: Optional[Any] = None,
    pmcid: Optional[Any] = None,
    doi: Optional[Any] = None,
) -> str:
    """Build the canonical id for a paper from whichever ids are known.

    Precedence is PMID, then PMCID, then DOI.

    Args:
        pmid: PubMed id (``123``, ``"123"`` or ``"PMID:123"``).
        pmcid: PMC id (``"PMC123"``, ``"123"`` or ``"PMC:PMC123"``).
        doi: DOI, with or without a ``doi:`` prefix.

    Returns:
        ``"PMID:<n>"``, ``"PMC:PMC<n>"`` or ``"DOI:<lowercased doi>"``.

    Raises:
        ValueError: If no id is supplied, or the supplied id is malformed.
    """
    if pmid:
        raw = str(pmid).strip()
        if raw.lower().startswith("pmid:"):
            raw = raw[len("pmid:"):].strip()
        if not _PMID_RE.match(raw):
            raise ValueError(f"Malformed PMID: {pmid!r}")
        return f"PMID:{raw}"

    if pmcid:
        raw = str(pmcid).strip()
        for prefix in ("pmcid:", "pmc:"):
            if raw.lower().startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if raw.isdigit():
            raw = f"PMC{raw}"
        if not _PMCID_RE.match(raw):
            raise ValueError(f"Malformed PMCID: {pmcid!r}")
        return f"PMC:PMC{raw[3:]}"

    if doi:
        raw = str(doi).strip()
        if raw.lower().startswith("doi:"):
            raw = raw[len("doi:"):].strip()
        if not raw:
            raise ValueError(f"Malformed DOI: {doi!r}")
        return f"DOI:{raw.lower()}"

    raise ValueError("canonical_paper_id requires at least one of pmid, pmcid, doi")


def _safe_canonical(**kwargs: Any) -> Optional[str]:
    """:func:`canonical_paper_id`, returning None instead of raising."""
    try:
        return canonical_paper_id(**kwargs)
    except ValueError:
        return None


def normalise_paper_id(text: Optional[Any]) -> Optional[str]:
    """Normalise any recognised publication-id spelling to its canonical form.

    Accepts bare ids (``"1234567"``, ``"PMC123"``, ``"10.1038/nature1"``),
    prefixed ids (``"PMID:1"``, ``"pmc:PMC123"``, ``"doi:10.x/y"``) and the
    usual web forms (``"https://doi.org/10.x/y"``,
    ``"https://pubmed.ncbi.nlm.nih.gov/123/"``).

    Args:
        text: The identifier text in any of the forms above.

    Returns:
        The canonical id, or None if ``text`` is not a recognisable
        publication id.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    match = _PUBMED_URL_RE.match(raw)
    if match:
        return _safe_canonical(pmid=match.group(1))

    match = _DOI_URL_RE.match(raw)
    if match:
        return _safe_canonical(doi=match.group(1))

    lowered = raw.lower()
    for prefix in ("pmid:", "pubmed:"):
        if lowered.startswith(prefix):
            return _safe_canonical(pmid=raw[len(prefix):].strip())
    for prefix in ("pmcid:", "pmc:"):
        if lowered.startswith(prefix):
            return _safe_canonical(pmcid=raw[len(prefix):].strip())
    if lowered.startswith("doi:"):
        return _safe_canonical(doi=raw[len("doi:"):].strip())

    if _PMID_RE.match(raw):
        return _safe_canonical(pmid=raw)
    if _PMCID_RE.match(raw):
        return _safe_canonical(pmcid=raw)
    if _DOI_RE.match(raw):
        return _safe_canonical(doi=raw)
    return None


def alias_query(alias: str, organism_terms: Sequence[str]) -> Optional[str]:
    """Build the Europe PMC query for one gene alias.

    Locus tags (``ACIAD0380``, ``ACIAD_RS00005``) are globally distinctive, so
    they are searched as an unqualified exact phrase.  Ordinary gene symbols
    (``dnaA``) are not, so they are conjoined with the organism terms.

    Args:
        alias: The gene alias to search for.
        organism_terms: Organism names/strain synonyms to disambiguate a
            non-locus-tag alias (e.g. ``["Acinetobacter baylyi", "ADP1"]``).

    Returns:
        A Europe PMC query string, or None if the alias is too short
        (fewer than :data:`MIN_ALIAS_LENGTH` characters) to search usefully.
    """
    if alias is None:
        return None
    term = str(alias).strip()
    if len(term) < MIN_ALIAS_LENGTH:
        return None

    quoted = f'"{_escape_quotes(term)}"'
    if _LOCUS_TAG_RE.match(term) or "_RS" in term:
        return quoted

    organisms = [
        f'"{_escape_quotes(str(entry).strip())}"'
        for entry in (organism_terms or [])
        if entry is not None and str(entry).strip()
    ]
    if not organisms:
        return quoted
    return f'{quoted} AND ({" OR ".join(organisms)})'


class MergedIds(NamedTuple):
    """Result of :func:`merge_ids`.

    Attributes:
        merged: canonical id -> merged record.
        aliases: every known id (canonical form) -> its canonical id.
        unkeyed: records carrying no publication id at all, in input order.
    """

    merged: Dict[str, Dict[str, Any]]
    aliases: Dict[str, str]
    unkeyed: List[Dict[str, Any]]


def _record_tokens(record: Dict[str, Any]) -> List[str]:
    """Canonical ids a record carries, in PMID > PMCID > DOI precedence."""
    tokens: List[str] = []
    for field in ("pmid", "pmcid", "doi"):
        value = record.get(field)
        if not value:
            continue
        token = _safe_canonical(**{field: value})
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def merge_ids(records: Iterable[Dict[str, Any]]) -> MergedIds:
    """Union records that are the same paper, transitively.

    Two records are the same paper if they share any pmid, pmcid or doi; the
    relation is closed transitively, so a record keyed only by DOI and one
    keyed only by PMID are merged when a third carries both.

    Args:
        records: Records with any of ``pmid``/``pmcid``/``doi`` set.

    Returns:
        A :class:`MergedIds` triple.  Records with no publication id are
        never given an arbitrary key -- they come back in ``unkeyed``.
    """
    parent: Dict[str, str] = {}

    def find(token: str) -> str:
        root = token
        while parent[root] != root:
            root = parent[root]
        while parent[token] != root:  # path compression
            parent[token], token = root, parent[token]
        return root

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    keyed: List[tuple] = []
    unkeyed: List[Dict[str, Any]] = []
    for record in records:
        tokens = _record_tokens(record)
        if not tokens:
            unkeyed.append(record)
            continue
        for token in tokens:
            parent.setdefault(token, token)
        for token in tokens[1:]:
            union(tokens[0], token)
        keyed.append((record, tokens))

    # Group in first-appearance order.
    groups: Dict[str, List[tuple]] = {}
    for record, tokens in keyed:
        groups.setdefault(find(tokens[0]), []).append((record, tokens))

    merged: Dict[str, Dict[str, Any]] = {}
    aliases: Dict[str, str] = {}
    for members in groups.values():
        combined: Dict[str, Any] = {}
        group_tokens: List[str] = []
        for record, tokens in members:
            for key, value in record.items():
                if value in (None, "", [], {}):
                    continue
                combined.setdefault(key, value)
            for token in tokens:
                if token not in group_tokens:
                    group_tokens.append(token)

        canonical = canonical_paper_id(
            pmid=combined.get("pmid"),
            pmcid=combined.get("pmcid"),
            doi=combined.get("doi"),
        )
        combined["canonical_id"] = canonical
        combined["canonical_ids"] = group_tokens
        merged[canonical] = combined
        for token in group_tokens:
            aliases[token] = canonical

    return MergedIds(merged=merged, aliases=aliases, unkeyed=unkeyed)


# ── request throttle ─────────────────────────────────────────────────────


class _Throttle:
    """Minimum-interval gate shared by every caller that holds it.

    The lock is held *across* the sleep on purpose.  Releasing it first would
    let every waiting thread (or every asyncio task that is about to make a
    blocking call) wake at the same instant and fire a burst, which is
    exactly what the throttle exists to prevent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: Optional[float] = None

    def wait(
        self,
        min_interval: float,
        now: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> float:
        """Block until the next request may be sent; return that moment."""
        with self._lock:
            started = now()
            if self._last is not None and min_interval > 0:
                remaining = min_interval - (started - self._last)
                if remaining > 0:
                    sleep(remaining)
                    started = now()
            self._last = started
            return started


#: Process-wide throttle: the rate limit belongs to the service, not to one
#: client object, so every default-constructed client shares this one.
_GLOBAL_THROTTLE = _Throttle()


# ── client ───────────────────────────────────────────────────────────────


class EuropePMCClient(SharedEnvUtils):
    """Search and resolve publications against the Europe PMC REST API.

    Args:
        session: HTTP session with a ``get(url, params=, timeout=)`` method.
            Defaults to a lazily created :class:`requests.Session`.
        clock: Monotonic time source.  Defaults to :func:`time.monotonic`.
        sleep: Blocking sleep.  Defaults to :func:`time.sleep`.
        cache: Shared literature cache.  Defaults to a
            :class:`~kbutillib.domains.literature.cache.LiteratureCache` at
            ``gene_literature.cache_path``.
        throttle: Rate gate.  Defaults to the process-wide throttle, except
            when ``clock`` or ``sleep`` is injected -- a fake clock gets a
            private throttle so tests neither perturb nor are perturbed by
            the process-wide one.
        search_url: Override for the ``/search`` endpoint.
        timeout: Per-request timeout in seconds.
        max_tries: Total attempts per request, including the first.
        **kwargs: Passed to :class:`SharedEnvUtils` (``config``,
            ``config_file``, ``token_file``, ...).

    Config:
        ``gene_literature.europepmc_rps``: requests per second
        (default 2); ``gene_literature.cache_path``: cache location.
    """

    def __init__(
        self,
        session: Optional[Any] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        cache: Optional[LiteratureCache] = None,
        throttle: Optional[_Throttle] = None,
        search_url: str = EUROPEPMC_SEARCH_URL,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_tries: int = MAX_TRIES,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.search_url = search_url
        self.timeout = timeout
        self.max_tries = max(1, int(max_tries))

        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        if throttle is not None:
            self._throttle = throttle
        elif clock is not None or sleep is not None:
            self._throttle = _Throttle()
        else:
            self._throttle = _GLOBAL_THROTTLE

        self.min_interval = self._resolve_min_interval()

        self._session = session
        self._cache = cache
        self._lazy_lock = threading.Lock()

    def _resolve_min_interval(self) -> float:
        """Seconds between requests, from ``gene_literature.europepmc_rps``."""
        configured = self.get_config_value(RPS_CONFIG_KEY, DEFAULT_REQUESTS_PER_SECOND)
        try:
            rps = float(configured)
        except (TypeError, ValueError):
            self.log_warning(
                f"Ignoring non-numeric {RPS_CONFIG_KEY}={configured!r}; "
                f"using {DEFAULT_REQUESTS_PER_SECOND} requests/s"
            )
            rps = DEFAULT_REQUESTS_PER_SECOND
        return 1.0 / rps if rps > 0 else 0.0

    # ── lazily built collaborators ────────────────────────────────────

    @property
    def session(self) -> Any:
        """The HTTP session, created on first use when not injected."""
        if self._session is None:
            with self._lazy_lock:
                if self._session is None:
                    session = requests.Session()
                    session.headers.update(
                        {"User-Agent": USER_AGENT, "Accept": "application/json"}
                    )
                    self._session = session
        return self._session

    @property
    def cache(self) -> LiteratureCache:
        """The shared literature cache, opened on first use when not injected."""
        if self._cache is None:
            with self._lazy_lock:
                if self._cache is None:
                    self._cache = LiteratureCache(
                        resolve_cache_path(self.get_config_value(CACHE_PATH_CONFIG_KEY))
                    )
        return self._cache

    # ── transport ─────────────────────────────────────────────────────

    def _cache_key(self, params: Dict[str, Any]) -> str:
        """Stable cache key for a request: the URL with sorted query params."""
        encoded = urlencode(sorted((key, str(value)) for key, value in params.items()))
        return f"{self.search_url}?{encoded}"

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return the response JSON for ``params``, from cache when possible."""
        key = self._cache_key(params)
        cached = self.cache.get(HTTP_CACHE_TABLE, key)
        if cached is not None:
            self.log_debug(f"Europe PMC cache hit: {key}")
            return cached

        data = self._fetch(params)
        self.cache.put(HTTP_CACHE_TABLE, key, data)
        return data

    def _fetch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Perform one throttled HTTP request, retrying 429 and 5xx."""
        for attempt in range(1, self.max_tries + 1):
            self._throttle.wait(self.min_interval, self._clock, self._sleep)
            response = self.session.get(
                self.search_url, params=params, timeout=self.timeout
            )
            status = getattr(response, "status_code", None)
            retryable = status == 429 or (status is not None and 500 <= status < 600)
            if retryable and attempt < self.max_tries:
                backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                self.log_warning(
                    f"Europe PMC returned HTTP {status} (attempt {attempt}/"
                    f"{self.max_tries}); retrying in {backoff:.1f}s"
                )
                self._sleep(backoff)
                continue
            # Non-retryable, or the retry budget is spent: let a bad status
            # raise and otherwise hand back the payload.
            response.raise_for_status()
            return response.json()
        raise RuntimeError("unreachable: retry loop exhausted without a result")

    # ── result shaping ────────────────────────────────────────────────

    @staticmethod
    def _parse_hit(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Reduce one Europe PMC ``core`` result to the fields we publish."""
        journal_info = raw.get("journalInfo") or {}
        journal = (journal_info.get("journal") or {}).get("title") or raw.get(
            "journalTitle"
        )
        year_value = raw.get("pubYear") or journal_info.get("yearOfPublication")
        year: Optional[int] = None
        if year_value is not None:
            digits = str(year_value).strip()[:4]
            if digits.isdigit():
                year = int(digits)
        return {
            "pmid": raw.get("pmid"),
            "pmcid": raw.get("pmcid"),
            "doi": raw.get("doi"),
            "title": raw.get("title"),
            "journal": journal,
            "year": year,
            "abstract": raw.get("abstractText"),
            "source": raw.get("source"),
        }

    # ── public API ────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> List[Dict[str, Any]]:
        """Run a Europe PMC query and return up to ``max_results`` hits.

        Args:
            query: Europe PMC query string (see :func:`alias_query`).
            page_size: Results per request, capped at 1000 by the service.
            max_results: Stop after this many hits.

        Returns:
            Hit dicts with keys ``pmid``, ``pmcid``, ``doi``, ``title``,
            ``journal``, ``year``, ``abstract`` and ``source``.
        """
        hits: List[Dict[str, Any]] = []
        if not query or max_results <= 0:
            return hits

        cursor = "*"
        seen_cursors = {cursor}
        while len(hits) < max_results:
            remaining = max_results - len(hits)
            params = {
                "query": query,
                "format": "json",
                "resultType": "core",
                "pageSize": max(1, min(page_size, remaining, MAX_PAGE_SIZE)),
                "cursorMark": cursor,
            }
            data = self._request(params)
            results = ((data or {}).get("resultList") or {}).get("result") or []
            for raw in results:
                hits.append(self._parse_hit(raw))
                if len(hits) >= max_results:
                    break

            next_cursor = (data or {}).get("nextCursorMark")
            if not results or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        self.log_info(f"Europe PMC query returned {len(hits)} hits: {query}")
        return hits

    def get(self, ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Look up publication records for a list of ids.

        Ids are normalised, de-duplicated, and OR-ed
        :data:`IDS_PER_REQUEST` at a time into a single search.

        Args:
            ids: PMIDs, PMCIDs and/or DOIs in any form
                :func:`normalise_paper_id` accepts.

        Returns:
            Canonical id -> record.  Each record is keyed by its own
            canonical id and, when the caller asked for the paper under a
            different canonical id (a DOI for a record that also has a PMID),
            under that id as well -- both keys hold the same record.
        """
        canonical: List[str] = []
        for raw in ids or []:
            identifier = normalise_paper_id(raw)
            if identifier is None:
                self.log_warning(f"Skipping unrecognised publication id: {raw!r}")
            elif identifier not in canonical:
                canonical.append(identifier)

        found: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(canonical), IDS_PER_REQUEST):
            batch = canonical[start:start + IDS_PER_REQUEST]
            clauses = [
                clause
                for clause in (_id_query_clause(entry) for entry in batch)
                if clause
            ]
            if not clauses:
                continue
            # Allow headroom: one id can match more than one source record
            # (a MED record and its PMC counterpart).
            budget = max(2 * len(batch), 25)
            for hit in self.search(
                " OR ".join(clauses),
                page_size=min(budget, MAX_PAGE_SIZE),
                max_results=budget,
            ):
                tokens = _record_tokens(hit)
                if not tokens:
                    continue
                found.setdefault(tokens[0], hit)
                for requested in batch:
                    if requested in tokens:
                        found.setdefault(requested, hit)
        return found

    def resolve(self, id: str) -> Optional[Dict[str, Any]]:
        """Resolve a single publication id to its record.

        Args:
            id: A PMID, PMCID or DOI in any accepted form.

        Returns:
            The record, extended with ``canonical_id`` (its primary canonical
            id) and ``canonical_ids`` (every canonical id it carries), or None
            if the id is unrecognisable or Europe PMC knows nothing about it.
        """
        canonical = normalise_paper_id(id)
        if canonical is None:
            self.log_warning(f"Unrecognised publication id: {id!r}")
            return None

        record = self.get([canonical]).get(canonical)
        if record is None:
            return None

        tokens = _record_tokens(record)
        resolved = dict(record)
        resolved["canonical_id"] = tokens[0] if tokens else canonical
        resolved["canonical_ids"] = tokens or [canonical]
        return resolved


def _id_query_clause(canonical_id: str) -> Optional[str]:
    """Europe PMC query clause matching one canonical publication id."""
    kind, _, value = canonical_id.partition(":")
    if kind == "PMID":
        return f"(EXT_ID:{value} AND SRC:MED)"
    if kind == "PMC":
        return f"(PMCID:{value})"
    if kind == "DOI":
        return f'(DOI:"{_escape_quotes(value)}")'
    return None


# ── Composition-based implementation ─────────────────────────────────────


class EuropePMCClientImpl:
    """Composition-based version of :class:`EuropePMCClient`.

    Holds ``env: SharedEnvUtils`` instead of inheriting, and delegates all
    method calls to an internal client instance.
    """

    def __init__(self, env, **kwargs):
        self._env = env
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        # Carry the environment's configuration across, so the delegate reads
        # the same gene_literature.* settings the env was built with.
        try:
            _kwargs["config"] = env.export_environment().get("config") or {}
        except Exception:
            pass
        _kwargs.update(kwargs)
        self._delegate = EuropePMCClient(**_kwargs)

    @property
    def env(self):
        return self._env

    def __getattr__(self, name):
        # Delegate all attribute access to the client instance.
        return getattr(self._delegate, name)
