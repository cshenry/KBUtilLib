"""The packaged default config carries a COMMENTED ALCF block and is otherwise
byte-identical to the base commit (64f997f), so adding the block cannot have
moved any live entry."""

from __future__ import annotations

import hashlib
from importlib.resources import files

import yaml

from kbutillib.domains.ai.council.config import _packaged_default, load_council_config

BEGIN = "  # >>> BEGIN ALCF BLOCK"
END = "# <<< END ALCF BLOCK <<<\n\n"
#: sha256 of default_config.yaml at base commit 64f997f7bc50538cb2d2bec59ff4003153a55c50.
BASE_SHA256 = "97bbf3aa3f130b1c84f3fb19a06d0e1f25bd6431b5840fd7701bf88bbf12c330"


def _text() -> str:
    return files("kbutillib.domains.ai.council").joinpath("default_config.yaml").read_text()


def _split():
    text = _text()
    i = text.index(BEGIN)
    j = text.index(END) + len(END)
    return text[:i] + text[j:], text[i:j]


def test_packaged_default_still_loads():
    config = load_council_config(_packaged_default())
    assert config.default_profile == "mixed-subscription"
    assert {"argo", "mixed-subscription"} <= set(config.profiles)


def test_file_is_byte_identical_to_base_outside_the_alcf_block():
    outside, _ = _split()
    assert hashlib.sha256(outside.encode()).hexdigest() == BASE_SHA256


def test_block_is_entirely_commented_and_declares_nothing_live():
    _, block = _split()
    assert all(line.strip() == "" or line.lstrip().startswith("#") for line in block.splitlines())
    data = _packaged_default()
    assert not [n for n, e in data["endpoints"].items() if e.get("kind") == "alcf"]
    assert "rates" not in data
    assert "sophia" not in "".join(
        line for line in _text().splitlines(True) if not line.lstrip().startswith("#")
    ).lower()


def _region(block: str, start: str, stop: str, strip: int) -> str:
    lines = block.splitlines()
    first = next(i for i, l in enumerate(lines) if l.startswith(start))
    last = next(i for i, l in enumerate(lines) if i > first and l.startswith(stop))
    return "\n".join(l[strip:] if l.strip() != "#" else "" for l in lines[first:last])


def test_block_when_uncommented_loads_and_resolves_the_alcf_profile():
    _, block = _split()
    endpoints = yaml.safe_load(_region(block, "  # alcf-tara:", "  # MODELS", 4))
    models = yaml.safe_load(_region(block, "  #   gpt-oss-120b@tara:", "  # FAILOVER", 6))
    models.update(yaml.safe_load(_region(block, "  #   gpt-oss-120b@metis:", "  # PROFILE", 6)))
    profiles = yaml.safe_load(_region(block, "  #   alcf-open:", "  # DO NOT SEAT", 6))
    rates = yaml.safe_load(_region(block, "  # rates:", "  # DEPLOYMENT", 4))
    assert sorted(endpoints) == ["alcf-metis", "alcf-minerva", "alcf-tara"]
    assert len(models) == 7

    raw = _packaged_default()
    raw["endpoints"].update(endpoints)
    raw["models"].update(models)
    raw["profiles"].update(profiles)
    raw["rates"] = rates["rates"]
    config = load_council_config(raw)
    assert config.endpoint("alcf-tara")["base_url"].endswith("/tara/api/v1")
    assert config.endpoint("alcf-minerva")["base_url"].endswith("/minerva/api/v1")
    assert {config.endpoint(n)["account"] for n in endpoints} == {"alcf-inference"}
    assert all(config.endpoint(n)["timeout"] == 300 for n in endpoints)
    assert "alcf-open" in config.profiles


def test_block_names_three_clusters_and_required_facts():
    _, block = _split()
    for needle in (
        "cluster: tara", "cluster: metis", "cluster: minerva", "account: alcf-inference",
        "timeout: 300", "max_requests_per_minute: 1200", "Mistral-Large-3-675B",
        "RUNNING WORKER MUST BE RESTARTED", "output_per_mtok: 0", "free and unlimited",
    ):
        assert needle in block, needle
    assert block.count("max_output_tokens: 32000") == 7
    assert block.count("  #   kind: alcf") == 3
