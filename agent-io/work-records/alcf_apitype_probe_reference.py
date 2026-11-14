"""Probe: does an `alcf` api type fix the /models blocker, and what do REAL
28-candidate GO-MF menus actually cost in tokens?

Throwaway. Registers nothing globally and writes nothing to any store. It
instantiates the backend directly so the design can be judged before any code
is proposed for KBUtilLib.

Two questions, one run:
  1. Does overriding models() to read /resource_server/list-endpoints make the
     endpoint servable? (The 404 on {base_url}/models is what blocked doctor,
     submit's preflight AND the worker's per-call availability check.)
  2. What are the real reasoning/completion token counts on a production item?
     The 2026-11-10 sweep used a 3-candidate menu; real menus are 24-33
     (mean 28.6), so the ceilings derived from that sweep are unsafe.

NOTE a deployment fact this exposes: alcf_tokens lives in its own uv tool venv,
NOT in the venv that serves the council (/home/chenry/.venvs/kbdl). A real
`alcf` api type therefore needs alcf-tokens as a dependency of the serving
environment. This probe shells out instead, which a production api type should
not do.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, List

from kbutillib.domains.ai.llm.apis.http import OpenAICompatibleBackend
from kbutillib.domains.ai.llm.types import LLMRequest

ALCF_TOKENS_BIN = "/home/chenry/.local/bin/alcf-tokens"
LIST_ENDPOINTS = "https://inference-api.alcf.anl.gov/resource_server/list-endpoints"


def _token() -> str:
    out = subprocess.run([ALCF_TOKENS_BIN, "get-token", "inference"],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"alcf-tokens failed: {out.stderr[:200]}")
    return out.stdout.strip()


class AlcfBackend(OpenAICompatibleBackend):
    """ALCF Inference Endpoints: OpenAI-compatible for /chat/completions ONLY.

    Differs from the plain `openai` kind in exactly two places, which is the
    whole proposed change:

    * ``_bearer`` resolves a Globus access token per call rather than reading a
      static env var. ALCF tokens live 48h and auto-refresh; a value exported
      once into a daemon's environment goes stale, and an ``auth`` failure
      HALTS the endpoint rather than retrying.
    * ``models`` reads ``/resource_server/list-endpoints`` and returns the
      models for THIS cluster/framework. ALCF answers 404 on
      ``{base_url}/models``, and the council reads an empty listing as
      "unservable" -- so without this the endpoint is unusable even though
      every chat call succeeds.
    """

    kind = "alcf"

    def _bearer(self):
        return _token(), None

    async def models(self) -> List[Dict[str, Any]]:
        try:
            r = await self.client.get(LIST_ENDPOINTS,
                                      headers=self._headers(self._bearer()[0]))
            r.raise_for_status()
            clusters = r.json().get("clusters") or {}
        except Exception as exc:
            print(f"  models(): list-endpoints failed: {type(exc).__name__}: {exc}")
            return []
        # base_url looks like .../resource_server/<cluster>/<framework>/v1
        parts = [p for p in self.base_url.split("/resource_server/")[-1].split("/") if p]
        cluster, framework = (parts + ["", ""])[:2]
        fw = ((clusters.get(cluster) or {}).get("frameworks") or {}).get(framework) or {}
        return [{"id": m} for m in (fw.get("models") or [])]


def build_prompt(spec: dict, evidence: dict, menu: list) -> tuple[str, str]:
    role = spec["roles"]["proposer"]
    block = "\n".join(
        f"{m.get('rank')} | {m.get('id')} | {m.get('score')} | {m.get('label')} | "
        f"{(m.get('description') or '')}"
        for m in menu
    )
    user = role["user"]
    for key, val in (("{text}", evidence.get("text") or ""),
                     ("{source}", evidence.get("source") or ""),
                     ("{candidates}", block), ("{candidate_block}", block)):
        user = user.replace(key, val)
    return role["system"], user


async def main() -> int:
    base = pathlib.Path("/scratch/chenry/council-alcf-test")
    spec = json.loads((base / "queries/go_mf_v4.spec.json").read_text())
    items = [json.loads(l) for l in (base / "pilot20.items.jsonl").read_text().splitlines() if l.strip()]
    item = max(items, key=lambda i: len(i["menu"]))      # worst case menu
    system, user = build_prompt(spec, item["evidence"], item["menu"])
    print(f"item {item['item_id']} menu={len(item['menu'])} "
          f"prompt_chars={len(system) + len(user)}")
    print(f"text={item['evidence'].get('text')!r}\n")

    SEATS = [
        ("alcf-tara",  "tara/api",  "openai/gpt-oss-120b"),
        ("alcf-tara",  "tara/api",  "google/gemma-4-31B-it-262k"),
        ("alcf-tara",  "tara/api",  "nemotron-3-ultra"),
        ("alcf-tara",  "tara/api",  "beam-1.0-fp8"),
        ("alcf-metis", "metis/api", "Mistral-Large-3-675B-Instruct-2512"),
    ]
    listing_checked: set[str] = set()
    print(f"{'model':38s} {'t(s)':>6s} {'ok':>5s} {'rsn':>6s} {'cmp':>6s} {'trunc':>6s} detail")
    for name, path, model in SEATS:
        cfg = {"name": name,
               "base_url": f"https://inference-api.alcf.anl.gov/resource_server/{path}/v1"}
        be = AlcfBackend(cfg)
        try:
            if name not in listing_checked:
                listing_checked.add(name)
                ms = await be.models()
                print(f"  models() for {name}: {len(ms)} model(s) "
                      f"-> {'SERVABLE' if ms else 'STILL UNSERVABLE'}")
            req = LLMRequest(model=model, system=system, user=user,
                             json_schema=spec["roles"]["proposer"].get("schema"),
                             max_output_tokens=4000, temperature=0, timeout_s=300)
            t0 = time.time()
            res = await be.complete(req)
            el = time.time() - t0
            u = res.usage or {}
            rsn = u.get("reasoning_tokens") or u.get("completion_reasoning_tokens") or 0
            detail = ""
            if res.ok:
                picks = (res.parsed or {}).get("picks") or (res.parsed or {}).get("go_ids")
                detail = f"picks={str(picks)[:46]}"
            else:
                detail = f"{res.error.kind if res.error else '?'}: {(res.error.message if res.error else '')[:70]}"
            print(f"{model:38s} {el:6.1f} {str(res.ok):>5s} {rsn:6} "
                  f"{u.get('completion_tokens') or 0:6} {str(res.truncated):>6s} {detail}")
        finally:
            await be.client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
