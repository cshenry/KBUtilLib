############################################################
#
# Vendored KBase NarrativeService client (kbaseapps/NarrativeService).
#
# NarrativeService is a DYNAMIC service -- it has no fixed
# "services/..." URL.  Unlike the other vendored clients in this
# package (WorkspaceClient.py, execution_engine2Client.py), this
# client is constructed with the ServiceWizard's URL and
# ``lookup_url=True`` so ``BaseClient._get_service_url`` resolves the
# NarrativeService URL on every call via a ``ServiceWizard.get_service_status``
# round-trip (see ``installed_clients/baseclient.py``).  No new static
# endpoint is added to ``kbase_endpoints.py`` -- the existing
# ``service_wizard`` suffix is reused as the entry point.
#
# Only the one method KBUtilLib's narrative-provenance feature needs
# (``create_new_narrative``) is wrapped; this is a small hand-written
# client, not a full SDK-compiler dump like the other vendored clients.
#
############################################################

try:
    # baseclient and this client are in a package
    from .baseclient import BaseClient as _BaseClient  # @UnusedImport
except ImportError:
    # no they aren't
    from baseclient import BaseClient as _BaseClient  # @Reimport


class NarrativeService:
    """Thin client for the dynamic ``NarrativeService`` module.

    Args:
        url: The **ServiceWizard** URL (e.g.
            ``kbase_endpoints.service_url("service_wizard", kb_version)``),
            NOT the NarrativeService URL itself -- ``BaseClient`` resolves
            the real dynamic-service URL lazily via ServiceWizard on each
            call (``lookup_url=True``).
        token: KBase auth token.
        service_version: NarrativeService release channel to resolve
            (``"release"`` / ``"beta"`` / ``"dev"``, or a git hash).
    """

    def __init__(
        self,
        url=None,
        token=None,
        timeout=30 * 60,
        service_version="release",
        trust_all_ssl_certificates=False,
    ):
        if url is None:
            raise ValueError("A url is required")
        self._service_ver = service_version
        self._client = _BaseClient(
            url,
            timeout=timeout,
            token=token,
            trust_all_ssl_certificates=trust_all_ssl_certificates,
            lookup_url=True,
        )

    def create_new_narrative(self, params, context=None):
        """Create a fresh Workspace + Narrative.

        Args:
            params: ``CreateNewNarrativeParams`` dict. Recognised keys
                (per the live ``NarrativeService.spec``): ``markdown``
                (the Tier-1 intro body), ``title``, and
                ``includeIntroCell`` (camelCase on the wire -- a
                boolean/int flag for whether to seed an intro cell).
                There is intentionally no ``workspace_id`` /
                ``wsid`` param -- the real service always creates a
                brand-new Workspace; it cannot attach a Narrative to
                an existing one.

        Returns:
            The raw ``CreateNewNarrativeOutput`` dict:
            ``{"workspaceInfo": <10-tuple>, "narrativeInfo": <18-elem
            extended object-info>}``. ``workspaceInfo[0]`` is the new
            workspace id; ``narrativeInfo[0]`` / ``narrativeInfo[4]``
            are the narrative object's id / version.
        """
        return self._client.call_method(
            "NarrativeService.create_new_narrative",
            [params],
            self._service_ver,
            context,
        )
