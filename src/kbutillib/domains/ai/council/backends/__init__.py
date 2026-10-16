"""Council backends -- now thin shims over the shared LLM wrapper.

M2.1 moved the four real ways of reaching a model out of the council and into
:mod:`kbutillib.domains.ai.llm`, so that ONE wrapper owns every model call in
KBUtilLib and a non-council caller can use them without importing a council.
``http``, ``http_common``, ``cli`` and ``cli_common`` here are re-export
shims over ``llm.apis.*``, kept so every pre-M2 import path still
resolves; ``testing`` still holds the in-process fakes, because they are test
scaffolding rather than a packaged way of reaching a provider.

New code should not import a provider class from anywhere.  Ask the wrapper::

    from kbutillib.domains.ai import llm

    result = await llm.acomplete(request, api_type="argo", endpoint=cfg)

which also means a plug-in kind declared by another distribution is reachable
exactly the same way as a built-in one.
"""
