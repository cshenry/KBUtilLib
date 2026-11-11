# Work Record — anthropic-config-and-literal

## task_id
anthropic-config-and-literal (phase 3 of 7, PRD `anthropic` api type)

## summary

The council can now SEAT an `anthropic` endpoint.

- `council/types.py`: `"anthropic"` added to `SeatSpec.endpoint_kind`'s
  `Literal` (the actual blocker) and to `ENDPOINT_KINDS`. NOT added to
  `HTTP_KINDS`, which means "OpenAI-shaped"; a comment beside the tuple records
  why. Grep of `src/`, `tests/` and `scripts/` found **no reader** of
  `HTTP_KINDS` other than its own export.
- `council/config.py`:
  - `DEFAULT_MAX_CONCURRENCY["anthropic"] = 4`. The comment follows PRD Q8:
    the token axes bind, neither lever expresses them, and the sustained-429
    halt covers the gap. An earlier version of this comment said "the
    per-minute rate binds first", which repeated the taskplan's pre-confront
    framing; it was corrected after checking against the PRD.
  - `EndpointConfig._check` gains an `anthropic` branch BEFORE the generic
    no-URL plug-in branch: defaults `base_url` to `https://api.anthropic.com`;
    defaults `billing` to `api` when unset and refuses an explicit
    `billing: subscription`; requires `account`; requires `billing_wallet`.
  - New optional `billing_wallet` endpoint field, defaulting to `account`
    for every other kind (so their `account`-derived values are unchanged; the
    endpoint dict does gain the key).
  - Module-level `ANTHROPIC_RATE_LIMIT_BUCKETS` (canonical -> bucket),
    overridable/extendable through a new top-level `rate_limit_buckets:` key.
  - `CouncilConfig._validate_anthropic_buckets` refuses at load: a model on an
    anthropic endpoint with no canonical identity or no bucket entry (naming
    the canonical and the `rate_limit_buckets` override); one endpoint spanning
    two buckets; endpoints sharing an `account` across buckets (naming both
    buckets); and any `account` that is not the resolved bucket name.
  - `DEFAULT_CANONICAL_BY_KIND["anthropic"]` with exactly the five design-time
    ids. The ARGO collision on Sonnet 5 / Opus 5 is deliberate and commented.
  - `_seat`: for an `anthropic` endpoint an omitted temperature becomes `None`;
    explicit values (including `0`) are kept; other kinds keep the `0` default.

## store: no migration

Read `council/store/schema.py`: `calls.endpoint_kind` is
`sa.Column("endpoint_kind", _ENUM, nullable=False)` with `_ENUM = sa.String(32)`
(line 125) and the file defines no `CheckConstraint`. `"anthropic"` fits; no
migration was written and `schema.py` is unchanged.

## models added

Only the five ids given in the design (`claude-haiku-4-5`, `claude-sonnet-5`,
`claude-sonnet-4-6`, `claude-opus-5`, `claude-opus-4-8`). No `GET /v1/models`
call was made (no key in this environment), so no other id was added. The
bucket table also carries canonical identities for Opus 4.7/4.6/4.5 and Sonnet
4.5 because the bucket-sharing facts name them; those are canonical identity
strings, not served model ids, and are reachable only through a model's
explicit `canonical:`.

## bucket names chosen

`anthropic-opus-5`, `anthropic-sonnet-5`, `anthropic-haiku-4.5`,
`anthropic-opus-4` (Opus 4.8/4.7/4.6/4.5), `anthropic-sonnet-4` (Sonnet 4.6/4.5).

## tests
`tests/domains/ai/council/test_anthropic_config.py`. The ARGO-vs-anthropic
Sonnet 5 test is labelled as a preserved REFUSAL. The `_build_limits` test
drives the unmodified `CouncilWorker` with a loaded config.
