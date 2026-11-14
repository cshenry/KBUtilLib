"""JSON-object extraction and the reasoning-budget truncation report.

Two shared-plumbing changes in ``llm/apis/http_common.py``:

1. ``extract_json_object`` no longer takes the OUTERMOST ``{...}`` span.  It
   enumerates top-level objects (``scan_json_objects``); two or more is
   ambiguous, returns ``None`` and is reported with the count.
2. ``finish_reason == "length"`` with ``content`` null is a truncation.

SYNTHETIC FIXTURE -- READ THIS BEFORE QUOTING IT.  ``TWO_OBJECT_REPLY`` below
is a CONSTRUCTED reply with the documented SHAPE of the Mistral-Large-3-675B
failure (a complete valid object, the prose "**Correction:** The correct JSON
... is:", then a second object, selecting GO:0016757 and GO:0008955).  It is
NOT a verbatim historical reply: the real 1,669-character reply is not
recoverable.  Nothing here reproduces real evidence.

Backend tests drive the real ``OpenAICompatibleBackend`` with only
``httpx.MockTransport`` substituted, through its ``transport`` constructor
argument; no hook is monkeypatched.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from typing import Any, Dict, Optional

import httpx
import pytest

from kbutillib.domains.ai.llm.apis.http import OpenAICompatibleBackend
from kbutillib.domains.ai.llm.apis.http_common import (
    EMPTY_OBJECT_LABEL,
    MAX_CANDIDATE_LABELS,
    MAX_KEY_LABEL_CHARS,
    JsonScan,
    describe_ambiguity,
    extract_json_object,
    scan_json_objects,
    strip_fences,
    strip_think,
)
from kbutillib.domains.ai.llm.types import LLMRequest

# SYNTHETIC -- see module docstring.
TWO_OBJECT_REPLY = (
    '{"picked": [{"go_id": "GO:0016757", "why": "glycosyltransferase"}], '
    '"overall_confidence": "high"}\n\n'
    "**Correction:** The correct JSON for this protein is:\n\n"
    '{"picked": [{"go_id": "GO:0008955", "why": "peptidoglycan glycosyltransferase"}], '
    '"overall_confidence": "high"}'
)


def run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def old_extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """The OUTERMOST-span implementation this change replaced, verbatim."""
    candidate = strip_fences(text)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# Replies on which the OLD implementation succeeded.
OLD_SUCCEEDS = [
    ("plain", '{"a": 1}'),
    ("nested", '{"picked": [{"go_id": "GO:1"}], "meta": {"k": {"z": 2}}}'),
    ("inside_list", '[{"go_id": "GO:1"}]'),
    ("prose_before", 'Here is my answer: {"a": 1}'),
    ("prose_after", '{"a": 1}\nHope that helps!'),
    ("fenced", '```json\n{"a": 1, "b": [1, 2]}\n```'),
    ("after_think", strip_think('<think>draft {"x": 0}</think>{"x": 1}')),
    ("unicode", '{"name": "café αβγ ☃", "n": 1}'),
    ("brace_in_string", '{"s": "a } and { b", "n": 2}'),
    ("whitespace_padded", '  \n {"a": {"b": {}}} \n '),
]


class TestSameAnswerAsOutermostSpan:
    """SAFETY PROPERTY: text the old parser accepted yields the identical dict."""

    def test_table_is_large_enough(self) -> None:
        assert len(OLD_SUCCEEDS) >= 8

    @pytest.mark.parametrize("name,text", OLD_SUCCEEDS, ids=[c[0] for c in OLD_SUCCEEDS])
    def test_identical_dict(self, name: str, text: str) -> None:
        old = old_extract_json_object(text)
        assert old is not None, "table row must be one the OLD implementation accepted"
        scan = scan_json_objects(text)
        assert scan.count == 1
        assert scan.obj == old
        assert extract_json_object(text) == old


class TestScanRules:
    def test_truncated_ballot_does_not_yield_inner_object(self) -> None:
        text = '{"picked": [{"go_id":"GO:1"}]'
        assert old_extract_json_object(text) is None
        assert extract_json_object(text) is None
        assert scan_json_objects(text).count == 0

    def test_two_objects_are_ambiguous_not_guessed(self) -> None:
        scan = scan_json_objects(TWO_OBJECT_REPLY)
        assert scan.count == 2
        assert scan.obj is None
        assert scan.ambiguous
        assert extract_json_object(TWO_OBJECT_REPLY) is None

    def test_no_object_counts_zero(self) -> None:
        for text in ("", "no braces", "{not json at all}"):
            scan = scan_json_objects(text)
            assert scan.count == 0 and scan.obj is None and not scan.ambiguous

    def test_count_is_returned_not_stored(self) -> None:
        a = scan_json_objects('{"a": 1} {"b": 2} {"c": 3}')
        b = scan_json_objects('{"a": 1}')
        assert (a.count, b.count) == (3, 1)  # no state leaks between calls
        assert isinstance(a, JsonScan)
        with pytest.raises(Exception):
            a.count = 9  # type: ignore[misc]  # frozen
        import kbutillib.domains.ai.llm.apis.http_common as hc

        assert not any("count" in n.lower() and not n.startswith("_") and n.islower()
                       and isinstance(getattr(hc, n), int) for n in dir(hc))

    def test_known_limitation_malformed_brace_between_two_objects(self) -> None:
        # KNOWN LIMITATION (documented, not fixed): the scan stops at the
        # malformed brace between the objects, so the competing second ballot
        # is never seen and the FIRST object is accepted.  Continuing past the
        # brace would need another rule and would reopen the nested-object
        # salvage that the first-brace rule refuses.
        text = (
            '{"picked":[]} explanation {not JSON} **Correction:** '
            '{"picked":[{"go_id":"GO:1"}]}'
        )
        scan = scan_json_objects(text)
        assert scan.count == 1
        assert scan.obj == {"picked": []}

    def test_one_in_list_accepted_two_in_list_rejected(self) -> None:
        assert extract_json_object('[{"id": "GO:1"}]') == {"id": "GO:1"}
        assert extract_json_object('[{"a": 1}, {"b": 2}]') is None


class TestKeyLabels:
    def test_names_first_key_of_each_object(self) -> None:
        msg = describe_ambiguity(scan_json_objects(TWO_OBJECT_REPLY))
        assert "2 JSON objects" in msg
        assert msg.count("'picked'") == 2
        assert "nothing was guessed" in msg and "ambiguous" in msg

    def test_empty_object_has_a_label(self) -> None:
        scan = scan_json_objects('{} {"x": 1}')
        assert scan.labels == (EMPTY_OBJECT_LABEL, "'x'")

    def test_hostile_keys_are_bounded_and_sanitised(self) -> None:
        huge = "A" * 100_000
        evil = "line1\nline2\x00\x1b[31m‮"
        text = json.dumps({huge: 1}) + " " + json.dumps({evil: 2})
        text += " " + " ".join('{"k%d": 1}' % i for i in range(1000))
        scan = scan_json_objects(text)
        assert scan.count == 1002
        assert len(scan.labels) == MAX_CANDIDATE_LABELS
        for label in scan.labels:
            assert len(label) <= MAX_KEY_LABEL_CHARS + 8
            assert all(32 <= ord(ch) < 127 for ch in label)
        msg = describe_ambiguity(scan)
        assert len(msg) < 500
        assert "1002 JSON objects" in msg and msg.endswith("nothing was guessed")


def _backend(content: Any, finish_reason: Optional[str] = "stop") -> OpenAICompatibleBackend:
    choice: Dict[str, Any] = {"index": 0, "message": {"role": "assistant", "content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    envelope = {"id": "x", "model": "m", "choices": [choice],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=envelope))
    return OpenAICompatibleBackend(
        {"name": "t", "base_url": "http://127.0.0.1:9/v1"}, transport=transport
    )


def _call(content: Any, finish_reason: Optional[str] = "stop", ceiling: int = 777) -> Any:
    b = _backend(content, finish_reason)
    try:
        return run(b.complete(LLMRequest(model="m", user="q", max_output_tokens=ceiling)))
    finally:
        run(b.client.aclose())


class TestCompletePaths:
    def test_two_object_reply_is_ambiguous_invalid_output(self) -> None:
        result = _call(TWO_OBJECT_REPLY)
        assert result.ok is False
        assert result.error.kind == "invalid_output"
        assert "2 JSON objects" in result.error.message
        assert "ambiguous" in result.error.message
        assert "nothing was guessed" in result.error.message
        assert result.parsed is None and result.truncated is False

    def test_no_object_message_unchanged(self) -> None:
        result = _call("just prose")
        assert result.error.kind == "invalid_output"
        assert "reply contained no JSON object" in result.error.message

    def test_single_object_still_ok(self) -> None:
        result = _call('prose {"a": 1} more')
        assert result.ok and result.parsed == {"a": 1}

    def test_null_content_with_length_is_truncation(self) -> None:
        result = _call(None, "length", ceiling=777)
        assert result.ok is False and result.truncated is True
        assert result.error.kind == "invalid_output"
        assert "token ceiling (777)" in result.error.message

    def test_null_content_without_finish_reason_is_no_content(self) -> None:
        result = _call(None, None)
        assert result.ok is False and result.truncated is False
        assert result.error.kind == "invalid_output"
        assert "reply carried no choices[0].message.content" in result.error.message

    def test_null_content_with_stop_is_no_content(self) -> None:
        result = _call(None, "stop")
        assert result.truncated is False
        assert "no choices[0].message.content" in result.error.message

    def test_non_null_length_truncation_unchanged(self) -> None:
        result = _call('{"a": ', "length")
        assert result.truncated is True and "token ceiling" in result.error.message


@pytest.mark.skipif(importlib.util.find_spec("anthropic") is None, reason="anthropic SDK absent")
def test_anthropic_diagnostic_names_the_count() -> None:
    """anthropic.py reports the ambiguous count the same way (needs httpx2)."""
    httpx2 = pytest.importorskip("httpx2")
    from kbutillib.domains.ai.llm.apis.anthropic import AnthropicBackend

    envelope = {
        "id": "msg_01", "type": "message", "role": "assistant", "model": "claude-x",
        "content": [{"type": "text", "text": TWO_OBJECT_REPLY}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json=envelope))
    b = AnthropicBackend({"api_key": "sk-ant-test-NOT-A-REAL-KEY-0123456789"}, transport=transport)
    try:
        result = run(b.complete(LLMRequest(model="claude-haiku-4-5", user="q", temperature=None)))
    finally:
        run(b.aclose())
    assert result.ok is False and result.error.kind == "invalid_output"
    assert "2 JSON objects" in result.error.message
