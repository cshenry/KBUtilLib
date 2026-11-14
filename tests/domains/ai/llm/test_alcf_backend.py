"""The ``alcf`` api type: ALCF Inference Endpoints.

Everything runs over :class:`httpx.MockTransport` (the backend's ``transport``
argument) and local child processes; nothing touches the live service.  The
token values below are obvious fakes.

PROVENANCE: the roster payload and usage object follow
``agent-io/work-records/alcf-live-smoke.md`` (reasoning tokens live at
``usage.completion_tokens_details.reasoning_tokens``).  The go_mf@4 META schema
is not packaged in this repository (the builtin is go_mf@1); it is built here
as the builtin META schema with ``additionalProperties`` true, which is the one
change the v4 spec makes.
"""

from __future__ import annotations

import asyncio
import copy
import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from kbutillib.domains.ai import llm
from kbutillib.domains.ai.council.queries import load_builtin_spec
from kbutillib.domains.ai.council.servable import probe_endpoint
from kbutillib.domains.ai.council.types import (
    SeatSpec,
    canonical_sha256,
    route_fingerprint,
)
from kbutillib.domains.ai.llm.apis import alcf as alcf_mod
from kbutillib.domains.ai.llm.apis.alcf import AlcfBackend, is_strict_compatible
from kbutillib.domains.ai.llm.halt_causes import RESUMABLE_HALT_CAUSES
from kbutillib.domains.ai.llm.types import LLMRequest

TOKEN = "alcf-FAKE-token-0123456789"
OPAQUE = "gAAAAABopaqueSecretShapedString0123456789abcdefXYZ"
ROOT = "https://inference-api.alcf.anl.gov/resource_server"

ROSTER = {
    "clusters": {
        "sophia": {"frameworks": {"vllm": {"models": ["other/sophia-model"]}}},
        "tara": {
            "frameworks": {
                "api": {
                    "models": [
                        "openai/gpt-oss-120b",
                        "google/gemma-4-31B-it-262k",
                    ]
                },
                "vllm": {"models": ["not-this-one"]},
            }
        },
        "metis": {
            "frameworks": {"api": {"models": ["Mistral-Large-3-675B-Instruct-2512"]}}
        },
    }
}

CHAT_OK = {
    "model": "openai/gpt-oss-120b",
    "choices": [
        {"finish_reason": "stop", "message": {"content": '{"picked": []}'}}
    ],
    "usage": {
        "prompt_tokens": 50,
        "completion_tokens": 40,
        "completion_tokens_details": {"reasoning_tokens": 30},
    },
}


@pytest.fixture(autouse=True)
def fresh_cache():
    alcf_mod.clear_token_cache()
    yield
    alcf_mod.clear_token_cache()


def run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def script(tmp_path: Path, name: str, body: str) -> str:
    """A tiny executable python script usable as a token_command."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def options(**extra: Any) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "cluster": "tara",
        "framework": "api",
        "token_command": ["printer-not-used"],
    }
    opts.update(extra)
    return opts


def make(
    handler: Callable[[httpx.Request], httpx.Response],
    opts: Optional[Dict[str, Any]] = None,
    **cfg: Any,
) -> AlcfBackend:
    config = {"name": "alcf-tara", "kind": "alcf", "options": opts or options()}
    config.update(cfg)
    return AlcfBackend(config, transport=httpx.MockTransport(handler))


def with_token(tmp_path: Path, token: str = TOKEN, **extra: Any) -> Dict[str, Any]:
    exe = script(tmp_path, "tok", f"print({token!r})\n")
    return options(token_command=[exe], **extra)


def chat(backend: AlcfBackend, **kw: Any) -> Any:
    req = LLMRequest(model="openai/gpt-oss-120b", system="s", user="u", **kw)

    async def _go() -> Any:
        try:
            return await backend.complete(req)
        finally:
            await backend.aclose()

    return run(_go())


def recorder(body: Any = CHAT_OK, status: int = 200):
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return handler, seen


def proposer_schema() -> Dict[str, Any]:
    return load_builtin_spec("go_mf")["roles"]["proposer"]["schema"]


def meta_v4_schema() -> Dict[str, Any]:
    schema = copy.deepcopy(load_builtin_spec("go_mf")["roles"]["meta"]["schema"])
    schema["additionalProperties"] = True
    return schema


# -- registration -------------------------------------------------------------


def test_registered_and_constructible() -> None:
    assert "alcf" in llm.api_types()
    assert "alcf" in llm.BUILTIN_API_TYPES
    assert list(llm.BUILTIN_API_TYPES) == sorted(llm.BUILTIN_API_TYPES)
    api = llm.get_api("alcf", {"name": "x", "options": options()})
    assert callable(api.complete) and callable(api.effective_params)
    assert "_status_error" not in vars(AlcfBackend)


# -- URL and chat POST --------------------------------------------------------


def test_chat_posts_to_derived_url_with_token(tmp_path: Path) -> None:
    handler, seen = recorder()
    result = chat(make(handler, with_token(tmp_path)))
    assert result.ok and result.schema_enforced is False
    assert len(seen) == 1
    assert str(seen[0].url) == f"{ROOT}/tara/api/v1/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert result.usage["completion_tokens"] == 40


def test_service_root_override_and_explicit_base_url(tmp_path: Path) -> None:
    handler, seen = recorder()
    chat(make(handler, with_token(tmp_path, service_root="http://gw.test/rs/")))
    assert str(seen[0].url) == "http://gw.test/rs/tara/api/v1/chat/completions"
    handler, seen = recorder()
    chat(make(handler, with_token(tmp_path), base_url="http://explicit.test/v1"))
    assert str(seen[0].url) == "http://explicit.test/v1/chat/completions"


def test_seat_top_p_still_reaches_body(tmp_path: Path) -> None:
    handler, seen = recorder()
    chat(make(handler, with_token(tmp_path)), params={"top_p": 0.9})
    assert json.loads(seen[0].content)["top_p"] == 0.9


# -- response_format ----------------------------------------------------------


def test_response_format_present_with_schema(tmp_path: Path) -> None:
    schema = proposer_schema()
    handler, seen = recorder()
    chat(make(handler, with_token(tmp_path)), json_schema=schema)
    rf = json.loads(seen[0].content)["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "kbu_" + canonical_sha256(schema)[:24]
    assert rf["json_schema"]["schema"] == schema
    assert rf["json_schema"]["strict"] is True


def test_response_format_absent_when_flag_false_or_no_schema(tmp_path: Path) -> None:
    handler, seen = recorder()
    chat(
        make(handler, with_token(tmp_path, request_response_format=False)),
        json_schema=proposer_schema(),
    )
    assert "response_format" not in json.loads(seen[0].content)
    handler, seen = recorder()
    chat(make(handler, with_token(tmp_path)))
    assert "response_format" not in json.loads(seen[0].content)


def test_meta_v4_schema_has_no_strict_and_proposer_has(tmp_path: Path) -> None:
    assert is_strict_compatible(proposer_schema())
    assert not is_strict_compatible(meta_v4_schema())
    handler, seen = recorder()
    chat(make(handler, with_token(tmp_path)), json_schema=meta_v4_schema())
    spec = json.loads(seen[0].content)["response_format"]["json_schema"]
    assert "strict" not in spec


def test_strict_gate_rejects_nested_open_object_and_forbidden_constructs() -> None:
    nested = copy.deepcopy(proposer_schema())
    del nested["properties"]["picked"]["items"]["additionalProperties"]
    assert not is_strict_compatible(nested)
    forbidden = copy.deepcopy(proposer_schema())
    forbidden["properties"]["notes"] = {"oneOf": [{"type": "string"}]}
    assert not is_strict_compatible(forbidden)


# -- roster -------------------------------------------------------------------


def roster_backend(handler, tmp_path: Path, **extra: Any) -> AlcfBackend:
    return make(handler, with_token(tmp_path, **extra))


def report(backend: AlcfBackend) -> Dict[str, Any]:
    async def _go() -> Any:
        try:
            return await backend.models_report()
        finally:
            await backend.aclose()

    return run(_go())


def test_models_report_selects_this_clusters_list(tmp_path: Path) -> None:
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ROSTER)

    got = report(roster_backend(handler, tmp_path))
    assert got["outcome"] == "answered"
    assert got["models"] == [
        {"id": "openai/gpt-oss-120b"},
        {"id": "google/gemma-4-31B-it-262k"},
    ]
    assert [str(r.url) for r in seen] == [f"{ROOT}/list-endpoints"]
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert "models_report" in vars(AlcfBackend)


def test_models_report_missing_cluster_or_framework_is_answered_empty(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ROSTER)

    for cluster, framework in (("nowhere", "api"), ("tara", "nofw")):
        opts = with_token(tmp_path)
        opts.update(cluster=cluster, framework=framework)
        got = report(make(handler, opts))
        assert (got["outcome"], got["models"]) == ("answered", [])


@pytest.mark.parametrize(
    "status,outcome",
    [
        (404, "not_implemented"),
        (405, "not_implemented"),
        (401, "credential_rejected"),
        (403, "credential_rejected"),
        (500, "http_error"),
    ],
)
def test_models_report_status_outcomes(tmp_path: Path, status: int, outcome: str) -> None:
    got = report(
        roster_backend(lambda r: httpx.Response(status, json={}), tmp_path)
    )
    assert (got["outcome"], got["models"]) == (outcome, [])


def test_models_report_unreachable_and_malformed(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert report(roster_backend(boom, tmp_path))["outcome"] == "unreachable"
    for body in (b"not json", b'{"clusters": []}', b'[1]'):
        got = report(
            roster_backend(lambda r, b=body: httpx.Response(200, content=b), tmp_path)
        )
        assert got["outcome"] == "malformed"
    bad = {"clusters": {"tara": {"frameworks": {"api": {"models": ["ok", 7]}}}}}
    got = report(roster_backend(lambda r: httpx.Response(200, json=bad), tmp_path))
    assert got["outcome"] == "malformed"


def test_models_report_token_failure_is_credential_rejected_without_request() -> None:
    called: List[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, json=ROSTER)

    got = report(make(handler, options(token_command=["definitely-not-a-real-binary-xyz"])))
    assert got["outcome"] == "credential_rejected" and not called


def test_probe_endpoint_reads_the_alcf_roster_not_models(tmp_path: Path) -> None:
    seen: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/list-endpoints"):
            return httpx.Response(200, json=ROSTER)
        return httpx.Response(404, json={})

    backend = make(handler, with_token(tmp_path))
    probe = run(probe_endpoint("alcf-tara", "alcf", backend))
    run(backend.aclose())
    assert probe["listing_outcome"] == "answered"
    assert seen == ["/resource_server/list-endpoints"]
    assert not any(p.endswith("/v1/models") for p in seen)


# -- token sources ------------------------------------------------------------


def auth_failure(opts: Dict[str, Any]) -> Any:
    handler, seen = recorder()
    result = chat(make(handler, opts))
    assert not seen, "no request may be sent without a token"
    return result


def test_token_command_nonzero_missing_empty(tmp_path: Path) -> None:
    bad = script(tmp_path, "bad", f"import sys\nsys.stderr.write({OPAQUE!r})\nsys.exit(7)\n")
    result = auth_failure(options(token_command=[bad]))
    assert not result.ok and result.error.kind == "auth"
    assert result.error.halts_endpoint is True
    assert result.error.halt_cause is None
    msg = result.error.message
    assert "bad" in msg and "status 7" in msg and "alcf-tokens login inference" in msg
    assert OPAQUE not in msg and str(tmp_path) not in msg

    missing = auth_failure(options(token_command=["definitely-not-a-real-binary-xyz"]))
    assert missing.error.kind == "auth" and "definitely-not-a-real-binary-xyz" in missing.error.message

    empty = script(tmp_path, "empty", "pass\n")
    assert auth_failure(options(token_command=[empty])).error.kind == "auth"


def test_token_command_timeout_kills_child(tmp_path: Path) -> None:
    marker = tmp_path / "survived"
    slow = script(
        tmp_path,
        "slow",
        f"""
        import time, pathlib
        time.sleep(3)
        pathlib.Path({str(marker)!r}).write_text("x")
        """,
    )
    result = auth_failure(options(token_command=[slow], token_timeout_s=0.5))
    assert result.error.kind == "auth" and "timed out" in result.error.message
    import time

    time.sleep(3.5)
    assert not marker.exists(), "timed-out child must have been killed"


def test_token_import_with_local_callable(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "alcf_tok_mod.py").write_text(
        f"def get(scope):\n    print('noise on stdout')\n    return {TOKEN!r} + '-' + scope\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    handler, seen = recorder()
    opts = options(token_import="alcf_tok_mod:get")
    del opts["token_command"]
    result = chat(make(handler, opts))
    assert result.ok
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}-inference"


def test_token_import_failures_do_not_leak(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "alcf_bad_mod.py").write_text(
        f"def get(scope):\n    raise RuntimeError({OPAQUE!r})\n"
        f"def blank(scope):\n    return ''\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    for target, needle in (
        ("alcf_bad_mod:get", "raised"),
        ("alcf_bad_mod:blank", "empty"),
        ("alcf_no_such_mod:get", "import failed"),
    ):
        opts = options(token_import=target)
        del opts["token_command"]
        result = auth_failure(opts)
        msg = result.error.message
        assert result.error.kind == "auth" and result.error.halt_cause is None
        assert needle in msg and "status" in msg and OPAQUE not in msg
        assert target.split(":")[0] in msg


def test_token_import_timeout(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "alcf_hang_mod.py").write_text(
        "import time\ntime.sleep(30)\ndef get(scope):\n    return 'x'\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    opts = options(token_import="alcf_hang_mod:get", token_timeout_s=0.5)
    del opts["token_command"]
    assert "timed out" in auth_failure(opts).error.message


def test_both_sources_configured_is_an_auth_error() -> None:
    opts = options(token_import="m:f")
    result = auth_failure(opts)
    assert result.error.kind == "auth" and result.error.halt_cause is None


def test_token_failure_halt_cause_is_not_resumable() -> None:
    result = auth_failure(options(token_command=["definitely-not-a-real-binary-xyz"]))
    assert result.error.halts_endpoint is True
    assert result.error.halt_cause is None
    assert result.error.halt_cause not in RESUMABLE_HALT_CAUSES


# -- cache --------------------------------------------------------------------


def counting_source(tmp_path: Path) -> tuple:
    counter = tmp_path / "count"
    exe = script(
        tmp_path,
        "counted",
        f"""
        import pathlib
        p = pathlib.Path({str(counter)!r})
        n = int(p.read_text()) if p.exists() else 0
        p.write_text(str(n + 1))
        print("tok-%d" % (n + 1))
        """,
    )
    return exe, counter


def test_cache_resolves_once_and_reresolves_after_expiry(tmp_path: Path, monkeypatch) -> None:
    exe, counter = counting_source(tmp_path)
    now = [1000.0]
    monkeypatch.setattr(alcf_mod, "_clock", lambda: now[0])
    backend = make(lambda r: httpx.Response(200, json={}), options(token_command=[exe], token_ttl_s=100))
    assert backend._bearer() == ("tok-1", None)
    assert backend._bearer() == ("tok-1", None)
    assert counter.read_text() == "1"
    now[0] += 99
    assert backend._bearer()[0] == "tok-1"
    now[0] += 2
    assert backend._bearer()[0] == "tok-2"
    assert counter.read_text() == "2"


def test_two_instances_sharing_a_source_invoke_it_once(tmp_path: Path) -> None:
    exe, counter = counting_source(tmp_path)
    a = make(lambda r: httpx.Response(200, json={}), options(token_command=[exe]), name="one")
    b = make(
        lambda r: httpx.Response(200, json={}),
        options(token_command=[exe], cluster="metis"),
        name="two",
        account="other",
    )
    assert a._bearer()[0] == b._bearer()[0] == "tok-1"
    assert counter.read_text() == "1"


def test_failures_are_not_cached(tmp_path: Path) -> None:
    flag = tmp_path / "ok"
    exe = script(
        tmp_path,
        "flaky",
        f"""
        import pathlib, sys
        if not pathlib.Path({str(flag)!r}).exists():
            sys.exit(1)
        print("good")
        """,
    )
    backend = make(lambda r: httpx.Response(200, json={}), options(token_command=[exe]))
    assert backend._bearer()[0] is None
    flag.write_text("x")
    assert backend._bearer() == ("good", None)


# -- route identity -----------------------------------------------------------


def seat() -> SeatSpec:
    return SeatSpec.model_construct(
        seat_name="p1",
        endpoint="alcf-tara",
        endpoint_kind="alcf",
        model="openai/gpt-oss-120b",
        canonical_model="gpt-oss-120b",
        max_output_tokens=4000,
        temperature=0,
        timeout_s=180,
        extra={},
        verified=True,
    )


def test_route_fingerprint_moves_with_flag_not_with_schema(tmp_path: Path) -> None:
    def fp(flag: bool, schema: Optional[Dict[str, Any]]) -> str:
        cfg = {"kind": "alcf", "options": options(request_response_format=flag)}
        backend = AlcfBackend({"name": "alcf-tara", **cfg})
        req = LLMRequest(model="openai/gpt-oss-120b", json_schema=schema)
        return route_fingerprint(seat(), cfg, backend.effective_params(req))

    on = fp(True, None)
    assert fp(False, None) != on
    assert fp(True, proposer_schema()) == on
    assert fp(True, meta_v4_schema()) == on
