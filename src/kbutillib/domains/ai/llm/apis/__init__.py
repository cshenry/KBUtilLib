"""The API types KBUtilLib ships: two HTTP gateways and two headless CLIs.

Each module here implements :class:`~..protocol.ApiType` -- ``http`` for ARGO
and any OpenAI-compatible server, ``cli`` for the headless Claude and Codex
CLIs -- over the shared machinery in ``http_common`` and ``cli_common``.

**Do not import from this package directly.**  Go through
:func:`kbutillib.domains.ai.llm.get_api` (or :func:`~.acomplete`), which
resolves a name to a cached instance, so a call site never learns a provider
class or its constructor and a plug-in kind is reachable the same way as a
built-in one.  Nothing is imported at package-import time: the built-in
registrations in ``llm/__init__.py`` are lazy factories, so ``httpx`` and the
subprocess machinery arrive only when an API type of that kind is actually
used.

``kbutillib.domains.ai.council.backends.{http,http_common,cli,cli_common}``
are thin re-export shims over these modules, kept so every pre-M2 import
path still resolves.
"""
