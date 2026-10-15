"""Council backends: the concrete ways a seat's model is reached.

Each submodule here implements the :class:`~kbutillib.domains.ai.council.protocols.Backend`
protocol -- ``http`` for ARGO and OpenAI-compatible gateways, ``cli`` for the
headless Claude and Codex CLIs, and ``testing`` for the in-process fakes used
by the test suite.  Import them through the lazy loader in
``kbutillib.domains.ai.council`` rather than directly, so nothing heavy is
pulled in until a backend is actually used.
"""
