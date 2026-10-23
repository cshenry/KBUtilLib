"""Domain-neutral, closed-menu LLM council.

A council asks N>=2 independent PROPOSER models to pick ids from a closed
candidate MENU for one item, has a CRITIC audit the vote table, and invokes a
META adjudicator only when the proposers disagree or the critic objects.  The
protocol is a port of Jose Faria's CONCORDIA GO council V3, generalised so the
engine knows nothing about GO (or any other vocabulary) -- that lives in a
query type.

Work is organised as JOBS (a batch of items against a saved query type) whose
model invocations are CALL rows in a STORE.  The call rows ARE the queue: a
worker (``kbu council run``, or a per-user daemon) claims pending calls, runs
them through the submitting user's own backends, and advances each item
proposers -> critic -> meta (only if needed) -> verdict.

Lazy loader: nothing is imported at ``import kbutillib`` time, exactly as in
the parent ``kbutillib.domains.ai`` namespace.  Every public name of the
package is mapped below, including names whose modules arrive in later
milestones -- lazy loading means an unwritten module only fails when someone
actually touches one of its names, and mapping them up front means no later
task has to edit this file.

Example::

    from kbutillib.domains.ai.council import load_council_config

    cfg = load_council_config()
    roster = cfg.roster("mixed-subscription")
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    # types
    "MenuEntry",
    "CouncilTask",
    "SeatSpec",
    "Roster",
    "CompletionRequest",
    "CompletionResult",
    "Pick",
    "Ballot",
    "CouncilVerdict",
    "CouncilError",
    "CouncilException",
    "ConfigError",
    "RosterError",
    "CallError",
    "TransportError",
    "CallTimeout",
    "QuotaExhausted",
    "AuthError",
    "BillingGuardRefused",
    "ModelUnavailable",
    "InvalidOutput",
    "BudgetExhausted",
    "BudgetUsageUnknown",
    "SeatUnavailable",
    "ERROR_KINDS",
    "as_council_error",
    "canonical_json",
    "canonical_sha256",
    "entity_hash",
    "data_hash",
    "prompt_hash",
    "route_fingerprint",
    "bench_fingerprint",
    "default_reuse_namespace",
    "seat_generation_params",
    # quote
    "SeatTokenFit",
    "TokenModel",
    "fit_token_model",
    "item_prompt_chars",
    # approval (stage 3's artifact and the gate stage 4 calls)
    "Approval",
    "ApprovalRefused",
    "approve",
    "validate_approval",
    # protocols
    "Backend",
    "QueryRenderer",
    "CouncilStore",
    # config
    "load_council_config",
    "load_legacy_model_inference",
    "CouncilConfig",
    "EndpointConfig",
    "ModelConfig",
    "ProfileConfig",
    "DEFAULT_CANONICAL",
    "DEFAULT_CANONICAL_VERSION",
    # backends
    "FakeBackend",
    "ReplayBackend",
    "ArgoBackend",
    "OpenAICompatibleBackend",
    "ClaudeCliBackend",
    "CodexCliBackend",
    # store
    "open_store",
    "SqliteStore",
    "ExecutionLock",
    "LockHeld",
    # queries / engine / runtime
    "QueryType",
    "QueryRegistry",
    "decide",
    "CouncilWorker",
    "evaluate",
    "current_user",
    "CouncilDaemon",
    "ModelRegistry",
    "GpuInventory",
    "ModelLifecycle",
    "ConcordiaServiceClient",
]

_MODULE_MAP: dict[str, str] = {
    # -- types ----------------------------------------------------------
    "MenuEntry": "types",
    "CouncilTask": "types",
    "SeatSpec": "types",
    "Roster": "types",
    "CompletionRequest": "types",
    "CompletionResult": "types",
    "Pick": "types",
    "Ballot": "types",
    "CouncilVerdict": "types",
    "CouncilError": "types",
    "CouncilException": "types",
    "ConfigError": "types",
    "RosterError": "types",
    "CallError": "types",
    "TransportError": "types",
    "CallTimeout": "types",
    "QuotaExhausted": "types",
    "AuthError": "types",
    "BillingGuardRefused": "types",
    "ModelUnavailable": "types",
    "InvalidOutput": "types",
    "BudgetExhausted": "types",
    "BudgetUsageUnknown": "types",
    "SeatUnavailable": "servable",
    "ERROR_KINDS": "types",
    "as_council_error": "types",
    "canonical_json": "types",
    "canonical_sha256": "types",
    "entity_hash": "types",
    "data_hash": "types",
    "prompt_hash": "types",
    "route_fingerprint": "types",
    "bench_fingerprint": "types",
    "default_reuse_namespace": "types",
    "seat_generation_params": "types",
    # -- quote (the token model a cost quote is priced from) -------------
    "SeatTokenFit": "quote",
    "TokenModel": "quote",
    "fit_token_model": "quote",
    "item_prompt_chars": "quote",
    # -- approval (the ceiling and the six checks before a run spends) ---
    "Approval": "approval",
    "ApprovalRefused": "approval",
    "approve": "approval",
    "validate_approval": "approval",
    # -- protocols ------------------------------------------------------
    "Backend": "protocols",
    "QueryRenderer": "protocols",
    "CouncilStore": "protocols",
    # -- config ---------------------------------------------------------
    "load_council_config": "config",
    "load_legacy_model_inference": "config",
    "CouncilConfig": "config",
    "EndpointConfig": "config",
    "ModelConfig": "config",
    "ProfileConfig": "config",
    "DEFAULT_CANONICAL": "config",
    "DEFAULT_CANONICAL_VERSION": "config",
    # -- backends -------------------------------------------------------
    "FakeBackend": "backends.testing",
    "ReplayBackend": "backends.testing",
    "ArgoBackend": "backends.http",
    "OpenAICompatibleBackend": "backends.http",
    "ClaudeCliBackend": "backends.cli",
    "CodexCliBackend": "backends.cli",
    # -- store ----------------------------------------------------------
    "open_store": "store",
    "SqliteStore": "store.sqlite",
    "ExecutionLock": "execlock",
    "LockHeld": "execlock",
    # -- queries --------------------------------------------------------
    "QueryType": "queries",
    "QueryRegistry": "queries",
    # -- engine ---------------------------------------------------------
    "decide": "engine.decide",
    "CouncilWorker": "engine.worker",
    "evaluate": "engine.worker",
    # -- runtime --------------------------------------------------------
    "current_user": "identity",
    "CouncilDaemon": "daemon",
    "ModelRegistry": "models",
    "GpuInventory": "models",
    "ModelLifecycle": "models",
    "ConcordiaServiceClient": "service_client",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", package=__name__)
    obj = getattr(module, name)
    globals()[name] = obj
    return obj
