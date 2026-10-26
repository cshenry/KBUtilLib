# External scan: standing function-to-GO council pipeline

Research date: 2026-10-22. **Recommendation: implement an immutable, content-addressed job protocol with a token-first quote, an approval bound to that quote and job, account-wide request pacing, and a transactional consumer inbox/outbox.** Dropbox transports artifacts; consumer verification and database transactions establish completeness and successful loading. A workflow engine can orchestrate this protocol but cannot supply its missing semantics automatically.

The sections below distinguish documented findings from proposed design choices. The run totals and two near-misses are supplied by the task, not independently verified telemetry. No paid inference, gateway load test, Dropbox experiment, or database load was performed. Source inspection was read-only; only this document is a deliverable.

## A. Cost estimation and dry-run quoting

### What the named systems actually provide

| System | Finding and implication |
|---|---|
| [OpenAI Batch API](https://developers.openai.com/api/docs/guides/batch) | JSONL requests are submitted for asynchronous execution, with a 50% discount and a 24-hour completion window. Validation is part of submission, not a documented standalone financial quote. Results need association through `custom_id`, not output ordering. Batch execution is useful only if the gateway supports it; an OpenAI-compatible synchronous endpoint does not establish Batch support. |
| [Anthropic Message Batches](https://platform.claude.com/docs/en/build-with-claude/batch-processing) | Independent Messages requests, identified by `custom_id`, execute asynchronously at discounted input/output prices; retrieve per-request outcomes. This is execution infrastructure, not an end-to-end council quote. Dependent critic/meta requests must be constructed after upstream answers arrive. |
| [Anthropic `POST /v1/messages/count_tokens`](https://platform.claude.com/docs/en/build-with-claude/token-counting) | Free, separately rate-limited input counting without creating a message. It accepts structured prompts, with documented exceptions for certain tool/remote-content inputs. It does not predict generated output or cache hits. It still makes a network request: distinguish “no inference spend” from “strictly offline.” |
| [OpenAI tokenizer guidance](https://developers.openai.com/cookbook/examples/how_to_count_tokens_with_tiktoken) | `tiktoken.encoding_for_model` supports local token counting. Message wrappers, tools and model-specific serialization matter; counting the function-description text alone misses most council input. An unrelated gateway model needs its own tokenizer or a calibrated proxy, explicitly labeled approximate. |
| [LiteLLM token/cost helpers](https://docs.litellm.ai/docs/completion/token_usage) and [budgets](https://docs.litellm.ai/docs/proxy/users) | `token_counter`, `cost_per_token`, `completion_cost`, and custom model registration can assemble estimates from known quantities. Current proxy documentation describes pre-request maximum-cost reservation, reconciled to actual cost, but explicitly says batch submission cannot reserve the whole file's cost from `input_file_id`. Budgets require a database; concurrency/reservation/fail-closed configuration matters. `max_budget` is not a durable whole-job approval or quote. |
| [Ray Data LLM](https://docs.ray.io/en/latest/data/working-with-llms.html) | Processors expose tokenization, batch size, concurrency and HTTP QPS controls. These are work-shaping primitives; the reviewed guide does not document a job-price quotation API. Multiple processors still need a shared account budget. |
| [vLLM offline inference](https://docs.vllm.ai/en/latest/serving/offline_inference/) | `LLM.generate`/`chat`, queues, profiling and metrics support high-throughput execution. No monetary preflight quote is documented in this API. For self-hosting, tokens do not alone determine GPU-hour cost: benchmark representative jobs on the actual hardware and include initialization/idle time. |
| [Bedrock batch inference](https://docs.aws.amazon.com/bedrock/latest/userguide/batch-inference.html), [`CountTokens`](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_CountTokens.html) | Batch jobs consume S3 inputs and publish S3 outputs. `CountTokens` is an explicit input-token preflight primitive, model-specific, not a forecast of generated tokens. Use the supported model/region's batch rate card; do not assume the gateway passes through AWS pricing or discounts. |
| [Databricks AI Functions](https://docs.databricks.com/aws/en/large-language-models/ai-functions), [serving cost monitoring](https://docs.databricks.com/aws/en/admin/system-tables/model-serving-cost) | `ai_query` is billed for its chosen serving endpoint; query compute can also be billed. System tables support measured cost attribution. The reviewed pages do not establish a free, complete pre-execution quote; SQL planning is not proof of LLM token spend. |
| [Helicone cost package](https://github.com/Helicone/helicone/blob/main/packages/cost/README.md) | A model/provider registry and calculation functions apply configured prices, including deployment overrides. They need a price source. The README's worked decimal examples contain inconsistent per-million conversions: verify arithmetic against the actual rate card instead of copying example constants. |
| [Langfuse usage/cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking) | Separates ingested usage from cost, with model-price inference and custom definitions. Useful measured-actuals ledger; no future council-output prediction follows from tracing alone. |
| [OpenMeter usage events](https://openmeter.io/docs/metering/events/usage-events) | CloudEvents carry arbitrary usage quantities, aggregated by configured meters. Tokens can be first-class units independent of dollars. Metering records consumption; a forecast must be computed separately. |

**Answer to the preflight question:** input counting and cost calculation/reservation are real primitives. I did not find a documented turnkey, binding quote for a conditional five-seat council among these execution/observability systems. Absence here means “not documented in the reviewed interfaces,” not a claim that no vendor feature exists anywhere.

### A quote that can be produced without spending on inference

Render known initial requests locally using the same renderer/configuration as execution. Freeze the input snapshot, ontology/candidate-menu version, model/deployment identities, prompt/schema versions, generation caps, batching policy and cache/reuse snapshot. Subsequent prompts depend on unknown proposer answers: include projections for both their generated text and its repeated inclusion in critic/meta inputs. Five seats does not necessarily mean five calls per item; this repo conditionally invokes meta ([`types.py:3`](../../../../src/kbutillib/domains/ai/council/types.py#L3)).

Use separate quantities per seat, gateway and usage category:

```text
projected tokens = known rendered input
                 + projected dependent-stage input
                 + projected generated output
                 + projected paid retry/repair work
                 - explicitly eligible reuse
USD = sum(projected billable units × applicable frozen unit price)
```

Do not deduct speculative cache savings from the conservative quote. Preserve uncached/cached input, output/reasoning and any tool charges according to provider billing semantics. If a rate card is unavailable, `usd_estimate: null`, `pricing_status: unknown` is honest; zero dollars is not. Optional proxy/list-price dollars must be labeled scenario costs, never gateway charges. OpenMeter's unit metering and providers' token quotas are concrete prior art for token-only accounting; they are not evidence of a universally standardized “token quote” format.

**Calibration recommendation (a proposed method, not a vendor-mandated standard):** store predicted and measured usage per request, including failed/truncated/paid retries, cache hits and missing-usage flags. Stratify by seat/model, prompt/menu size, batching regime and escalation outcome. Fit output and dependent-input projections from comparable historical data; backtest by holding out complete chunks/runs. Bootstrap whole chunks to preserve correlated escalation/retry behavior. Report expected usage, an empirical prediction interval if supported by enough independent observations, and a separate conservative scenario/cap. A single historic aggregate cannot justify a calibrated “95% confidence” claim. Distinguish uncertainty in workload generation from unknown tariff and unknown billing usage.

From the supplied run, `199,200,000 / 19,276 = 10,334.1` total tokens per submitted item. Thus `N × 10,334.1` is a useful baseline, with a prominent “same workload/configuration mix” assumption. The 9.46-hour runtime implies about 5,849 tokens/s overall; neither value is a gateway RPM ceiling. The 19,268 output rows leave eight submitted items to explain, not automatically eight permanent failures. The repo already maintains measured per-seat output means for batch sizing ([`worker.py:1434`](../../../../src/kbutillib/domains/ai/council/engine/worker.py#L1434)); that successful-call heuristic excludes some costs and is not a complete financial estimator.

The quote should record estimator version, calibration run IDs, input/config digests, item and projected request counts, token breakdown/range, pricing provenance/date, assumptions, retry allowance, expiry and executable token/dollar ceilings. Enforce ceilings with reservations before concurrent dispatch, including retries; if usage or provider output caps are unverified, do not advertise a mathematically hard total-token ceiling. A paid calibration sample is a separate approved job, never a hidden part of QUOTE.

## B. Human approval gates

### Approval artifacts and their limits

| System | Actual artifact and resume model | Audit / binding limit |
|---|---|---|
| [Argo suspend / intermediate inputs](https://argoproj.github.io/argo-workflows/intermediate-inputs/) | A suspended workflow node receives supplied output parameters, such as YES/NO, then downstream steps test the value. State is in the Kubernetes Workflow object. | Authenticate resume through Argo/Kubernetes permissions and retain workflow records plus Kubernetes audit logs. A YES attached to a node does not hash external input files. Avoid auto-expiring a timed suspend into permission to spend. |
| [Airflow HITL](https://airflow.apache.org/docs/apache-airflow/stable/tutorial/hitl.html), [operator API](https://airflow.apache.org/docs/apache-airflow-providers-standard/stable/_api/airflow/providers/standard/operators/hitl/index.html) | Native `ApprovalOperator`, `HITLOperator`, entry/branch variants bind responses to task instances in Airflow's persisted state. Added in 3.1; current docs describe scheduler-managed waiting in 3.3. | Assigned users and response timeouts are available. Preserve responder/task/run records and configured audit logs. Sensors plus a separate approval row or manual DagRun parameters remain alternatives, but manual triggering itself is not an approval validator. |
| [Prefect interactive inputs](https://docs.prefect.io/v3/develop/inputs), [flow-run API](https://reference.prefect.io/prefect/flow_runs/) | `pause_flow_run` waits in-process; `suspend_flow_run` exits for later rescheduling. Typed `RunInput`/Pydantic data is supplied through UI/API and persisted against the flow run. | Input schema validation does not validate the referenced job bytes. Suspension/re-entry requires persisted task results and idempotent pre-gate work; do not regenerate a different job while resuming. |
| [Dagster RunRequest](https://docs.dagster.io/api/dagster/schedules-sensors), [blocking checks](https://docs.dagster.io/api/dagster/asset-checks) | A sensor can read an approval and yield `RunRequest(run_key=...)`; Dagster persists run/tick records. A blocking ERROR asset check blocks dependent assets in runs that include it. | The external approval record is application-owned. A run key deduplicates sensor launches; it is not a signature or proof of job contents. Asset checks are quality gates, not intrinsically a human-approval inbox. |
| [Temporal messages](https://docs.temporal.io/develop/go/message-passing), [event history](https://docs.temporal.io/encyclopedia/event-history) | A Signal's payload becomes a durable workflow-history event; workflow code waits for approval. An Update can add validation and return application acceptance. | Target Workflow ID **and Run ID**, validate quote/job digest and nonce in workflow logic, and deduplicate application requests. Server acceptance of a Signal is not confirmation the workflow accepted its business meaning. History is durable/auditable, not automatically a cryptographically signed human statement. |
| [GitHub environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments), [reviewing deployments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments) | An authenticated approve/reject review unblocks a pending deployment job; deployment/run records hold the context. Required reviewers and prevention of self-review are configurable, subject to repository/plan support. | A run's commit identifies code, not mutable Dropbox contents. Pin artifact digest, disable unintended bypasses, and retain review records. Required reviewers do not inherently mean all listed reviewers must approve. |
| [Step Functions `.waitForTaskToken`](https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html) | A callback token identifies a waiting task; `SendTaskSuccess`/`SendTaskFailure` supplies its result. Execution history records progress; IAM controls callback access. | Timeout creates a new token. The token identifies task execution, not the human or external document; an authenticated approval service must attach actor and job digest. Do not leak the bearer-like token into a broadly writable folder. |

**Correction to the prompt's framing:** historical Airflow sensor/BranchPythonOperator workarounds are not the current native approval mechanism. A deprecated import path or old approval idiom does not mean that branching as a workflow concept is deprecated. Check the installed Airflow/provider versions before choosing an implementation.

### Binding approval to exactly what was quoted

All these systems scope decisions to an execution/task; none of the reviewed documentation promises automatic content binding for arbitrary mutable external datasets. Use their authenticated event plus an application-level immutable envelope:

```json
{
  "schema_version": 1,
  "approval_id": "unique-decision-id",
  "job_id": "unique-job-id",
  "job_manifest_sha256": "...",
  "quote_sha256": "...",
  "decision": "approve",
  "actor": "operator-identity",
  "decided_at": "RFC3339 timestamp",
  "expires_at": "RFC3339 timestamp",
  "max_total_tokens": 230000000,
  "max_usd": null,
  "nonce": "single-authorization-id"
}
```

The values are illustrative, not a recommendation to approve 230M tokens. Hash a specified canonical encoding or exact immutable bytes; exclude the signature field from the signed payload. Execution checks digest equality, expiry, decision, actor authorization and permitted ceilings **before its first spend**, then records consumption of the authorization in a local durable ledger. Restart resumes that execution and its remaining budget, rather than interpreting the same approval as permission for another full run. Any change to input, prompt, model fallback, configuration or quote requires a new quote/approval unless an exact alternative was already included and approved. A human may lower a budget in the approval; changing the workload is a new proposal.

A content hash detects mismatch, not forgery: a writer who can edit both files can recompute both hashes. For a file-only gate, use an operator-signed approval with a pinned verification key, or an authenticated approval tool that writes an independently protected audit record. A plain `approved=true` JSON with an actor string is useful operationally but is neither authenticated nor tamper-evident against other folder writers. Retain immutable decisions, rejections and supersessions. Engine database history also needs access controls/retention; auditability is not equivalent to cryptographic tamper evidence.

## C. Publishing a complete dataset through file sync

### Documented guarantees do not compose into directory atomicity

* Hadoop's classic committer writes an optional, usually empty `_SUCCESS`; the [manifest committer](https://hadoop.apache.org/docs/stable/hadoop-mapreduce-client/hadoop-mapreduce-client-core/manifest_committer.html) can write a richer success summary. This establishes **producer** completion under that committer, not arrival of every member on another machine. Do not assume its summary enumerates every file for a consumer checksum contract.
* The [Hadoop manifest protocol](https://hadoop.apache.org/docs/stable3/hadoop-mapreduce-client/hadoop-mapreduce-client-core/manifest_committer_protocol.html) depends on specific filesystem operations; even consistent S3 does not satisfy its rename requirements. A same-filesystem atomic rename or symlink/pointer swap can atomically expose a local completed generation. Dropbox may replicate component changes independently; local rename atomicity is not a documented cross-machine transaction.
* [rclone](https://rclone.org/docs/) normally uses temporary partial filenames on applicable destinations before finalizing; backend capabilities matter. [rsync](https://www.samba.org/ftp/rsync/rsync.1) normally stages individual files, while `--partial` retains interrupted data and `--inplace` exposes it. `--delay-updates` postpones final placement and improves consistency but does not turn a multi-file tree into one atomic transaction. These are useful staging precedents, not Dropbox ordering guarantees.
* [Dropbox `status` and `filestatus`](https://help.dropbox.com/installs/linux-commands) are desktop CLI diagnostics, **not Dropbox API folder-transaction endpoints**. `filestatus` reports individual file status; its default can omit dot entries. [Dropbox's status help](https://help.dropbox.com/sync/check-sync-status) says “up to date” describes files synced with the account, and separately documents indexing, selective sync and errors. It does not promise a producer-defined generation has arrived on every device, nor dependency ordering of files and a marker.
* The [Dropbox change API](https://docs.dropboxapi.com/dropbox-api/docs/detecting-changes) supplies listings/cursors/notifications about account changes. It does not certify that another device has downloaded those bytes. Dropbox [`content_hash`](https://docs.dropboxapi.com/dropbox-api/docs/technical-reference/content-hash) is a SHA-256 **of concatenated 4-MiB block hashes**, not ordinary file SHA-256. Confusing them would recreate a hash-contract defect.
* [S3 has offered strong read-after-write consistency since December 2020](https://aws.amazon.com/about-aws/whats-new/2020/12/amazon-s3-now-delivers-strong-read-after-write-consistency-automatically-for-all-applications/). “S3 is eventually consistent” is obsolete as a present-day generalization. Strong object consistency still does not make several object writes one dataset commit. Older consistency layers/committers addressed listing visibility and commit protocols; their exact guarantees do not transfer to Dropbox.

The observed missing third file is task evidence. I did not find a documented guarantee that would make the desktop status a valid dataset-completeness barrier, or enough evidence to identify the precise cause of that incident. Selective sync, online-only availability, pending producer changes and per-file errors are distinct documented states/limitations; do not assert that any one caused this failure.

### Recommended publish/consume protocol

Manifest-last **plus consumer verification** is a sound application protocol for this constraint; “last” alone is insufficient because arrival order can differ. Use unique, immutable `job_id/generation_id` directories, never overwrite a published generation. Build outside the watched surface, close files, compute manifest entries (`relative_path`, bytes, SHA-256, media/schema version, row count), then move/copy into sync and publish `manifest.json` last. Include all required outputs, terminal item counts and references to the approved input/quote. A final READY file may carry the manifest's hash but is only a discovery hint.

The consumer polls, validates manifest schema and safe relative paths, waits for **every listed file**, checks byte size and SHA-256, copies verified bytes to a private local staging area, and loads that snapshot. Verify the staged copy, not a path another process may replace between verification and parsing. Manifest/READY arrival before payload is expected and safe. Reject duplicate manifest paths, conflicting generations and unlisted files being used as inputs. Parsing and row-count validation follow byte verification.

A single immutable archive (for example `results.tar.zst` plus outer SHA-256 and inner manifest) is a useful simplification for three coordinated outputs: fewer independently arriving members. It still needs checksum verification, safe extraction and semantic checks. It is not a stronger distributed transaction. A true transactional store/catalog would offer stronger coordination but violates the file-sync-only boundary and is unnecessary here.

Consumer states should distinguish:

| Observation | Action |
|---|---|
| No complete/parseable manifest yet | Pending discovery; retry with bounded backoff. A persistently malformed final manifest becomes an alert. |
| Missing payload, changing size, transient read failure, initial hash mismatch | `awaiting_files`; preserve first-seen time and observed progress, retry; do not load or acknowledge success. |
| Same mismatch or missing file beyond a configured transfer deadline | `delivery_failed`/quarantine with expected and observed hashes/sizes; alert and request investigation. A timeout proves delivery failure, not physical corruption. |
| Verified bytes but invalid schema/counts/GO references | `validation_failed`; deterministic content defect, fail promptly. |
| All checks pass | Stage locally and attempt transactional load. |

With only an eventually delivered local view, a consumer cannot conclusively distinguish “will arrive later” from “lost/corrupt forever” at an instant. The deadline is an operational policy (chosen from measured transfer sizes/latencies), not a consistency proof. Do not wait forever; do not label a first mismatch irrecoverable. Preserve failed evidence rather than silently editing the manifest.

## D. Idempotent cross-machine loading and receipts

[Hive partition DDL](https://hive.apache.org/docs/latest/language/languagemanual-ddl/) separates storage files from metastore visibility: copying partition files alone does not register a partition. [Hive transactions](https://hive.apache.org/docs/latest/user/hive-transactions/) document the historical partition-level boundary. [Delta's optimistic commits](https://docs.delta.io/concurrency-control/) and [Iceberg's immutable metadata plus atomic swap](https://iceberg.apache.org/docs/1.7.0/reliability/) make a committed version the visibility boundary. The relevant lesson is explicit commit metadata, **not** that synchronizing a Delta/Iceberg directory magically preserves a transaction on another machine.

[dbt source freshness](https://docs.getdbt.com/reference/resource-configs/freshness) detects age against configured timestamps/thresholds. Freshness does not prove join coverage, row completeness or exactly-once effects.

Use at-least-once delivery with **exactly-once database effects** within a named target, rather than promising exactly-once file delivery. The [idempotent-consumer pattern](https://microservices.io/patterns/communication-style/idempotent-consumer.html) stores processed IDs transactionally with business changes; the [transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) closes the database-write/notification dual-write gap. Adapt those patterns to files:

1. Verify/stage the immutable result generation. Identify it by `publication_id + manifest_sha256` and the intended logical target/database instance, not filename alone.
2. In the destination database transaction, reserve an inbox identity under a unique constraint; reject the same publication ID with a different digest. Run join/semantic assertions, then upsert results under stable `(input_id, input_revision, semantic_config_hash)` keys. Do not collapse different processing versions into one row unintentionally.
3. Store committed load counts and a receipt payload in that **same transaction**, alongside loaded rows and inbox completion. Rollback means no success. Concurrent duplicate consumers serialize on the unique inbox identity.
4. After commit, export the stored receipt through a local temporary file and rename to the receipt's final immutable name in sync. Crash before export? Re-export from the outbox. Crash after export? Re-export identical bytes. Never send a success file before commit. A replay returns the original committed receipt, optionally with a separate replay event; it does not replace the original counts with zero.
5. Producer reconciles published identities against valid committed receipts. Receipt loss prompts re-export/status reconciliation, not a second expensive inference job. Schedule consumer reconciliation of manifest item IDs against stored result keys so accidental later deletion or a restored database is detectable. A receipt names a database instance/epoch: an old receipt alone cannot prove the current restored database still contains the rows.

### Receipt contract

Use versioned JSON with at least:

* `receipt_id`, publication/job/generation IDs, input/quote/approval references, output manifest SHA-256, consumer identity, loader version and target database logical ID plus restore epoch.
* `status` (`committed`, `rejected`, `partial` if explicitly allowed), start/commit timestamps and database transaction/load-ledger identifier; schema/query/ontology versions.
* Counts for submitted inputs, terminal successful verdicts, explicit no-term verdicts, failed/excluded items, expected loadable rows, parsed rows, distinct/non-null input keys, matched and unmatched input keys, ambiguous/multiple matches, inserted, updated, already-present-identical, rejected and resulting verified stored keys. GO association rows can exceed item rows; label units.
* Join-contract/canonicalization ID, coverage ratio, assertion thresholds/results, bounded examples or an error-artifact digest for unmatched/ambiguous keys, and a digest of the accepted input-key set.
* Actual tokens by gateway/model/category, unknown-usage counts and price provenance where measured; these are run reconciliation data, not proof of database correctness.

For this run, a successful first-load receipt could establish `expected_loadable=19268`, `parsed=19268`, `matched_distinct_inputs=19268`, `unmatched=0`, `ambiguous=0`, then explain inserted/updated/unchanged totals and account for the other eight submitted items. A legitimate replay might insert zero while verifying 19,268 already-present keys. A broken join with matched zero must produce a rejected attempt, never a committed success. Counts alone can also match while the wrong keys load; verify key-set and version coverage.

Receipts traverse the same untrusted-in-order sync surface: bind them to manifest digests and the configured target, validate them, and use consumer signatures if authenticity against other folder writers is required. Keep database files local; synchronize immutable exports and receipts, not a live database.

## E. Deriving work missing a verdict

[dbt incremental models](https://docs.getdbt.com/docs/build/incremental-models) use `is_incremental()` to conditionally select new/changed rows and `unique_key` to merge matching records. A high-water mark is a scanning optimization: it can miss late arrivals, edited old rows and failed prior work. [Airflow catchup/backfill](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.html) covers scheduled data intervals; it is not an item-level result ledger.

[Bazel remote caching](https://bazel.build/remote/caching) keys reusable action outputs on action identity; [Nix derivations](https://nix.dev/manual/nix/2.32/language/derivations) describe precise build inputs; [DVC parameter dependencies](https://doc.dvc.org/user-guide/pipelines/defining-pipelines) invalidate stages only when the tracked relevant parameters change. These establish dependency-aware reuse, not permission to relabel old semantics as new.

Define a successful result key explicitly:

```text
K = (stable input ID, input-content revision,
     semantic configuration digest)
semantic configuration = prompt/query + renderer + output schema
                       + GO release/candidate/evidence selection
                       + model/deployment/generation semantics
                       + adjudication policy
```

Keep transport location, operator, timestamp and chunk number out of the semantic identity unless they affect results. Conversely, if batching changes model context or behavior, include its relevant policy. Preserve an independent original input ID for joins; a digest is a revision/provenance key, not a replacement for a shared identity contract.

For a frozen eligible corpus snapshot, use an anti-join:

```sql
SELECT i.*
FROM eligible_snapshot i
WHERE NOT EXISTS (
  SELECT 1 FROM verdict_ledger v
  WHERE v.input_id = i.input_id
    AND v.input_revision = i.input_revision
    AND v.semantic_config_hash = :requested_config
    AND v.status = 'successful_terminal'
);
```

“Missing ANY result” deliberately omits the config predicate; label it as a distinct policy. Decide whether it means any historical revision or the current input revision. A successful explicit no-MF-term verdict counts as processed; a transport failure, truncated response or merely queued item does not. Track unrun, in-flight, run-complete/unlanded, landed and retryable-failed separately. Reserve outstanding items locally so repeated DETECT does not create duplicate jobs while an approval or receipt is pending; that reservation does not count as a verdict.

**Version-bump contradiction:** if the bump changes semantics for every item, full coverage at that version requires new results for every affected item. No cache can both skip semantically changed computations and guarantee equivalence without additional evidence. Avoid unnecessary reruns by separating packaging/loader revisions from semantic versions, hashing relevant dependencies narrowly, reusing unchanged substage calls, and recording explicit reviewed compatibility mappings when equivalence is defensible. If strict policy requires literal new prompt/query version results, a compatibility alias is not the same thing as an actual new inference and must be reported separately.

This repo already distinguishes whitespace-normalized entity hashes, evidence/menu hashes and full prompt hashes ([`types.py:180`](../../../../src/kbutillib/domains/ai/council/types.py#L180), [`types.py:266`](../../../../src/kbutillib/domains/ai/council/types.py#L266)); reuse selects successful matching calls ([`queries.py:436`](../../../../src/kbutillib/domains/ai/council/store/queries.py#L436)). Its prompt hash explicitly includes query version, so a blanket version bump invalidates even otherwise identical invocations. Reuse must not be assumed merely because two prompt texts look alike.

Mixed-version coverage is a reasonable transitional policy, not a universal industry rule. Store provenance per result and report, for a fixed corpus denominator: exact-current coverage, older-version-only coverage, compatible-reused coverage, missing-any, failed-current and in-flight. These overlap unless deliberately partitioned; define the dashboard categories. Offer an explicit “latest available” view with visible version metadata, and an exact-version view for reproducible analysis. Never silently describe a mixed corpus as fully current. Include corpus snapshot/date and counts of excluded/deleted/changed inputs.

## F. Catching silent join-key mismatch before commit

[dbt tests](https://docs.getdbt.com/docs/build/data-tests) provide `unique`, `not_null` and `relationships`; [relationships excludes NULL values](https://docs.getdbt.com/reference/resource-properties/data-tests). Thus relationships alone does not establish nonempty input or non-null keys. [Soda reference checks and metrics](https://docs.soda.io/soda-documentation/soda-v3/sodacl-reference/metrics-and-checks) combine cross-dataset key checks with row-count/missing/duplicate assertions. [Great Expectations `ExpectColumnValuesToBeInSet`](https://greatexpectations.io/expectations/expect_column_values_to_be_in_set/) measures membership and supports a `mostly` threshold plus unexpected counts. For a large target key space prefer a database anti-join/query expectation over materializing all keys in Python.

**The required assertion is input-to-target coverage BEFORE filtering unmatched inputs away.** Testing referential integrity on an already-empty inner-join result can pass vacuously. In a staged load, compute:

```text
N = distinct eligible source item/revision keys (must match manifest)
U = source keys having zero target matches
A = source keys having more than one target match
M = source keys having exactly one target match
Require: no null keys; no conflicting duplicate source keys;
         N > 0 unless this is an explicitly approved empty job;
         U = 0; A = 0; M = N.
```

For permitted partial imports, replace `U=0` with a versioned explicit exclusion/coverage policy agreed before loading; always fail on zero matched keys for a nonempty expected import. Count distinct source keys and matches per source, not just joined rows: a many-to-many join can inflate row counts enough to camouflage omissions. Check expected GO term validity and result cardinality separately; multiple MF terms per input can be legitimate.

Run assertions and mutation in one transaction against a stable snapshot/appropriate locks and uniqueness constraints; otherwise target changes between precheck and commit defeat the check. On failure rollback and emit a rejected attempt with key-contract version, metrics and samples. On success verify that every accepted `(input, revision, config)` key exists with expected result digest before recording the committed receipt. `affected_rows > 0` is inadequate because safe replays may make zero changes.

The repo's `entity_hash` hashes a whitespace-normalized **JSON string**, not raw text bytes ([`types.py:163`](../../../../src/kbutillib/domains/ai/council/types.py#L163)); `data_hash` hashes evidence and ordered menu. Both are valid hashes of different things. Establish a single export/loader key contract with named algorithm, exact encoding, normalization and test vectors (Unicode, repeated whitespace, quotes, null/empty distinctions). Carry source-native ID plus exact original text and input revision through the job/results. The reported 100% text match is strong diagnostic evidence but is not a safe automatic fallback unless text uniqueness and normalization are established. Use representative canary IDs for quick diagnosis, followed by the full coverage assertion; canaries alone cannot prove all 19,268 joins.

## G. Rate-limit-aware work shaping

### Evidence and terminology

[`Retry-After`, RFC 9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3) is an HTTP date or nonnegative delay in seconds. [RFC 6585 §4](https://www.rfc-editor.org/rfc/rfc6585.html#section-4) defines 429 and permits `Retry-After`; it does not prescribe one quota identity/counting algorithm. The researched [HTTPAPI draft revision 11](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-ratelimit-headers-11), dated May 2026, defines `RateLimit-Policy` and `RateLimit`. **It is a draft, not a final universal `RateLimit-*` header standard.** Older drafts/provider `X-RateLimit-*` fields differ. Treat advertised quota as a hint, not guaranteed service capacity, and implement adapters for actual gateway headers.

[Netflix concurrency-limits](https://github.com/Netflix/concurrency-limits) documents loss-based AIMD and latency/queue-based Vegas approaches. They adapt the outstanding-request window; they do not abolish an explicit requests-per-minute ceiling. [AWS's retries/backoff/jitter guidance](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/) explains retry multiplication, overload and token-bucket retry budgets. [AWS SDK retry behavior](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html) separately models retry quota and adaptive send-rate limiting. Copy the separation, not provider-specific retry assumptions.

Token buckets admit bursts up to their capacity while enforcing an average refill rate. A bucket initialized with 120 tokens can send 120 immediately: that is unsuitable if the gateway enforces a rolling minute or tight subwindows. A leaky-bucket/paced scheduler smooths departures. Window semantics and allowed bursts must be known before selecting bucket capacity; “120 RPM” alone does not state them.

### Correct architecture for the supplied limits

Use **one admission authority per billing/quota account across all its models, aliases, workers and retries**, with two independent constraints: at most 20 in flight and at most the account's allowed request starts over its enforcement windows. A single dispatcher process with a durable queue is simplest on the runner machine. Multiple processes require a genuinely shared coordinator or conservatively allocated fixed shares; Dropbox is not a safe distributed token-bucket lock. Other applications using the same account consume the same allowance, so negotiate their share or leave headroom.

For the supplied 120 RPM ceiling, theoretical average starts cannot exceed 2/s; pace at a conservative configurable rate below this, with near-zero accumulated burst credit unless the provider documents burst allowance. If exact rolling-window enforcement matters, maintain a sliding start-time log as well as pacing. Acquire permission just before dispatch when a concurrency slot is available, and count every HTTP attempt, including retries. Avoid spending permits while waiting behind occupied slots and later releasing a burst of queued sends. Maintain gateway-wide cooldown on 429; honor valid `Retry-After`, then apply bounded jitter/backoff and re-enter the same admission queue. One layer owns retries, with per-item attempt/deadline limits and an account-wide retry budget; otherwise nested SDK/task/chunk retries multiply demand. Do not retry auth or exhausted-financial-quota failures as transient rate limits.

Approximate steady request throughput is bounded by `min(2 requests/s, 20 / mean_request_latency_seconds)` before headroom and other traffic. At 15-second mean latency, 20 concurrency supports only about 1.33 requests/s. At short latency, RPM binds first. Batch size changes requests per item and context/output risk; count actual requests rather than seats or proteins. Dependent critic/meta stages need fair scheduling so proposer floods cannot starve completion. Retries/fallbacks consume both the approved spend budget and the destination account's rate budget.

The checked-out config validates aggregate queue concurrency against account caps ([`config.py:1001`](../../../../src/kbutillib/domains/ai/council/config.py#L1001)), and worker limits group endpoints by account ([`worker.py:512`](../../../../src/kbutillib/domains/ai/council/engine/worker.py#L512)). A targeted search of `src/kbutillib/domains/ai` did not find an RPM/token-bucket or `Retry-After` implementation. This is a source-inspection finding, not proof about external gateway SDKs. Concurrency arithmetic alone does not meet the stated 120-RPM constraint.

### Discover sustained capacity without a retry storm

First obtain documented/account-console/operator quotas and ask whether they are rolling/fixed windows, shared across models, token-limited, and affected by concurrent tenants. No finite black-box test proves a hard account ceiling when other traffic is unknown. Treat discovery as a separately authorized, capped experiment, not free QUOTE work.

Start below the believed sustainable rate with representative prompt/output sizes and both gateway routes isolated in metrics. Hold each rate for several complete enforcement windows, after warm-up, long enough to fill and drain concurrent work. For example, five one-minute windows per step is a starting procedure, not an IETF requirement. Record **offered first attempts, retries, successful completions, 429s, timeouts, outstanding requests, latency quantiles and tokens** separately. Slowly raise send rate only while errors remain low and queue/latency remain stable; at the first sustained throttle, cool down and reduce materially (AIMD is appropriate). Stop the experiment at a small predefined error/spend bound. Do not run an unbounded “find maximum concurrency” burst, and do not mask overload by counting retried requests as fresh throughput.

Save a dated safe operating envelope by account, workload/configuration, competing traffic and observation duration. Revalidate cautiously after provider/model changes. A burst result measures transient admission and bucket depth; steady completions per minute across multiple windows measure useful capacity. The reported 3× throughput error and 85% failure rate are consistent with ignoring this distinction, but telemetry would be needed to prove the exact causal mechanism.

## STALL REPORT

1. **No reproducible job/quote identity:** a mutable folder plus unscoped YES cannot establish what was approved. Bind immutable input/config and quote digests, parameters, ceilings and expiry; consume authorization durably before spend.
2. **Unknown prices represented as zero:** this yields a false dollar quote. Preserve null/unknown dollar cost and token estimates with explicit calibration assumptions. No reviewed platform predicts the whole council's conditional output for us.
3. **Paid calibration hidden in QUOTE, or a mean labeled a cap:** violates “before spending anything” or understates risk. Reuse prior telemetry; separately approve samples. Distinguish forecast ranges from verified enforceable limits and reserve concurrent spend.
4. **Five seats treated as five identical calls per protein:** conditional meta, repeated downstream context, batching, cache reuse and paid repairs break that arithmetic. Quote the execution graph and policy, not just input text length.
5. **`_SUCCESS`, rename or Dropbox “up to date” used as arrival proof:** the consumer can read an incomplete generation. Require all manifest-listed staged bytes to pass size/checksum/schema verification. The marker may arrive first.
6. **All mismatches treated as either immediate corruption or endless syncing:** first observation cannot distinguish the two. Persist progress, retry under a transfer deadline, then alert as failed delivery with evidence; do not claim the timeout proves physical corruption.
7. **Database commit and receipt publication are unrelated writes:** crashes either duplicate effects or lose acknowledgement. Commit data, inbox identity and receipt outbox together; re-export the same receipt safely after restart.
8. **Loader succeeds when an inner join returns zero:** relationships checked only after the join can pass vacuously. Require nonempty expected input, non-null keys, unique target mapping and full approved source-key coverage before committing; report unmatched/ambiguous keys.
9. **A global watermark or “has any verdict” selects work for a requested version:** old failures/edits and mixed versions disappear. Anti-join the fixed corpus against successful `(input revision, semantic config)` results and separately track pending/unlanded work.
10. **Semantic version bumps promised without affected recomputation:** this is impossible absent defensible compatibility/reuse evidence. Scope dependency hashes narrowly and expose exact-current versus reused/older coverage.
11. **Independent per-model semaphores or retries enforce account-wide quotas:** they do not. Coordinate all attempts per account under both concurrency and sustained-start limits, including other account consumers; backoff alone is not pacing.
12. **“Loaded 19,268” taken as complete reconciliation:** the supplied run began with 19,276. Account for every submitted item, distinguish input items from GO association rows, verify key sets and identify the destination database epoch in receipts.

## FREE CRITIQUE (non-binding)

* Start with a local scheduler/CLI and versioned JSON/JSONL artifacts; a new Argo/Temporal/Airflow deployment is not required to solve a two-machine file protocol. If a workflow platform already exists, use its authenticated approval and history while retaining the same artifact contracts.
* Separate immutable artifacts by responsibility: `input-manifest.json`, `quote.json`, `approvals/<id>.json`, `results/<generation>/manifest.json`, and `receipts/<target>/<publication>.json`. Give each file family one writer; never have both machines rewrite a shared `status.json`. Derive status from immutable events plus local ledgers.
* Define “no verdict” precisely before automating DETECT. For this scientific workflow, explicit abstention/no-MF-term can be a successful terminal answer; invalid output cannot. Preserve ontology release, candidate retrieval and original function text so biological results remain interpretable after ontology changes.
* Make the first developer acceptance exercise failure-oriented: marker before payload, one missing file, wrong hash contract, 0% and partial joins, duplicate consumers, crash immediately after DB commit, expired approval, changed job after approval, pending receipt, model fallback and sustained 429s. This research did not run those tests; they are the highest-value way to raise implementation confidence.
* Retain original model responses and per-call usage provenance where policy allows, but keep credentials out of manifests. Track unmeasured usage as unknown. Compare every completed job's estimate to actual spend and retry/escalation outcomes before reusing its calibration.

Confidence: **high** in the protocol-level recommendations and documented distinctions; **medium** in their exact integration with these two gateways and the destination loader. Actual gateway contracts/headers, historical per-call telemetry, the database schema and controlled cross-machine failure tests would raise integration confidence. Live docs can change; pin engine/provider versions and protocol schemas when implementing.
